"""Candidate / active / last-known-good (LKG) configuration state --
Phase 2B/2C of the encoder hardening effort, hardened further in the
2026-08-10 review-fix pass (crash-consistent versioned LKG storage,
script<->metadata integrity binding, shared desired-vs-accepted
destination resolution, exception-safe filesystem boundaries).

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
(0700 directories, 0600 script files, owned by the service user -- the
same user isadoraair-encoders and isadoraair-monitoring already both
run as), not by trying to avoid the duplication. The separate,
non-secret `.json` metadata (0644) is what admin/monitoring code
should read for display -- it never contains a password.

Crash-consistency (2026-08-10 review-fix pass): an LKG's script and
its metadata must never be readable as a mismatched pair -- e.g. a
script for a just-rejected SID-5 edit paired with metadata still
describing the previously-proven SID-1 destinations. The earlier flat
`<slug>.liq` + `<slug>.json` layout wrote these as two SEPARATE
os.replace() calls; a crash between them could leave exactly such a
mismatch, and there was no way to detect it. Fixed with a versioned
layout: each promotion writes a complete, immutable, uniquely-named
version directory (script + metadata together, fsynced) and only THEN
atomically swaps a small pointer file to reference it. A reader always
resolves either the fully-old or fully-new version -- never a mix --
and additionally verifies the script's SHA-256 against the hash
metadata itself records, failing closed (treating it as "no valid LKG"
rather than trusting a corrupt pair) on any mismatch. See write_lkg()/
read_lkg() below.

    LKG_DIR/<slug>/
        versions/<version_id>/script.liq    (0600)
                              /metadata.json (0644, incl. script_sha256)
        current                              (0644, atomic text pointer)

At most _KEEP_VERSIONS version directories are retained (current +
however many of the immediately-superseded ones fit) -- enough for
basic forensics without unbounded growth; this is deliberately NOT a
general configuration-history system.

Exception safety: every function in this module that touches the
filesystem can raise OSError (ENOSPC, EACCES, EROFS, I/O errors, ...).
This module does NOT swallow those itself -- encoder_manager.py's
callers are responsible for containing them so a disk/permission
failure can never take down the supervisor process (see that module's
own "Phase 2 review-fix pass 2" comments for exactly where and why).
The one exception is best-effort cleanup (cleanup_candidate, pruning
old LKG versions), which never raises -- a failure to delete something
is not a reason to fail the operation that already succeeded."""
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

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

# How many LKG versions to retain per slug (current + this many of the
# immediately-preceding ones) -- see this module's own docstring.
_KEEP_VERSIONS = 2

# Renderer/config-schema version -- included in compute_fingerprint's
# hashed payload (see below) specifically so that a SOFTWARE change to
# build_liquidsoap_script (encoders/services/encoder_manager.py) -- or
# any other runtime-affecting rendering/semantic behavior -- forces
# fresh candidate validation/preflight/qualification even when no
# Encoder database row changed at all. Without this, "desired DB
# fingerprint == persisted LKG fingerprint" could be mistaken for
# "still the same proven configuration" after an update silently
# changed what that configuration actually renders/does.
#
# Bump this by hand whenever such a change is made -- deliberately
# manual and obvious (see compute_fingerprint's own docstring for why
# this is NOT derived by hashing source files). Cross-referenced at
# build_liquidsoap_script's own definition in encoder_manager.py.
ENCODER_CONFIG_FORMAT_VERSION = 1


def _ensure_dir(path, mode):
    """mkdir -p, then an explicit chmod -- mkdir's own mode= parameter
    is filtered through the process umask, so a permissive umask on
    some box could silently leave a directory more open than intended.
    The explicit chmod afterward is what actually guarantees the mode,
    same "don't trust the implicit path" spirit as write_lkg's own
    flush+fsync+os.replace() below."""
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, mode)


def _fsync_dir(path):
    """Durabilize a directory's own entries (e.g. a file just created
    inside it) -- fsync on the file itself guarantees the file's own
    content is on disk, but on some filesystems the directory ENTRY
    (the fact that the file now exists under this name) needs its own
    fsync to be crash-safe. Cheap, and the standard POSIX technique for
    this -- matches the same belt-and-suspenders spirit as everything
    else in this module's write path."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_text(path, text, mode):
    """temp-write + fsync + os.replace -- the same atomic-write idiom
    used throughout this project (see encoder_manager.py's
    _atomic_write_json). Used here for both the version-pointer file
    and (via write_lkg) the script/metadata files themselves."""
    tmp_path = path.with_name(path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


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
    change need the encoder manager to reconcile this row's group"
    (encoders/admin.py) also determine "is this a different
    configuration," which is the correct relationship:
    nothing outside that set can affect the rendered script at all.
    `enabled` is excluded (fingerprints are only ever computed over an
    already-filtered enabled set, so it would always be a constant
    True and add nothing). Explicitly excludes -- by construction, not
    by a separate exclusion list -- every volatile runtime field
    (generation, pid, timestamps, listener counts): none of those are
    in RUNTIME_AFFECTING_FIELDS, so they can never leak into the
    fingerprint no matter what future fields get added to Encoder.

    Also includes ENCODER_CONFIG_FORMAT_VERSION (see its own docstring
    above) -- a manual, explicit renderer/schema version, NOT a hash of
    source code. Bump that constant, not this function, when a code
    change alters what gets generated/launched for the same DB rows.

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
    payload = {"version": ENCODER_CONFIG_FORMAT_VERSION, "input_device": input_device, "encoders": rows}
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
    written before either is cleaned up.

    Raises OSError on any filesystem failure (ENOSPC, EACCES, ...) --
    callers (encoder_manager.py's _launch_group, admin.py's
    _run_predispatch_preflight) are responsible for containing it; see
    this module's own docstring."""
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
    running config never changed, nothing left to reference it). Never
    raises -- a failure to delete a temp file is not a reason to fail
    whatever operation already completed."""
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# --- Last-known-good -------------------------------------------------------

def _slug_dir(slug):
    return LKG_DIR / slug


def _versions_dir(slug):
    return _slug_dir(slug) / "versions"


def _current_pointer_path(slug):
    return _slug_dir(slug) / "current"


def _version_dir(slug, version_id):
    return _versions_dir(slug) / version_id


def _new_version_id():
    # Timestamp prefix purely for human-readability (ls sorts sanely);
    # correctness never depends on version_id ordering -- the `current`
    # pointer is the sole source of truth for which version is active.
    return f"{int(time.time())}-{uuid.uuid4().hex[:12]}"


def _read_current_version_id(slug):
    """Best-effort read of the `current` pointer file's contents, or
    None if it doesn't exist / can't be read / is empty. Does not
    raise -- a missing or unreadable pointer just means "no LKG,"
    handled uniformly by read_lkg()."""
    pointer_path = _current_pointer_path(slug)
    if not pointer_path.is_file():
        return None
    try:
        version_id = pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return version_id or None


def read_lkg(slug):
    """Returns (script_text, meta) for the persisted LKG of this slug,
    or (None, None) if no VALID LKG exists -- covers both "never
    promoted for this group" and "exists on disk but failed
    script<->metadata integrity verification" (hash mismatch, missing
    version directory, unreadable pointer) alike, since a caller must
    treat those identically: there is nothing safe to relaunch either
    way. Use describe_lkg_problem(slug) separately if a caller wants a
    human-readable reason for a (None, None) result (e.g. for a more
    specific critical-event message) -- that's purely diagnostic and
    never changes this function's own fail-closed behavior.

    A legacy metadata file with no "script_sha256" field (predates this
    integrity check entirely) is tolerated -- the script is still
    returned, with its metadata, unverified -- rather than treated as a
    hard failure; this project has never actually deployed an LKG under
    that older shape (confirmed before this change), so it's a pure
    forward-compatibility allowance, not a live migration concern.

    Never raises OSError itself for the ordinary "file doesn't exist"
    case (checked via .is_file() first); a genuine I/O error while
    reading an existing file DOES propagate -- callers are responsible
    for containing that, same as every other filesystem boundary in
    this module (see module docstring)."""
    version_id = _read_current_version_id(slug)
    if version_id is None:
        return None, None
    version_dir = _version_dir(slug, version_id)
    script_path = version_dir / "script.liq"
    meta_path = version_dir / "metadata.json"
    if not script_path.is_file():
        return None, None
    script_text = script_path.read_text(encoding="utf-8")

    meta = None
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None

    if meta is None or "script_sha256" not in meta:
        # No hash to verify against -- return unverified rather than
        # failing closed (see docstring: legacy/corrupt metadata is
        # still informationally useful and was always tolerated here).
        return script_text, meta

    actual_hash = hashlib.sha256(script_text.encode("utf-8")).hexdigest()
    if actual_hash != meta["script_sha256"]:
        # Fail closed -- never pair a script with metadata that
        # doesn't provably describe it (Issue 2's core requirement).
        return None, None
    return script_text, meta


def describe_lkg_problem(slug):
    """Best-effort diagnostic for why read_lkg(slug) returned
    (None, None) -- distinguishes "no LKG has ever been promoted for
    this group" from "one exists on disk but failed integrity
    verification," purely for a more specific operator-facing message
    (e.g. encoder_manager.py's _start_rollback). Returns "" if
    read_lkg(slug) would actually succeed. Never raises -- this is a
    diagnostic convenience, not a control-flow decision; on any
    unexpected error while investigating, returns a generic message
    rather than propagating."""
    try:
        version_id = _read_current_version_id(slug)
        if version_id is None:
            return "no last-known-good configuration has ever been promoted for this group"
        version_dir = _version_dir(slug, version_id)
        script_path = version_dir / "script.liq"
        if not script_path.is_file():
            return f"last-known-good pointer refers to a missing version ({version_id!r})"
        script_text, meta = read_lkg(slug)
        if script_text is None:
            return f"last-known-good version {version_id!r} exists but failed script/metadata integrity verification"
        return ""
    except OSError as exc:
        return f"last-known-good state could not be inspected: {exc}"


def read_lkg_meta(slug):
    """Metadata only (no script read) -- for admin/monitoring display,
    where the actual script content (credentials included) should
    never be needed. Returns None if no valid LKG/metadata exists."""
    _, meta = read_lkg(slug)
    return meta


def write_lkg(slug, script_text, meta):
    """Persist a newly-qualified configuration as this slug's LKG, as
    a crash-consistent versioned unit (see module docstring): the
    complete new version (script + metadata, including the metadata's
    own script_sha256) is written and fsynced into a fresh, uniquely-
    named directory BEFORE the `current` pointer is atomically swapped
    to reference it. A crash at any point before that final swap
    leaves `current` referring only to the complete PREVIOUS version
    (or nothing, if this is the very first promotion) -- the
    half-written new version is simply an orphaned directory, cleaned
    up (best-effort) on a later successful write.

    `meta` must NOT contain a password -- see this module's own
    docstring; nothing here enforces that structurally, callers
    (encoder_manager.py's promotion path) are responsible for only
    passing non-secret fields, matching the same trust boundary as
    every other event/state writer in this project (e.g.
    monitoring.models.emit_event's own "detail should be non-secret"
    contract). This function adds "script_sha256" to a COPY of `meta`
    -- the caller's own dict is never mutated.

    Raises OSError on any filesystem failure -- callers are
    responsible for containing it (see module docstring); this is
    exactly the boundary Issue 3 of the 2026-08-10 review-fix pass
    exists to guard in encoder_manager.py's _promote_candidate.

    Directory-fsync chain (2026-08-10 second review pass): file
    fsync() alone only guarantees a file's own DATA is durable, not
    that its directory ENTRY (the fact that it exists under that name)
    is -- each level below is fsynced only after the entry it just
    gained is written, and only before the next level's pointer to it
    is made durable, so a crash anywhere in the sequence can never
    leave a durable reference to a not-yet-durable target:

        mkdir/chmod LKG_DIR, slug_dir  -> fsync LKG_DIR (durabilizes
            slug_dir's own directory entry, mostly relevant for the
            very first promotion of a brand-new slug)
        mkdir/chmod versions_dir, version_dir
        write+fsync script.liq, metadata.json -> fsync version_dir
            (durabilizes their directory entries)
        -> fsync versions_dir (durabilizes version_dir's own entry --
            without this, `current` could durably reference a
            version_id whose directory entry was never guaranteed to
            survive a crash, even though the FILES inside it were)
        write+fsync temp current, os.replace(current) -> fsync
            slug_dir (durabilizes current's -- and versions_dir's,
            if this was the first promotion -- directory entries)."""
    _ensure_dir(LKG_DIR, _DIR_MODE)
    slug_dir = _slug_dir(slug)
    # Both levels explicitly chmod'd: mkdir(parents=True) would
    # silently create LKG_DIR/slug_dir as each other's parent without
    # ever chmod'ing them specifically, leaving them at whatever the
    # process umask produces rather than the intended 0700 -- bit for
    # bit the same gap already fixed once for slug_dir/versions_dir;
    # caught again one level up while reviewing the fsync chain.
    _ensure_dir(slug_dir, _DIR_MODE)
    _fsync_dir(LKG_DIR)

    versions_dir = _versions_dir(slug)
    _ensure_dir(versions_dir, _DIR_MODE)

    old_version_id = _read_current_version_id(slug)

    version_id = _new_version_id()
    version_dir = _version_dir(slug, version_id)
    _ensure_dir(version_dir, _DIR_MODE)

    meta = dict(meta)
    meta["script_sha256"] = hashlib.sha256(script_text.encode("utf-8")).hexdigest()

    _atomic_write_text(version_dir / "script.liq", script_text, _SECRET_FILE_MODE)
    _atomic_write_text(version_dir / "metadata.json", json.dumps(meta, ensure_ascii=False, sort_keys=True), _META_FILE_MODE)
    _fsync_dir(version_dir)
    _fsync_dir(versions_dir)

    _atomic_write_text(_current_pointer_path(slug), version_id, _META_FILE_MODE)
    _fsync_dir(slug_dir)

    _prune_old_versions(versions_dir, keep={v for v in (version_id, old_version_id) if v})


def _prune_old_versions(versions_dir, keep):
    """Best-effort deletion of every version directory NOT in `keep`
    (see _KEEP_VERSIONS / module docstring). Never raises -- pruning
    failure must never undo or fail an already-successful promotion;
    a leftover old version directory is, at worst, a little wasted
    disk, not a correctness problem (read_lkg only ever resolves
    through the `current` pointer)."""
    import shutil
    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name in keep:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            pass


def lkg_exists(slug):
    """True only if a VALID (integrity-verified) LKG exists -- same
    fail-closed semantics as read_lkg() itself, not merely "a pointer
    file happens to be present."""
    script_text, _ = read_lkg(slug)
    return script_text is not None


# --- Desired-vs-accepted destination resolution ---------------------------

def destinations_from_lkg_meta(lkg_meta):
    """Reconstructs lightweight, health-check-only stand-ins for an
    LKG's own destinations from its non-secret metadata (see
    encoder_manager.py's _promote_candidate, which writes the
    "destinations" field this reads). Used by resolve_expected_
    destinations below, and directly by encoder_manager.py's
    _start_rollback for the SAME reason: rollback qualification must
    check what was actually relaunched (the LKG's own frozen identity),
    never re-derived from the live DB -- the DB may hold exactly the
    rejected configuration a rollback exists to recover from.

    Returns a list of SimpleNamespace(id, name, host, port,
    shoutcast_sid, protocol, mount) -- exactly the attributes
    evaluate_encoder_group_health's Shoutcast-destination check (host/
    port/shoutcast_sid) AND encoders.services.validation.
    normalized_destination_key (protocol/host/port/mount) each read off
    an "encoder"-like object (see monitoring/services/probes.py and
    encoder_manager.py's _cross_group_destination_conflicts). Returns
    [] if lkg_meta is falsy or predates the "destinations" field
    entirely (an LKG promoted before it existed) -- evaluate_encoder_
    group_health treats an empty encoders list as status "unknown"
    (see its own docstring), never a false "ok," so a group whose
    destinations can't be reconstructed simply never qualifies/reports
    healthy rather than being silently checked against the wrong SIDs.

    `protocol`/`mount` (Phase 3M) are themselves individually optional
    per entry -- an LKG promoted before those two fields were added to
    _promote_candidate's metadata (encoder_manager.py) has entries
    without them; the resulting stand-in's .protocol/.mount are simply
    None, which normalized_destination_key already treats as "can't
    normalize this row, no collision key" rather than a false match --
    a safe degrade, not a crash, resolved automatically at that
    group's next promotion."""
    if not lkg_meta:
        return []
    destinations = lkg_meta.get("destinations") or []
    return [
        SimpleNamespace(
            id=d.get("encoder_id"), name=d.get("name"),
            host=d.get("host"), port=d.get("port"), shoutcast_sid=d.get("shoutcast_sid"),
            protocol=d.get("protocol"), mount=d.get("mount"),
        )
        for d in destinations
    ]


def resolve_expected_destinations(slug, encoders, launch_kind="accepted"):
    """The single, shared desired-vs-accepted decision: which encoder-
    destination identities should be treated as "currently running,"
    for health-check purposes, given this group's launch_kind. Shared
    by the ordinary dashboard probe (monitoring.services.probes.
    probe_encoder_group, a separate process with no in-memory link to
    the manager -- reads launch_kind off the on-disk group-state file,
    same pattern encoders/admin.py's desired-vs-accepted banner already
    uses) so it can never disagree with the rule the manager's own
    candidate/rollback qualification already applies in-process.

    Rule:
      - "candidate": the group is actively proving the DB's desired
        configuration -- `encoders` (the DB rows) themselves ARE what's
        running. Return them directly.
      - anything else (including "accepted", "rollback", or a missing/
        legacy/unrecognized launch_kind) with an LKG on disk: the LKG
        is authoritative for "what's actually running" -- an "accepted"
        group with a DB configuration that still matches its LKG
        describes the identical destinations either way, and a group
        that's "accepted" with a DB configuration that has since
        drifted (e.g. after a rollback, or while critical-stopped) is
        EXACTLY the case this function exists to get right: the LKG's
        frozen destinations, never the DB's. A legacy LKG with no
        recorded destinations fails closed to [] (see
        destinations_from_lkg_meta) rather than silently substituting
        the DB's rows, which could reintroduce the exact wrong-SID risk
        this mechanism exists to prevent.
      - no LKG exists at all (bootstrap -- nothing has ever been
        accepted for this group): `encoders` (the DB rows) is the only
        available signal, matching this group's behavior before its
        first-ever promotion.

    Returns a list of encoder-like objects (real Encoder rows, or
    lightweight destinations_from_lkg_meta stand-ins) -- exactly what
    evaluate_encoder_group_health expects as its `encoders` argument.

    Deliberately does NOT special-case "does the DB fingerprint match
    the LKG's" -- once an LKG exists, using its own frozen destinations
    is correct (and safe) regardless of whether the DB currently agrees
    with it; the two describe the identical set whenever they do agree,
    so there's no information lost, and no need for the extra
    fingerprint-comparison work on every call."""
    encoders = list(encoders)
    if launch_kind == "candidate":
        return encoders
    script_text, lkg_meta = read_lkg(slug)
    if script_text is None:
        # No valid LKG at all (bootstrap, or one that failed integrity
        # verification) -- the DB rows are the only available signal.
        return encoders
    return destinations_from_lkg_meta(lkg_meta)
