"""Filtered Library CSV export (/api/tracks/export/).

One CSV row per Track matching the operator's current /library/ filters, in the
operator's current sort order, regardless of pagination or selection.

Format -- exactly ``Artist,Title,Album,Category,Duration``:

* Artist    Track.artist.name (blank if the Track somehow has no Artist).
* Title     Track.title.
* Album     Album title, or blank.
* Category  the PRIMARY Category code (what the Library table's Category column
            shows), or blank. Additional-category membership only decides
            whether a Track is INCLUDED under a Category filter; it adds no row
            and no column.
* Duration  ``m:ss``, or blank when unknown. Rounding is the Library table's own
            (``fmtDuration`` in library.html): round the WHOLE number of seconds
            to the nearest integer, half up, THEN split into minutes/seconds, so
            119.6 reads 2:00 and never 1:60.

Encoding: UTF-8 with a leading byte-order mark. The BOM is what makes Excel (and
other spreadsheet importers that default to a legacy code page) read accented
artist and title text correctly; decoders that honour it (``utf-8-sig``, every
spreadsheet application) drop it, so the header cell is still exactly "Artist".
Set ``CSV_BOM = b""`` to omit it.

Formula-injection protection (CWE-1236) -- see ``spreadsheet_safe``. Metadata is
operator- or file-tag-supplied text, and a cell that begins ``=``, ``+``, ``-``
or ``@`` is executed as a formula by Excel / LibreOffice / Sheets when the file
is opened. Every TEXT cell (Artist, Title, Album, Category) is therefore passed
through ``spreadsheet_safe``, which prefixes a single apostrophe (``'``) to any
value that would otherwise be interpreted as a formula. The apostrophe is the
standard (OWASP) neutraliser: spreadsheets display the rest of the value as
literal text. This is the ONLY alteration ever made to a value; it is applied
only to values that begin (after any leading whitespace) with ``= + - @``, or
that begin with a TAB, CR or LF. A title such as ``-30-`` or ``+44`` therefore
exports as ``'-30-`` / ``'+44``; everything else is exported verbatim, Unicode
included. Duration is generated here as digits and a colon and is never guarded.

Memory: ``stream_rows`` iterates the queryset with ``QuerySet.iterator`` (a
bounded chunk at a time) and yields output in bounded batches, so neither the
Track rows nor the CSV text are ever held whole. The query is one SELECT over
``values_list`` of five columns plus the Track id: the artist/album/category
names come from the same JOINed row, so there are no per-Track queries.

The id is selected, and travels with every row, ONLY so that DISTINCT (which the
Category filter applies to collapse its duplicate join rows) is evaluated per
Track record: without it, two different Tracks with identical exported metadata
would be collapsed into one row. It is never written to the file.
"""
import csv
import math

HEADER = ("Artist", "Title", "Album", "Category", "Duration")

#: UTF-8 byte-order mark; see the module docstring.
CSV_BOM = b"\xef\xbb\xbf"

#: Rows fetched from the database per round trip (QuerySet.iterator chunk_size).
ITERATOR_CHUNK_SIZE = 2000
#: Rows rendered per yielded chunk of response body.
BATCH_ROWS = 500

#: A value starting with one of these (after leading whitespace) is a formula to a spreadsheet.
FORMULA_TRIGGERS = ("=", "+", "-", "@")
#: A value starting with one of these is treated as a formula prefix by some importers (OWASP).
CONTROL_TRIGGERS = ("\t", "\r", "\n")
NEUTRALISER = "'"


def spreadsheet_safe(value):
    """Return ``value`` as text that no spreadsheet will execute as a formula.

    ``None`` becomes "". A value beginning (after any leading whitespace) with
    ``=``, ``+``, ``-`` or ``@``, or beginning with a TAB, CR or LF, gets a
    single leading apostrophe. Everything else is returned unchanged.
    """
    if value is None:
        return ""
    text = str(value)
    if not text:
        return text
    if text[0] in CONTROL_TRIGGERS or text.lstrip()[:1] in FORMULA_TRIGGERS:
        return NEUTRALISER + text
    return text


def format_duration(seconds):
    """``m:ss`` exactly as the Library table's fmtDuration shows it; "" when unknown.

    Round the whole value half-up (JavaScript Math.round; NOT Python's banker's
    round, which would give 0:02 for 2.5 and 0:02 for 1.5) before splitting.
    Zero is a real duration (0:00); None and non-finite values are unknown.
    """
    if seconds is None:
        return ""
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(value):
        return ""
    total = math.floor(value + 0.5)
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def export_queryset(qs):
    """The (id, artist, title, album, category code, duration) rows of an ALREADY filtered and
    ordered Track queryset. The id is carried for DISTINCT correctness only (module docstring)."""
    return qs.values_list(
        "pk", "artist__name", "title", "album__title", "category__code", "duration_seconds")


class _Echo:
    """csv.writer target that returns what it is given instead of buffering it."""
    def write(self, text):
        return text


def stream_rows(rows, batch_rows=None):
    """Yield the CSV body (bytes) for ``rows`` -- an iterable of
    ``(id, artist, title, album, category, duration)`` -- in bounded batches.
    The first chunk is the BOM and header, so a client sees bytes immediately."""
    batch_rows = batch_rows or BATCH_ROWS
    writer = csv.writer(_Echo())
    yield CSV_BOM + writer.writerow(HEADER).encode("utf-8")
    batch = []
    for _pk, artist, title, album, category, duration in rows:
        batch.append(writer.writerow((
            spreadsheet_safe(artist), spreadsheet_safe(title), spreadsheet_safe(album),
            spreadsheet_safe(category), format_duration(duration))))
        if len(batch) >= batch_rows:
            yield "".join(batch).encode("utf-8")
            batch = []
    if batch:
        yield "".join(batch).encode("utf-8")
