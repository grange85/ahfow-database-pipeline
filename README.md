# AHFOW SQLite — Database & Data Pipeline

This repo contains the SQLite database schema, import scripts, data generation
script, and title-checking tools for the **A Head Full of Wishes** music
database (Dean Wareham's musical universe: Galaxie 500, Luna, Dean & Britta,
Damon & Naomi, Magic Hour).

The database powers the Jekyll site via `generate_data.py`, which produces
`_data/` YAML files consumed by `jekyll-datapage-generator` templates.

> Editing code or data here? See `CLAUDE.md` for the invariants and conventions
> that must be respected (song/version model, import order, slug rules, etc.).

---

## Project structure

```
schema.sql              Database schema (v4)
sort_title.py           MusicBrainz sort title generation (shared module)
mb_titlecase.py         MusicBrainz English title style checker/converter
mb_titlecase_sheets.gs  Google Apps Script version for Google Sheets

import_tracks.py        Import tracks.csv → songs table (run first and last)
import_releases.py      Import release sheets → releases/editions/song_versions
import_shows.py         Import show sheets → shows/venues/setlists

generate_data.py        Query DB → Jekyll _data/ YAML files

music.db                The database (not committed — lives in S3)
```

---

## Database overview

The data model has two key ideas: **songs are global** (one row per song,
regardless of act) and **acts own versions** (each act's recording or
performance of a song is a separate `song_versions` row). `tracks.csv` is the
authoritative source for song titles. Setlists are matched to songs by
normalised title rather than slug. See `CLAUDE.md` for the full set of design
invariants.

### Tables

| Table | Purpose |
|---|---|
| `acts` | Galaxie 500, Luna, Dean & Britta, Damon & Naomi, Magic Hour |
| `songs` | Global song catalogue — one row per song |
| `song_versions` | Act's recording/version of a song |
| `releases` | Album/Single/EP/Misc titles |
| `editions` | Physical pressings of a release |
| `edition_tracks` | Tracklisting per edition |
| `venues` | Deduplicated venue data |
| `shows` | Live performances |
| `show_audio` | Stream/download links per show |
| `setlists` | Songs performed at a show, in order |

### Acts and slugs

| Act | Slug |
|---|---|
| Galaxie 500 | `galaxie-500` |
| Luna | `luna` |
| Dean & Britta | `dean-and-britta` |
| Dean Wareham | `dean-wareham` |
| Damon & Naomi | `damon-and-naomi` |
| Magic Hour | `magic-hour` |

`&` is preserved in display names but replaced with `and` in slugs.

### Slug conventions

- Hyphenated throughout: `blue-thunder` not `bluethunder`
- Apostrophes stripped before hyphenation: `dont-let-our-youth-go-to-waste`
- `&` → `and`: `dean-and-britta`
- `?` and other URL-unsafe characters stripped
- Song slugs: globally unique across all acts
- Release slugs: unique within an act (`UNIQUE(act_id, slug)`)
- Show slugs: globally unique (include act slug + date + venue)

---

## Import order

**Always follow this sequence:**

```bash
# 1. Seed the global song catalogue
python import_tracks.py tracks.csv --db music.db

# 2. Import all release sheets — one sheet per artist, act via --act
#    (order between sheets doesn't matter; release_type is a column in the sheet)
python import_releases.py galaxie-500-releases.csv       --db music.db --act "Galaxie 500"
python import_releases.py luna-releases.csv              --db music.db --act "Luna"
python import_releases.py damon-naomi-releases.csv       --db music.db --act "Damon & Naomi"
# ... all acts ...

# 3. Import all show sheets (after releases so setlists resolve cleanly)
python import_shows.py galaxie-500-shows.csv             --db music.db --act "Galaxie 500"
python import_shows.py luna-shows.csv                    --db music.db --act "Luna"
python import_shows.py dean-wareham-shows.csv            --db music.db   # multi-act: omit --act
python import_shows.py damon-naomi-shows.csv             --db music.db   # multi-act: omit --act

# 4. Re-run tracks to reconcile full_title against authoritative titles
python import_tracks.py tracks.csv --db music.db
```

Step 4 is fast (no inserts, just two UPDATE queries) and ensures `full_title`
on `song_versions` always reflects `songs.title` regardless of capitalisation
in release sheets or setlists.

### Release sheets and release types

Each artist has **one releases CSV**. There is no MASTER row and no `Artist`
column — the act is supplied with the required `--act` flag. Every row is one
**edition**; rows that share a `release_title` are grouped into a single
release. The release's year is the earliest edition year, and its sleeve /
`album_artist` come from the first matching row.

`release_type` is a per-row column. Its sheet values are mapped to the stored
(legacy) set used by `generate_data.py`:

| Sheet value | Stored |
|---|---|
| `album` | `Album` |
| `single` | `Single` |
| `ep` | `EP` |
| `compilation`, `demo`, `video`, anything else | `Misc` |

### Multi-act show sheets

The Dean & Britta / Dean Wareham shows are in one CSV with `artistname`
driving the act per row. Same for Damon & Naomi / Magic Hour. The
`import_shows.py` `--act` flag is optional — omit it for multi-act sheets.

### Updating from the CSV (`--update` / `--dry-run`)

By default the importers are **additive** — re-running never changes rows that
already exist in the DB. When the CSV is the master copy and you want edits to
propagate, both `import_shows.py` and `import_releases.py` accept `--update`:

```bash
# Preview what would change, without writing anything
python import_shows.py    galaxie-500-shows.csv    --db music.db --act "Galaxie 500" --update --dry-run
python import_releases.py galaxie-500-releases.csv --db music.db --act "Galaxie 500" --update --dry-run

# Apply the changes (drop --dry-run)
python import_shows.py    galaxie-500-shows.csv    --db music.db --act "Galaxie 500" --update
python import_releases.py galaxie-500-releases.csv --db music.db --act "Galaxie 500" --update
```

Common behaviour in update mode:

- Scalar fields on existing rows are patched to match the CSV.
- Child records are deleted and rebuilt from the CSV when their content changed
  (shows: setlist + audio; releases: editions + tracklists).
- It is **non-destructive**: rows in the DB but absent from the CSV are *not*
  deleted — they are listed in an "In DB but not in CSV" report so you can review
  them and add them to the sheet if appropriate.

For **shows** specifically: venues are deduplicated, so each venue slug is
applied once per run (first occurrence wins). When the same venue appears in
multiple rows with inconsistent spelling/details, the disagreements are reported
for you to fix in the sheet rather than flip-flopping silently.

For **releases** specifically: edition rebuilds compare on edition fields plus
*song identity* (which song + version_tag), not on the `full_title` string — so
capitalisation drift between editions does not trigger spurious rebuilds
(`full_title` is reconciled by step 4 anyway).

`--dry-run` performs all the work in a transaction and rolls it back, printing
the same summary and reports — useful before overwriting hand-edited DB data.

### Special track prefixes in release sheets

- `^Track title` — track by an unrelated artist on a split single. Silently
  skipped; the rest of the tracklist is unaffected.

### Setlist annotation conventions in show sheets

- `^encore` or `[^encore]` — set break, encore marker, stage note. Stored as
  `annotation` with `song_version_id = NULL`. Not treated as a song.
- `Song Title [descriptive note]` — real song with an annotation (e.g.
  `"Don't Let Our Youth Go to Waste [with Peter Buck of REM on guitar]"`).
  Song is matched on the bare title; annotation stored in `setlists.annotation`.

---

## Generating Jekyll data

```bash
# Generate all _data/ files
python generate_data.py --db music.db --output _data/

# Generate for one act only (songs and lists are always global)
python generate_data.py --db music.db --output _data/ --act galaxie-500
```

### Output structure

```
_data/
    acts.yml                    Master list for navigation
    acts/
        galaxie-500.yml
        luna.yml  ...
    releases/
        galaxie-500/
            today.yml           Full release with editions and tracklists
            on-fire.yml  ...
    songs/
        blue-thunder.yml        Song with all act versions, releases, shows
        flowers.yml  ...
    shows/
        galaxie-500/
            galaxie-500-1988-03-19-....yml   Full show with setlist
    lists/
        tracks_az.yml           All songs A-Z
        covers_az.yml           Cover versions A-Z
        recorded_shows.yml      Shows with audio available
```

---

## Deployment workflow

```
Edit DB locally (DB Browser for SQLite)
  ↓
Test locally: python generate_data.py → bundle exec jekyll serve
  ↓
Push music.db to S3 (private bucket, versioning enabled)
  ↓
Trigger GitHub Action (workflow_dispatch or push)
  ↓
Action: aws s3 cp s3://bucket/music.db _db/music.db
Action: python generate_data.py --db _db/music.db --output _data/
Action: bundle exec jekyll build
Action: aws s3 sync _site/ s3://site-bucket/
```

The site bucket is public; the DB bucket is private.

---

## Title style

Titles should follow **MusicBrainz English title style** (sentence case with
exceptions — see https://musicbrainz.org/doc/Style/Language/English).

### Checking titles

```bash
# Check a CSV file
python mb_titlecase.py tracks.csv
python mb_titlecase.py releases.csv --col Title --slug slug

# Check a single title
python mb_titlecase.py --title "Walk On The Wild Side"

# Convert a title
python mb_titlecase.py --convert "walk on the wild side"

# Run self-tests
python mb_titlecase.py
```

Titles flagged as "needs human review" include:
- Possible phrasal verb particles (`In`, `On`, `Up` mid-title)
- Non-English titles starting with `Le`, `La`, `Les` etc. — French uses
  sentence case so `Le chat noir` is correct; don't auto-correct these.

### Google Sheets

Paste `mb_titlecase_sheets.gs` into Apps Script (Extensions > Apps Script)
for `=MB_TITLE()`, `=MB_TITLE_CHECK()`, `=MB_TITLE_OK()` functions and a
"MB Title Tools" menu with bulk check/convert options.

---

## Sort titles

`sort_title.py` generates sort titles for A-Z indexing, used by `import_tracks.py`.
Rules: strip leading articles (`The`, `A`, `An`, `Le`, `La`, `Les`, `L'`,
`Un`, `Une`), strip diacritics, uppercase. Special cases handled via
`OVERRIDES` dict (numerals spelled out, one AACR2 move-to-end case).

`A Silver Thread` → `SILVER THREAD, A` (the only move-to-end case).
`The Flowers of Romance` → `FLOWERS OF ROMANCE`.

---

## Dependencies

```bash
pip install pyyaml faker
```

`pyyaml` — required for `generate_data.py` and `generate_benchmark_data.py`
`faker` — required for `generate_benchmark_data.py` (benchmark only)

On systems that complain about system Python:
```bash
pip install pyyaml --break-system-packages
```

---

## Known data issues / decisions

- `time-cagney-and-lacee` — double-hyphen slug fixed; `&` in `Cagney & Lacee`
  now correctly becomes `and` via `sanitise_slug()`
- `disambiguate` field on songs — used when two different songs share a title
  (Indian Summer, Love, Time). The "primary" association gets the bare slug;
  the less common gets the artist appended: `indian-summer-the-doors`
- `album_artist` on releases — display credit when sleeve differs from act,
  e.g. "Various Artists" for compilations, "Dean & Britta & Sonic Boom" for
  credited collaborations
- French titles — correct capitalisation is sentence case (`Le chat noir`),
  not English title case. The MB title checker flags these for human review
  rather than auto-correcting
- `release_versions` table — intentionally omitted. Blog post links to
  releases will be handled by a future `bookmarks` table
