#!/usr/bin/env python3
"""
generate_data.py
----------------
Queries music.db and writes Jekyll _data/ YAML files.
Run before jekyll build:
    python generate_data.py --db _db/music.db --output _data/

Output structure:
    _data/
        acts.yml                          # master list for navigation
        acts/
            galaxie-500.yml
            luna.yml  ...
        releases/
            galaxie-500/
                today.yml  ...
        songs/
            blue-thunder.yml  ...
        shows/
            galaxie-500/
                galaxie-500-1988-03-19-....yml  ...
        lists/
            tracks_az.yml
            covers_az.yml
            recorded_shows.yml

Options:
    --act galaxie-500   Only generate for one act (releases + shows).
                        Songs and lists are always global.
"""

import argparse
import sqlite3
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def connect(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    return con


def q(con, sql, params=()):
    return [dict(r) for r in con.execute(sql, params)]


def q1(con, sql, params=()):
    r = con.execute(sql, params).fetchone()
    return dict(r) if r else None


def clean(d):
    """Strip None values from a dict for tidier YAML."""
    return {k: v for k, v in d.items() if v is not None}


def write_yaml(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        yaml.dump(data, f, allow_unicode=True,
                  default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Acts
# ---------------------------------------------------------------------------

def generate_acts(con, out):
    acts = q(con, 'SELECT id, name, slug FROM acts ORDER BY name')

    for act in acts:
        aid = act['id']

        rc = q1(con, """
            SELECT count(*) as total,
                sum(CASE WHEN release_type='Album'  THEN 1 ELSE 0 END) as albums,
                sum(CASE WHEN release_type='Single' THEN 1 ELSE 0 END) as singles,
                sum(CASE WHEN release_type='EP'     THEN 1 ELSE 0 END) as eps,
                sum(CASE WHEN release_type='Misc'   THEN 1 ELSE 0 END) as misc
            FROM releases WHERE act_id=?
        """, (aid,))

        sc = q1(con, """
            SELECT count(*) as total,
                   sum(has_recording) as recorded,
                   min(date) as first_show,
                   max(date) as last_show
            FROM shows WHERE act_id=? AND cancelled=0
        """, (aid,))

        write_yaml(out / 'acts' / f"{act['slug']}.yml", {
            'name':            act['name'],
            'slug':            act['slug'],
            'release_count':   rc['total'] or 0,
            'albums':          rc['albums'] or 0,
            'singles':         rc['singles'] or 0,
            'eps':             rc['eps'] or 0,
            'misc':            rc['misc'] or 0,
            'show_count':      sc['total'] or 0,
            'recorded_shows':  sc['recorded'] or 0,
            'first_show':      sc['first_show'],
            'last_show':       sc['last_show'],
        })

    write_yaml(out / 'acts.yml',
               [{'name': a['name'], 'slug': a['slug']} for a in acts])
    print(f'  acts:     {len(acts)}')


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def generate_releases(con, out, act_filter=None):
    where  = 'AND a.slug=?' if act_filter else ''
    params = (act_filter,) if act_filter else ()

    releases = q(con, f"""
        SELECT r.id, r.title, r.slug, r.release_type, r.year,
               r.sleeve_url, r.notes, r.album_artist,
               a.name as act_name, a.slug as act_slug
        FROM releases r JOIN acts a ON a.id=r.act_id
        WHERE 1=1 {where}
        ORDER BY a.slug, r.year, r.title
    """, params)

    for r in releases:
        editions = q(con, """
            SELECT e.id, e.format, e.country, e.year, e.label,
                   e.catalogue_no, e.sleeve_url, e.notes,
                   e.ahfow_ref, e.my_collection_url
            FROM editions e WHERE e.release_id=?
            ORDER BY e.year, e.country, e.format
        """, (r['id'],))

        for e in editions:
            e['tracks'] = [clean(t) for t in q(con, """
                SELECT et.position, et.side, et.side_position,
                       s.title, s.slug, sv.version_tag, sv.full_title
                FROM edition_tracks et
                JOIN song_versions sv ON sv.id=et.song_version_id
                JOIN songs s ON s.id=sv.song_id
                WHERE et.edition_id=?
                ORDER BY et.position
            """, (e.pop('id'),))]  # pop id — not needed in YAML

        write_yaml(
            out / 'releases' / r['act_slug'] / f"{r['slug']}.yml",
            clean({
                'title':        r['title'],
                'slug':         r['slug'],
                'release_type': r['release_type'],
                'year':         r['year'],
                'sleeve_url':   r['sleeve_url'],
                'notes':        r['notes'],
                'album_artist': r['album_artist'],
                'act':          r['act_name'],
                'act_slug':     r['act_slug'],
                'editions':     [clean(e) for e in editions],
            })
        )

    print(f'  releases: {len(releases)}')


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------

def generate_songs(con, out):
    songs = q(con, """
        SELECT id, title, slug, sort_title, authors, original_artist,
               is_cover, originals_url, lyrics, disambiguate, notes
        FROM songs ORDER BY sort_title
    """)

    for s in songs:
        song_id  = s.pop('id')
        is_cover = bool(s.pop('is_cover'))

        # Versions grouped by act
        versions = q(con, """
            SELECT sv.id, sv.version_tag, sv.full_title,
                   a.name as act_name, a.slug as act_slug
            FROM song_versions sv JOIN acts a ON a.id=sv.act_id
            WHERE sv.song_id=?
            ORDER BY a.name, sv.version_tag
        """, (song_id,))

        acts_map = {}
        for sv in versions:
            aslug = sv['act_slug']
            acts_map.setdefault(aslug, {
                'act':      sv['act_name'],
                'act_slug': aslug,
                'versions': []
            })

            sv_id = sv['id']

            appears_on_raw = q(con, """
                SELECT r.title as release_title, r.slug as release_slug,
                       r.release_type, r.year,
                       e.format, e.country, e.year as edition_year,
                       e.label, e.catalogue_no,
                       et.side, et.position
                FROM edition_tracks et
                JOIN editions e ON e.id=et.edition_id
                JOIN releases r ON r.id=e.release_id
                WHERE et.song_version_id=?
                ORDER BY r.year, e.country, e.format
            """, (sv_id,))
            appears_on = [clean(r) for r in appears_on_raw]

            played_live = [clean(r) for r in q(con, """
                SELECT sh.slug as show_slug, sh.date, sh.year,
                       sh.series, sh.event,
                       v.name as venue, v.city, v.country,
                       sl.position, sl.annotation
                FROM setlists sl
                JOIN shows sh ON sh.id=sl.show_id
                LEFT JOIN venues v ON v.id=sh.venue_id
                WHERE sl.song_version_id=?
                ORDER BY sh.date
            """, (sv_id,))]

            acts_map[aslug]['versions'].append(clean({
                'version_tag': sv['version_tag'],
                'full_title':  sv['full_title'],
                'appears_on':  appears_on,
                'played_live': played_live,
            }))

        write_yaml(
            out / 'songs' / f"{s['slug']}.yml",
            clean({**s,
                   'is_cover': is_cover,
                   'acts':     list(acts_map.values())})
        )

    print(f'  songs:    {len(songs)}')


# ---------------------------------------------------------------------------
# Shows
# ---------------------------------------------------------------------------

def generate_shows(con, out, act_filter=None):
    where  = 'AND a.slug=?' if act_filter else ''
    params = (act_filter,) if act_filter else ()

    shows = q(con, f"""
        SELECT sh.id, sh.slug, sh.date, sh.year,
               sh.radio, sh.cancelled, sh.i_was_there, sh.has_recording,
               sh.performers, sh.support, sh.series, sh.event,
               sh.poster_url, sh.ticket_url, sh.notes, sh.setlist_source,
               v.name as venue_name, v.slug as venue_slug,
               v.city, v.state, v.country, v.url as venue_url,
               a.name as act_name, a.slug as act_slug
        FROM shows sh
        JOIN acts a ON a.id=sh.act_id
        LEFT JOIN venues v ON v.id=sh.venue_id
        WHERE 1=1 {where}
        ORDER BY sh.date
    """, params)

    for sh in shows:
        show_id  = sh.pop('id')
        act_slug = sh['act_slug']

        setlist = [clean(r) for r in q(con, """
            SELECT sl.position, sl.raw_title, sl.annotation,
                   s.title as canonical_title, s.slug as song_slug,
                   sv.version_tag, sv.full_title
            FROM setlists sl
            LEFT JOIN song_versions sv ON sv.id=sl.song_version_id
            LEFT JOIN songs s ON s.id=sv.song_id
            WHERE sl.show_id=?
            ORDER BY sl.position
        """, (show_id,))]

        audio = [clean(r) for r in q(con, """
            SELECT audio_type, url, label
            FROM show_audio WHERE show_id=?
        """, (show_id,))]

        data = clean(sh)
        data['radio']         = bool(sh['radio'])
        data['cancelled']     = bool(sh['cancelled'])
        data['i_was_there']   = bool(sh['i_was_there'])
        data['has_recording'] = bool(sh['has_recording'])
        data['setlist']       = setlist
        if audio:
            data['audio']     = audio

        write_yaml(
            out / 'shows' / act_slug / f"{sh['slug']}.yml",
            data
        )

    print(f'  shows:    {len(shows)}')


# ---------------------------------------------------------------------------
# Lists (always global)
# ---------------------------------------------------------------------------

def generate_lists(con, out):
    # A-Z tracks
    tracks = q(con, """
        SELECT s.title, s.slug, s.sort_title, s.authors,
               s.is_cover, s.original_artist, s.disambiguate,
               GROUP_CONCAT(DISTINCT a.name) as acts
        FROM songs s
        LEFT JOIN song_versions sv ON sv.song_id=s.id
        LEFT JOIN acts a ON a.id=sv.act_id
        GROUP BY s.id ORDER BY s.sort_title
    """)
    write_yaml(out / 'lists' / 'tracks_az.yml', [clean(t) for t in tracks])
    print(f'  tracks_az: {len(tracks)}')

    # A-Z covers
    covers = q(con, """
        SELECT s.title, s.slug, s.sort_title, s.authors,
               s.original_artist, s.originals_url, s.notes,
               GROUP_CONCAT(DISTINCT a.name) as acts
        FROM songs s
        LEFT JOIN song_versions sv ON sv.song_id=s.id
        LEFT JOIN acts a ON a.id=sv.act_id
        WHERE s.is_cover=1
        GROUP BY s.id ORDER BY s.sort_title
    """)
    write_yaml(out / 'lists' / 'covers_az.yml', [clean(c) for c in covers])
    print(f'  covers_az: {len(covers)}')

    # Recorded shows
    recorded = q(con, """
        SELECT sh.slug, sh.date, sh.year, sh.series, sh.event,
               sh.cancelled, sh.radio,
               v.name as venue, v.city, v.country,
               a.name as act, a.slug as act_slug,
               sa.audio_type, sa.url as audio_url, sa.label as audio_label
        FROM shows sh
        JOIN acts a ON a.id=sh.act_id
        LEFT JOIN venues v ON v.id=sh.venue_id
        LEFT JOIN show_audio sa ON sa.show_id=sh.id
        WHERE sh.has_recording=1
        ORDER BY sh.date
    """)
    write_yaml(out / 'lists' / 'recorded_shows.yml',
               [clean(r) for r in recorded])
    print(f'  recorded:  {len(recorded)}')


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Generate Jekyll _data/ YAML from music.db')
    parser.add_argument('--db',     required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--act',    default=None,
                        help='Limit to one act slug (optional)')
    args = parser.parse_args()

    con = connect(args.db)
    out = Path(args.output)

    print('Generating _data/:')
    generate_acts(con, out)
    generate_releases(con, out, args.act)
    generate_songs(con, out)
    generate_shows(con, out, args.act)
    generate_lists(con, out)

    con.close()

    total = sum(1 for _ in out.rglob('*.yml'))
    size  = sum(f.stat().st_size for f in out.rglob('*.yml'))
    print(f'\nDone: {total} files, {size/1024/1024:.1f} MB')


if __name__ == '__main__':
    main()
