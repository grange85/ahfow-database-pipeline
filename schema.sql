-- =============================================================
-- Music Database Schema  (v4)
-- =============================================================
-- Key design decisions:
--   • songs        = global catalogue, no act scoping
--   • song_versions= act's recording of a song (act_id lives here)
--   • tracks.csv   = sole authority for songs table
--   • release_type = Album / Single / EP / Misc
--   • shows        = act resolved per row from artistname column
--   • bookmarks    = future table for editorial links
-- =============================================================

PRAGMA foreign_keys = ON;

-- -------------------------------------------------------------
-- Acts
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS acts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    slug        TEXT NOT NULL UNIQUE,
    image_url	TEXT
);

-- -------------------------------------------------------------
-- Songs  — global catalogue, independent of any act
-- Populated from tracks.csv only.
-- slug: hyphenated, globally unique ("blue-thunder")
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS songs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    sort_title      TEXT NOT NULL,
    authors         TEXT,
    original_artist TEXT,
    is_cover        INTEGER NOT NULL DEFAULT 0,
    originals_url   TEXT,
    lyrics          TEXT,
    disambiguate    TEXT,
    notes           TEXT
);

-- -------------------------------------------------------------
-- Song versions  — a specific act's recording or mix of a song
-- act_id here, not on songs.
-- version_tag NULL = canonical recording by that act.
-- UNIQUE(song_id, act_id, version_tag) ensures:
--   - Galaxie 500 / Blue Thunder / NULL       (canonical)
--   - Galaxie 500 / Blue Thunder / w/sax      (variant)
--   - Dean Wareham / Blue Thunder / NULL      (cover/live version)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS song_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    song_id     INTEGER NOT NULL REFERENCES songs(id),
    act_id      INTEGER NOT NULL REFERENCES acts(id),
    version_tag TEXT,
    full_title  TEXT NOT NULL,
    UNIQUE(song_id, act_id, version_tag)
);

-- -------------------------------------------------------------
-- Releases  — one per album/EP/single title
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS releases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    act_id          INTEGER NOT NULL REFERENCES acts(id),
    title           TEXT NOT NULL,
    slug            TEXT NOT NULL,          -- hyphenated, unique within act
    release_type    TEXT NOT NULL,          -- Album / Single / EP / Misc
    year            INTEGER,
    sleeve_url      TEXT,
    notes           TEXT,
    album_artist    TEXT,                   -- sleeve credit if different from act
                                            -- e.g. "Various Artists", "Dean & Britta & Sonic Boom"
    UNIQUE(act_id, slug),
    UNIQUE(act_id, title, release_type)
);

-- -------------------------------------------------------------
-- Editions  — one physical pressing per release
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS editions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    release_id          INTEGER NOT NULL REFERENCES releases(id),
    format              TEXT NOT NULL,
    country             TEXT,
    year                INTEGER,
    label               TEXT,
    catalogue_no        TEXT,
    sleeve_url          TEXT,
    notes               TEXT,
    ahfow_ref           TEXT,
    my_collection_url   TEXT
);

-- -------------------------------------------------------------
-- Edition tracks  — tracklisting per edition
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS edition_tracks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id      INTEGER NOT NULL REFERENCES editions(id),
    song_version_id INTEGER NOT NULL REFERENCES song_versions(id),
    position        INTEGER NOT NULL,
    side            TEXT,
    side_position   INTEGER,
    UNIQUE(edition_id, position)
);

-- -------------------------------------------------------------
-- Venues
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS venues (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    city        TEXT,
    state       TEXT,
    country     TEXT,
    url         TEXT
);

-- -------------------------------------------------------------
-- Shows  — act_id per row (multi-act sheets supported)
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    act_id          INTEGER NOT NULL REFERENCES acts(id),
    slug            TEXT NOT NULL UNIQUE,
    date            TEXT NOT NULL,
    year            INTEGER NOT NULL,
    venue_id        INTEGER REFERENCES venues(id),
    radio           INTEGER NOT NULL DEFAULT 0,
    cancelled       INTEGER NOT NULL DEFAULT 0,
    i_was_there     INTEGER NOT NULL DEFAULT 0,
    has_recording   INTEGER NOT NULL DEFAULT 0,
    performers      TEXT,
    support         TEXT,
    series          TEXT,                   -- e.g. "Dean Wareham plays Galaxie 500"
    event           TEXT,                   -- e.g. "Glastonbury Festival"
    poster_url      TEXT,
    ticket_url      TEXT,
    notes           TEXT,
    setlist_source  TEXT
);

-- -------------------------------------------------------------
-- Show audio
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS show_audio (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id     INTEGER NOT NULL REFERENCES shows(id),
    audio_type  TEXT,
    url         TEXT NOT NULL,
    label       TEXT
);

-- -------------------------------------------------------------
-- Setlists  — song_version_id NULL for annotations
-- -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS setlists (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    show_id         INTEGER NOT NULL REFERENCES shows(id),
    position        INTEGER NOT NULL,
    song_version_id INTEGER REFERENCES song_versions(id),
    raw_title       TEXT,
    annotation      TEXT,
    UNIQUE(show_id, position)
);

-- =============================================================
-- Views
-- =============================================================

-- Full tracklisting for any edition
CREATE VIEW IF NOT EXISTS v_edition_tracklist AS
SELECT
    a.name              AS act,
    r.title             AS release_title,
    r.release_type,
    e.id                AS edition_id,
    e.format,
    e.country,
    e.year,
    e.label,
    e.catalogue_no,
    et.side,
    et.side_position,
    et.position,
    s.title             AS canonical_title,
    s.slug,
    s.sort_title,
    sv.version_tag,
    sv.full_title       AS track_title
FROM edition_tracks et
JOIN song_versions sv   ON sv.id = et.song_version_id
JOIN songs s            ON s.id = sv.song_id
JOIN acts a             ON a.id = sv.act_id
JOIN editions e         ON e.id = et.edition_id
JOIN releases r         ON r.id = e.release_id
ORDER BY e.id, et.position;

-- All editions of every release
CREATE VIEW IF NOT EXISTS v_release_editions AS
SELECT
    a.name          AS act,
    r.title         AS release_title,
    r.release_type,
    r.year          AS original_year,
    e.id            AS edition_id,
    e.format,
    e.country,
    e.year          AS edition_year,
    e.label,
    e.catalogue_no,
    e.notes,
    e.ahfow_ref,
    e.my_collection_url
FROM editions e
JOIN releases r ON r.id = e.release_id
JOIN acts a     ON a.id = r.act_id
ORDER BY r.title, e.year, e.country;

-- All versions of a song across all acts — for song pages
CREATE VIEW IF NOT EXISTS v_song_appearances AS
SELECT
    s.title             AS canonical_title,
    s.slug,
    s.sort_title,
    s.authors,
    s.original_artist,
    s.is_cover,
    sv.id               AS song_version_id,
    sv.act_id,
    a.name              AS act,
    a.slug              AS act_slug,
    sv.version_tag,
    sv.full_title       AS track_title,
    -- release appearances
    r.title             AS release_title,
    r.release_type,
    r.year              AS release_year,
    e.format,
    e.country,
    e.year              AS edition_year,
    -- show appearances
    sh.date,
    sh.year             AS show_year,
    v.name              AS venue,
    v.city,
    v.country           AS show_country
FROM songs s
JOIN song_versions sv       ON sv.song_id = s.id
JOIN acts a                 ON a.id = sv.act_id
LEFT JOIN edition_tracks et ON et.song_version_id = sv.id
LEFT JOIN editions e        ON e.id = et.edition_id
LEFT JOIN releases r        ON r.id = e.release_id
LEFT JOIN setlists sl       ON sl.song_version_id = sv.id
LEFT JOIN shows sh          ON sh.id = sl.show_id
LEFT JOIN venues v          ON v.id = sh.venue_id
ORDER BY s.sort_title, a.name, sv.version_tag;

-- Most-played songs across all acts
CREATE VIEW IF NOT EXISTS v_song_play_counts AS
SELECT
    s.title,
    s.slug,
    s.sort_title,
    a.name          AS act,
    sv.version_tag,
    COUNT(*)        AS times_played
FROM setlists sl
JOIN song_versions sv   ON sv.id = sl.song_version_id
JOIN songs s            ON s.id = sv.song_id
JOIN acts a             ON a.id = sv.act_id
JOIN shows sh           ON sh.id = sl.show_id
WHERE sl.song_version_id IS NOT NULL
GROUP BY s.id, sv.act_id, sv.version_tag
ORDER BY times_played DESC;

-- Covers index
CREATE VIEW IF NOT EXISTS v_covers AS
SELECT
    s.title,
    s.slug,
    s.sort_title,
    s.authors,
    s.original_artist,
    s.originals_url,
    s.notes
FROM songs s
WHERE s.is_cover = 1
ORDER BY s.sort_title;

-- Shows with recordings
CREATE VIEW IF NOT EXISTS v_recorded_shows AS
SELECT
    a.name          AS act,
    sh.date,
    sh.year,
    v.name          AS venue,
    v.city,
    v.country,
    sa.audio_type,
    sa.url,
    sa.label,
    sh.radio,
    sh.slug         AS show_slug
FROM shows sh
JOIN acts a         ON a.id = sh.act_id
LEFT JOIN venues v  ON v.id = sh.venue_id
LEFT JOIN show_audio sa ON sa.show_id = sh.id
WHERE sh.has_recording = 1
ORDER BY sh.date;

-- Full setlist for any show
CREATE VIEW IF NOT EXISTS v_setlist AS
SELECT
    a.name          AS act,
    sh.date,
    sh.year,
    v.name          AS venue,
    v.city,
    v.country,
    sl.position,
    sl.raw_title,
    sl.annotation,
    s.title         AS canonical_title,
    s.slug          AS song_slug,
    sv.version_tag,
    sh.slug         AS show_slug
FROM setlists sl
JOIN shows sh               ON sh.id = sl.show_id
JOIN acts a                 ON a.id = sh.act_id
LEFT JOIN venues v          ON v.id = sh.venue_id
LEFT JOIN song_versions sv  ON sv.id = sl.song_version_id
LEFT JOIN songs s           ON s.id = sv.song_id
ORDER BY sh.date, sl.position;
