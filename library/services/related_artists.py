"""Single shared home for everything related to Track.related_artists --
parsing/formatting the stored field, normalizing names for comparison,
building a track's full artist-identity set for rotation separation,
conservatively extracting credited artists from tag/filename text, and
deriving human-readable fallback metadata for files with no usable
embedded tags.

Nothing else in the project should re-implement any of this. Before
this module existed, analyze_tracks.py, fix_unknown_artists.py, and
the various upload/import paths each had their own (subtly different)
version of "guess artist/title from a filename" -- exactly the kind of
drift that made two same-artist tracks fail to exclude each other in
rotation (see Artist.get_or_create_ci's docstring for a real incident
of that same class of bug). Every caller in this codebase goes through
this module now.

Mutual identity-set semantics (used by log_builder.py): a track's
"identity" for artist-separation purposes is its normalized primary
Artist.name PLUS every normalized name in its related_artists field.
Two tracks conflict whenever their identity sets intersect -- this is
symmetric by construction (see track_identity_keys): if track B's
primary artist appears in track A's related_artists, then A's identity
set contains B's primary-artist key, so A conflicts with B, and
because set intersection is symmetric, B's identity set (which
contains its own primary artist) also intersects A's -- B conflicts
with A too, with no separate code path needed for "the other
direction." Comparisons throughout are case-insensitive and
whitespace-normalized via normalize_name()'s casefold().

False negatives are always preferred over false positives in the
extraction functions here -- see extract_credited_artists's docstring.
"""
import re

from library.models import Artist, Track


def _related_artists_max_length():
    """Sourced from the model field itself (not a hardcoded 500) so this
    module can never silently drift out of sync with Track.related_
    artists' actual max_length -- see canonicalize_related_artists,
    merge_related_artists_detailed, and the API PATCH handler, which
    all rely on this instead of a duplicated magic number."""
    return Track._meta.get_field("related_artists").max_length


def existing_artist_names_casefolded():
    """Bulk-loaded once per run (never per track/candidate) -- gates
    extract_credited_artists's bare '&'/'and' split. Shared by every
    caller that needs it (analyze_tracks, autofill_related_artists_for_
    queryset below) so there's exactly one place this query is
    written."""
    return frozenset(n.casefold() for n in Artist.objects.values_list("name", flat=True))

# ---------------------------------------------------------------
# Normalization / parsing / formatting
# ---------------------------------------------------------------


def normalize_name(name):
    """Canonical comparison key for an artist name: Unicode-aware
    casefold() (not .lower() -- casefold handles things like German
    ß -> ss that lower() doesn't, and is the correct primitive for
    caseless matching per the Unicode standard), whitespace-collapsed,
    trimmed. Returns "" for anything falsy."""
    if not name:
        return ""
    return re.sub(r"\s+", " ", str(name)).strip().casefold()


def _clean_name(raw):
    """Trim, collapse internal whitespace, and strip surrounding
    quote/punctuation/bracket debris that can be left over from
    extraction (a flat-text credit tail that happened to start inside
    an unclosed bracket, a trailing '.'/'-' from a split, etc)."""
    if not raw:
        return ""
    name = re.sub(r"\s+", " ", str(raw)).strip()
    name = name.strip("'\"")
    name = name.strip(" .,;:-_()[]{}")
    return name.strip()


def canonicalize_related_artists(value, primary_artist_name=None):
    """Parse the stored comma-separated field into a clean, ordered
    list: split on commas, trim + collapse whitespace each entry, drop
    blanks, deduplicate case-insensitively (first-seen spelling wins),
    and drop any entry that exactly equals (after normalization) the
    track's primary artist name. Never reorders -- manual/existing
    entry order is always preserved. This is the one place that rule
    is enforced, so it applies uniformly whether the value came from a
    fresh manual edit or is the "existing" side of an auto-merge."""
    primary_key = normalize_name(primary_artist_name) if primary_artist_name else ""
    seen = set()
    result = []
    for part in (value or "").split(","):
        name = _clean_name(part)
        if not name:
            continue
        key = normalize_name(name)
        if not key or key in seen or key == primary_key:
            continue
        seen.add(key)
        result.append(name)
    return result


def parse_related_artists(value):
    """canonicalize_related_artists with no primary-artist context --
    for callers (e.g. identity-set building) that handle the primary
    artist name separately and just need the related-artist list as
    written."""
    return canonicalize_related_artists(value, primary_artist_name=None)


def format_related_artists(names):
    """Canonical stored/displayed form: 'David Gilmour, Roger Waters,
    Richard Wright' -- comma-space joined, no other transformation.
    Callers should pass an already-cleaned list (e.g. from
    canonicalize_related_artists or merge_related_artists)."""
    return ", ".join(names)


def _clip_to_length(names, max_length):
    """Drop trailing entries -- never truncate mid-name -- until the
    comma-joined string fits within max_length. Purely defensive: every
    write path in this module now enforces the limit going forward, and
    Track.related_artists is a real DB varchar(500) that has rejected
    over-limit writes since it was created, so an over-limit `names`
    list shouldn't be reachable in practice. Kept anyway so a future
    caller (or a row seeded some other way, e.g. a raw SQL fixture)
    can never make merge_related_artists_detailed's OWN output exceed
    the limit."""
    result = list(names)
    while result and len(format_related_artists(result)) > max_length:
        result.pop()
    return result


def merge_related_artists_detailed(existing_value, discovered_names, primary_artist_name=None, max_length=None):
    """Same append-only merge as merge_related_artists, but also reports
    exactly which discovered names were appended and which were skipped
    because they wouldn't fit within `max_length` (defaults to Track.
    related_artists' actual max_length -- see _related_artists_max_
    length). Skipped names are whichever ones happen to sort last in
    `discovered_names`' own deterministic source order that don't fit
    the remaining budget after everything before them -- greedy,
    first-fit, in order. An entry is only ever appended whole; nothing
    is ever truncated mid-name, and nothing already in `existing_value`
    is ever removed to make room.

    Returns (final_value, appended_names, skipped_names)."""
    if max_length is None:
        max_length = _related_artists_max_length()
    existing = canonicalize_related_artists(existing_value, primary_artist_name)
    existing = _clip_to_length(existing, max_length)
    seen = {normalize_name(n) for n in existing}
    primary_key = normalize_name(primary_artist_name) if primary_artist_name else ""
    appended = []
    skipped = []
    for raw_name in discovered_names:
        name = _clean_name(raw_name)
        key = normalize_name(name)
        if not key or key in seen or key == primary_key:
            continue
        candidate = existing + [name]
        if len(format_related_artists(candidate)) > max_length:
            # This whole name doesn't fit -- skip it (not truncate it)
            # and keep trying the rest of the discovered list; a later,
            # shorter name might still fit even though this one didn't.
            skipped.append(name)
            continue
        seen.add(key)
        existing = candidate
        appended.append(name)
    return format_related_artists(existing), appended, skipped


def merge_related_artists(existing_value, discovered_names, primary_artist_name=None, max_length=None):
    """Append-only merge used by every automatic-discovery path
    (analyzer, autofill command/endpoint) and, with an empty
    discovered_names list, by a fresh manual edit too. Existing/manual
    entries are canonicalized but never removed or reordered; each
    name in `discovered_names` (assumed already in deterministic
    source order) is appended only if it isn't already present
    case-insensitively, doesn't exactly match the primary artist, AND
    fits within max_length (see merge_related_artists_detailed) --
    never overflows the field, never truncates an entry in half.
    Returns the final canonical string, ready to save. Callers that
    need to know which names were appended/skipped for reporting
    purposes (the autofill service) should call merge_related_artists_
    detailed directly instead."""
    value, _appended, _skipped = merge_related_artists_detailed(
        existing_value, discovered_names, primary_artist_name=primary_artist_name, max_length=max_length,
    )
    return value


def track_identity_keys(primary_artist_name, related_artists_value):
    """The full artist-identity set for one track: normalized primary
    Artist.name plus every normalized related-artist entry. See the
    module docstring for the mutual-conflict semantics this enables."""
    keys = set()
    primary_key = normalize_name(primary_artist_name)
    if primary_key:
        keys.add(primary_key)
    for name in parse_related_artists(related_artists_value):
        key = normalize_name(name)
        if key:
            keys.add(key)
    return frozenset(keys)


# ---------------------------------------------------------------
# Filename humanization (fallback metadata only -- never for the
# on-disk filename itself, and never for a value that came from a
# real embedded tag)
# ---------------------------------------------------------------


def humanize_filename_stem(stem):
    """Underscore -> space, collapse repeated whitespace, trim. For
    deriving human-readable FALLBACK metadata from a filename stem
    (e.g. the original client-supplied upload filename, or an
    on-disk stem for a directory import) when embedded tags are
    absent. get_valid_filename()'s underscore-substituted form stays
    the on-disk name unchanged -- this function only ever feeds a DB
    title/artist fallback value, never a rename.

    Defense in depth against a directory-like caller-supplied "stem":
    pathlib.Path(...).stem already strips a leading '/'-style path
    prefix on every platform, but PosixPath (this server is Linux)
    does NOT treat '\\' as a separator -- an untrusted, client-supplied
    original upload filename containing backslashes (e.g. a raw
    Windows-style path like "Users\\john\\Music\\Pink_Floyd_-_Time")
    would otherwise survive whole into fallback metadata, showing up
    as a misleading path-like title/artist. Normalized here (the one
    shared boundary every fallback-metadata caller routes through)
    rather than requiring every call site to pre-sanitize -- keeps
    only the final path component regardless of separator style."""
    if not stem:
        return ""
    text = str(stem).replace("\\", "/").rsplit("/", 1)[-1]
    text = text.replace("_", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------
# Conservative "Artist - Title" filename/title splitter -- the single
# shared implementation (previously duplicated as
# fix_unknown_artists.parse_artist_title).
# ---------------------------------------------------------------

# Any of ASCII hyphen, en dash, em dash, with optional surrounding
# whitespace. Split on the FIRST such delimiter.
_ARTIST_TITLE_SPLIT_RE = re.compile(r"\s*[-–—]\s*")

# Skip syndicated-show style titles like "2015-07-30 MITD Part 1" -- the
# leading token is a date, not an artist.
_DATE_PREFIX_RE = re.compile(r"^\d{4}[-–—]\d{2}[-–—]\d{2}\b")

# "01 " prefix on ripped-from-album titles ("01 The Lumineers - ...").
_LEADING_TRACK_NUM_SPACE_RE = re.compile(r"^\d+\s+")
# "05 - " prefix ("05 - Sugar Moth - ...") -- second pass after the
# first has run, so a two-digit-plus-space wasn't already stripped.
_LEADING_TRACK_NUM_DASH_RE = re.compile(r"^\d+\s*[-–—]\s*")


def parse_artist_title(source):
    """Parse "Artist - Title" out of a source string. Skips
    date-formatted prefixes and strips a leading track-number token.
    Returns (artist, title), or (None, None) if no clean split is
    found. Shared by fix_unknown_artists and the metadata-less-
    filename fallback path below -- keep this the single
    implementation; do not fork another copy."""
    if not source:
        return None, None
    if _DATE_PREFIX_RE.match(source):
        return None, None
    cleaned = _LEADING_TRACK_NUM_SPACE_RE.sub("", source, count=1)
    cleaned = _LEADING_TRACK_NUM_DASH_RE.sub("", cleaned, count=1)
    parts = _ARTIST_TITLE_SPLIT_RE.split(cleaned, maxsplit=1)
    if len(parts) == 2:
        artist = parts[0].strip()
        title = parts[1].strip()
        if artist and title:
            return artist, title
    return None, None


def resolve_fallback_metadata(original_stem, tag_title, tag_artist):
    """(title, artist_name) to actually store, for a file whose
    embedded tags may be partially or fully absent. `original_stem` is
    the file's ORIGINAL name (before any filesystem-safety
    sanitization) with the extension removed -- e.g. the client's
    uploaded filename for /library/import/, or the on-disk stem for a
    directory import. `tag_title`/`tag_artist` are whatever was
    actually read from embedded tags (falsy/None if absent; a
    placeholder like "Unknown Artist" counts as absent).

    Contract (never overwrites a real tag value, only fills gaps):
      1. Humanize the original filename stem (underscores -> spaces).
      2. Attempt the conservative "Artist - Title" split on that.
      3. If a clean split is found, use it to fill ONLY whichever of
         title/artist was actually missing.
      4. Any tag value that was already present is returned unchanged.
      5. If no clean split is found, the title falls back to the
         humanized stem (or stays as the existing tag if present) and
         the artist falls back to "Unknown Artist" (or stays as the
         existing tag if present).

    This is what keeps a metadata-less upload like
    "Pink_Floyd_-_Time.wav" from landing in the DB as literal
    underscores -- see the module docstring's sibling functions for
    the on-disk-filename-safety distinction this deliberately does
    NOT touch."""
    have_title = bool(tag_title)
    have_artist = bool(tag_artist) and tag_artist != "Unknown Artist"

    if have_title and have_artist:
        return tag_title, tag_artist

    humanized = humanize_filename_stem(original_stem)
    guessed_artist, guessed_title = parse_artist_title(humanized)

    if guessed_artist and guessed_title:
        title = tag_title if have_title else guessed_title
        artist_name = tag_artist if have_artist else guessed_artist
        return title, artist_name

    title = tag_title if have_title else humanized
    artist_name = tag_artist if have_artist else "Unknown Artist"
    return title, artist_name


# ---------------------------------------------------------------
# Conservative credited-artist extraction
# ---------------------------------------------------------------

# feat/feat./ft/ft./featuring, case-insensitive. The trailing \b before
# the optional literal '.' means this matches "feat"/"feat."/"ft"/
# "ft."/"featuring" but not inside a longer word like "feathers" --
# \b requires an actual word-boundary transition right after the
# marker letters, which "feat." has (t -> .) but "feathers" doesn't
# (t -> h, both word chars). "ft" as a bare word is an inherent,
# spec-accepted ambiguity (e.g. "5 ft tall") -- required to be
# recognized regardless.
_FEAT_MARKER_RE = re.compile(r"\b(?:feat|ft|featuring)\b\.?\s*", re.IGNORECASE)

# Bracket-group content, e.g. the inside of "(...)" or "[...]" --
# non-nested (band/track naming doesn't nest brackets in practice, and
# nesting support isn't worth the complexity for a conservative
# extractor).
_BRACKET_GROUP_RE = re.compile(r"[\(\[]([^\(\)\[\]]*)[\)\]]")

# Approved bracket-form "with": the bracket's content must START with
# "with " -- "(with Artist B)" / "[with Artist B]". A bracket whose
# content merely CONTAINS "with" somewhere in the middle doesn't
# qualify (e.g. "(Live with Orchestra)" is not a credit construction).
_WITH_BRACKET_RE = re.compile(r"^\s*with\s+(.+)$", re.IGNORECASE)

# Approved dash-form "with": must be immediately preceded by a dash
# separator ("... - with Artist B"). This is deliberately narrow --
# see extract_credited_artists's docstring for why a bare "with"
# anywhere in running text (e.g. "A Room With a View") must NOT match.
_WITH_DASH_RE = re.compile(r"[-–—]{1,2}\s*with\s+(.+)$", re.IGNORECASE)

# Where a flat (non-bracket) credit tail stops: a dash-style separator,
# a slash separator, or the start of a bracket group / a stray closing
# bracket -- whichever comes first. Conservative: never consumes
# unrelated trailing title text past one of these.
_FLAT_TAIL_BOUNDARY_RE = re.compile(r"\s[-–—]{1,2}\s|\s/\s|[\(\[\)\]\}]")

# Splits a credit tail that names multiple artists: "A, B", "A & B",
# "A and B", "A / B", "A + B".
_MULTI_SPLIT_RE = re.compile(r"\s*[,&/]\s*|\s+and\s+|\s*\+\s*", re.IGNORECASE)

# A single BARE '&' or standalone 'and' -- used only for the
# existing-Artist-gated split (see _bare_conjunction_credits). Matches
# the first occurrence only (str.split-style via .search + slicing).
_BARE_CONJUNCTION_RE = re.compile(r"\s*&\s*|\s+and\s+", re.IGNORECASE)


def _split_multi(tail):
    return [n for n in (_clean_name(p) for p in _MULTI_SPLIT_RE.split(tail)) if n]


def _flat_tail(text, start):
    rest = text[start:]
    m = _FLAT_TAIL_BOUNDARY_RE.search(rest)
    return rest[:m.start()] if m else rest


def _feat_credits(text, is_artist_field):
    """Every feat/ft/featuring occurrence in `text` (a flat field
    string, not pre-restricted to inside/outside brackets -- brackets
    are handled again, redundantly but harmlessly, by
    _bracket_group_credits; duplicates collapse in the final dedup
    pass). Returns the credited tail's names, plus -- only when
    `is_artist_field` -- the leading component before the marker, so
    "Artist A feat. Artist B" can later conflict with a solo "Artist
    A" track too (see module docstring)."""
    names = []
    for m in _FEAT_MARKER_RE.finditer(text):
        if is_artist_field:
            leading = _clean_name(text[:m.start()])
            if leading:
                names.append(leading)
        tail = _flat_tail(text, m.end())
        names.extend(_split_multi(tail))
    return names


def _with_credits(text):
    """Flat (non-bracket) approved 'with' context only: '... - with
    Artist B'. Bracketed 'with' is covered by _bracket_group_credits.
    Deliberately does NOT match a bare 'with' anywhere else."""
    names = []
    m = _WITH_DASH_RE.search(text)
    if m:
        tail = _flat_tail(text, m.start(1))
        names.extend(_split_multi(tail))
    return names


def _bracket_group_credits(inner_text):
    """feat/ft/featuring/with credits strictly inside one bracket
    group's own content -- the group's own boundary is the stop, no
    separate tail search needed."""
    names = []
    for m in _FEAT_MARKER_RE.finditer(inner_text):
        names.extend(_split_multi(inner_text[m.end():]))
    wm = _WITH_BRACKET_RE.match(inner_text)
    if wm:
        names.extend(_split_multi(wm.group(1)))
    return names


def _bare_conjunction_credits(text, existing_artist_names_casefolded):
    """Split `text` on the FIRST bare '&' or standalone 'and' into two
    sides. Both sides count as discovered names ONLY if they BOTH
    case-insensitively match an existing Artist row -- otherwise this
    is almost certainly a permanent group/duo name ("Simon &
    Garfunkel"), not two separately-credited artists, and must be left
    alone. `existing_artist_names_casefolded` must be bulk-loaded once
    by the caller (see extract_credited_artists) -- never queried here
    per candidate."""
    if not existing_artist_names_casefolded:
        return []
    m = _BARE_CONJUNCTION_RE.search(text)
    if not m:
        return []
    left = _clean_name(text[:m.start()])
    right = _clean_name(text[m.end():])
    if not left or not right:
        return []
    if (left.casefold() in existing_artist_names_casefolded
            and right.casefold() in existing_artist_names_casefolded):
        return [left, right]
    return []


def extract_credited_artists(artist_text, title_text, primary_artist_name,
                              existing_artist_names_casefolded=frozenset()):
    """Conservative discovery of credited artist names from a track's
    artist and/or title text -- operates ONLY on those two DB fields,
    deliberately no filename parameter (see module docstring: once a
    track has human-readable artist/title, inspecting the filename too
    would be a second, potentially-conflicting extraction source).

    Recognizes, case-insensitively, wherever they form a credible
    credit construction (including inside parentheses/brackets):
      - feat., feat, ft., ft, featuring
      - with, but ONLY in "(with X)", "[with X]", or "... - with X"
        form -- never a bare "with" in running text (a title like
        "A Room With a View" must not produce a related artist)
      - a bare '&' or 'and', but ONLY when both resulting sides
        already exist as Artist rows (gated by
        `existing_artist_names_casefolded`, bulk-loaded once by the
        caller) -- otherwise a permanent group name containing '&'/
        'and' (e.g. "Simon & Garfunkel") is left intact.
    When a feat/ft/featuring marker is found within `artist_text`
    itself, the leading component before the marker is ALSO returned,
    so "Artist A feat. Artist B" can conflict with both a solo
    "Artist A" track and a solo "Artist B" track.

    Returns an ordered, case-insensitively deduplicated list (first-
    seen spelling kept) with any entry that exactly normalizes to
    `primary_artist_name` removed. This is raw discovery output --
    NOT yet merged with any existing related_artists value; see
    merge_related_artists for that.

    False negatives are always preferred over false positives:
    no band-membership/songwriter/producer/album-artist inference,
    no guessing at arbitrary words after "with", no splitting a
    permanent group name just because it contains '&'/'and'."""
    discovered = []
    for text, is_artist_field in ((artist_text or "", True), (title_text or "", False)):
        if not text:
            continue
        for bm in _BRACKET_GROUP_RE.finditer(text):
            discovered.extend(_bracket_group_credits(bm.group(1)))
        discovered.extend(_feat_credits(text, is_artist_field))
        discovered.extend(_with_credits(text))
        discovered.extend(_bare_conjunction_credits(text, existing_artist_names_casefolded))

    primary_key = normalize_name(primary_artist_name)
    seen = set()
    result = []
    for name in discovered:
        key = normalize_name(name)
        if not key or key == primary_key or key in seen:
            continue
        seen.add(key)
        result.append(name)
    return result


# ---------------------------------------------------------------
# Shared autofill application -- the single implementation behind both
# the /library/ "Auto-fill Related Artists" toolbar action
# (api_track_autofill_related_artists) and the autofill_related_artists
# management command. Neither one re-implements this; the view does
# NOT shell out to the command, and the command does NOT hit the HTTP
# endpoint -- both call this function directly.
# ---------------------------------------------------------------

SAMPLE_CAP = 25  # cap on how many before/after examples callers get back


def autofill_related_artists_for_queryset(qs, apply=True):
    """Run automatic related-artist discovery across every track in
    `qs` -- already filtered by the caller (see track_filters.
    filter_tracks); this function does no filtering of its own, and
    processes the COMPLETE queryset, not a page of it.

    apply=False computes everything (what WOULD change) without
    writing to the DB -- the management command's default dry-run
    mode. apply=True writes each changed track's canonical
    related_artists. Every write is append-only via merge_related_
    artists -- an existing manual value is never replaced, only added
    to.

    One bulk query loads the existing-Artist-name set up front (never
    re-queried per track), and Track data is pulled via a single
    .values_list() rather than instantiating full model objects.

    Returns a dict: scanned, changed, appended (total artist names
    added across all changed tracks), unchanged_no_discoveries,
    unchanged_already_current, unchanged_overflow (discoveries existed
    but none of them fit within the 500-char field -- the existing
    value was left exactly as it was, nothing truncated), overflow_
    skipped (total individual discovered names dropped across the
    whole run because they didn't fit -- may be > 0 even on a track
    that otherwise changed, if SOME of its discoveries fit and others
    didn't), errors, and samples (up to SAMPLE_CAP {track_id, title,
    artist, before, after} dicts for changed tracks, so a caller can
    show a few concrete examples without dumping the entire result
    set).

    Read side: `qs.values_list(...).iterator(chunk_size=...)` streams
    rows from the DB in bounded chunks rather than materializing the
    complete filtered queryset (which could be the entire library) in
    memory at once.

    Write side: changed tracks are buffered and written via bulk_
    update() in batches of BATCH_SIZE -- one round trip per batch
    instead of one per track. If a batch write fails for any reason,
    that batch (and only that batch) falls back to writing its rows
    one at a time, so a single bad row can never silently discard an
    entire batch's worth of otherwise-good writes, and a row that
    fails even in the fallback is deducted from `changed`/`appended`
    and counted in `errors` rather than being reported as applied when
    it wasn't."""
    # Named distinctly from the existing_artist_names_casefolded()
    # function itself -- assigning to that same name here would make
    # Python treat it as a local for the whole function body (shadowing
    # the module-level function) and the call on the right-hand side
    # would raise UnboundLocalError.
    known_artist_names_cf = existing_artist_names_casefolded()
    max_length = _related_artists_max_length()

    counts = {
        "scanned": 0, "changed": 0, "appended": 0,
        "unchanged_no_discoveries": 0, "unchanged_already_current": 0,
        "unchanged_overflow": 0, "overflow_skipped": 0,
        "errors": 0,
    }
    samples = []
    pending_writes = []
    BATCH_SIZE = 200

    def _flush_batch(batch):
        if not batch:
            return
        try:
            stubs = [Track(pk=tid, related_artists=val) for tid, val in batch]
            Track.objects.bulk_update(stubs, ["related_artists"], batch_size=len(stubs))
        except Exception:
            # Batched write failed as a whole (e.g. a transient DB
            # error) -- fall back to one UPDATE per row so a single
            # unwritable row can't cost the rest of the batch its
            # already-computed, otherwise-valid update.
            for tid, val in batch:
                try:
                    Track.objects.filter(id=tid).update(related_artists=val)
                except Exception as exc:
                    counts["errors"] += 1
                    counts["changed"] -= 1
                    print(f"  [autofill_related_artists] track {tid}: write failed: {exc}")

    rows = (
        qs.values_list("id", "title", "artist__name", "related_artists")
        .iterator(chunk_size=2000)
    )
    for track_id, title, artist_name, existing_related in rows:
        counts["scanned"] += 1
        try:
            discovered = extract_credited_artists(
                artist_name, title, artist_name,
                existing_artist_names_casefolded=known_artist_names_cf,
            )
            if not discovered:
                counts["unchanged_no_discoveries"] += 1
                continue

            before_list = canonicalize_related_artists(existing_related, primary_artist_name=artist_name)
            new_value, appended_names, skipped_names = merge_related_artists_detailed(
                existing_related, discovered, primary_artist_name=artist_name, max_length=max_length,
            )
            after_list = parse_related_artists(new_value)

            if skipped_names:
                counts["overflow_skipped"] += len(skipped_names)

            if new_value == format_related_artists(before_list):
                # Nothing to write -- either every discovery was
                # already present case-insensitively (already_current),
                # or every discovery that WASN'T already present
                # couldn't fit within the 500-char limit (overflow):
                # the existing value is left exactly as it was, never
                # truncated to make room.
                if skipped_names:
                    counts["unchanged_overflow"] += 1
                else:
                    counts["unchanged_already_current"] += 1
                continue

            counts["changed"] += 1
            counts["appended"] += len(appended_names)
            if apply:
                pending_writes.append((track_id, new_value))
                if len(pending_writes) >= BATCH_SIZE:
                    _flush_batch(pending_writes)
                    pending_writes = []
            if len(samples) < SAMPLE_CAP:
                samples.append({
                    "track_id": track_id, "title": title, "artist": artist_name or "",
                    "before": format_related_artists(before_list), "after": new_value,
                })
        except Exception as exc:
            # Never let one bad row (e.g. unexpected data) abort the
            # whole run -- same error-isolation convention as
            # fix_unknown_artists.py's per-track try/except.
            counts["errors"] += 1
            print(f"  [autofill_related_artists] track {track_id}: {exc}")

    if apply:
        _flush_batch(pending_writes)

    counts["samples"] = samples
    return counts
