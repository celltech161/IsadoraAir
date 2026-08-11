r"""Centralized Encoder configuration validation -- Phase 2A of the
encoder hardening effort.

Why this exists as a SEPARATE layer from Encoder.clean(): Django's
Model.clean() only runs through ModelForm/full_clean() -- a direct ORM
write (Encoder.objects.create(...), a fixture load, a data migration, a
management-shell one-liner) bypasses it entirely. This module is the
single place every one of those paths, plus the admin, plus the
candidate-rendering pipeline in encoders/services/encoder_manager.py,
can call to get the SAME answer. Encoder.clean() now delegates to
validate_single_encoder() below rather than duplicating logic (see
encoders/models.py) -- it isn't being replaced, just no longer allowed
to silently diverge from the one real implementation.

Three levels, matching Phase 2A's own framing:
  validate_single_encoder(encoder)      -- one row in isolation
  validate_group(encoders)              -- cross-row constraints over a set
  validate_full_configuration(encoders) -- both of the above together,
                                            the actual pre-script-generation
                                            gate encoder_manager.py calls

Every validator returns a plain list[str] of human-readable error
messages (empty list = valid) rather than raising, so callers can
choose whether to raise (ConfigValidationError, for the candidate
pipeline) or attach to Django form fields (the admin, via clean())
without this module caring which.

Liquidsoap injection note (found empirically while building this, not
assumed from documentation): Liquidsoap's double-quoted string literals
support `#{expr}` interpolation -- confirmed live via `liquidsoap
interp_test.liq` with `y = "hello #{x} world"` actually evaluating `x`.
There is no backslash escape for it (`\#` is a parse error, not an
escape -- also confirmed live), so encoder_manager.py's own
_liq_string() cannot neutralize a value containing literal `#{` the way
it neutralizes `\` and `"`. This validator rejects any field value
containing `#{` outright (see _REJECTED_SUBSTRINGS) -- the only correct
fix given Liquidsoap has no escape sequence for it. _liq_string() itself
additionally now refuses to embed `#{` as a structural second layer (see
encoder_manager.py), independent of whether a row somehow reached script
generation without going through this module."""
import re

from .. import models as encoders_models

# --- Shoutcast Stream ID -----------------------------------------------

# Optional single leading slash, then one or more ASCII digits, nothing
# else -- deliberately strict. Rejects blank, multiple slashes, embedded
# whitespace, signs, decimal points, and any non-digit "garbage" in one
# pattern rather than a series of ad-hoc string checks. \Z, not $ --
# Python's $ matches immediately before a trailing newline as well as
# true end-of-string, so "4\n" would otherwise WRONGLY match (confirmed
# by this module's own test suite catching it). \Z only ever matches
# the absolute end of the string.
_SID_PATTERN = re.compile(r"\A/?[0-9]+\Z")


def normalize_shoutcast_sid(raw):
    """Parse a Shoutcast 2 Stream ID form value ("4", "/4") into a
    positive int. Raises ConfigValidationError with a human-readable
    reason for anything else -- blank, "0", negative, non-numeric,
    multiple slashes, or a Liquidsoap/source-code-looking fragment.

    Returns an int, never a string -- the generated script's `icy_id=`
    argument must come from this parsed integer, never raw DB text
    (see _output_block in encoder_manager.py), so there is no code path
    left where an unparsed SID string could reach the script.

    Only plain ASCII spaces are trimmed, not `.strip()`'s full
    whitespace set -- a trailing/leading control character (a stray
    "\\n" from a pasted value, say) must be REJECTED, not silently
    cleaned away and let through as if it were never there."""
    text = (raw or "").strip(" ")
    if not text:
        raise ConfigValidationError("Shoutcast Stream ID is required for Shoutcast 2.")
    if not _SID_PATTERN.match(text):
        raise ConfigValidationError(
            f"Shoutcast Stream ID {raw!r} is not valid -- expected a positive "
            "integer, optionally with a single leading slash (e.g. \"4\" or \"/4\")."
        )
    value = int(text.lstrip("/"))
    if value <= 0:
        raise ConfigValidationError("Shoutcast Stream ID must be a positive integer (not 0).")
    return value


class ConfigValidationError(Exception):
    """Raised by normalize_shoutcast_sid and available for any caller
    that wants an exception instead of a list-of-strings return. Plain
    Exception, not django.core.exceptions.ValidationError -- this
    module is imported from encoder_manager.py, a standalone process
    entry point that has no reason to depend on Django's forms/admin
    error plumbing."""


# --- MP3 rate mode -------------------------------------------------------

def effective_mp3_rate_mode(encoder):
    """Resolves Encoder.mp3_rate_mode ("auto"/"cbr"/"abr") to the actual
    "cbr"/"abr" that will be rendered -- the ONE place this resolution
    rule lives, shared by encoder_manager.py's _format_block (so the
    renderer never disagrees with what validation just approved) and by
    validate_provider_policy below (so e.g. "Live365 requires MP3 to be
    CBR" is checked against what will ACTUALLY be rendered, not just
    the raw mp3_rate_mode field value).

    "auto" reproduces this project's original, pre-3.10 behavior EXACTLY
    (encoder_manager.py's _format_block, before mp3_rate_mode existed):
    bitrate < 192 kbps -> abr, >= 192 kbps -> cbr. "cbr"/"abr" always
    return themselves regardless of bitrate -- an explicit operator
    choice, not a default to second-guess.

    Not meaningful for a non-MP3 encoder (AAC/Vorbis have no CBR/ABR
    concept here) -- callers only ever call this for format=="mp3"."""
    mode = getattr(encoder, "mp3_rate_mode", "auto") or "auto"
    if mode in ("cbr", "abr"):
        return mode
    return "cbr" if encoder.bitrate_kbps >= 192 else "abr"


# --- Protocol / format compatibility ------------------------------------

# Verified against this project's actual targeted Liquidsoap (2.4.0+dev,
# confirmed live via `liquidsoap --version`) and real-world Shoutcast/
# Icecast protocol semantics, not just Liquidsoap's own capabilities --
# Liquidsoap's output.shoutcast operator will happily attempt %vorbis,
# but no real Shoutcast 1 or 2 server has ever accepted an Ogg/Vorbis
# stream (Shoutcast is fundamentally an ICY/MP3-lineage protocol; AAC
# was added in v2, Vorbis never was). The AAC+Shoutcast1 rule already
# existed in Encoder.clean() before this pass; the Vorbis+Shoutcast
# rules are new hardening -- confirmed against live production data
# before adding them (both real Encoder rows on this box are
# shoutcast2/mp3 and shoutcast2/aac; neither uses vorbis, so this adds
# no regression).
_FORMAT_PROTOCOL_MATRIX = {
    "mp3": {"icecast", "shoutcast1", "shoutcast2"},
    "aac": {"icecast", "shoutcast2"},
    "vorbis": {"icecast"},
}


def validate_protocol_format(protocol, fmt):
    """Returns a list[str] (empty if compatible)."""
    allowed = _FORMAT_PROTOCOL_MATRIX.get(fmt)
    if allowed is None:
        return [f"Unknown format {fmt!r}."]
    if protocol not in allowed:
        fmt_label = dict(encoders_models.Encoder.FORMAT_CHOICES).get(fmt, fmt)
        proto_label = dict(encoders_models.Encoder.PROTOCOL_CHOICES).get(protocol, protocol)
        return [f"{fmt_label} is not supported over {proto_label}."]
    return []


# --- Connection field / injection safety --------------------------------

# Every C0 control character (0x00-0x1F) plus DEL (0x7F) -- NUL, CR, LF,
# and "any other unsafe control character" in one sweep rather than a
# per-character allowlist. Tab is included in this range (0x09) and
# deliberately NOT special-cased back in: every field this validates is
# a single-line value embedded in a one-line Liquidsoap argument list,
# where a literal tab has no legitimate use and only adds ambiguity.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

# Liquidsoap string interpolation trigger -- see this module's own
# docstring for the empirical verification. No accepted field may
# contain this substring; there is no way to escape it (`_liq_string()`
# only escapes `\` and `"`, and Liquidsoap has no `\#` escape), so
# rejection is the only correct response.
_INTERPOLATION_TRIGGER = "#{"

_URL_SCHEME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def _check_unsafe_text(value, field_label):
    """Shared control-character + interpolation-trigger guard for every
    string field that reaches _liq_string() in encoder_manager.py --
    host, password, station_name, genre, url, mount, username. Returns
    a list[str]."""
    errors = []
    if _CONTROL_CHAR_PATTERN.search(value or ""):
        errors.append(f"{field_label} contains a control character (NUL/CR/LF/other), which is not allowed.")
    if _INTERPOLATION_TRIGGER in (value or ""):
        errors.append(f"{field_label} may not contain \"#{{\" (Liquidsoap string interpolation syntax).")
    return errors


def validate_connection_fields(encoder):
    """Host/port/mount/username/station_name/password checks. Returns
    list[str]. Does not touch protocol/format compatibility (see
    validate_protocol_format) or the Shoutcast SID (see
    normalize_shoutcast_sid) -- those are validated separately by
    validate_single_encoder so each concern has one clear owner."""
    errors = []

    host = encoder.host or ""
    errors += _check_unsafe_text(host, "Host")
    if not host.strip():
        errors.append("Host is required.")
    else:
        if _URL_SCHEME_PATTERN.match(host):
            errors.append("Host must be a hostname or IP address, not a URL (remove the http:// or https:// prefix).")
        if "@" in host:
            errors.append("Host must not contain embedded credentials (an \"@\").")
        if "/" in host or "?" in host or "#" in host:
            errors.append("Host must not contain a path, query string, or fragment.")
        if re.search(r"\s", host):
            errors.append("Host must not contain whitespace.")

    port = encoder.port
    if port is None or not (1 <= int(port) <= 65535):
        errors.append("Port must be between 1 and 65535.")

    if encoder.protocol == "icecast":
        mount = encoder.mount or ""
        errors += _check_unsafe_text(mount, "Mount")
        if not mount.startswith("/"):
            errors.append("Icecast mount must begin with \"/\".")
        elif mount == "/":
            errors.append("Icecast mount must not be just \"/\".")
        username = encoder.username or ""
        errors += _check_unsafe_text(username, "Username")
        if not username.strip():
            errors.append("Icecast source username is required.")
    elif encoder.protocol == "shoutcast2":
        mount = encoder.mount or ""
        errors += _check_unsafe_text(mount, "Shoutcast Stream ID")
        try:
            normalize_shoutcast_sid(mount)
        except ConfigValidationError as exc:
            errors.append(str(exc))

    station_name = encoder.station_name or ""
    errors += _check_unsafe_text(station_name, "Station name")
    if encoder.protocol in ("shoutcast1", "shoutcast2") and not station_name.strip():
        # Shoutcast-specific, matching a real documented production
        # incident (see encoder_manager.py's own module docstring): a
        # live Shoutcast 2 test server rejected the connection outright
        # with "Bad icy header string [icy-name:]" the moment `name=`
        # was empty. Icecast's own `name=` (same field, same _output_
        # block "common" params) has no equivalent documented failure
        # and the model field is blank=True, so it stays optional there
        # -- not broadened past what's actually been shown to matter.
        errors.append("Station name is required for Shoutcast 1/2 (a blank icy-name is rejected by the server).")

    password = encoder.password or ""
    errors += _check_unsafe_text(password, "Password")
    if not password.strip():
        errors.append("Password is required.")

    for field_name, label in (("genre", "Genre"), ("url", "URL")):
        errors += _check_unsafe_text(getattr(encoder, field_name, "") or "", label)

    return errors


# --- Provider policy (roadmap 3.10) --------------------------------------
#
# Provider != Protocol. Live365 and Radio.co are NOT additional
# `protocol` values -- Live365 is fundamentally an Icecast source
# destination, Radio.co is fundamentally a Shoutcast-1-style source
# destination. This section only ever NARROWS what validate_protocol_
# format/validate_connection_fields above already accept for an
# already-generic-valid row -- it never substitutes for them, and a
# "generic" provider row is completely unaffected (every check below is
# a no-op for provider=="generic", preserving 100% of pre-3.10
# behavior). Layered on, called from validate_single_encoder alongside
# the existing checks, so a direct ORM write (bypassing Encoder.clean())
# is caught here exactly like every other rule in this module.

_LIVE365_ALLOWED_FORMATS = {"mp3", "aac"}


def validate_provider_policy(encoder):
    """Returns list[str] (empty if compatible). Dispatches on
    Encoder.provider; unknown values (shouldn't happen -- it's a
    choices field -- but a stale in-memory instance or a direct ORM
    write with a stray string is possible) are reported rather than
    silently treated as generic."""
    provider = getattr(encoder, "provider", "generic") or "generic"
    if provider == "generic":
        return []
    if provider == "live365":
        return _validate_live365(encoder)
    if provider == "radio_co":
        return _validate_radio_co(encoder)
    return [f"Unknown provider {provider!r}."]


def _format_label(fmt):
    return dict(encoders_models.Encoder.FORMAT_CHOICES).get(fmt, fmt)


def _protocol_label(protocol):
    return dict(encoders_models.Encoder.PROTOCOL_CHOICES).get(protocol, protocol)


def _validate_live365(encoder):
    """Live365 = an Icecast source destination, MP3 or AAC only, MP3
    must be effective CBR (Live365's own current guidance for constant-
    bitrate MP3 source audio). Host/port/mount/username/password
    "required" checks are intentionally NOT duplicated here --
    protocol=="icecast" already makes validate_connection_fields
    enforce all of those (with its own, already-clear wording); Live365
    just needs the row to actually BE an Icecast row for those checks
    to apply at all, which is exactly what the protocol check below
    guarantees."""
    errors = []
    if encoder.protocol != "icecast":
        errors.append(
            f"Live365 requires Icecast as the connection protocol (this row is set to "
            f"{_protocol_label(encoder.protocol)} -- Live365 is fundamentally an Icecast "
            f"source destination)."
        )
    if encoder.format not in _LIVE365_ALLOWED_FORMATS:
        errors.append(
            f"Live365 does not support {_format_label(encoder.format)} -- only MP3 and AAC "
            f"are supported for the Live365 provider preset."
        )
    elif encoder.format == "mp3" and effective_mp3_rate_mode(encoder) != "cbr":
        errors.append(
            "Live365 requires MP3 to use CBR (set MP3 Rate Mode to CBR, or leave it on Auto "
            "with a bitrate of 192 kbps or higher)."
        )
    return errors


def _validate_radio_co(encoder):
    """Radio.co = a Shoutcast-1-style external-broadcaster source
    connection, MP3 only, always effective CBR. No mount/username
    requirement -- Shoutcast 1 has no mount concept and
    validate_connection_fields already never requires either for it,
    matching Radio.co's own documented external-broadcaster fields
    (host/port/password only)."""
    errors = []
    if encoder.protocol != "shoutcast1":
        errors.append(
            f"Radio.co requires Shoutcast 1 as the connection protocol (this row is set to "
            f"{_protocol_label(encoder.protocol)} -- Radio.co is fundamentally a Shoutcast-1-"
            f"style source destination)."
        )
    if encoder.format != "mp3":
        errors.append(
            f"Radio.co only supports MP3 for external broadcaster source connections -- "
            f"{_format_label(encoder.format)} is not supported."
        )
    elif effective_mp3_rate_mode(encoder) != "cbr":
        errors.append(
            "Radio.co requires MP3 to use CBR (set MP3 Rate Mode to CBR, or leave it on Auto "
            "with a bitrate of 192 kbps or higher)."
        )
    return errors


def validate_single_encoder(encoder):
    """Full single-row validation: protocol/format compatibility +
    connection fields + provider policy. Returns list[str] (empty =
    valid). This is what Encoder.clean() delegates to (see
    encoders/models.py) and what encoder_manager.py calls per-row
    before ever including a row in a candidate script."""
    errors = list(validate_protocol_format(encoder.protocol, encoder.format))
    errors += validate_connection_fields(encoder)
    errors += validate_provider_policy(encoder)
    return errors


# --- Duplicate destination / group-level validation ---------------------

def normalized_destination_key(encoder):
    """A tuple identifying the real destination this Encoder row would
    connect to, scoped per protocol family since each protocol
    addresses "which stream on this server" differently:
      icecast:    (host, port, mount)      -- Icecast's own addressing
      shoutcast1: (host, port)             -- single-stream, no SID concept
      shoutcast2: (host, port, sid)        -- Shoutcast 2 multiplexes by SID

    Two shoutcast2 rows sharing a host+port are NOT a collision if their
    SIDs differ (that's the normal multi-stream case) -- only an exact
    match on the full tuple is a real duplicate. Returns None if the row
    can't be normalized at all (e.g. an invalid SID) -- callers should
    validate the row on its own first; duplicate-checking a row that's
    already invalid for other reasons isn't meaningful."""
    host = (encoder.host or "").strip().lower()
    port = encoder.port
    if encoder.protocol == "icecast":
        mount = (encoder.mount or "").rstrip("/") or "/"
        return ("icecast", host, port, mount)
    if encoder.protocol == "shoutcast1":
        return ("shoutcast1", host, port)
    if encoder.protocol == "shoutcast2":
        try:
            sid = normalize_shoutcast_sid(encoder.mount)
        except ConfigValidationError:
            return None
        return ("shoutcast2", host, port, sid)
    return None


def validate_group(encoders):
    """Cross-row checks over a candidate enabled set. Currently: duplicate
    normalized destinations. Returns list[str] naming the conflicting
    rows by name/id so the operator can find them.

    Telnet/aircheck single-owner is deliberately NOT checked here --
    it's controlled entirely by input_device == DEFAULT_INPUT_DEVICE
    (a constant match in encoder_manager.py's _start_group), not by any
    Encoder-row field, so two groups can't structurally both claim it
    regardless of what's in the DB. Nothing to validate."""
    errors = []
    by_key = {}
    for enc in encoders:
        key = normalized_destination_key(enc)
        if key is None:
            continue  # invalid row -- already reported by validate_single_encoder
        by_key.setdefault(key, []).append(enc)
    for key, rows in by_key.items():
        if len(rows) > 1:
            names = ", ".join(f"{r.name!r} (id={r.id})" for r in rows)
            errors.append(f"Duplicate destination {key[:2]} mount/SID={key[2:]!r}: {names} all target the same stream.")
    return errors


def validate_full_configuration(encoders=None):
    """The pre-script-generation gate: every row's own validation plus
    group-level duplicate checks over the complete enabled set.
    Defaults to Encoder.objects.filter(enabled=True) if encoders isn't
    passed explicitly (candidate-pipeline callers usually pass the
    exact list they're about to render, so both entry points exist).
    Returns list[str]."""
    if encoders is None:
        encoders = list(encoders_models.Encoder.objects.filter(enabled=True))
    else:
        encoders = list(encoders)
    errors = []
    for enc in encoders:
        row_errors = validate_single_encoder(enc)
        errors += [f"[{enc.name or f'id={enc.id}'}] {e}" for e in row_errors]
    errors += validate_group(encoders)
    return errors
