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
pure generator's job."""

from rbds.services.charset import normalize_text

PS_FRAME_WIDTH = 8

MODE_FIXED_CELLS = 0        # Mode 0 -- 8-character fixed cells, no scrolling
MODE_SLIDING_WINDOW = 1     # Mode 1 -- 1-character sliding window
MODE_WORD_ALIGNED = 2       # Mode 2 -- left-aligned, word-wrapped cells
MODE_SCROLL_WITH_BLANK = 3  # Mode 3 -- Mode 1 windowing over blank-padded text

_VALID_MODES = (MODE_FIXED_CELLS, MODE_SLIDING_WINDOW, MODE_WORD_ALIGNED, MODE_SCROLL_WITH_BLANK)


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
