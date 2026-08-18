"""Deterministic Dynamic/Rotating PS frame generation -- Modes 0-3,
modeled on the established Dynamic PS modes used by RDS encoder
families such as Pira (IsadoraAir roadmap [P1] 2.3B, 2026-08-18).

Pure frame-GENERATION logic only: converts an arbitrary source string
into the ordered list of already-8-character PS frames a runtime
rotation/timing layer would cycle through. This module does NOT decide
WHEN to advance between frames or how long each one is shown -- that's
rbds/services/rotation.py's PSRotation, a timing/state machine over
already-created RBDSPSFrame rows, deliberately untouched here. Nothing
in this module reads a clock, touches the database, opens a socket, or
mutates any state outside its own call stack.

Character normalization: generate_ps_frames() normalizes its input via
rbds.services.charset.normalize_text() INTERNALLY, as its very first
step, before any frame boundary is computed. Callers pass raw,
un-normalized Unicode text; they must NOT pre-normalize before calling
this module. This is a deliberate API choice (the "hardest for future
callers to misuse" option), not an oversight: normalize_text() can
change string LENGTH -- the "..." -> "..." ellipsis expansion is the
concrete, already-documented example in charset.py -- so computing
frame boundaries on anything other than its OUTPUT would put those
boundaries in the wrong place relative to what actually reaches the
transmitter. Requiring callers to remember to normalize first, in the
right order, is exactly the kind of easy-to-get-wrong contract this
module avoids by just doing it internally, unconditionally, every
time. normalize_text() is also idempotent (NFC-canonicalization, the
smart-punctuation table, and the control-character translation are all
no-ops on their own already-normalized output), so a caller that
happens to ALSO normalize the same text elsewhere before handing it
here (e.g. RT text already run through normalize_text() upstream) is
harmless, not a bug.

encode_rds_g0() (also in charset.py) is intentionally NEVER called
here. It's strictly length-preserving (one output byte per input
character), so it plays no role in frame-BOUNDARY placement -- it
belongs at the existing uecp.mec_ps() call site, same as today,
applied to each already-8-character frame this module returns. This
module has no G0 awareness at all; an unsupported (non-G0) character
occupies exactly one character position in the normalized stream, same
as any other, and is left completely alone here -- encode_rds_g0()
substitutes it with a space only later, at actual transmission time,
which is also the only place any character is ever "lost" in the
existing pipeline.

Output contract: generate_ps_frames(text, mode) returns a list[str],
each element exactly PS_FRAME_WIDTH (8) characters, in display order,
ready to pass straight to uecp.mec_ps() -- whose own `text[:8].ljust(8)`
becomes a no-op for an already-exactly-8-character frame. Deterministic:
same input always produces the same output, no database/clock/socket
access, no global state mutated anywhere. An unsupported `mode` value
raises ValueError rather than silently falling back to some default
mode. No arbitrary text-length limit is imposed here -- UI/config
length policy is a separate, later concern (roadmap 2.3C), not this
pure generator's job.

--- Source composition (IsadoraAir roadmap [P1] 2.3E, 2026-08-18) ---

compose_dynamic_ps_source() and validate_dynamic_ps_format(), below,
are a SEPARATE, EARLIER pipeline stage from everything above: they
build the raw SOURCE STRING that generate_ps_frames() then consumes,
by combining RBDSConfig.dynamic_ps_text with a now-playing snapshot
per RBDSConfig.dynamic_ps_format. This is still Generated Rotating PS
-- not a fourth ps_mode -- Modes 0-3 above are completely unaware this
stage exists; they just receive whatever final string the caller hands
them, exactly as before 2.3E. Same file-level I/O-free contract as the
rest of this module applies to both: no database, network, clock, or
filesystem access, and no eval() -- format parsing goes through
Python's own string.Formatter().parse(), never str.format() against
untrusted-shaped input without that same validation having already run
first. now_playing is a plain {"artist": ..., "title": ...} dict the
CALLER already read elsewhere (RBDSManager._tick() reads
now_playing.json once per tick and passes that same snapshot into both
PS resolution and RT resolution) -- this module never touches that
file itself and has no opinion about where the dict came from.

Composition happens BEFORE normalization, same ordering principle as
plain dynamic_ps_text already followed: the composed source is hard-
coded to flow straight into generate_ps_frames(), which normalizes and
computes frame boundaries on the COMPLETE composed string, never on
dynamic_text or the now-playing values in isolation -- see
generate_ps_frames()'s own docstring above for why boundary placement
must always happen after the final string exists, not before."""

import string

from rbds.services.charset import normalize_text

PS_FRAME_WIDTH = 8

MODE_FIXED_CELLS = 0        # Mode 0 -- 8-character fixed cells, no scrolling
MODE_SLIDING_WINDOW = 1     # Mode 1 -- 1-character sliding window
MODE_WORD_ALIGNED = 2       # Mode 2 -- left-aligned, word-wrapped cells
MODE_SCROLL_WITH_BLANK = 3  # Mode 3 -- Mode 1 windowing over blank-padded text

_VALID_MODES = (MODE_FIXED_CELLS, MODE_SLIDING_WINDOW, MODE_WORD_ALIGNED, MODE_SCROLL_WITH_BLANK)

# The ONLY placeholder names compose_dynamic_ps_source()/
# validate_dynamic_ps_format() ever accept -- [P1] 2.3E. Deliberately
# NOT sourced from RT/RT+/RBDSMessage/weather/category/PTYN in any way;
# {now_playing}/{artist}/{title} come exclusively from the now_playing
# dict the caller passes in, which is the same now-playing.json engine.py
# already writes for the CURRENTLY PLAYING track -- not RT's independent
# promo/message rotation. See this module's own [P1] 2.3E docstring
# section above for the full rationale.
DYNAMIC_PS_FORMAT_FIELDS = frozenset({"text", "now_playing", "artist", "title"})


def generate_ps_frames(text, mode):
    """Converts `text` (raw, un-normalized Unicode -- see module
    docstring for why callers must NOT pre-normalize) into the ordered
    list of PS_FRAME_WIDTH-character PS frames Mode `mode` (0-3)
    defines for it. Raises ValueError for any other `mode` value --
    never silently falls back to a default mode."""
    if mode not in _VALID_MODES:
        raise ValueError(
            f"generate_ps_frames: unsupported mode {mode!r} (expected one of {_VALID_MODES})"
        )
    normalized = normalize_text(text)
    if mode == MODE_FIXED_CELLS:
        return _fixed_chunks(normalized)
    if mode == MODE_SLIDING_WINDOW:
        return _sliding_window(normalized)
    if mode == MODE_WORD_ALIGNED:
        return _word_aligned(normalized)
    return _scroll_with_blank(normalized)


def _fixed_chunks(text):
    """Mode 0 (also reused by _word_aligned() for chunking a single
    overlength word -- see that function): consecutive, non-overlapping
    PS_FRAME_WIDTH-character chunks of `text` exactly as given --
    internal spaces are preserved verbatim, never collapsed. This is
    the raw/fixed-cell mode; manual formatting in the source text is
    meaningful here, unlike Mode 2's word-wrapping. Only the FINAL
    chunk is right-padded; every earlier chunk is already exactly
    PS_FRAME_WIDTH wide by construction. Empty text -> one all-blank
    frame (the "nothing to show" convention every mode in this module
    uses)."""
    if not text:
        return [" " * PS_FRAME_WIDTH]
    chunks = [text[i:i + PS_FRAME_WIDTH] for i in range(0, len(text), PS_FRAME_WIDTH)]
    chunks[-1] = chunks[-1].ljust(PS_FRAME_WIDTH)
    return chunks


def _sliding_window(text):
    """Mode 1 (also reused by _scroll_with_blank() for Mode 3, which is
    defined purely as this same algorithm applied to a padded string):
    every consecutive PS_FRAME_WIDTH-character window, advancing
    exactly one character per frame. No artificial leading/trailing
    padding is added here, and no circular wraparound -- the last
    window ends at the literal end of `text`; a runtime rotation
    looping back to frame 0 afterward is a separate, later timing
    concern, not this function's. Text no longer than PS_FRAME_WIDTH
    has no room for a second distinct window -- exactly one
    right-padded frame, same convention as Mode 0's short-text case."""
    if len(text) <= PS_FRAME_WIDTH:
        return [text.ljust(PS_FRAME_WIDTH)]
    return [text[i:i + PS_FRAME_WIDTH] for i in range(len(text) - PS_FRAME_WIDTH + 1)]


def _word_aligned(text):
    """Mode 2: left-aligned, greedy word-wrapping into
    PS_FRAME_WIDTH-character cells, one space between words within a
    cell. `text.split()` (no arguments) is Python's own "collapse any
    run of whitespace, discard leading/trailing" behavior -- exactly
    the "normalize ordinary whitespace between words to a single
    separator" this mode calls for, reused rather than reimplemented.
    Punctuation is never treated as a separator, so it always stays
    attached to its word -- no character is lost here beyond whatever
    normalize_text() already performed upstream, before this function
    ever sees the text.

    A word longer than PS_FRAME_WIDTH can never be truncated -- it's
    instead split into its own consecutive fixed-width chunks (see
    _fixed_chunks(), reused here too) and each chunk becomes a frame of
    its own. Per this module's deliberately simple, first-cut rule: an
    overlength word always starts on a fresh frame (whatever short
    word(s) were accumulating get flushed/padded first) and always
    ends one too -- the short, padded remainder of its final chunk is
    NEVER shared with whatever word comes next. This keeps the mode
    fully deterministic without introducing a second,
    one-character-scrolling sub-mode just for long words.

    No words at all (empty or whitespace-only text) -> one all-blank
    frame, the same "nothing to show" convention every mode in this
    module uses."""
    words = text.split()
    if not words:
        return [" " * PS_FRAME_WIDTH]

    frames = []
    current = ""
    for word in words:
        if len(word) > PS_FRAME_WIDTH:
            if current:
                frames.append(current.ljust(PS_FRAME_WIDTH))
                current = ""
            frames.extend(_fixed_chunks(word))
            continue
        candidate = word if not current else f"{current} {word}"
        if len(candidate) <= PS_FRAME_WIDTH:
            current = candidate
        else:
            frames.append(current.ljust(PS_FRAME_WIDTH))
            current = word
    if current:
        frames.append(current.ljust(PS_FRAME_WIDTH))
    return frames


def _scroll_with_blank(text):
    """Mode 3: IsadoraAir-DEFINED behavior (explicitly not a claimed
    reproduction of any particular vendor firmware's private
    implementation) as PS_FRAME_WIDTH blank characters + `text` +
    PS_FRAME_WIDTH blank characters, then Mode 1's sliding-window
    algorithm (_sliding_window(), reused directly -- this mode is a
    padding step in front of Mode 1, not a distinct windowing
    algorithm of its own) applied to that padded string. The result
    visibly scrolls the text into and back out of the 8-character PS
    display, with one complete blank cell separating repetitions.

    Empty `text` is special-cased to a single all-blank frame rather
    than running the padded (all-blank, 2*PS_FRAME_WIDTH-character)
    working string through the sliding window -- that would otherwise
    produce PS_FRAME_WIDTH + 1 = 9 identical blank windows for zero
    actual content, which is technically correct but not a meaningful
    distinct sequence worth generating."""
    if not text:
        return [" " * PS_FRAME_WIDTH]
    padding = " " * PS_FRAME_WIDTH
    return _sliding_window(padding + text + padding)


# --- Source composition (IsadoraAir roadmap [P1] 2.3E) ---
# See this module's own docstring, "Source composition" section, for
# the full contract. Everything below is a pipeline stage that runs
# BEFORE generate_ps_frames() ever sees a string -- Modes 0-3 above are
# completely unaware it exists.

def _parse_format_fields(format_string):
    """Yields (field_name, conversion, format_spec) for every REAL
    placeholder in `format_string` -- literal-text-only segments
    (field_name is None) are skipped. The single shared parse used by
    both validate_dynamic_ps_format() and compose_dynamic_ps_source()'s
    own now-playing-referenced check, so the two can never drift out of
    sync about what counts as "a real placeholder." Uses Python's own
    string.Formatter().parse() -- never eval(), never str.format()
    against a not-yet-validated template. Malformed syntax (e.g. an
    unmatched '{') raises ValueError from within Formatter.parse()
    itself, mid-iteration; this function does not catch it, so it
    propagates to the caller as a plain ValueError, same exception type
    every other rejection in this section raises."""
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(format_string):
        if field_name is not None:
            yield field_name, conversion, format_spec


def _references_field(format_string, name):
    """True if `format_string` contains a real {name} placeholder.
    Treats a malformed format_string as "doesn't reference it" rather
    than raising -- compose_dynamic_ps_source()'s own try/except around
    the actual formatting step is where a malformed template's fallback
    behavior is decided; this helper is only ever used for the
    empty-now-playing fallback decision, not for validation."""
    try:
        return any(field_name == name for field_name, _conversion, _spec in _parse_format_fields(format_string))
    except ValueError:
        return False


def validate_dynamic_ps_format(format_string):
    """Rejects anything in `format_string` other than a plain
    {field} substitution where field is one of DYNAMIC_PS_FORMAT_FIELDS
    -- no attribute access ({text.foo}), no indexing ({text[0]}), no
    conversion specifiers ({text!r}), no format specs ({text:>8}), no
    unknown field names, no bare positional {} placeholders. Escaped
    literal braces ({{ / }}) are always fine -- string.Formatter itself
    already treats those as literal text, never as a field.

    Attribute access and indexing are caught for free: Formatter.parse()
    returns the WHOLE "text.foo" / "text[0]" grammar production as one
    field_name string, which then simply fails the exact-membership
    check against DYNAMIC_PS_FORMAT_FIELDS below like any other unknown
    name would -- no separate dotted/bracket detection needed.

    Raises ValueError (never returns a truthy/falsy verdict) with a
    specific, operator-facing reason for the first problem found.
    Callers that need a Django ValidationError (RBDSConfig.clean())
    catch this ValueError and wrap it themselves, matching how
    generate_ps_frames()'s own ValueError is already handled at its own
    call sites (e.g. the admin preview)."""
    for field_name, conversion, format_spec in _parse_format_fields(format_string):
        if field_name == "":
            raise ValueError(
                "Empty {} placeholders are not supported -- use one of the named fields below."
            )
        if field_name not in DYNAMIC_PS_FORMAT_FIELDS:
            allowed = ", ".join(f"{{{f}}}" for f in sorted(DYNAMIC_PS_FORMAT_FIELDS))
            raise ValueError(f"Unknown placeholder {{{field_name}}} -- supported fields are {allowed}.")
        if conversion is not None:
            raise ValueError(
                f"Conversion specifiers are not supported: {{{field_name}!{conversion}}}. "
                f"Use plain {{{field_name}}} instead."
            )
        if format_spec:
            raise ValueError(
                f"Format specifications are not supported: {{{field_name}:{format_spec}}}. "
                f"Use plain {{{field_name}}} instead."
            )


def compose_dynamic_ps_source(format_string, dynamic_text, now_playing):
    """Builds the raw SOURCE STRING for Generated Rotating PS by
    combining operator-entered `dynamic_text` (RBDSConfig.dynamic_ps_text)
    with `now_playing` (a plain {"artist": ..., "title": ...} dict --
    see this module's docstring) according to `format_string`
    (RBDSConfig.dynamic_ps_format). Pure and deterministic: no
    database/network/clock/filesystem access, same input always
    produces the same output. The result is NOT normalized here --
    that's generate_ps_frames()'s own job, applied to the complete
    composed string, never to dynamic_text or the now-playing values in
    isolation (see this module's docstring for why boundary placement
    must happen after composition, not before).

    {now_playing} resolves to the friendly "Artist - Title" join when
    both are present, just the title or just the artist when only one
    is, and "" when neither is -- the same join convention
    RBDSManager._resolve_rt_content() already uses for RT, kept
    identical here so the two independent displays read the same way
    when they happen to show the same information. {artist}/{title}
    substitute directly (blank when absent) with no such combining --
    intended for advanced templates that want each piece separately;
    {now_playing} is the recommended token when a friendly, gap-free
    fallback is wanted.

    Empty-now-playing fallback: if BOTH artist and title are blank
    (after stripping, so whitespace-only counts as blank) AND
    format_string references {now_playing}, the ENTIRE composed source
    collapses to `dynamic_text` alone -- e.g. format
    "{text} | Now Playing: {now_playing}" with nothing playing resolves
    to just "Oak Grove Radio 98.5", never a dangling
    "Oak Grove Radio 98.5 | Now Playing: ". This is a narrow, literal
    rule (checks specifically for the {now_playing} placeholder, not a
    general "any reference to now-playing" heuristic) -- deliberately
    not a general punctuation-cleanup algorithm. A template using only
    {artist}/{title} (no {now_playing}) does NOT get this collapse --
    those tokens simply substitute as blank, per their own documented
    contract above.

    format_string is trusted to have already passed
    validate_dynamic_ps_format() -- RBDSConfig.clean() is the
    enforcement point, matching how RBDSConfig.now_playing_format is
    already trusted by _resolve_rt_content() elsewhere in this project
    without re-validating its grammar on every tick. A malformed/
    unformattable format_string (only reachable by bypassing admin
    validation, e.g. a direct ORM write -- see af_frequencies_mhz's own
    precedent comment in rbds/models.py for this exact class of gap)
    falls back to `dynamic_text` alone rather than raising, the same
    defensive shape _resolve_rt_content() already uses for a malformed
    now_playing_format (KeyError/IndexError -> fall back to bare
    title) -- never raises."""
    artist = now_playing.get("artist") or ""
    title = now_playing.get("title") or ""
    has_artist = bool(artist.strip())
    has_title = bool(title.strip())

    if has_artist and has_title:
        friendly_now_playing = f"{artist} - {title}"
    elif has_title:
        friendly_now_playing = title
    elif has_artist:
        friendly_now_playing = artist
    else:
        friendly_now_playing = ""

    if not has_artist and not has_title and _references_field(format_string, "now_playing"):
        return dynamic_text

    try:
        return format_string.format(text=dynamic_text, now_playing=friendly_now_playing, artist=artist, title=title)
    except (KeyError, IndexError, ValueError):
        return dynamic_text
