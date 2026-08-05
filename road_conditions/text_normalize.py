"""Deterministic text transforms for KanDrive road-report speech --
NO generative model, just anchored regex/lookup substitutions, matching
weather's own PRONUNCIATION_MAP idiom (wx_forecast.py) of fixing
specific, verified Kokoro mispronunciations rather than a general-
purpose text rewriter.

Every substitution here is anchored to a route-number pattern, a whole-
word match, or a structured enum value -- never a bare compass letter
or common English word replaced without context (see LINK_DIRECTION_WORDS
below, which only ever fires on a structured `primary_direction` value
this project itself controls, never on free text). This is deliberate:
the task this module supports explicitly warns against corrupting
unrelated source text by over-eagerly replacing short abbreviations.
"""
import re

# Kansas state highways are "KS <number>" in the live API (verified
# 2026-08-04 reconnaissance -- NOT "K-<number>"). Interstates are
# "I-70"/"I 70"/"I70" in free text (all three forms seen in KDOT
# descriptions); US routes are "US 81". All three patterns require a
# route-number immediately after the prefix, so they can't misfire on
# unrelated text containing a bare "US" or "KS".
_ROUTE_PATTERNS = [
    (re.compile(r"\bI[-\s]?(\d+)\b"), r"Interstate \1"),
    (re.compile(r"\bUS[-\s]?(\d+)\b"), r"U.S. \1"),
    (re.compile(r"\bKS[-\s]?(\d+)\b"), r"Kansas Highway \1"),
    (re.compile(r"\bK[-\s](\d+)\b"), r"Kansas Highway \1"),  # the "K-15" shorthand,
    # in case it ever appears in free text (e.g. a detour description) even though
    # the API's own route-designator field never uses it -- see RoadConditionsConfiguration.routes'
    # help_text for the same K-<n>-vs-KS-<n> discrepancy documented for the config field.
]

# NB/SB/EB/WB as whole words are unambiguous roadway shorthand (not
# real English words), low collision risk. Case-insensitive, but only
# matched as a complete word.
_DIRECTION_ABBREV_PATTERNS = [
    (re.compile(r"\bNB\b", re.IGNORECASE), "northbound"),
    (re.compile(r"\bSB\b", re.IGNORECASE), "southbound"),
    (re.compile(r"\bEB\b", re.IGNORECASE), "eastbound"),
    (re.compile(r"\bWB\b", re.IGNORECASE), "westbound"),
]

# The source's structured `link-direction`/`primary_direction` enum
# values (see road_conditions/services.py's normalize_event -- these
# come straight from the LinkDirection enum in the CARS API's own
# schema) -- safe to map unconditionally since these are never free
# text, always one exact enum string.
LINK_DIRECTION_WORDS = {
    "N": "northbound",
    "NE": "northeastbound",
    "E": "eastbound",
    "SE": "southeastbound",
    "S": "southbound",
    "SW": "southwestbound",
    "W": "westbound",
    "NW": "northwestbound",
    "NORTHBOUND": "northbound",
    "SOUTHBOUND": "southbound",
    "EASTBOUND": "eastbound",
    "WESTBOUND": "westbound",
    "BOTH_DIRECTIONS": "in both directions",
    "NOT_DIRECTIONAL": "",
    "POSITIVE_DIRECTION": "",
    "NEGATIVE_DIRECTION": "",
    "ANY_OTHER": "",
}

_TWENTY_FOUR_SEVEN_RE = re.compile(r"\b24/7\b")

# KDOT boundary text commonly uses "X/Y" to mean "also known as" between
# two alternate names for the same line, e.g. "Clay County Line/Dickinson
# County Line" -- read as a run-together word by Kokoro otherwise.
# Applied AFTER the 24/7 special case above so that's already handled;
# only fires between two alphabetic words (never between digits), so a
# genuine fraction-like token is left alone.
_SLASH_BETWEEN_WORDS_RE = re.compile(r"(?<=[A-Za-z])/(?=[A-Za-z])")

# Kokoro's phonemizer treats "." between two digits as a sentence-end
# pause (same issue weather's own PRONUNCIATION_MAP documents for
# decimal numbers like "20.2 feet") -- spell out "point" so a width
# restriction like "12.5 feet" doesn't get read as "twelve [pause] five
# feet". Matches weather's own fix verbatim (same anchoring).
_DECIMAL_POINT_RE = re.compile(r"(?<=\d)\.(?=\d)")

# "11:59PM" -> "11:59 PM" -- a bare space before AM/PM reads more
# naturally than the source's jammed-together form. Time zone
# abbreviations (CDT/CST) are left as-is; Kokoro reads them fine as
# letters, and spelling out "Central Daylight Time" every occurrence
# would be more verbose than useful on air.
_TIME_AMPM_RE = re.compile(r"(\d{1,2}:\d{2})\s*([AP])\.?M\.?\b", re.IGNORECASE)

_WHITESPACE_RE = re.compile(r"\s+")

# KDOT's own `description` text sometimes ends with a "Next update
# time H:MM AM/PM TZ, M/D/YY" clause -- internal CARS record-keeping
# (when KDOT'S OWN SYSTEM next expects a human to revise this record),
# not a real-world fact a motorist can use. Squarely the "internal CARS
# terminology... database language" the task explicitly asks to avoid.
# Anchored to this exact, verified phrase (observed on multiple real
# live events, always as the final clause) -- never a general "strip
# anything after a date" rule, which would risk eating a real,
# motorist-relevant timing statement elsewhere in the same sentence.
_NEXT_UPDATE_TIME_RE = re.compile(
    r"\s*Next update time \d{1,2}:\d{2}\s*[AP]M\s+\w+,\s*\d{1,2}/\d{1,2}/\d{2,4}\.?\s*$",
    re.IGNORECASE,
)

# KDOT's own description text sometimes points the reader at its own
# web map for a detour's actual path ("...detour around the closure.
# See map for detour(s)."), verified live on a real event
# (CARS5-11605). A radio listener already driving can't act on "see
# map" -- exactly the "avoid raw URLs / don't send listeners to a
# website for something that should be stated directly" principle this
# task calls out, and it's also redundant with the presence-only
# "Motorists should use the posted detour" line report.py's
# has_detour() already adds. Anchored to this exact, verified phrase
# (not a general "strip anything mentioning a map" rule) -- wherever it
# appears in the text, not just at the end, since a future event could
# plausibly have it mid-sentence with more real content following.
_SEE_MAP_RE = re.compile(r"\s*See map for detour\(s\)\.?", re.IGNORECASE)


def normalize_route_notation(text):
    """I-70/I 70/I70 -> Interstate 70, US 81 -> U.S. 81, KS 15 -> Kansas
    Highway 15, K-15 -> Kansas Highway 15 (defensive, see _ROUTE_PATTERNS).
    Order matters: I-<n> before K-<n> so "I-70" is never misread as a
    K-route by an overzealous shared pattern (it isn't currently, since
    the patterns use different prefix letters, but kept explicit)."""
    for pattern, repl in _ROUTE_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def normalize_direction_abbreviations(text):
    """NB/SB/EB/WB whole-word only -- see module docstring for why this
    is safe where a bare compass letter would not be."""
    for pattern, repl in _DIRECTION_ABBREV_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def link_direction_to_words(value):
    """Maps a structured LinkDirection enum value (never free text) to
    spoken words. Unknown/blank values degrade to "" rather than
    raising -- KDOT's schema could introduce a new enum value at any
    time (see road_conditions/services.py's own normalize_event
    docstring on the same principle)."""
    if not value:
        return ""
    return LINK_DIRECTION_WORDS.get(value.upper().replace(" ", "_"), "")


def strip_internal_admin_text(text):
    """Removes KDOT-internal record-keeping/self-reference text that
    carries no actionable information for a motorist listening while
    driving: a trailing "Next update time ..." clause (see
    _NEXT_UPDATE_TIME_RE) and any "See map for detour(s)." reference
    (see _SEE_MAP_RE). Applied before other normalization since
    _NEXT_UPDATE_TIME_RE operates on the ORIGINAL date/time formatting
    (H:MM AM/PM), not the AM/PM-spaced form normalize_time_and_misc
    produces."""
    text = _NEXT_UPDATE_TIME_RE.sub("", text)
    text = _SEE_MAP_RE.sub("", text)
    return text


_WORD_RE = re.compile(r"[a-z0-9']+")

# What fraction of a sentence's OWN words must already appear somewhere
# in the sentences kept SO FAR (cumulative, not just the immediately
# preceding one) to count as "this sentence is substantially restating
# already-said content" -- a real, verified pattern in KDOT's own
# description field: a human-written "Comment:" section often restates
# the auto-generated portion above it, but frequently RECOMBINES pieces
# from more than one earlier sentence with different punctuation/order
# ("KS 14, between X and Y." pulls its route from one earlier sentence
# and its boundary from another). A pairwise Jaccard-against-one-prior-
# sentence comparison misses this (verified live: it caught 2 of 3 near-
# duplicate clauses in a real event and left a third, fully-redundant
# "Comment: <route>, between <boundary>" restatement in place, just
# under threshold). Cumulative CONTAINMENT -- what share of THIS
# sentence's words were already used, regardless of which earlier
# sentence(s) supplied them -- correctly identifies that recombination
# as redundant while still measuring only real word overlap. A
# genuinely distinct fact almost always contributes at least one new,
# distinguishing word (a different milepost, a different width/lane
# count, a new noun like "detour" or "shoo-fly") that keeps it below
# threshold -- verified against real captured events for both the
# should-drop and the should-keep case. Deliberately NOT 1.0 (exact-
# match only, which is what this superseded and was proven insufficient).
_SENTENCE_CONTAINMENT_THRESHOLD = 0.80

# Below this many distinct words, a sentence is only dropped at FULL
# (1.0) containment -- not the fuzzy 0.80 threshold -- since with so
# few words, 80%-but-not-100% containment is too coarse a signal
# (one word is a quarter of a 4-word sentence) and could too easily
# drop a short, genuinely distinct addition ("Use caution." / "Right
# lane closed."). Full containment on a short sentence is still a very
# strong signal -- verified live on real KDOT events where the exact
# same short clause (e.g. "Width limit 12'0"." for a bridge's width
# restriction) is repeated verbatim in both the auto-generated and
# Comment portions of one description.
_MIN_WORDS_FOR_DEDUPE = 4


def _sentence_word_set(sentence):
    return set(_WORD_RE.findall(sentence.lower()))


# A plain `text.split(". ")` is not abbreviation-aware -- and this
# pipeline itself introduces exactly one abbreviation containing its
# own ". " sequence: normalize_route_notation turns "US 24" into
# "U.S. 24". Splitting naively on ". " breaks that single sentence
# into two fragments ("...and U.S" / "24 (Beloit)..."). This was
# caught live: when only ONE of two duplicated "U.S. 24 (Beloit)"
# occurrences got deduped away (its overlap score happened to fall on
# opposite sides of the threshold for each half), the surviving half
# was left dangling mid-word with no continuation -- corrupted,
# confusing audio, exactly what this module exists to prevent. Fixed
# by protecting "U.S." from the split (it is never legitimately
# sentence-final -- a route number always follows) rather than by
# tuning the similarity threshold, which wouldn't address the root
# cause and could recur with any other near-duplicate pair.
_US_PLACEHOLDER = "\x00US\x00"


def _split_into_sentences(text):
    protected = text.replace("U.S.", _US_PLACEHOLDER)
    sentences = [s for s in protected.split(". ") if s.strip()]
    return [s.replace(_US_PLACEHOLDER, "U.S.") for s in sentences]


def dedupe_similar_sentences(text):
    """Splits `text` into sentences (see _split_into_sentences -- NOT a
    bare ". " split, which is not abbreviation-aware) and drops any
    sentence whose words are, cumulatively, already covered by
    sentences kept so far -- a deterministic, explainable boilerplate-
    removal step (a plain set-overlap calculation on WORDS, not raw
    strings, and not a generative/paraphrase-detection model), aimed
    specifically at KDOT's own real, observed pattern of a "Comment:"
    section substantially restating the auto-generated portion of the
    same description, sometimes by recombining pieces of more than one
    earlier sentence, and sometimes repeating a short clause (e.g. a
    width restriction) verbatim. Comparing WORD SETS rather than raw
    sentence text deliberately also sidesteps a punctuation mismatch
    that broke an earlier, string-equality version of this check: the
    very last sentence in a description keeps its trailing period
    (nothing follows it to consume that period as a separator) while
    an identical EARLIER occurrence of the same clause does not (its
    period was consumed splitting it from the next sentence) -- byte-
    for-byte string comparison saw those as two different sentences;
    word-set comparison correctly sees them as the same words.

    The containment threshold is 1.0 (exact word-set match required)
    for short sentences and 0.80 for longer ones -- see
    _MIN_WORDS_FOR_DEDUPE and _SENTENCE_CONTAINMENT_THRESHOLD. Never
    removes a sentence that doesn't overlap strongly with what's
    already been said -- a Comment that adds genuinely new information
    (real events do this too, e.g. explaining a detour or citing
    "until further notice") is left untouched, since it necessarily
    contributes at least one word not already seen."""
    sentences = _split_into_sentences(text)
    kept = []
    seen_words = set()
    for sentence in sentences:
        words = _sentence_word_set(sentence)
        if words:
            containment = len(words & seen_words) / len(words)
            threshold = _SENTENCE_CONTAINMENT_THRESHOLD if len(words) >= _MIN_WORDS_FOR_DEDUPE else 1.0
            if containment >= threshold:
                continue
        kept.append(sentence)
        seen_words |= words
    return ". ".join(kept)


def normalize_time_and_misc(text):
    """24/7 -> twenty four seven; decimal point -> spoken "point";
    AM/PM spacing; collapses repeated whitespace. Applied last, after
    route/direction substitutions, since none of those introduce the
    patterns this step targets."""
    text = _TWENTY_FOUR_SEVEN_RE.sub("twenty four seven", text)
    text = _SLASH_BETWEEN_WORDS_RE.sub(" and ", text)
    text = _DECIMAL_POINT_RE.sub(" point ", text)
    text = _TIME_AMPM_RE.sub(lambda m: f"{m.group(1)} {m.group(2).upper()}M", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text


def normalize_for_speech(text):
    """The full pipeline, in order: strip internal admin text, route
    notation, direction abbreviations, time/misc cleanup, THEN near-
    duplicate sentence removal last -- deliberately after route/
    direction normalization rather than before, so both occurrences of
    e.g. "KS 14" / "Kansas Highway 14" are already in the SAME spoken
    form before their word-overlap is compared (maximizes detection
    accuracy; comparing "KS 14" against an unnormalized "Kansas Highway
    14" would under-count the overlap on the route-number word alone).
    Idempotent -- running it twice on already-normalized text is a
    no-op (no substitution pattern matches its own replacement text,
    and a text with no remaining near-duplicate sentences is unchanged
    by dedupe_similar_sentences)."""
    if not text:
        return ""
    text = strip_internal_admin_text(text)
    text = normalize_route_notation(text)
    text = normalize_direction_abbreviations(text)
    text = normalize_time_and_misc(text)
    text = dedupe_similar_sentences(text)
    return text
