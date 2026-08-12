"""Release/version-skew visibility (1.7 roadmap item) -- the monitoring-
side merge step. Combines:

  * the current CHECKOUT identity (isadoraair.version_info.get_checkout_
    identity -- what's on disk right now, cached briefly there), with
  * each covered first-party service's own RUNTIME identity (captured
    once, at that service's own process startup -- see version_info.py's
    own docstring for why the two must never be conflated)

into a small per-systemd_unit lookup that monitoring/views.py's
api_monitoring_status merges into the existing checks payload, and a
top-level "checkout" block for the page's own compact release line.

Only the 5 first-party services that have an established heartbeat/state
channel are covered (Playback Engine, Gunicorn, Monitoring Service, RBDS
Service, Stream Encoders). Third-party units (nginx, StereoTool) and non-
systemd checks (RBDS Connection) are deliberately excluded -- there is no
runtime-commit concept for a process this project doesn't own or start,
and a bare "unknown" for those would just be noise on every single card.

Every read here is best-effort: a missing/stale/malformed state file
degrades that one service to "unknown" and never raises. This module
performs NO git subprocess calls of its own -- it only reads
get_checkout_identity() (already cached, see version_info.py) and JSON
state files other processes already write for other reasons.

Per-unit "state" values: "current" (SHA match, clean checkout --
genuinely confirmed), "indeterminate" (SHA match, but the checkout is
dirty -- the commit is right but uncommitted changes mean the running
process's match to the CURRENT filesystem state can't be proven; see
_version_state's own docstring), "stale" (SHA mismatch -- unconditional,
a dirty checkout doesn't soften this), "unknown" (either side
unavailable)."""
import json
from pathlib import Path

from isadoraair.version_info import get_checkout_identity, get_web_runtime_commit, short_commit

STATE_DIR = Path("/run/isadoraair")

ENGINE_STATE_PATH = STATE_DIR / "engine_state.json"
MONITORING_STATE_PATH = STATE_DIR / "monitoring_state.json"
RBDS_STATE_PATH = STATE_DIR / "rbds_state.json"
ENCODER_GROUP_GLOB = "encoder_group_*.json"

# The only units this module knows how to source a runtime commit for --
# see this module's own docstring for why the list stops here.
ENGINE_UNIT = "isadoraair-engine.service"
GUNICORN_UNIT = "isadoraair-gunicorn.service"
MONITORING_UNIT = "isadoraair-monitoring.service"
RBDS_UNIT = "isadoraair-rbds.service"
ENCODERS_UNIT = "isadoraair-encoders.service"

COVERED_UNITS = frozenset({ENGINE_UNIT, GUNICORN_UNIT, MONITORING_UNIT, RBDS_UNIT, ENCODERS_UNIT})


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_runtime_commit_from_file(path):
    data = _read_json(path)
    if not isinstance(data, dict):
        return None
    return data.get("runtime_commit")


def _read_engine_runtime_commit():
    return _read_runtime_commit_from_file(ENGINE_STATE_PATH)


def _read_monitoring_runtime_commit(monitoring_state=None):
    """monitoring_state, if given, is the already-parsed dict the caller
    (api_monitoring_status) just read from MONITORING_STATE_PATH for its
    own purposes -- this avoids reading that file twice per request. Falls
    back to a fresh read (e.g. from tests, or any other caller) when not
    given."""
    if isinstance(monitoring_state, dict):
        return monitoring_state.get("runtime_commit")
    return _read_runtime_commit_from_file(MONITORING_STATE_PATH)


def _read_rbds_runtime_commit():
    return _read_runtime_commit_from_file(RBDS_STATE_PATH)


def _read_encoders_runtime_commit():
    """No process-wide heartbeat exists for the encoder manager -- only
    per-group state files (see encoders/services/encoder_manager.py's
    __init__ comment). Every group file that process writes carries the
    same single process's runtime_commit, so reading any ONE of them is
    sufficient; "unknown" (None) if zero groups are configured/running,
    an accepted degradation, not an error."""
    try:
        candidates = sorted(STATE_DIR.glob(ENCODER_GROUP_GLOB))
    except OSError:
        return None
    for path in candidates:
        commit = _read_runtime_commit_from_file(path)
        if commit is not None:
            return commit
    return None


def _version_state(runtime_commit, checkout_commit, checkout_dirty=False):
    """Compares full SHAs (never truncated short SHAs, which could
    collide) -- "unknown" if either side is unavailable (never claim a
    match with incomplete information -- see version_info.py's
    rollout-backward-compatibility requirement), else "stale" on a
    genuine SHA mismatch (this is unconditional -- a dirty checkout
    doesn't soften a real mismatch, it's still exactly as stale).

    On a SHA match, "current" requires the checkout to be clean too.
    Commit equality alone does NOT prove the running process reflects
    what's on disk right now: the running process only ever loaded a
    committed tree, and uncommitted working-tree changes are by
    definition newer than any commit it could have started from. A
    dirty checkout with a matching commit means "this process is running
    the code as of that commit" -- true -- but NOT "this process
    reflects the current filesystem state" -- unproven, since the
    filesystem has diverged from that commit since. That distinct,
    weaker claim is "indeterminate", never "current". checkout_dirty is
    only treated as a downgrade when it's definitively True; None
    (checkout reachable but `git status` itself failed) is not treated
    as evidence of dirtiness -- see get_checkout_identity's own
    docstring on why dirty can be None independently of commit."""
    if not runtime_commit or not checkout_commit:
        return "unknown"
    if runtime_commit != checkout_commit:
        return "stale"
    if checkout_dirty:
        return "indeterminate"
    return "current"


def build_version_lookup(checkout, monitoring_state=None):
    """Returns {systemd_unit: {"runtime_commit", "runtime_short", "state"}}
    for exactly the COVERED_UNITS. `checkout` is the dict returned by
    get_checkout_identity(). `monitoring_state`, if supplied, is reused
    instead of re-reading MONITORING_STATE_PATH (see
    _read_monitoring_runtime_commit)."""
    checkout_commit = checkout.get("commit")
    checkout_dirty = checkout.get("dirty")
    readers = {
        ENGINE_UNIT: _read_engine_runtime_commit,
        GUNICORN_UNIT: get_web_runtime_commit,
        MONITORING_UNIT: lambda: _read_monitoring_runtime_commit(monitoring_state),
        RBDS_UNIT: _read_rbds_runtime_commit,
        ENCODERS_UNIT: _read_encoders_runtime_commit,
    }
    lookup = {}
    for unit, reader in readers.items():
        try:
            runtime_commit = reader()
        except Exception:
            runtime_commit = None
        lookup[unit] = {
            "runtime_commit": runtime_commit,
            "runtime_short": short_commit(runtime_commit),
            "state": _version_state(runtime_commit, checkout_commit, checkout_dirty),
        }
    return lookup


def get_release_status(monitoring_state=None):
    """Top-level entry point for monitoring/views.py. Returns
    (checkout_dict, version_lookup) -- checkout_dict is exactly
    get_checkout_identity()'s return value (safe to serialize directly:
    commit/short_commit/dirty, all None-able), version_lookup is
    build_version_lookup()'s per-unit dict above."""
    checkout = get_checkout_identity()
    lookup = build_version_lookup(checkout, monitoring_state=monitoring_state)
    return checkout, lookup
