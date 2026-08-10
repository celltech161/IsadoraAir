"""Candidate / active / last-known-good (LKG) configuration state --
Phase 2B/2C of the encoder hardening effort.

Three distinct concepts, deliberately never overloaded onto one file
the way the pre-Phase-2 pipeline used a single `.liq` path for
everything:

  candidate   A rendered script for a NOT-YET-QUALIFIED configuration.
              Lives under CANDIDATE_DIR (tmpfs, 0700), never touches
              the live/LKG paths. Only encoder_manager.py's
              _start_group ever Popen()s a candidate, and only after
              validation (encoders/services/validation.py) and static
              preflight (encoders/services/preflight.py) both pass.

  active      The script actually Popen()'d for the CURRENT running
              child -- unchanged from the pre-Phase-2 layout
              (SCRIPT_DIR/encoders_<slug>.liq, see encoder_manager.py).
              This module doesn't touch that path at all; it's the
              manager's own concern.

  LKG         The exact script text that last passed live health
              qualification, persisted OUTSIDE /run (which is tmpfs
              and does not survive reboot) so it remains meaningful
              after a normal restart. This is what rollback restores.

Threat model / why the LKG script contains credentials (documented
here explicitly, not left implicit): storing only Encoder row IDs and
re-fetching credentials from the DB at rollback time was considered
and rejected -- the DB is exactly what may have just gone bad (a
rejected candidate's configuration lives in the DB too, that's the
whole scenario rollback exists for). The LKG must be self-contained
and independent of current DB state to be trustworthy as a fallback,
which means it necessarily duplicates the password that already lives
in Encoder.password. This is mitigated with filesystem permissions
(0700 directory, 0600 files, owned by the service user -- the same
user isadoraair-encoders and isadoraair-monitoring already both run
as), not by trying to avoid the duplication. The separate, non-secret
`.json` metadata sidecar (0644) is what admin/monitoring code should
read for display -- it never contains a password."""
import hashlib
import json
import os
import time
from pathlib import Path

from django.conf import settings

from encoders.models import RUNTIME_AFFECTING_FIELDS

# tmpfs, matching encoder_manager.py's own SCRIPT_DIR -- candidates are
# exactly as ephemeral as the active script, just structurally kept out
# of the path that actually gets Popen()'d until proven.
CANDIDATE_DIR = Path("/run/isadoraair/liquidsoap/candidate")

# Persistent -- see ENCODER_STATE_ROOT's own settings.py docstring.
LKG_DIR = Path(settings.ENCODER_STATE_ROOT) / "lkg"

_DIR_MODE = 0o700
_SECRET_FILE_MODE = 0o600
_META_FILE_MODE = 0o644


def _ensure_dir(path, mode):
    """mkdir -p, then an explicit chmod -- mkdir's own mode= parameter
    is filtered through the process umask, so a permissive umask on
    some box could silently leave a directory more open than intended.
    The explicit chmod afterward is what actually guarantees the mode,
    same "don't trust the implicit path" spirit as _atomic_write_json's
    own flush+fsync+os.replace() in encoder_manager.py."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def compute_fingerprint(input_device, encoders):
    """Deterministic SHA-256 hex digest over the runtime-affecting
    configuration of `encoders` (an iterable of Encoder rows, already
    filtered to the ones sharing `input_device`) -- a one-way hash, so
    it's safe to store in the non-secret metadata sidecar even though
    the underlying fields (via RUNTIME_AFFECTING_FIELDS) include
    `password`: a changed password IS a different configuration that
    must re-qualify, but the digest itself never leaks it.

    Uses RUNTIME_AFFECTING_FIELDS (encoders/models.py) as the exact
    field set -- the SAME fields that already determine "does this
    change need a restart" (encoders/admin.py) also determine "is this
    a different configuration," which is the correct relationship:
    nothing outside that set can affect the rendered script at all.
    `enabled` is excluded (fingerprints are only ever computed over an
    already-filtered enabled set, so it would always be a constant
    True and add nothing). Explicitly excludes -- by construction, not
    by a separate exclusion list -- every volatile runtime field
    (generation, pid, timestamps, listener counts): none of those are
    in RUNTIME_AFFECTING_FIELDS, so they can never leak into the
    fingerprint no matter what future fields get added to Encoder.

    Rows are sorted by their full canonical representation (every
    RUNTIME_AFFECTING_FIELDS value, in the same fixed field order)
    before hashing, NOT just (host, port, mount) -- that partial key
    (an earlier draft) is not fully canonical: two rows can tie on
    host+port+mount while differing in protocol, format, or any other
    runtime field, in which case their relative order would fall back
    to whatever order `encoders` happened to arrive in (a DB queryset's
    order is not guaranteed stable across queries). Two runs over the
    identical set of rows could then land the tied pair in a different
    order and produce two DIFFERENT fingerprints for the SAME effective
    configuration -- exactly what this function exists to prevent.
    Sorting by the full row instead means two rows only ever tie when
    they're genuinely identical, so the result no longer depends on
    input order at all. Every value is stringified for the sort key
    only (not for hashing) so the comparison itself can never raise on
    mixed types (e.g. a None next to an int)."""
    fields = sorted(RUNTIME_AFFECTING_FIELDS - {"enabled"})
    rows = [
        {f: getattr(enc, f) for f in fields}
        for enc in encoders
    ]
    rows.sort(key=lambda d: tuple(str(d.get(f)) for f in fields))
    payload = {"input_device": input_device, "encoders": rows}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


# --- Candidate -----------------------------------------------------------

def write_candidate(slug, script_text):
    """Write a new candidate script to a fresh, private path under
    CANDIDATE_DIR. Returns the Path. Never touches the active or LKG
    paths. Directory 0700, file 0600 -- the script contains plaintext
    credentials (identical to what the active script already contains
    on tmpfs), so it gets the same protection.

    Collision-safe naming: <slug>_<hex-suffix-of-current-time-ns>.liq
    -- unique per call without needing a caller-supplied id, so two
    back-to-back candidates for the same slug (e.g. an operator saving
    two quick edits) never collide on the filesystem even if both are
    written before either is cleaned up."""
    _ensure_dir(CANDIDATE_DIR, _DIR_MODE)
    suffix = format(time.time_ns(), "x")
    path = CANDIDATE_DIR / f"{slug}_{suffix}.liq"
    path.write_text(script_text, encoding="utf-8")
    os.chmod(path, _SECRET_FILE_MODE)
    return path


def cleanup_candidate(path):
    """Best-effort removal of a candidate script -- called once a
    candidate has been promoted (its content is now the LKG, the
    temporary file itself is no longer needed) or rejected (the
    running config never changed, nothing left to reference it)."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# --- Last-known-good -------------------------------------------------------

def _lkg_script_path(slug):
    return LKG_DIR / f"{slug}.liq"


def _lkg_meta_path(slug):
    return LKG_DIR / f"{slug}.json"


def read_lkg(slug):
    """Returns (script_text, meta_dict) for the persisted LKG of this
    slug, or (None, None) if no LKG exists yet (fresh install / this
    group has never qualified). Tolerates a missing or corrupt meta
    file distinctly from a missing script -- a script with no readable
    metadata is still usable for a rollback launch (the metadata is
    informational, not required to relaunch), but is reported as such
    so callers can decide whether to trust it for a fingerprint
    comparison."""
    script_path = _lkg_script_path(slug)
    if not script_path.is_file():
        return None, None
    try:
        script_text = script_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    meta = None
    meta_path = _lkg_meta_path(slug)
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None
    return script_text, meta


def read_lkg_meta(slug):
    """Metadata only (no script read) -- for admin/monitoring display,
    where the actual script content (credentials included) should
    never be needed. Returns None if no LKG/metadata exists."""
    _, meta = read_lkg(slug)
    return meta


def write_lkg(slug, script_text, meta):
    """Persist a newly-qualified configuration as this slug's LKG,
    atomically enough for this use case: the script is written first
    (via a temp-then-replace within the SAME directory, so a reader
    mid-write can never see a truncated script), then the metadata.
    A crash between the two leaves, at worst, a new script with stale
    metadata -- read_lkg tolerates that (meta is optional) and the
    NEXT successful promotion overwrites both together. `meta` must
    NOT contain a password -- see this module's own docstring; nothing
    here enforces that structurally, callers (encoder_manager.py's
    promotion path) are responsible for only passing non-secret
    fields, matching the same trust boundary as every other event/
    state writer in this project (e.g. monitoring.models.emit_event's
    own "detail should be non-secret" contract)."""
    _ensure_dir(LKG_DIR, _DIR_MODE)

    script_path = _lkg_script_path(slug)
    tmp_script = script_path.with_suffix(".liq.tmp")
    with open(tmp_script, "w", encoding="utf-8") as f:
        f.write(script_text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_script, _SECRET_FILE_MODE)
    os.replace(tmp_script, script_path)

    meta_path = _lkg_meta_path(slug)
    tmp_meta = meta_path.with_suffix(".json.tmp")
    with open(tmp_meta, "w", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False, sort_keys=True))
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_meta, _META_FILE_MODE)
    os.replace(tmp_meta, meta_path)


def lkg_exists(slug):
    return _lkg_script_path(slug).is_file()
