#!/usr/bin/env python3
"""
import_tracks.py  (v4 — songs as global catalogue)
----------------------------------------------------
Imports tracks.csv as the sole authority for the songs table.
Songs are global — no act scoping. Acts are associated at the
song_versions level, not here.

Two modes (both run by default):
  --check    Report discrepancies between tracks.csv and songs table.
  --import   Upsert all tracks into songs table.

Usage:
    python import_tracks.py tracks.csv --db music.db [--check] [--import]

Slug convention:
    tracks.csv uses hyphenated slugs ("blue-thunder") — these are canonical.
    Any squished slugs already in the DB are migrated to hyphenated on import.
"""

import argparse
import csv
import html as _html_mod
import re
import sqlite3
from pathlib import Path

from sort_title import make_sort_title


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def to_plain(text):
    """Strip HTML tags and unescape entities for plain text storage."""
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
    """Ensure slug is URL-safe: lowercase, hyphens only, no punctuation.
    & is replaced with 'and' per style guide.
    Strips question marks, apostrophes, and other non-alphanumeric characters
    that may have crept into source slugs.
    """
    slug = slug.lower().strip()
    slug = slug.replace('&', 'and')
    slug = re.sub(r"['']", '', slug)  # strip apostrophes before hyphenating
    # Replace any character that isn't alphanumeric or hyphen with a hyphen
    slug = re.sub(r'[^a-z0-9-]+', '-', slug)
    # Collapse multiple hyphens and strip leading/trailing
    slug = re.sub(r'-{2,}', '-', slug).strip('-')
    return slug


def load_tracks(csv_path: str) -> tuple[dict, dict]:
    rows_by_slug = {}
    norm_to_slug = {}
    with open(csv_path, newline='', encoding='utf-8-sig') as fh:
        for row in csv.DictReader(fh):
            slug = sanitise_slug(row['slug'].strip())
            rows_by_slug[slug] = row
            norm_to_slug[norm(slug)] = slug
    return rows_by_slug, norm_to_slug


# ---------------------------------------------------------------------------
# Consistency check
# ---------------------------------------------------------------------------

def run_check(cur, tracks, norm_to_slug):
    issues = []
    db_songs = cur.execute(
        'SELECT id, slug, title FROM songs ORDER BY sort_title'
    ).fetchall()

    for row in db_songs:
        track = tracks.get(row['slug']) or tracks.get(
                norm_to_slug.get(norm(row['slug']), ''))
        if not track:
            issues.append({'type': 'missing_from_tracks', 'slug': row['slug'],
                           'detail': f"In DB but not in tracks.csv — {row['title']!r}"})
        elif norm(row['title']) != norm(track['title']):
            issues.append({'type': 'title_mismatch', 'slug': row['slug'],
                           'detail': f"DB: {row['title']!r}  tracks.csv: {track['title']!r}"})

    db_norms = {norm(r['slug']) for r in db_songs}
    for slug in tracks:
        if norm(slug) not in db_norms:
            issues.append({'type': 'not_yet_in_db', 'slug': slug,
                           'detail': f"Not yet in DB: {tracks[slug]['title']!r}"})
    return issues


def print_issues(issues):
    by_type = {}
    for iss in issues:
        by_type.setdefault(iss['type'], []).append(iss)
    labels = {
        'missing_from_tracks': 'In DB but MISSING from tracks.csv',
        'title_mismatch':      'Title mismatches',
        'not_yet_in_db':       'In tracks.csv, not yet in DB',
    }
    for t, label in labels.items():
        group = by_type.get(t, [])
        if not group:
            continue
        print(f"\n{'─'*60}\n{label}  ({len(group)})\n{'─'*60}")
        for iss in group:
            print(f"  [{iss['slug']}]  {iss['detail']}")
    problems = sum(len(by_type.get(t, []))
                   for t in ('missing_from_tracks', 'title_mismatch'))
    print(f"\n{'═'*60}")
    print(f"Problems: {problems}   "
          f"Not yet imported: {len(by_type.get('not_yet_in_db', []))}")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def run_import(cur, tracks, norm_to_slug):
    inserted = updated = 0
    for hyph_slug, track in sorted(tracks.items(),
                                   key=lambda x: make_sort_title(x[1]['title'])):
        title           = track['title'].strip()
        sort_title      = make_sort_title(title)
        authors         = track['authors'].strip() or None
        original_artist = track['original'].strip() or None
        is_cover        = 1 if track['cover'].strip() == 'Y' else 0
        originals_url   = track['originals'].strip() or None
        lyrics          = to_plain(track['lyrics'].strip()) or None
        disambiguate    = track['disambiguate'].strip() or None
        notes           = to_plain(track['notes'].strip()) or None

        cur.execute(
            "SELECT id, slug FROM songs WHERE REPLACE(REPLACE(LOWER(slug),'-',''),'_','')=?",
            (norm(hyph_slug),)
        )
        existing = cur.fetchone()

        if existing:
            song_id = existing['id']
            if existing['slug'] != hyph_slug:
                cur.execute('UPDATE songs SET slug=? WHERE id=?', (hyph_slug, song_id))
            cur.execute("""
                UPDATE songs SET
                    sort_title      = COALESCE(NULLIF(sort_title,''), ?),
                    authors         = COALESCE(authors, ?),
                    original_artist = COALESCE(original_artist, ?),
                    is_cover        = CASE WHEN is_cover=0 THEN ? ELSE is_cover END,
                    originals_url   = COALESCE(originals_url, ?),
                    lyrics          = COALESCE(lyrics, ?),
                    disambiguate    = COALESCE(disambiguate, ?),
                    notes           = COALESCE(notes, ?)
                WHERE id=?
            """, (sort_title, authors, original_artist, is_cover,
                  originals_url, lyrics, disambiguate, notes, song_id))
            updated += 1
        else:
            cur.execute("""
                INSERT INTO songs
                    (title, slug, sort_title, authors, original_artist,
                     is_cover, originals_url, lyrics, disambiguate, notes)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (title, hyph_slug, sort_title, authors, original_artist,
                  is_cover, originals_url, lyrics, disambiguate, notes))
            inserted += 1

    print(f"  songs — inserted: {inserted}  updated: {updated}")


# ---------------------------------------------------------------------------
# Reconcile full_title against authoritative songs.title
# ---------------------------------------------------------------------------

def reconcile_full_titles(cur):
    """
    Update song_versions.full_title to use the authoritative title from
    songs.title rather than whatever raw string came in from release sheets
    or setlists.

    Canonical versions (version_tag NULL):  full_title = songs.title
    Tagged versions (version_tag NOT NULL): full_title = songs.title + ' [' + version_tag + ']'
    """
    # Canonical versions
    cur.execute("""
        UPDATE song_versions
        SET full_title = (
            SELECT s.title FROM songs s WHERE s.id = song_versions.song_id
        )
        WHERE version_tag IS NULL
        AND full_title != (
            SELECT s.title FROM songs s WHERE s.id = song_versions.song_id
        )
    """)
    canonical_updated = cur.rowcount

    # Tagged versions
    cur.execute("""
        UPDATE song_versions
        SET full_title = (
            SELECT s.title || ' [' || song_versions.version_tag || ']'
            FROM songs s WHERE s.id = song_versions.song_id
        )
        WHERE version_tag IS NOT NULL
        AND full_title != (
            SELECT s.title || ' [' || song_versions.version_tag || ']'
            FROM songs s WHERE s.id = song_versions.song_id
        )
    """)
    tagged_updated = cur.rowcount

    if canonical_updated or tagged_updated:
        print(f"  full_title reconciled — "
              f"canonical: {canonical_updated}, tagged: {tagged_updated}")
    else:
        print("  full_title already consistent with songs.title ✓")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('csv',  help='Path to tracks CSV')
    parser.add_argument('--db', default='music.db')
    parser.add_argument('--check',            action='store_true')
    parser.add_argument('--import', dest='do_import', action='store_true')
    args = parser.parse_args()

    if not args.check and not args.do_import:
        args.check = args.do_import = True

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    cur = con.cursor()
    con.executescript((Path(__file__).parent / 'schema.sql').read_text())

    tracks, norm_to_slug = load_tracks(args.csv)
    print(f"Loaded {len(tracks)} tracks from {args.csv}")

    if args.check:
        print('\n── Consistency check ──')
        print_issues(run_check(cur, tracks, norm_to_slug))

    if args.do_import:
        print('\n── Importing ──')
        run_import(cur, tracks, norm_to_slug)
        reconcile_full_titles(cur)
        con.commit()
        print('Done.')

    con.close()


if __name__ == '__main__':
    main()
