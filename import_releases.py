#!/usr/bin/env python3
"""
import_releases.py  (v5 — one sheet per artist; --act / --update / --dry-run)
-----------------------------------------------------------------------------
Imports a releases CSV into the music SQLite database.

Usage:
    python import_releases.py galaxie-500-releases.csv --db music.db --act "Galaxie 500"
    python import_releases.py galaxie-500-releases.csv --db music.db --act "Galaxie 500" --update
    python import_releases.py galaxie-500-releases.csv --db music.db --act "Galaxie 500" --update --dry-run

Sheet format (one sheet per artist):
    Columns: release_title, release_type, year, album_artist, format, country,
             label, cat_no, sleeve, notes, ahfow, my_collection, A, B, C, D
    There is no MASTER row and no Artist column — the act is supplied with
    --act. Each row is one edition; rows sharing a release_title group into a
    single release. release_type is per-row.

release_type mapping (sheet value -> stored value):
    album -> Album,  single -> Single,  ep -> EP,
    everything else (compilation, demo, video, ...) -> Misc

Songs must already be in the DB (run import_tracks.py first).
This script creates song_versions (linking act + song + version_tag) and
edition_tracks. It will NOT create new songs — a track that can't be matched
to an existing song is reported as unresolved.

Default (additive) mode inserts releases/editions. --update makes the CSV the
master: release scalar fields are patched and each release's editions +
tracklists are rebuilt from the CSV when their content has changed. Releases in
the DB but absent from the CSV are reported, not deleted. --dry-run previews
everything and rolls back.

Song version detection:
    "Blue Thunder [w/sax]" -> song slug "blue-thunder", version_tag "w/sax"
    "Blue Thunder"          -> song slug "blue-thunder", version_tag NULL
"""

import argparse
import csv
import html as _html_mod
import re
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path


# Sheet release_type -> stored (legacy) release_type. Unknown values -> Misc.
RELEASE_TYPE_MAP = {
    'album':  'Album',
    'single': 'Single',
    'ep':     'EP',
}


def map_release_type(raw: str) -> str:
    return RELEASE_TYPE_MAP.get((raw or '').strip().lower(), 'Misc')


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




_VERSION_RE = re.compile(r'^(.*?)\s*\[([^\]]+)\]\s*$')


def split_version(full_title: str) -> tuple[str, str | None]:
    m = _VERSION_RE.match(full_title.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return full_title.strip(), None


def parse_tracks(row: dict) -> list[tuple[str, str | None]]:
    fmt   = (row.get('format') or '').strip()
    is_cd = fmt in ('CD', '2×CD', '2xCD')
    tracks = []
    for side in ('A', 'B', 'C', 'D'):
        raw = (row.get(side) or '').strip()
        if not raw:
            continue
        for title in raw.split('|'):
            title = title.strip()
            if title:
                tracks.append((title, None if is_cd else side))
    return tracks


def title_to_norm(title: str) -> str:
    """
    Normalise a track title for matching against the songs table.
    Strips diacritics, punctuation, lowercases.
    Track-index is intentionally ignored — title matching is used instead.
    """
    import unicodedata
    title = ''.join(
        c for c in unicodedata.normalize('NFD', title)
        if unicodedata.category(c) != 'Mn'
    )
    return re.sub(r'[^a-z0-9]', '', title.lower())


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

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


def build_song_lookup(cur) -> dict:
    """Build norm(title) -> song_id map for all songs. Called once per import."""
    return {title_to_norm(r['title']): r['id']
            for r in cur.execute('SELECT id, title FROM songs')}


def resolve_song_version(cur, act_id: int, full_title: str,
                         song_lookup: dict) -> int | None:
    """
    Find song by normalised title using pre-built lookup, then
    get_or_create its song_version for this act.
    Returns song_version.id or None if song not found.
    Track-index slugs are intentionally ignored.
    """
    canonical_title, version_tag = split_version(full_title)
    song_id = song_lookup.get(title_to_norm(canonical_title))
    if not song_id:
        return None
    return get_or_create(
        cur, 'song_versions',
        {'song_id': song_id, 'act_id': act_id, 'version_tag': version_tag},
        {'full_title': full_title}
    )


def upsert_release(cur, act_id, slug, title, fields, update):
    """Insert/lookup a release keyed on (act_id, slug); patch in update mode.

    Returns (release_id, status, changed_keys) with status in
    {'new','updated','unchanged','existing'} (see import_shows.upsert).
    """
    row = cur.execute(
        'SELECT * FROM releases WHERE act_id = ? AND slug = ?',
        (act_id, slug)).fetchone()
    if row is None:
        data = {'act_id': act_id, 'slug': slug, 'title': title, **fields}
        cols = ', '.join(data.keys())
        ph   = ', '.join('?' * len(data))
        cur.execute(f'INSERT INTO releases ({cols}) VALUES ({ph})',
                    list(data.values()))
        return cur.lastrowid, 'new', list(fields.keys()) + ['title']

    rid = row['id']
    if not update:
        return rid, 'existing', []
    allf = {'title': title, **fields}
    changed = [k for k, v in allf.items() if row[k] != v]
    if changed:
        set_clause = ', '.join(f'{k} = ?' for k in changed)
        cur.execute(f'UPDATE releases SET {set_clause} WHERE id = ?',
                    [allf[k] for k in changed] + [rid])
        return rid, 'updated', changed
    return rid, 'unchanged', []


# ---------------------------------------------------------------------------
# Edition field extraction + signatures (used for insert and change detection)
# ---------------------------------------------------------------------------

def edition_fields(erow: dict, canonical_sleeve: str | None) -> dict:
    """The stored edition column values derived from a CSV row.

    Shared by the insert path and the signature comparison so the two never
    diverge. sleeve is nulled when it equals the release's canonical sleeve;
    a my_collection value containing ';' is treated as absent (legacy quirk).
    """
    sleeve = (erow.get('sleeve') or '').strip() or None
    if sleeve == canonical_sleeve:
        sleeve = None
    my_coll = (erow.get('my_collection') or '').strip() or None
    if my_coll and ';' in my_coll:
        my_coll = None
    year_raw = (erow.get('year') or '').strip()
    return {
        'format':            (erow.get('format') or '').strip(),
        'country':           (erow.get('country') or '').strip() or None,
        'year':              int(year_raw) if year_raw.isdigit() else None,
        'label':             (erow.get('label') or '').strip() or None,
        'catalogue_no':      (erow.get('cat_no') or '').strip() or None,
        'sleeve_url':        sleeve,
        'notes':             to_plain((erow.get('notes') or '').strip()) or None,
        'ahfow_ref':         (erow.get('ahfow') or '').strip() or None,
        'my_collection_url': my_coll,
    }


def csv_track_sig(erow: dict, song_lookup: dict) -> tuple:
    """(side, song_id, version_tag) for each resolvable, non-split track.

    An edition_track is defined by *which song_version it links* — i.e.
    (song, version_tag) — not by the raw title string. Comparing on song
    identity avoids spurious "changed" reports when the same song appears
    with inconsistent capitalisation across editions (full_title is the
    first-seen casing and is reconciled later by import_tracks.py anyway).
    ^-prefixed split tracks and titles with no matching song are excluded.
    """
    out = []
    for full_title, side in parse_tracks(erow):
        if full_title.startswith('^'):
            continue
        canonical, version_tag = split_version(full_title)
        song_id = song_lookup.get(title_to_norm(canonical))
        if song_id:
            out.append((side, song_id, version_tag))
    return tuple(out)


def csv_release_signature(erows, canonical_sleeve, song_lookup) -> list:
    return [(edition_fields(e, canonical_sleeve), csv_track_sig(e, song_lookup))
            for e in erows]


def db_release_signature(cur, release_id) -> list:
    sig = []
    for e in cur.execute(
            'SELECT * FROM editions WHERE release_id = ? ORDER BY id',
            (release_id,)).fetchall():
        fields = {
            'format':            e['format'],
            'country':           e['country'],
            'year':              e['year'],
            'label':             e['label'],
            'catalogue_no':      e['catalogue_no'],
            'sleeve_url':        e['sleeve_url'],
            'notes':             e['notes'],
            'ahfow_ref':         e['ahfow_ref'],
            'my_collection_url': e['my_collection_url'],
        }
        tracks = tuple(
            (t['side'], t['song_id'], t['version_tag']) for t in cur.execute(
                '''SELECT et.side, sv.song_id, sv.version_tag FROM edition_tracks et
                   JOIN song_versions sv ON sv.id = et.song_version_id
                   WHERE et.edition_id = ? ORDER BY et.position''',
                (e['id'],)).fetchall())
        sig.append((fields, tracks))
    return sig


def _import_edition(cur, erow, release_id, act_id,
                    canonical_sleeve, song_lookup) -> list[tuple]:
    """Insert one edition row + its tracklist. Returns unresolved (title,) tuples."""
    f = edition_fields(erow, canonical_sleeve)
    cur.execute(
        '''INSERT INTO editions
           (release_id, format, country, year, label, catalogue_no,
            sleeve_url, notes, ahfow_ref, my_collection_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (release_id, f['format'], f['country'], f['year'], f['label'],
         f['catalogue_no'], f['sleeve_url'], f['notes'], f['ahfow_ref'],
         f['my_collection_url']))
    edition_id = cur.lastrowid

    unresolved = []
    side_counters: dict = {}
    for position, (full_title, side) in enumerate(parse_tracks(erow), start=1):
        # ^ prefix = track by an unrelated artist on a split single — skip
        if full_title.startswith('^'):
            continue
        sv_id = resolve_song_version(cur, act_id, full_title, song_lookup)
        if sv_id is None:
            unresolved.append((full_title,))
            continue
        side_counters[side] = side_counters.get(side, 0) + 1
        cur.execute(
            '''INSERT OR IGNORE INTO edition_tracks
               (edition_id, song_version_id, position, side, side_position)
               VALUES (?,?,?,?,?)''',
            (edition_id, sv_id, position, side, side_counters[side]))
    return unresolved


def _delete_editions(cur, release_id):
    """Remove a release's editions and their tracklists (no ON DELETE CASCADE)."""
    ed_ids = [r['id'] for r in cur.execute(
        'SELECT id FROM editions WHERE release_id = ?', (release_id,)).fetchall()]
    for eid in ed_ids:
        cur.execute('DELETE FROM edition_tracks WHERE edition_id = ?', (eid,))
    cur.execute('DELETE FROM editions WHERE release_id = ?', (release_id,))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_csv(csv_path: str, db_path: str, act_name: str,
               update: bool = False, dry_run: bool = False) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    cur = con.cursor()
    con.executescript((Path(__file__).parent / 'schema.sql').read_text())

    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print('Empty CSV.')
        con.close()
        return

    act_id = get_or_create_act(cur, act_name)
    song_lookup = build_song_lookup(cur)

    mode = 'UPDATE' if update else 'additive'
    print(f"Loaded {len(rows)} rows from {csv_path}  (act: {act_name}, "
          f"mode: {mode}{', dry-run' if dry_run else ''})")

    # Group edition rows by release_title (preserving sheet order).
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        title = (r.get('release_title') or '').strip()
        if not title:
            continue
        groups.setdefault(title, []).append(r)

    stats = dict(rel_new=0, rel_updated=0, rel_unchanged=0,
                 editions_rebuilt=0)
    change_log: list[str] = []
    unresolved: list[tuple] = []
    seen_slugs: set[str] = set()

    for title, erows in groups.items():
        first            = erows[0]
        release_type     = map_release_type(first.get('release_type', ''))
        slug             = sanitise_slug(title)
        canonical_sleeve = (first.get('sleeve') or '').strip() or None
        album_artist     = (first.get('album_artist') or '').strip() or None
        years            = [int(e['year']) for e in erows
                            if (e.get('year') or '').strip().isdigit()]
        release_fields = {
            'release_type': release_type,
            'year':         min(years) if years else None,
            'sleeve_url':   canonical_sleeve,
            'album_artist': album_artist,
        }
        seen_slugs.add(slug)

        release_id, status, changed = upsert_release(
            cur, act_id, slug, title, release_fields, update)
        if status == 'new':
            stats['rel_new'] += 1
        elif status == 'updated':
            stats['rel_updated'] += 1
            change_log.append(f"  ~ release {slug}: {', '.join(changed)}")
        elif status == 'unchanged':
            stats['rel_unchanged'] += 1

        # Editions ------------------------------------------------------
        if update and status != 'new':
            # Rebuild only when the edition set / tracklists actually changed,
            # so re-running an unchanged sheet is a no-op.
            csv_sig = csv_release_signature(erows, canonical_sleeve, song_lookup)
            db_sig  = db_release_signature(cur, release_id)
            if csv_sig != db_sig:
                _delete_editions(cur, release_id)
                for erow in erows:
                    unresolved.extend(
                        _import_edition(cur, erow, release_id, act_id,
                                        canonical_sleeve, song_lookup))
                stats['editions_rebuilt'] += 1
                change_log.append(
                    f"  ~ editions {slug}: {len(db_sig)} -> {len(csv_sig)}")
        else:
            # New release (either mode) or additive re-import: insert editions.
            for erow in erows:
                unresolved.extend(
                    _import_edition(cur, erow, release_id, act_id,
                                    canonical_sleeve, song_lookup))

    # Releases in the DB for this act that are absent from the CSV (review).
    missing = []
    if update:
        db_rels = cur.execute(
            'SELECT slug, title, release_type FROM releases '
            'WHERE act_id = ? ORDER BY title', (act_id,)).fetchall()
        missing = [(r['title'], r['release_type'], r['slug'])
                   for r in db_rels if r['slug'] not in seen_slugs]

    if dry_run:
        con.rollback()
    else:
        con.commit()
    con.close()

    # ----- Summary -----------------------------------------------------
    print()
    if update:
        print(f"Releases:  {stats['rel_new']} new, "
              f"{stats['rel_updated']} updated, "
              f"{stats['rel_unchanged']} unchanged")
        print(f"Editions:  {stats['editions_rebuilt']} releases rebuilt "
              f"(content changed)")
        if change_log:
            print(f"\nChanges ({len(change_log)}):")
            for line in change_log[:100]:
                print(line)
            if len(change_log) > 100:
                print(f"  ... and {len(change_log) - 100} more")
    else:
        print(f"Imported:  {stats['rel_new']} releases "
              f"from {len(groups)} titles")

    if update:
        if missing:
            print(f"\nIn DB but not in CSV ({len(missing)}) "
                  f"— review and add to the sheet if needed:")
            for title, rtype, slug in missing:
                print(f"  {slug}  [{rtype}]  {title!r}")
        else:
            print("\nEvery DB release for this act is present in the CSV ✓")

    if unresolved:
        print(f'\nUnresolved tracks ({len(unresolved)}) '
              f'— not found in songs table (run import_tracks.py first?):')
        seen = set()
        for (track_title,) in unresolved:
            if track_title not in seen:
                print(f'    {track_title!r}')
                seen.add(track_title)
    else:
        print('\nAll tracks resolved ✓')

    if dry_run:
        print("\n[dry-run] rolled back — no changes written")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Import a per-artist releases CSV into the music database.')
    parser.add_argument('csv', help='Path to CSV file')
    parser.add_argument('--db', default='music.db')
    parser.add_argument('--act', required=True,
                        help='Act name for every row, e.g. "Galaxie 500"')
    parser.add_argument('--update', action='store_true',
                        help='CSV is master: update existing releases and '
                             'rebuild their editions/tracklists from the CSV')
    parser.add_argument('--dry-run', action='store_true',
                        help='Preview changes without committing (rolls back)')
    args = parser.parse_args()
    import_csv(args.csv, args.db, args.act,
               update=args.update, dry_run=args.dry_run)
