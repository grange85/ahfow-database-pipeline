# AHFOW SQLite — agent notes

SQLite database + Python pipeline for the *A Head Full of Wishes* music
database. CSV sheets → `music.db` → `_data/*.yml` → Jekyll site.

See `README.md` for setup, full command reference, deployment, and output
structure. This file holds the invariants and conventions to respect when
editing code or data.

## Files

```
schema.sql            Database schema (v4)
sort_title.py         Sort-title generation (shared module, used by import_tracks)
mb_titlecase.py       MusicBrainz English title style checker/converter
import_tracks.py      tracks.csv → songs        (run FIRST and LAST)
import_releases.py    release sheets → releases/editions/song_versions
import_shows.py       show sheets → shows/venues/setlists
generate_data.py      query DB → Jekyll _data/ YAML
music.db              the database (not committed — lives in S3)
```

## Design invariants — do not break these

- **Songs are global.** `songs` has no `act_id`; one row per song regardless of
  how many acts recorded/performed it.
- **Acts own versions** via `song_versions.act_id`. Each act's take on a song is
  a separate `song_versions` row linked to the one `songs` row. `version_tag`:
  `NULL` = canonical, e.g. `"w/sax"` = tagged variant by the same act.
- **`tracks.csv` is the authority** for song titles/metadata. `full_title` on
  `song_versions` is reconciled against `songs.title` on every import run — this
  is why `import_tracks.py` runs both first and last.
- **Setlists match by normalised title, not slug.** The `setlist-index` column in
  show sheets is ignored. Titles are normalised (strip diacritics/punctuation,
  lowercase) and matched against `songs.title` to absorb capitalisation drift.
- **`song_versions` are created on the fly** when a song is performed live but
  hasn't appeared on an imported release — a live performance establishes the
  act→song link.

## Import order — follow exactly

1. `import_tracks.py` — seed the global song catalogue
2. `import_releases.py` — all release sheets (order between sheets doesn't matter)
3. `import_shows.py` — all show sheets (after releases, so setlists resolve)
4. `import_tracks.py` again — reconcile `full_title` against authoritative titles
   (fast; two UPDATEs, no inserts)

`--release-type` ∈ {`Album`, `Single`, `EP`, `Misc`}. Compilations use the
singles sheet structure, imported as `Misc`.

**Multi-act show sheets**: Dean & Britta / Dean Wareham share one CSV, as do
Damon & Naomi / Magic Hour. `artistname` drives the act per row — omit the
`--act` flag for these; pass it for single-act sheets.

## Parsing conventions

Release sheets:
- `^Track title` — track by an unrelated artist on a split single. Silently
  skipped; rest of the tracklist unaffected.

Show-sheet setlists:
- `^encore` / `[^encore]` — set break, encore, or stage note. Stored as
  `annotation` with `song_version_id = NULL`; not a song.
- `Song Title [descriptive note]` — real song with annotation. Matched on the
  bare title; annotation stored in `setlists.annotation`.

## Slug conventions

- Hyphenated: `blue-thunder` not `bluethunder`
- Apostrophes stripped before hyphenation: `dont-let-our-youth-go-to-waste`
- `&` → `and`: `dean-and-britta` (preserved in display names)
- `?` and other URL-unsafe characters stripped
- Song slugs globally unique; release slugs unique per act (`UNIQUE(act_id, slug)`);
  show slugs globally unique (act + date + venue)

Acts: Galaxie 500 `galaxie-500`, Luna `luna`, Dean & Britta `dean-and-britta`,
Dean Wareham `dean-wareham`, Damon & Naomi `damon-and-naomi`, Magic Hour `magic-hour`.

## Title & sort rules

- Titles follow **MusicBrainz English title style** (sentence case with
  exceptions): https://musicbrainz.org/doc/Style/Language/English
- French titles use sentence case (`Le chat noir`) — the checker flags these for
  human review rather than auto-correcting. Same for possible phrasal-verb
  particles mid-title (`In`, `On`, `Up`).
- `sort_title.py`: strip leading articles (`The`, `A`, `An`, `Le`, `La`, `Les`,
  `L'`, `Un`, `Une`), strip diacritics, uppercase. `OVERRIDES` handles special
  cases. Only move-to-end case: `A Silver Thread` → `SILVER THREAD, A`.

## Known data decisions

- `disambiguate` on `songs` — used when two different songs share a title (Indian
  Summer, Love, Time). Primary association gets the bare slug; the less common
  gets the artist appended: `indian-summer-the-doors`.
- `album_artist` on `releases` — display credit when the sleeve differs from the
  act (e.g. "Various Artists", "Dean & Britta & Sonic Boom").
- `release_versions` table — intentionally omitted; blog-post links to releases
  will be handled by a future `bookmarks` table.
- `time-cagney-and-lacee` — double-hyphen slug fixed; `&` now correctly becomes
  `and` via `sanitise_slug()`.
