#!/usr/bin/env python3
"""
import_releases.py  (v4 — act_id on song_versions, global song slugs)
----------------------------------------------------------------------
Imports a releases CSV into the music SQLite database.

Usage:
    python import_releases.py <csv_file> --db music.db --release-type Album
    python import_releases.py singles.csv --db music.db --release-type Single
    python import_releases.py misc.csv    --db music.db --release-type Misc

release_type reflects what was encoded in the original sheet name:
    Album / Single / EP / Misc

Songs must already be in the DB (run import_tracks.py first).
This script creates song_versions (linking act + song + version_tag)
and edition_tracks. It will NOT create new songs — if a track slug
from the release sheet can't be matched to an existing song, it is
reported as unresolved.

Song version detection:
    "Blue Thunder [w/sax]" → song slug "blue-thunder", version_tag "w/sax"
    "Blue Thunder"          → song slug "blue-thunder", version_tag NULL
"""

import argparse
import csv
import html as _html_mod
import re
import sqlite3
import sys
from pathlib import Path

from sort_title import make_sort_title


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
    fmt   = row.get('Format', '')
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


def parse_versions(versions_str: str) -> list[tuple[str, str]]:
    results = []
    for part in versions_str.split(';'):
        part = part.strip()
        if not part:
            continue
        if '|' in part:
            label, url = part.split('|', 1)
            results.append((label.strip(), url.strip()))
        else:
            results.append((part, ''))
    return results


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


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_csv(csv_path: str, db_path: str, release_type: str) -> None:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    cur = con.cursor()
    con.executescript((Path(__file__).parent / 'schema.sql').read_text())

    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        rows = list(csv.DictReader(fh))

    if not rows:
        print('Empty CSV.')
        return

    master_row   = next((r for r in rows if r.get('Format') == 'MASTER'), None)
    edition_rows = [r for r in rows if r.get('Format') != 'MASTER']

    if not edition_rows:
        print('No edition rows found.')
        return

    meta     = master_row or edition_rows[0]
    act_name = meta.get('Artist', '').strip()
    if not act_name:
        print('ERROR: No Artist value found.', file=sys.stderr)
        sys.exit(1)

    act_id = get_or_create_act(cur, act_name)

    # Singles/Misc/EP: group editions by their own Title column
    single_release_sheets = release_type in ('Single', 'Misc', 'EP')

    unresolved = []

    if single_release_sheets:
        seen_releases: dict[str, int] = {}
        song_lookup = build_song_lookup(cur)
        for erow in edition_rows:
            title      = erow.get('Title', '').strip()
            year_raw   = (erow.get('Date') or '').strip()
            year       = int(year_raw) if year_raw else None
            sleeve_url = (erow.get('Sleeve') or '').strip() or None

            if title not in seen_releases:
                release_id = get_or_create(
                    cur, 'releases',
                    {'act_id': act_id, 'title': title, 'release_type': release_type},
                    {'slug': sanitise_slug(title), 'year': year, 'sleeve_url': sleeve_url,
                     'album_artist': (erow.get('AlbumArtist') or '').strip() or None}
                )
                seen_releases[title] = release_id
                print(f'Release: "{title}" ({release_type})  id={release_id}')
            else:
                release_id = seen_releases[title]

            u = _import_edition(cur, erow, release_id, act_id, None, song_lookup)
            unresolved.extend(u)
    else:
        release_title = meta.get('Title', '').strip()
        year          = int(meta['Date']) if (meta.get('Date') or '').strip() else None
        sleeve_url    = (meta.get('Sleeve') or '').strip() or None

        release_id = get_or_create(
            cur, 'releases',
            {'act_id': act_id, 'title': release_title, 'release_type': release_type},
            {'slug': sanitise_slug(release_title), 'year': year, 'sleeve_url': sleeve_url,
             'album_artist': (meta.get('AlbumArtist') or '').strip() or None}
        )
        print(f'Release: "{release_title}" ({release_type})  id={release_id}')

        song_lookup = build_song_lookup(cur)
        for erow in edition_rows:
            u = _import_edition(cur, erow, release_id, act_id, sleeve_url, song_lookup)
            unresolved.extend(u)

    if unresolved:
        print(f'\n  Unresolved tracks ({len(unresolved)}) '
              f'— not found in songs table (run import_tracks.py first?):')
        for (title,) in unresolved:
            print(f'    {title!r}')

    con.commit()
    con.close()
    print('\nDone.')


def _import_edition(cur, erow, release_id, act_id,
                    canonical_sleeve, song_lookup) -> list[tuple]:
    """Import one edition row. Returns list of (title,) unresolved tracks."""
    fmt      = erow.get('Format', '').strip()
    country  = (erow.get('Country') or '').strip() or None
    year_raw = (erow.get('Date') or '').strip()
    year     = int(year_raw) if year_raw else None
    label    = (erow.get('Label') or '').strip() or None
    cat_no   = (erow.get('CatNo') or '').strip() or None
    sleeve   = (erow.get('Sleeve') or '').strip() or None
    if sleeve == canonical_sleeve:
        sleeve = None
    notes    = to_plain((erow.get('Notes') or '').strip()) or None
    ahfow    = (erow.get('AHFOW') or '').strip() or None
    my_coll  = (erow.get('My-record-collection') or '').strip() or None
    if my_coll and ';' in my_coll:
        my_coll = None

    cur.execute(
        '''INSERT INTO editions
           (release_id, format, country, year, label, catalogue_no,
            sleeve_url, notes, ahfow_ref, my_collection_url)
           VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (release_id, fmt, country, year, label, cat_no,
         sleeve, notes, ahfow, my_coll)
    )
    edition_id = cur.lastrowid
    print(f'  Edition: {fmt:6}  {country or "--":3}  {year or "----"}  '
          f'{label or "":25}  {cat_no or ""}')

    tracks = parse_tracks(erow)
    unresolved = []

    if not tracks:
        return unresolved

    side_counters: dict = {}
    for position, (full_title, side) in enumerate(tracks, start=1):
        # ^ prefix = track by unrelated artist on a split single — skip
        if full_title.startswith('^'):
            print(f'    (skipping split track: {full_title[1:].strip()!r})')
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
            (edition_id, sv_id, position, side, side_counters[side])
        )

    return unresolved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Import a releases CSV into the music SQLite database.')
    parser.add_argument('csv', help='Path to CSV file')
    parser.add_argument('--db', default='music.db')
    parser.add_argument('--release-type', default='Album',
                        help='Album | Single | EP | Misc')
    args = parser.parse_args()
    import_csv(args.csv, args.db, args.release_type)
