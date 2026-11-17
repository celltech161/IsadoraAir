# Library "Export to .csv"

`/library/` has an **Export to .csv** button. It downloads one CSV containing **every Track matching the
filters and sort currently shown** (search box, Category, status, sort column and direction) - not the
current page and not the checkbox selection.

* Endpoint: `GET /api/tracks/export/` (read-only; no migrations; Gunicorn restart only).
  Parameters: `q`, `category`, `ready2air`, `sort`, `dir` - identical meaning to `/api/tracks/`
  (it calls the same `library/services/track_filters.filter_tracks` and the same sort table).
  `page`, `per_page` and any selection are ignored. A non-numeric `category` is a 400.
* Access: the URL is under the `/api/tracks/` GroupAccess prefix, so exactly the accounts that can read the
  Library list can export, and only the five columns below are ever written (no file paths, ISRC,
  related artists, etc.).
* File name: `isadoraair-library-YYYY-MM-DD.csv`, dated in the station timezone.
* Encoding: UTF-8 **with a byte-order mark** so Excel reads accented text correctly; line endings CRLF;
  commas, quotes and embedded line breaks are quoted per RFC 4180.
* Columns, in order: `Artist,Title,Album,Category,Duration`
  * Artist - primary Artist name. Title - Track title. Album - Album title or blank.
  * Category - the **primary** Category code (as in the Library table) or blank. A Track included because it
    is an *additional* member of the filtered Category still shows its own primary code.
  * Duration - `m:ss`, or blank when unknown. Same rounding as the table: the whole number of seconds is
    rounded to the nearest integer (half up) *before* splitting, so 119.6 is `2:00`, never `1:60`;
    3599.5 is `60:00`; 0 is `0:00`.
* Each Track appears exactly once (the Category filter's join duplicates are collapsed per Track record),
  and two different Tracks with identical metadata are two rows.
* Sort ties are broken by Track id so a repeated download is byte-identical while the data is unchanged.
* Empty result: a valid header-only file.

## Formula-injection protection (the only value change)

Spreadsheet applications run a cell that starts with `=`, `+`, `-` or `@` as a formula. Metadata comes from
file tags and operator edits, so every text cell (Artist, Title, Album, Category) is passed through
`track_csv_export.spreadsheet_safe`: a value that begins - after any leading whitespace - with `=`, `+`, `-`
or `@`, **or begins with a TAB, CR or LF**, gets **one leading apostrophe (`'`)**. Examples:
`=1+1` -> `'=1+1`, `-30-` -> `'-30-`, `+44 20 7946` -> `'+44 20 7946`, ` @home` -> `' @home`.
Nothing else is altered (`A=B`, `Rock & Roll`, `Beyoncé`, `1+1=2` export verbatim). Removing the one
leading apostrophe recovers the original. Duration is generated digits-and-colon and is never guarded.
Not covered (by design): look-alike Unicode operators (e.g. full-width `=`), which spreadsheets do not treat
as formulas.

## Memory and I/O

The query is one `SELECT` (artist, album and category names come from the same joined row - no per-Track
queries) read with `QuerySet.iterator(chunk_size=2000)` and written to the response in batches of 500 rows
through `StreamingHttpResponse`; nothing is held whole and no temporary file is created
(`X-Accel-Buffering: no` asks nginx to pass the stream through rather than spool it).
