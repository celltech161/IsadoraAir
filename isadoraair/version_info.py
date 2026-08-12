"""Compact release/version-skew visibility (1.7 roadmap item) -- answers
two DISTINCT questions that must never be conflated:

  1. CHECKOUT identity: what commit is on disk in the production
     checkout RIGHT NOW. Recomputed on a short cache interval (see
     get_checkout_identity) since /monitoring/ may poll frequently and
     this shells out to `git`.

  2. RUNTIME identity: what commit a given long-lived process (the
     playback engine, monitoring poller, RBDS client, encoder manager,
     a gunicorn worker) loaded AT STARTUP. Captured ONCE, by each
     service, the moment it starts -- never recomputed -- so it stays
     fixed for that process's entire lifetime even if the on-disk
     checkout is updated out from under it later. That fixed value is
     the whole point: comparing it against the (live) checkout
     identity is what proves -- or disproves -- that a running process
     is actually executing the code currently on disk. A bare
     `git rev-parse HEAD` shown on the page would only ever answer
     question 1, never question 2, which is why this module keeps them
     structurally separate: capture_runtime_commit() must be called
     exactly ONCE per process, by the CALLER, at startup, and the
     result stored on the caller -- this module does not (and cannot)
     enforce that from the outside, but see that function's own
     docstring for why re-calling it defeats its purpose.

No authoritative release/version-string mechanism exists anywhere else
in this project (confirmed by inspection before writing this module --
no __version__, no build-time-generated file, no tag-reading code
elsewhere), so commit SHA alone is the correct, sufficient identity for
now. Nothing here invents a release-tagging system; if one is added
later, the display layer can grow a "v1.0.0 · <short_commit>" line
without any change to the identity logic below.

Fails safe to "unknown" (None fields) EVERYWHERE: a packaged/non-git
install, a missing git binary, a bad project root, or any subprocess
error must never crash a service or the monitoring page. Every public
function here returns None/empty rather than raising.

Security / subprocess discipline: every git invocation uses a FIXED
argument list (never shell=True, never string interpolation), a
bounded timeout, and the known project root (PROJECT_ROOT, derived from
this file's own location, never a caller-supplied path). Nothing here
is reachable from an HTTP request parameter -- callers only ever invoke
these functions with no arguments (or force_refresh=True, a plain
bool)."""
import subprocess
import time
from pathlib import Path

# This file lives at <project_root>/isadoraair/version_info.py --
# same derivation settings.py's own BASE_DIR uses, so this always
# points at the real checkout root regardless of the process's cwd
# (systemd units set WorkingDirectory explicitly, but nothing here
# should depend on that).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

GIT_TIMEOUT_SECONDS = 3
SHORT_SHA_LEN = 7

# How long a cached checkout-identity read stays valid before the next
# caller triggers a fresh `git` read. /monitoring/ may poll every few
# seconds; git rev-parse/status are cheap, single, local, non-network
# calls, but there's no reason to run them on every request when the
# checkout only actually changes at deploy time (item 14: "prefer one
# cached/current checkout identity shared across the page").
CHECKOUT_CACHE_SECONDS = 15

_checkout_cache = {"value": None, "computed_at": 0.0}

# Set once, by isadoraair/wsgi.py, the moment each gunicorn worker
# process imports the application -- see capture_web_runtime_commit's
# own docstring for why this lives here rather than as a plain
# wsgi.py-local variable (the monitoring view needs to read it from a
# different module without importing wsgi.py itself).
_web_runtime_commit_holder = {"captured": False, "value": None}


def _run_git(*args):
    """Fixed-argument-list git invocation against PROJECT_ROOT, bounded
    timeout, never shell=True, no caller-supplied arguments accepted
    anywhere in this module. Returns stripped stdout on success, None
    on ANY failure (git missing, PROJECT_ROOT not a repo, timeout,
    non-zero exit, or any other subprocess-layer failure) -- every
    caller treats None uniformly as "unknown" and never needs to
    distinguish why.

    Catches a bare Exception, not just (OSError, TimeoutExpired):
    confirmed live during this feature's own test-suite validation --
    running many real git subprocess calls back-to-back in one process
    (each of several manager classes now calls capture_runtime_commit()
    once per real construction, and the existing test suite constructs
    them hundreds of times) intermittently raised a bare ValueError out
    of subprocess.run()'s own internals, not a documented subprocess
    exception type. This module's own docstring already promises
    "fails safe to unknown EVERYWHERE... every public function here
    returns None/empty rather than raising" -- that promise was
    incompletely implemented before this catch was widened; a
    subprocess-layer failure of any kind is exactly the class of thing
    this function exists to absorb, so there's no failure mode here
    that should ever be allowed to propagate to a caller."""
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _compute_checkout_identity():
    """Uncached -- always shells out. Callers should normally go
    through get_checkout_identity() instead; this is split out
    separately so capture_runtime_commit() can call it directly without
    silently participating in the checkout-identity cache (a runtime
    capture must reflect the checkout at THIS exact moment, not
    whatever the page's cache happened to be holding)."""
    full_sha = _run_git("rev-parse", "HEAD")
    if not full_sha:
        return {"commit": None, "short_commit": None, "dirty": None}
    # `git status --porcelain` output is empty for a clean tree
    # (tracked AND untracked); any output at all means "modified".
    # Never itself exposed to the client -- only the derived boolean
    # is (see this module's own docstring: no filenames, no `git
    # status` output, in the normal Monitoring UI).
    porcelain = _run_git("status", "--porcelain")
    dirty = None if porcelain is None else bool(porcelain)
    return {"commit": full_sha, "short_commit": full_sha[:SHORT_SHA_LEN], "dirty": dirty}


def get_checkout_identity(force_refresh=False):
    """The revision currently present in the production checkout on
    disk, right now. Recomputed at most once every
    CHECKOUT_CACHE_SECONDS -- cheap, local git calls, but no reason to
    re-run them on every single monitoring poll.

    Returns {"commit": full_sha_or_None, "short_commit": 7charsOrNone,
    "dirty": True/False/None}. All three are None together if git is
    unavailable, PROJECT_ROOT isn't a checkout, or the read failed --
    callers render "version unknown" in that case, never crash.
    `dirty` alone can independently be None (git reachable, `rev-parse`
    succeeded, but `git status` itself failed) while commit/short_commit
    are populated -- callers must check dirty separately, never assume
    it's set just because commit is."""
    now = time.monotonic()
    cached = _checkout_cache["value"]
    if not force_refresh and cached is not None and (now - _checkout_cache["computed_at"]) < CHECKOUT_CACHE_SECONDS:
        return cached
    value = _compute_checkout_identity()
    _checkout_cache["value"] = value
    _checkout_cache["computed_at"] = now
    return value


def capture_runtime_commit():
    """Call exactly ONCE, at process startup, from each long-lived
    service that wants to report its own runtime identity -- store the
    return value (a full SHA string, or None if unavailable) on the
    caller (e.g. self._runtime_commit) and include it in whatever
    heartbeat/state payload that service already writes.

    Deliberately does NOT read or write the module-level checkout
    cache above, and is NOT itself memoized -- every call re-shells to
    git fresh. That's intentional: it's the CALLER's own discipline
    (call this once, keep the result, never call it again) that gives
    the returned value its "fixed for this process's lifetime"
    meaning, which is the entire semantic this feature depends on.
    Calling this on every heartbeat tick instead of once at startup
    would silently turn a process's own runtime identity into a live
    mirror of the checkout -- indistinguishable from just showing
    `git rev-parse HEAD` on the page, which is exactly the naive
    approach this feature's design explicitly rejects (see this
    module's own top docstring)."""
    return _compute_checkout_identity()["commit"]


def capture_web_runtime_commit():
    """Called ONCE from isadoraair/wsgi.py at module import time --
    i.e. once per gunicorn WORKER process, the moment that worker loads
    the WSGI application (gunicorn is run without --preload here, so
    each of the configured workers imports the app independently after
    forking and gets its own correct, independent capture -- see this
    module's docstring in the completion report for the full
    reasoning). Stores this worker's own runtime identity so /monitoring/'s
    Gunicorn card can read it later via get_web_runtime_commit(), from
    whichever worker happens to handle that particular request.

    Idempotent guard (the "captured" flag): an accidental second call
    in the same process (e.g. Django's autoreloader re-executing
    wsgi.py under `runserver`) can't silently overwrite an
    already-fixed value with a later one."""
    if not _web_runtime_commit_holder["captured"]:
        _web_runtime_commit_holder["value"] = capture_runtime_commit()
        _web_runtime_commit_holder["captured"] = True
    return _web_runtime_commit_holder["value"]


def get_web_runtime_commit():
    """The CURRENT process's own captured web-runtime commit -- None if
    capture_web_runtime_commit() was never called in this process
    (e.g. `manage.py runserver`/management commands/tests, which don't
    import wsgi.py -- the Gunicorn card correctly falls back to
    "unknown" in that case, never crashes, never claims a match)."""
    return _web_runtime_commit_holder["value"]


def short_commit(full_sha):
    """Shared short-SHA formatting -- 7 chars, or None for a falsy
    input -- so every card/badge renders an identical style regardless
    of which code path produced the full SHA."""
    if not full_sha:
        return None
    return full_sha[:SHORT_SHA_LEN]
