"""Station-editable dedication/request spoken-text policy.

This module owns everything about turning a listener's raw
requester_name/dedication_message plus the requested Track's title/
artist into the final spoken script for a dedication or request intro:

  * the trusted placeholder allowlist and station-template validation
  * requester/message normalization (whitespace, control characters,
    Unicode form, terminal punctuation)
  * title/artist spoken normalization ("feat." -> "featuring")
  * the local, authoritative on-air length policy (independent of
    whatever the public site's own transport validation allows)
  * final template rendering

Kept out of the already-large webrequests/services.py, and deliberately
its own module rather than living in isadoraair/announcements: this is
Web Requests feature policy (wording, listener-input rules, station
configuration), not generic artifact-rendering mechanics. Dependency
direction stays one-way:

    webrequests dedication policy (this module)
            |
            v
    webrequests/services.py
            |
            v
    SpeechSpliceAnnouncement
            |
            v
    generic announcement renderer (isadoraair/announcements)
            |
            v
    shared StationTTSVoice

This module must NOT import isadoraair.announcements or anything else
from the generic renderer -- it only ever produces a plain string.
"""
from __future__ import annotations

import re
import string
import unicodedata

# ---------------------------------------------------------------------
# "feat." spoken normalization (unchanged semantics from pre-r0056
# webrequests/services.py::build_dedication_intro_text -- moved here
# verbatim, not altered. See docs/SPEECH_TEXT_NORMALIZATION_AUDIT.md's
# "Web Requests dedication speech" table).
# ---------------------------------------------------------------------
# "feat." (any case, with the period -- requiring it is what keeps this
# from also mangling "feat" used as an actual word, e.g. a title like
# "Incredible Feat") can be spoken as the rhyming word ("feet") rather
# than expanded to "featuring". Word-boundary on the left only, so it
# matches both "(feat. X)" and "feat. X" but never touches "featuring"
# itself (which the same \bfeat\. pattern can't match -- "feat" there
# is followed by "u", not a period).
_FEATURED_ARTIST_ABBREV_RE = re.compile(r"\bfeat\.", re.IGNORECASE)


def expand_featuring(value: str) -> str:
    """"feat." -> "featuring" in a spoken title/artist value. Does not
    touch Track metadata -- callers pass this module a copy of the
    string, never the model field itself."""
    return _FEATURED_ARTIST_ABBREV_RE.sub("featuring", value or "")


# ---------------------------------------------------------------------
# Trusted substitution allowlist + template validation
# ---------------------------------------------------------------------
ALLOWED_PLACEHOLDERS = frozenset({"title", "artist", "requester_name", "dedication_message"})

# Matches WebRequestConfig's template CharField max_length -- keep the
# two in sync; both exist so an excessively long station template is
# rejected at both the Admin/DB layer and here at runtime.
TEMPLATE_MAX_LENGTH = 512

# Conservative, documented ceiling on the FINAL rendered script handed
# to Speech Splice synthesis -- guards against unexpectedly huge Track
# title/artist metadata bypassing the listener-message limit below.
# This is short spoken material (a few seconds ahead of the requested
# song), not a code-owned knob a station would reasonably need to
# raise, so it is a constant rather than a WebRequestConfig field.
MAX_RENDERED_SCRIPT_LENGTH = 600

# The listener-message limit IS station-editable (WebRequestConfig.
# dedication_message_spoken_limit) -- these are its field-level bounds,
# also used as this module's fallback if ever called without a cfg
# value at hand.
DEFAULT_DEDICATION_MESSAGE_SPOKEN_LIMIT = 300
MIN_DEDICATION_MESSAGE_SPOKEN_LIMIT = 50
MAX_DEDICATION_MESSAGE_SPOKEN_LIMIT = 1000


class DedicationTextPolicyError(RuntimeError):
    """A dedication/request script could not be safely rendered.
    Always a DETERMINISTIC condition -- the same request/config
    combination will fail again identically until an operator or the
    listener changes something. Callers must treat this exactly like
    "no intro this time": the requested song's own scheduling and
    fulfillment must never be affected, and because the check that
    raises this happens before any shared-TTS/ffmpeg work begins,
    retrying it every command cycle costs essentially nothing."""


class DedicationTemplateError(DedicationTextPolicyError):
    """A station-edited template itself is unsafe or invalid: an
    unknown placeholder, disallowed format/conversion/attribute/index
    syntax, malformed braces, or an oversized template. Raised by both
    Admin-time validation (so a bad template is rejected before save)
    and runtime rendering (so stale data written outside Admin --
    migrations, shell edits, fixtures -- still fails safely instead of
    being trusted)."""


class DedicationInputPolicyError(DedicationTextPolicyError):
    """Listener input or track metadata is individually well-formed but
    produces a script that violates the local on-air length policy.
    Never truncated -- the intro is simply not generated this time."""


def validate_template(template: str) -> None:
    """Raises DedicationTemplateError unless `template` is safe to
    render with str.format() against ALLOWED_PLACEHOLDERS only.

    Uses stdlib string.Formatter().parse() -- deterministic structural
    parsing, not regex guessing -- to enumerate every replacement
    field in the template and reject anything beyond a bare {name}
    reference to one of the four trusted keys: unknown placeholders,
    attribute access ({track.title}), index access ({foo[0]}),
    conversion syntax ({title!r}), format specs ({title:>20}), and
    malformed braces. Substituted VALUES are never reparsed as
    templates -- str.format() only interprets the template argument,
    so braces inside a listener's own text remain ordinary data."""
    if not isinstance(template, str) or not template.strip():
        raise DedicationTemplateError("template must be a non-empty string")
    if len(template) > TEMPLATE_MAX_LENGTH:
        raise DedicationTemplateError(
            f"template exceeds the {TEMPLATE_MAX_LENGTH}-character limit"
        )

    try:
        fields = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise DedicationTemplateError(f"malformed template braces: {exc}") from exc

    for _literal_text, field_name, format_spec, conversion in fields:
        if field_name is None:
            continue  # trailing literal text after the last replacement field
        if conversion is not None:
            raise DedicationTemplateError(
                f"conversion syntax is not allowed: {{{field_name}!{conversion}}}"
            )
        if format_spec:
            raise DedicationTemplateError(
                f"format specs are not allowed: {{{field_name}:{format_spec}}}"
            )
        if "." in field_name or "[" in field_name or "]" in field_name:
            raise DedicationTemplateError(
                f"attribute/index access is not allowed: {{{field_name}}}"
            )
        if field_name == "" or field_name not in ALLOWED_PLACEHOLDERS:
            raise DedicationTemplateError(f"unknown placeholder: {{{field_name}}}")


# ---------------------------------------------------------------------
# Requester / message normalization
# ---------------------------------------------------------------------
def _is_unsafe_control_character(ch: str) -> bool:
    """A control character (Unicode general category "Cc" -- covers
    both the C0 range \\x00-\\x1f/\\x7f AND the C1 range \\x80-\\x9f in
    one check, rather than two hand-maintained regex ranges that can
    silently miss one of them) that Python does NOT also consider
    whitespace. Deliberately category-based rather than a fixed regex
    range list: a small number of C0/C1 control characters (e.g.
    \\x1c-\\x1f "separator" controls, and \\x85 NEL in the C1 range)
    are BOTH "Cc" and whitespace per str.isspace() -- those must be
    left alone here and collapsed to a plain space below via
    str.split()/" ".join(), the same as an ordinary tab or newline, or
    two words on either side of one would be silently concatenated
    with no space at all. Only a control character that is not
    whitespace at all (e.g. a stray \\x00, ESC, or a C1 control like
    \\x90) is eliminated outright."""
    return unicodedata.category(ch) == "Cc" and not ch.isspace()


def _strip_unsafe_control_characters(value: str) -> str:
    return "".join(ch for ch in value if not _is_unsafe_control_character(ch))


def _clean_text(raw, *, field_label: str) -> str:
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise DedicationInputPolicyError(f"{field_label} must be a string")
    value = unicodedata.normalize("NFC", raw)  # conservative: preserves accents/punctuation
    value = _strip_unsafe_control_characters(value)
    return " ".join(value.split())  # collapse all whitespace (incl. newlines/tabs/NEL), strip ends


def normalize_requester_name(raw) -> str:
    """Trim/collapse whitespace, drop unsafe control characters,
    Unicode-normalize conservatively. Preserves ordinary punctuation,
    apostrophes, accents, and names -- no profanity/content
    moderation, no silent rewriting of ordinary words. Length itself
    remains bounded upstream by the model's own 100-character
    CharField, same as the public site's transport limit."""
    return _clean_text(raw, field_label="requester name")


def normalize_dedication_message(raw) -> str:
    """Same whitespace/control-character/Unicode policy as
    normalize_requester_name, plus: if the result is non-empty and
    lacks terminal punctuation (./!/?), append a period so the
    template substitution below never needs its own trailing
    punctuation logic. Existing terminal punctuation is never
    doubled."""
    value = _clean_text(raw, field_label="dedication message")
    if value and not value.endswith((".", "!", "?")):
        value += "."
    return value


# ---------------------------------------------------------------------
# Final rendering
# ---------------------------------------------------------------------
def render_dedication_script(
    *,
    title,
    artist,
    requester_name,
    dedication_message,
    named_message_template: str,
    named_request_template: str,
    anonymous_message_template: str,
    anonymous_request_template: str,
    dedication_message_spoken_limit: int = DEFAULT_DEDICATION_MESSAGE_SPOKEN_LIMIT,
) -> str:
    """Normalizes listener input plus title/artist, picks exactly one
    of the four explicit station templates based on whether a
    (normalized) requester name and/or dedication message is present,
    validates that template, and renders the final script.

    Raises DedicationTemplateError if the selected template is unsafe/
    invalid, or DedicationInputPolicyError if the normalized message or
    the final rendered script exceeds the local on-air length policy.
    Never truncates. Both exceptions are DedicationTextPolicyError --
    callers must treat either as "no intro this time", not as a reason
    to touch the requested song's own scheduling/fulfillment."""
    spoken_title = expand_featuring(str(title or ""))
    spoken_artist = expand_featuring(str(artist or ""))
    name = normalize_requester_name(requester_name)
    message = normalize_dedication_message(dedication_message)

    if len(message) > dedication_message_spoken_limit:
        raise DedicationInputPolicyError(
            f"dedication message ({len(message)} normalized characters) exceeds "
            f"the {dedication_message_spoken_limit}-character on-air spoken limit"
        )

    if name and message:
        template = named_message_template
    elif name:
        template = named_request_template
    elif message:
        template = anonymous_message_template
    else:
        template = anonymous_request_template

    validate_template(template)

    values = {
        "title": spoken_title,
        "artist": spoken_artist,
        "requester_name": name,
        "dedication_message": message,
    }
    try:
        rendered = template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        # Unreachable given validate_template's guarantees above --
        # defensive only (e.g. a race between validation and use).
        raise DedicationTemplateError(f"template could not be rendered: {exc}") from exc

    if len(rendered) > MAX_RENDERED_SCRIPT_LENGTH:
        raise DedicationInputPolicyError(
            f"rendered dedication script ({len(rendered)} characters) exceeds "
            f"the {MAX_RENDERED_SCRIPT_LENGTH}-character on-air script limit"
        )
    return rendered
