#!/usr/bin/env python3
"""
import_shows.py  (v4 — act resolved per row from artistname column)
--------------------------------------------------------------------
Imports a live shows CSV into the music SQLite database.

Usage:
    python import_shows.py shows.csv --db music.db
    python import_shows.py shows.csv --db music.db --act "Galaxie 500"

--act is optional. If supplied it overrides the artistname column for
every row — use this for single-act sheets where artistname is absent
or unreliable. For multi-act sheets (Dean Wareham / Dean & Britta,
Damon & Naomi / Magic Hour) omit --act and let artistname drive it.

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


# ---------------------------------------------------------------------------
# Core import
# ---------------------------------------------------------------------------

def import_shows(csv_path: str, db_path: str, act_override: str | None) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    cur = con.cursor()
    con.executescript((Path(__file__).parent / 'schema.sql').read_text())

    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))

    print(f"Loaded {len(rows)} rows from {csv_path}")

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

    shows_imported = 0
    setlist_entries = 0
    unresolved: list[tuple[str, str, str]] = []

    for row in rows:
        # Resolve act — override takes precedence, else artistname column
        act_name = act_override or (row.get('artistname') or '').strip()
        if not act_name:
            print(f"  WARNING: no act name for row {row.get('show-slug','?')}, skipping")
            continue
        act_id = get_or_create_act(cur, act_name)

        # Venue
        venue_slug = row['_venue-slug'].strip()
        venue_url  = row.get('venue-url', '').strip() or None
        venue_id   = get_or_create(
            cur, 'venues',
            {'slug': venue_slug},
            {'name':    row['venue'].strip(),
             'city':    row['city'].strip() or None,
             'state':   row['state'].strip() or None,
             'country': row['country'].strip() or None,
             'url':     venue_url}
        )

        show_slug     = row['show-slug'].strip()
        date          = row['date'].strip()
        year          = int(row['year'].strip())
        radio_raw     = row['radio'].strip()
        radio         = 1 if radio_raw else 0
        cancelled     = 1 if row['cancelled'].strip().upper() == 'TRUE' else 0
        i_was_there   = 1 if row['_i-was-there'].strip() else 0
        has_recording = 1 if row['_recording'].strip() or row['audio'].strip() else 0
        performers    = row['performers'].strip() or None
        support       = row['support'].strip() or None
        series        = row.get('series', '').strip() or None
        event         = row.get('event', '').strip() or None
        poster_url    = row['poster-url'].strip() or None
        ticket_url    = row.get('ticket-url', '').strip() or None
        notes_parts   = [p for p in [to_plain(row['notes'].strip()),
                                     radio_raw if radio else ''] if p]
        notes         = '  '.join(notes_parts) or None
        setlist_src   = row['setlist-source'].strip() or None

        show_id = get_or_create(
            cur, 'shows',
            {'slug': show_slug},
            {'act_id': act_id, 'date': date, 'year': year,
             'venue_id': venue_id, 'radio': radio, 'cancelled': cancelled,
             'i_was_there': i_was_there, 'has_recording': has_recording,
             'performers': performers, 'support': support,
             'series': series, 'event': event,
             'poster_url': poster_url, 'ticket_url': ticket_url,
             'notes': notes, 'setlist_source': setlist_src}
        )
        shows_imported += 1

        # Audio
        for audio_type, url, label in parse_audio(row['audio']):
            get_or_create(cur, 'show_audio',
                          {'show_id': show_id, 'url': url},
                          {'audio_type': audio_type, 'label': label})

        # Setlist — matched by normalised title, setlist-index ignored
        setlist_str = row['setlist'].strip()
        if setlist_str:
            entries = parse_setlist(setlist_str)
            for position, (raw_title, version_annot, set_break) in enumerate(entries, 1):
                sv_id      = None
                annotation = set_break or version_annot  # store whichever is set

                if not set_break:
                    # Strip any bracketed suffix to get the bare title for matching
                    m          = _TITLE_ANNOT_RE.match(raw_title)
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
                            # A live performance is sufficient to establish the link.
                            sv_id = get_or_create(
                                cur, 'song_versions',
                                {'song_id': song_id, 'act_id': act_id,
                                 'version_tag': None},
                                {'full_title': match_title}
                            )
                            # Add to lookup so subsequent shows don't repeat this
                            sv_lookup.setdefault(n, {})[act_id] = sv_id
                        else:
                            unresolved.append((date, raw_title,
                                               'not in songs table — check tracks.csv'))

                cur.execute(
                    '''INSERT OR IGNORE INTO setlists
                       (show_id, position, song_version_id, raw_title, annotation)
                       VALUES (?,?,?,?,?)''',
                    (show_id, position, sv_id, raw_title, annotation)
                )
                setlist_entries += 1

    con.commit()
    con.close()

    unique_venues = len(set(r['_venue-slug'] for r in rows))
    print(f"\nImported:  {shows_imported} shows,  {setlist_entries} setlist entries")
    print(f"Venues:    {unique_venues} unique")

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
    args = parser.parse_args()
    import_shows(args.csv, args.db, args.act)
