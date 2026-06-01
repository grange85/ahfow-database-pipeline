#!/usr/bin/env python3
"""
import_shows.py  (v5 — adds --update / --dry-run; CSV is authoritative)
-----------------------------------------------------------------------
Imports a live shows CSV into the music SQLite database.

Usage:
    python import_shows.py shows.csv --db music.db
    python import_shows.py shows.csv --db music.db --act "Galaxie 500"
    python import_shows.py shows.csv --db music.db --update
    python import_shows.py shows.csv --db music.db --update --dry-run

--act is optional. If supplied it overrides the artistname column for
every row — use this for single-act sheets where artistname is absent
or unreliable. For multi-act sheets (Dean Wareham / Dean & Britta,
Damon & Naomi / Magic Hour) omit --act and let artistname drive it.

Default (additive) mode:
    New shows/venues/setlists/audio are inserted. Existing rows are left
    untouched — re-running never changes data already in the DB.

--update mode (CSV is the master):
    Scalar fields on existing shows and venues are updated to match the
    CSV. Each matched show's setlist and audio are deleted and rebuilt
    from the CSV, so reordering and added/removed entries propagate.
    Shows that exist in the DB but are absent from the CSV are NOT
    deleted — they are listed in a "review" report so they can be added
    to the sheet if appropriate.

--dry-run:
    Do everything but roll back instead of committing, printing a preview
    of what would change. Most useful combined with --update.

Setlist annotation detection:
    Titles starting with ^ or [^ are stage notes, not songs.
    These are stored in setlists.annotation with song_version_id = NULL.

Songs must already be in the DB (run import_tracks.py first).
If a setlist slug can't be matched, it is reported but not fatal.
"""

import argparse
import csv
import html as _html_mod
import re
import sqlite3
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_plain(text):
    if not text:
        return text
    text = _html_mod.unescape(text)
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r' {2,}', ' ', text)
    text = '\n'.join(line.strip() for line in text.split('\n'))
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip() or None


def norm(slug: str) -> str:
    return re.sub(r'[^a-z0-9]', '', slug.lower())

def sanitise_slug(slug: str) -> str:
    """Ensure slug is URL-safe: lowercase, hyphens, no punctuation.
    & is replaced with 'and' per style guide (song slugs only —
    act display names keep & but their slugs also use this function).
    """
    slug = slug.lower().strip()
    slug = slug.replace('&', 'and')
    slug = re.sub(r"['']", '', slug)  # strip apostrophes before hyphenating
    slug = re.sub(r'[^a-z0-9-]+', '-', slug)
    slug = re.sub(r'-{2,}', '-', slug).strip('-')
    return slug




def get_or_create(cur, table, lookup, extra=None):
    extra = extra or {}
    where = ' AND '.join(
        f'{k} IS NULL' if v is None else f'{k} = ?'
        for k, v in lookup.items()
    )
    params = [v for v in lookup.values() if v is not None]
    cur.execute(f'SELECT id FROM {table} WHERE {where}', params)
    row = cur.fetchone()
    if row:
        return row['id']
    data = {**lookup, **extra}
    cols = ', '.join(data.keys())
    ph   = ', '.join('?' * len(data))
    cur.execute(f'INSERT INTO {table} ({cols}) VALUES ({ph})', list(data.values()))
    return cur.lastrowid


def upsert(cur, table, key_col, key_val, fields, update):
    """Insert a row keyed on key_col=key_val; in update mode patch changed fields.

    Returns (row_id, status, changed_keys):
        status 'new'       — row was inserted
        status 'updated'   — existing row had changed fields written (update mode)
        status 'unchanged' — existing row already matched the CSV (update mode)
        status 'existing'  — existing row, left untouched (additive mode)
    """
    row = cur.execute(
        f'SELECT * FROM {table} WHERE {key_col} = ?', (key_val,)).fetchone()
    if row is None:
        data = {key_col: key_val, **fields}
        cols = ', '.join(data.keys())
        ph   = ', '.join('?' * len(data))
        cur.execute(f'INSERT INTO {table} ({cols}) VALUES ({ph})',
                    list(data.values()))
        return cur.lastrowid, 'new', list(fields.keys())

    row_id = row['id']
    if not update:
        return row_id, 'existing', []

    changed = [k for k, v in fields.items() if row[k] != v]
    if changed:
        set_clause = ', '.join(f'{k} = ?' for k in changed)
        cur.execute(f'UPDATE {table} SET {set_clause} WHERE id = ?',
                    [fields[k] for k in changed] + [row_id])
        return row_id, 'updated', changed
    return row_id, 'unchanged', []


def get_or_create_act(cur, name: str) -> int:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower().replace('&', 'and')).strip('-')
    return get_or_create(cur, 'acts', {'name': name}, {'slug': slug})


def parse_audio(audio_str: str) -> list[tuple[str, str, str | None]]:
    if not audio_str.strip():
        return []
    parts = [p.strip() for p in audio_str.split('|')]
    if len(parts) >= 3:
        return [(parts[0], parts[1], parts[2])]
    if len(parts) == 2:
        return [(parts[0], parts[1], None)]
    return []


# Matches a trailing bracketed suffix on a song title
# e.g. "Blue Thunder [w/sax]" or "Here She Comes Now [Velvet Underground cover]"
_TITLE_ANNOT_RE = re.compile(r'^(.*?)\s*\[([^\]]+)\]\s*$')


def title_to_norm(title: str) -> str:
    """
    Normalise a setlist title for matching against the songs table.
    Strips leading articles, punctuation, diacritics, lowercases.
    Used instead of setlist-index slugs (which are inconsistent).
    """
    import unicodedata
    # Strip diacritics
    title = ''.join(
        c for c in unicodedata.normalize('NFD', title)
        if unicodedata.category(c) != 'Mn'
    )
    # Strip everything except alphanumerics
    return re.sub(r'[^a-z0-9]', '', title.lower())


def parse_setlist(setlist_str: str) -> list[tuple[str, str | None, str | None]]:
    """
    Parse a pipe-separated setlist string.
    Returns [(raw_title, version_annotation, set_break_annotation), ...]

    - raw_title: the original title string
    - version_annotation: bracketed suffix on a song title, e.g. "w/sax",
        "with Peter Buck of REM on guitar". Stored in setlists.annotation.
    - set_break_annotation: for ^text or [^text] entries — set breaks,
        encores, stage notes. Song lookup is skipped for these.

    setlist-index is intentionally ignored — title matching is used instead.
    """
    titles = [t.strip() for t in setlist_str.split('|') if t.strip()]
    result = []

    for title in titles:
        # Set break / encore / stage note: starts with ^ or [^
        if title.startswith('[^') or title.startswith('^'):
            note = re.sub(r'^\[?\^', '', title).rstrip(']').strip()
            result.append((title, None, note))
            continue

        # Version/descriptive annotation in brackets at end of title
        m = _TITLE_ANNOT_RE.match(title)
        if m:
            clean_title    = m.group(1).strip()
            version_annot  = m.group(2).strip()
            result.append((title, version_annot, None))
        else:
            result.append((title, None, None))

    return result


def resolve_setlist_rows(parsed, act_id, sv_lookup, song_lookup,
                         cur, unresolved, date):
    """Resolve parsed setlist entries to insertable rows.

    Returns a list of (position, song_version_id, raw_title, annotation).
    Creates a canonical song_version on the fly when the song is known but
    has no version for this act yet (a live performance establishes the link).
    Unmatched titles are appended to `unresolved` and stored with NULL sv.
    """
    rows = []
    for position, (raw_title, version_annot, set_break) in enumerate(parsed, 1):
        sv_id      = None
        annotation = set_break or version_annot  # store whichever is set

        if not set_break:
            # Strip any bracketed suffix to get the bare title for matching
            m           = _TITLE_ANNOT_RE.match(raw_title)
            match_title = m.group(1).strip() if m else raw_title
            n           = title_to_norm(match_title)

            act_svs = sv_lookup.get(n, {})
            sv_id   = act_svs.get(act_id) or (
                       next(iter(act_svs.values())) if act_svs else None)

            if sv_id is None:
                song_id = song_lookup.get(n)
                if song_id:
                    # Song known but no version for this act yet —
                    # create the canonical song_version on the fly.
                    sv_id = get_or_create(
                        cur, 'song_versions',
                        {'song_id': song_id, 'act_id': act_id,
                         'version_tag': None},
                        {'full_title': match_title}
                    )
                    # Cache so subsequent shows don't repeat this
                    sv_lookup.setdefault(n, {})[act_id] = sv_id
                else:
                    unresolved.append((date, raw_title,
                                       'not in songs table — check tracks.csv'))

        rows.append((position, sv_id, raw_title, annotation))
    return rows


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------

def import_shows(csv_path: str, db_path: str, act_override: str | None,
                 update: bool = False, dry_run: bool = False) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    cur = con.cursor()
    con.executescript((Path(__file__).parent / 'schema.sql').read_text())

    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))

    mode = 'UPDATE' if update else 'additive'
    print(f"Loaded {len(rows)} rows from {csv_path}  (mode: {mode}"
          f"{', dry-run' if dry_run else ''})")

    # Build two lookups keyed on normalised title (not slug):
    #   sv_lookup:   norm(title) -> {act_id -> sv_id}  for songs WITH versions
    #   song_lookup: norm(title) -> song_id             for ALL songs (fallback)
    all_songs = cur.execute("SELECT id, title FROM songs").fetchall()
    song_lookup: dict[str, int] = {}
    for s in all_songs:
        song_lookup[title_to_norm(s['title'])] = s['id']

    all_sv = cur.execute("""
        SELECT sv.id, sv.act_id, sv.version_tag, s.title as song_title
        FROM song_versions sv
        JOIN songs s ON s.id = sv.song_id
        WHERE sv.version_tag IS NULL
    """).fetchall()

    # norm(title) -> act_id -> canonical sv_id
    sv_lookup: dict[str, dict[int, int]] = {}
    for sv in all_sv:
        n = title_to_norm(sv['song_title'])
        sv_lookup.setdefault(n, {})[sv['act_id']] = sv['id']

    stats = dict(shows_new=0, shows_updated=0, shows_unchanged=0,
                 venues_new=0, venues_updated=0,
                 setlists_replaced=0, audio_changed=0)
    setlist_entries = 0
    unresolved: list[tuple[str, str, str]] = []
    change_log: list[str] = []
    seen_act_ids: set[int] = set()
    venue_cache: dict[str, int] = {}        # slug -> id (first occurrence wins)
    venue_first: dict[str, dict] = {}       # slug -> fields of first occurrence
    venue_conflicts: dict[str, list] = {}   # slug -> differing field variants

    for row in rows:
        # Resolve act — override takes precedence, else artistname column
        act_name = act_override or (row.get('artistname') or '').strip()
        if not act_name:
            print(f"  WARNING: no act name for row {row.get('show-slug','?')}, skipping")
            continue
        act_id = get_or_create_act(cur, act_name)
        seen_act_ids.add(act_id)

        # Venue ---------------------------------------------------------
        # Venues are a deduplicated lookup, not owned by a single show row.
        # Handle each slug once per run (first occurrence canonical, matching
        # additive mode) so a venue referenced by many rows with inconsistent
        # spellings doesn't flip-flop. Disagreements are reported for review.
        venue_slug = row['_venue-slug'].strip()
        venue_fields = {
            'name':    row['venue'].strip(),
            'city':    row['city'].strip() or None,
            'state':   row['state'].strip() or None,
            'country': row['country'].strip() or None,
            'url':     row.get('venue-url', '').strip() or None,
        }
        if venue_slug in venue_cache:
            venue_id = venue_cache[venue_slug]
            if venue_fields != venue_first[venue_slug]:
                variants = venue_conflicts.setdefault(
                    venue_slug, [venue_first[venue_slug]])
                if venue_fields not in variants:
                    variants.append(venue_fields)
        else:
            venue_id, v_status, _ = upsert(
                cur, 'venues', 'slug', venue_slug, venue_fields, update)
            venue_cache[venue_slug] = venue_id
            venue_first[venue_slug] = venue_fields
            if v_status == 'new':
                stats['venues_new'] += 1
            elif v_status == 'updated':
                stats['venues_updated'] += 1

        # Show ----------------------------------------------------------
        show_slug     = row['show-slug'].strip()
        radio_raw     = row['radio'].strip()
        radio         = 1 if radio_raw else 0
        notes_parts   = [p for p in [to_plain(row['notes'].strip()),
                                     radio_raw if radio else ''] if p]
        show_fields = {
            'act_id':        act_id,
            'date':          row['date'].strip(),
            'year':          int(row['year'].strip()),
            'venue_id':      venue_id,
            'radio':         radio,
            'cancelled':     1 if row['cancelled'].strip().upper() == 'TRUE' else 0,
            'i_was_there':   1 if row['_i-was-there'].strip() else 0,
            'has_recording': 1 if row['_recording'].strip() or row['audio'].strip() else 0,
            'performers':    row['performers'].strip() or None,
            'support':       row['support'].strip() or None,
            'series':        row.get('series', '').strip() or None,
            'event':         row.get('event', '').strip() or None,
            'poster_url':    row['poster-url'].strip() or None,
            'ticket_url':    row.get('ticket-url', '').strip() or None,
            'notes':         '  '.join(notes_parts) or None,
            'setlist_source': row['setlist-source'].strip() or None,
        }
        show_id, s_status, changed = upsert(
            cur, 'shows', 'slug', show_slug, show_fields, update)
        show_existed = s_status != 'new'
        if s_status == 'new':
            stats['shows_new'] += 1
        elif s_status == 'updated':
            stats['shows_updated'] += 1
            change_log.append(f"  ~ show {show_slug}: {', '.join(changed)}")
        else:
            stats['shows_unchanged'] += 1

        # Audio ---------------------------------------------------------
        new_audio = parse_audio(row['audio'])
        if update:
            old_audio = cur.execute(
                'SELECT audio_type, url, label FROM show_audio WHERE show_id = ?',
                (show_id,)).fetchall()
            old_set = {(a['audio_type'], a['url'], a['label']) for a in old_audio}
            if show_existed and old_set != set(new_audio):
                stats['audio_changed'] += 1
                change_log.append(f"  ~ audio {show_slug}: "
                                  f"{len(old_set)} -> {len(new_audio)}")
            cur.execute('DELETE FROM show_audio WHERE show_id = ?', (show_id,))
            for audio_type, url, label in new_audio:
                cur.execute(
                    'INSERT INTO show_audio (show_id, audio_type, url, label) '
                    'VALUES (?,?,?,?)', (show_id, audio_type, url, label))
        else:
            for audio_type, url, label in new_audio:
                get_or_create(cur, 'show_audio',
                              {'show_id': show_id, 'url': url},
                              {'audio_type': audio_type, 'label': label})

        # Setlist — matched by normalised title, setlist-index ignored
        setlist_str = row['setlist'].strip()
        parsed      = parse_setlist(setlist_str) if setlist_str else []
        new_rows    = resolve_setlist_rows(
            parsed, act_id, sv_lookup, song_lookup, cur, unresolved,
            show_fields['date'])

        if update:
            old_rows = cur.execute(
                'SELECT position, raw_title, annotation FROM setlists '
                'WHERE show_id = ? ORDER BY position', (show_id,)).fetchall()
            old_cmp = [(r['position'], r['raw_title'], r['annotation'])
                       for r in old_rows]
            new_cmp = [(p, rt, an) for (p, _sv, rt, an) in new_rows]
            if show_existed and old_cmp != new_cmp:
                stats['setlists_replaced'] += 1
                change_log.append(f"  ~ setlist {show_slug}: "
                                  f"{len(old_cmp)} -> {len(new_cmp)} entries")
            cur.execute('DELETE FROM setlists WHERE show_id = ?', (show_id,))
            for position, sv_id, raw_title, annotation in new_rows:
                cur.execute(
                    'INSERT INTO setlists '
                    '(show_id, position, song_version_id, raw_title, annotation) '
                    'VALUES (?,?,?,?,?)',
                    (show_id, position, sv_id, raw_title, annotation))
                setlist_entries += 1
        else:
            for position, sv_id, raw_title, annotation in new_rows:
                cur.execute(
                    'INSERT OR IGNORE INTO setlists '
                    '(show_id, position, song_version_id, raw_title, annotation) '
                    'VALUES (?,?,?,?,?)',
                    (show_id, position, sv_id, raw_title, annotation))
                setlist_entries += 1

    # Shows in DB (for the acts in this CSV) that are absent from the CSV.
    missing: list[tuple[str, str, str]] = []
    if update and seen_act_ids:
        csv_slugs = {r['show-slug'].strip() for r in rows
                     if r.get('show-slug', '').strip()}
        ph = ','.join('?' * len(seen_act_ids))
        db_shows = cur.execute(
            f'SELECT sh.slug, sh.date, a.name AS act FROM shows sh '
            f'JOIN acts a ON a.id = sh.act_id '
            f'WHERE sh.act_id IN ({ph}) ORDER BY sh.date',
            list(seen_act_ids)).fetchall()
        missing = [(s['act'], s['date'], s['slug'])
                   for s in db_shows if s['slug'] not in csv_slugs]

    if dry_run:
        con.rollback()
    else:
        con.commit()
    con.close()

    # ----- Summary -----------------------------------------------------
    print()
    if update:
        print("Shows:     "
              f"{stats['shows_new']} new, "
              f"{stats['shows_updated']} updated, "
              f"{stats['shows_unchanged']} unchanged")
        print("Venues:    "
              f"{stats['venues_new']} new, {stats['venues_updated']} updated")
        print("Replaced:  "
              f"{stats['setlists_replaced']} setlists, "
              f"{stats['audio_changed']} audio (on existing shows)")
        print(f"Setlist entries written: {setlist_entries}")
        if change_log:
            print(f"\nChanges ({len(change_log)}):")
            for line in change_log[:100]:
                print(line)
            if len(change_log) > 100:
                print(f"  ... and {len(change_log) - 100} more")
    else:
        unique_venues = len(set(r['_venue-slug'] for r in rows))
        print(f"Imported:  {stats['shows_new']} shows,  "
              f"{setlist_entries} setlist entries")
        print(f"Venues:    {unique_venues} unique")

    # ----- Shows present in DB but not in CSV (review only) ------------
    if update:
        if missing:
            print(f"\nIn DB but not in CSV ({len(missing)}) "
                  f"— review and add to the sheet if needed:")
            for act, date, slug in missing:
                print(f"  {date}  {slug}  [{act}]")
        else:
            print("\nEvery DB show for these acts is present in the CSV ✓")

    # ----- Venue details that disagree across rows (review only) -------
    if update and venue_conflicts:
        print(f"\nVenue details differ across rows ({len(venue_conflicts)}) "
              f"— first occurrence was used; fix the sheet to make consistent:")
        for slug, variants in venue_conflicts.items():
            print(f"  {slug}:")
            for v in variants:
                detail = ', '.join(f'{k}={v[k]!r}' for k in v if v[k])
                print(f"    {detail}")

    # ----- Unresolved setlist songs ------------------------------------
    if unresolved:
        no_version = [(d,t,r) for d,t,r in unresolved
                      if r == 'song exists, no version for act yet']
        no_song    = [(d,t,r) for d,t,r in unresolved
                      if r == 'not in songs table — check tracks.csv']

        if no_version:
            print(f"\nSetlist songs with no act version yet ({len(no_version)}) "
                  f"— will resolve once release sheets are imported:")
            # Deduplicate — same song unresolved across many shows is just noise
            seen = set()
            for date, title, _ in no_version:
                if title not in seen:
                    print(f"  {title!r}")
                    seen.add(title)

        if no_song:
            print(f"\nSetlist songs not in songs table ({len(no_song)}) "
                  f"— check tracks.csv:")
            seen = set()
            for date, title, _ in no_song:
                if title not in seen:
                    print(f"  {date}  {title!r}")
                    seen.add(title)
    else:
        print("\nAll setlist entries resolved ✓")

    if dry_run:
        print("\n[dry-run] rolled back — no changes written")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Import live shows CSV into the music SQLite database.')
    parser.add_argument('csv', help='Path to shows CSV')
    parser.add_argument('--db',  default='music.db')
    parser.add_argument('--act', default=None,
                        help='Override act for all rows (omit for multi-act sheets)')
    parser.add_argument('--update', action='store_true',
                        help='CSV is master: update existing shows/venues and '
                             'rebuild their setlists/audio from the CSV')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without committing (rolls back)')
    args = parser.parse_args()
    import_shows(args.csv, args.db, args.act,
                 update=args.update, dry_run=args.dry_run)
