"""Fail-closed filesystem isolation for Django test runs.

Why this exists
---------------
Test code reaches the filesystem through settings that default to the station's
real data directories (LIBRARY_ROOT, WAVEFORMS_DIR, PRODUCTION_MEDIA_ROOT, ...)
and that a development ``.env`` may name explicitly.  A test that overrides one
of them (say LIBRARY_ROOT) but not its neighbours silently writes through the
others.  Test databases hand out small Track ids that collide with real ones,
so on 2026-10-29 mocked analysis overwrote ``/srv/isadoraair/waveforms/<id>.json``
for 165 real tracks while changing only disposable database rows.

This module is the systemic control.  It does not rely on a test author or on
the developer's ``.env``.

1. **Per-run namespace.**  A test invocation owns one disposable directory
   (``isadoraair-test-data.XXXXXX``, 0700, created by this process or handed
   down by ``production/tests/run_with_validation_service.sh``).  Every
   writable station root setting is pointed at a sub-directory of it *while the
   settings module is still being imported*, so module-level captures such as
   ``library.storage.royalty_report_storage`` bind to scratch, not to the
   station.  Values from the environment or ``.env`` are never honoured for
   these roots; a configured value that names a protected path is recorded and
   reported as a stale-configuration notice.
2. **Write boundary.**  A process-wide ``sys.addaudithook`` hook refuses any
   mutation (open for write, create, rename, delete, copy, chmod, ...) whose
   resolved target lies inside a protected root, whatever settings or
   ``override_settings`` say.  Symlinks and ``..`` are resolved first.  The
   refusal is a ``BaseException`` so a broad ``except Exception`` in the code
   under test cannot swallow it, and every refusal is recorded so the runner
   fails the run even if something does swallow it.  An audit hook cannot be
   removed once installed.
3. **Effective-settings validation** before any test runs: roots inside the
   namespace, ingest URL/key blank, Hour Refill unarmed, no production service
   sockets, development database identity, and the code is not the production
   checkout.

It is stdlib-only because ``isadoraair/settings.py`` imports it, and it is
reached only when the process is a test invocation (``manage.py test`` or the
namespace environment variable is set); a normal station process never
imports it.

Limits (see docs/TEST_ISOLATION.md): the audit hook sees Python-level
operations only, not writes made by child processes such as ffmpeg (those are
covered by the root redirection), hard links or bind mounts.
"""
from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import stat
import sys
import tempfile
import threading
import traceback

NAMESPACE_ENV = "ISADORAAIR_TEST_DATA_ROOT"
NAMESPACE_PREFIX = "isadoraair-test-data."
ISOLATED_CI_ENV = "ISADORAAIR_TEST_ISOLATED_CI"
DEV_DATABASE_NAME = "isadoraair_dev"
DEV_DATABASE_USER = "isadoraair_dev"

#: setting -> sub-directory of the namespace.
PATH_SETTINGS = {
    "LIBRARY_ROOT": "music",
    "WAVEFORMS_DIR": "waveforms",
    "PRODUCTION_MEDIA_ROOT": "production-media",
    "REPORTS_ROOT": "reports",
    "WEATHER_DATA_DIR": "weather",
    "ENCODER_STATE_ROOT": "encoders",
}
#: Unix-socket settings: a production service socket is replaced by an unreachable
#: namespace path; a socket handed down by the guarded runner is kept.
SOCKET_SETTINGS = {
    "PRODUCTION_VALIDATION_SOCKET": "validator.sock",
    "PRODUCTION_RENDER_SOCKET": "render.sock",
}
PRODUCTION_SOCKET_DIRS = ("/run/isadoraair", "/run/isadoraair-validation", "/run/isadoraair-render")
BLANK_SETTINGS = ("WEB_REQUESTS_INGEST_URL", "WEB_REQUESTS_INGEST_API_KEY")

#: Fixed protected roots (plus the live checkout reached through /opt/isadoraair
#: and any path the production environment file names; see ``protected_roots``).
STATIC_PROTECTED_ROOTS = (
    "/srv/isadoraair",
    "/var/lib/isadoraair",
    "/opt/isadoraair",
    "/opt/isadoraair-runtime",
    "/etc/isadoraair",
)
PRODUCTION_APP_ROOT = "/opt/isadoraair"
PRODUCTION_ENVIRONMENT_FILE = PRODUCTION_APP_ROOT + "/.env"
STATION_CONFIG_PATH = "/etc/isadoraair/station.json"

CATEGORY_NOT_ENGAGED = "ISOLATION_NOT_ENGAGED"
CATEGORY_NAMESPACE = "NAMESPACE_UNSAFE"
CATEGORY_SETTINGS = "EFFECTIVE_SETTINGS_UNSAFE"
CATEGORY_DATABASE = "DATABASE_IDENTITY_UNSAFE"
CATEGORY_CHECKOUT = "PRODUCTION_CHECKOUT"


class UnsafeTestEnvironmentError(RuntimeError):
    """The test process is not provably isolated from station data."""

    def __init__(self, category, rule, *, detail=None):
        self.category = category
        self.rule = rule
        lines = [f"Refusing to run tests: {category}", f"  rule:   {rule}"]
        if detail:
            lines.append(f"  detail: {detail}")
        lines.append(
            "  fix:    run tests only with `manage.py test` (or "
            "production/tests/run_with_validation_service.sh) from a feature worktree whose "
            ".env uses the development database; see docs/TEST_ISOLATION.md."
        )
        super().__init__("\n".join(lines))


class ProtectedPathWriteBlocked(BaseException):
    """A mutation targeting protected station storage was refused.

    A ``BaseException`` on purpose: code under test that wraps a write in
    ``except Exception`` (analysis does) must not be able to swallow the refusal.
    """

    def __init__(self, event, path, root):
        self.event, self.path, self.root = event, path, root
        super().__init__(
            f"blocked {event} on {path!r}: inside protected station storage {root!r}. "
            "Tests must write only under their per-run namespace or a temporary directory."
        )


# --------------------------------------------------------------------------- paths


def _lexical(path):
    return os.path.normpath(os.path.abspath(os.fsdecode(path)))


def _real(path):
    return os.path.realpath(_lexical(path))


def _forms(path, follow=True):
    """Lexical and symlink-resolved forms. ``follow=False`` leaves a final symlink
    component alone (removing or renaming a link does not touch its target)."""
    lexical = _lexical(path)
    if follow:
        return {lexical, os.path.realpath(lexical)}
    head, tail = os.path.split(lexical)
    return {lexical, os.path.join(os.path.realpath(head), tail)}


def _within(child, root):
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


_EXTRA_PROTECTED = []
_static_cache = []
_cache_lock = threading.Lock()


def _read_production_paths(env_file=PRODUCTION_ENVIRONMENT_FILE):
    """Only the path keys are read from the production environment file."""
    found = []
    try:
        with open(env_file, encoding="utf-8") as stream:
            for line in stream:
                key, separator, value = line.strip().partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                if separator and key in PATH_SETTINGS:
                    value = value.strip().strip("'\"")
                    if value.startswith("/"):
                        found.append(value)
    except (OSError, ValueError):
        pass
    return found


def protected_roots():
    """Canonical protected roots: fixed, derived from this host, and test-registered."""
    with _cache_lock:
        if not _static_cache:
            roots = set()
            for raw in list(STATIC_PROTECTED_ROOTS) + _read_production_paths():
                if raw and raw != "/":
                    roots.update(_forms(raw))
            roots.discard("/")
            _static_cache.append(frozenset(roots))
        roots = set(_static_cache[0])
        for extra in _EXTRA_PROTECTED:
            roots.update(_forms(extra))
        return frozenset(roots)


def reset_protected_cache():
    with _cache_lock:
        del _static_cache[:]


@contextlib.contextmanager
def protect_for_test(path):
    """Register ``path`` as protected for the duration (regression-test seam).

    It only ever *adds* protection; there is no way to remove a fixed root.
    """
    _EXTRA_PROTECTED.append(os.fspath(path))
    try:
        yield
    finally:
        _EXTRA_PROTECTED.remove(os.fspath(path))


def protected_match(path, *, ancestors=False, follow=True):
    """The protected root ``path`` is inside (or, with ``ancestors``, contains), else None."""
    forms = _forms(path, follow)
    for root in protected_roots():
        for form in forms:
            if _within(form, root) or (ancestors and _within(root, form)):
                return root
    return None


def approved_bases():
    """Directories a namespace may be created in (never a protected location)."""
    bases = {_real(tempfile.gettempdir()), _real("/tmp"), _real("/var/tmp")}
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        bases.add(_real(runtime))
    return frozenset(base for base in bases if protected_match(base, ancestors=True) is None)


# ----------------------------------------------------------------------- namespace

_created_here = []


def validate_namespace(path):
    """Raise unless ``path`` is a private, real, unprotected namespace directory."""
    def refuse(rule, detail=None):
        raise UnsafeTestEnvironmentError(CATEGORY_NAMESPACE, rule, detail=detail)

    if not isinstance(path, str) or not os.path.isabs(path):
        refuse("the namespace must be an absolute path", repr(path))
    if _lexical(path) != path or os.path.realpath(path) != path:
        refuse("the namespace path must be canonical (no '..', no symlink component)", path)
    root = protected_match(path, ancestors=True)
    if root:
        refuse("the namespace overlaps protected station storage", f"{path} overlaps {root}")
    try:
        info = os.lstat(path)
    except OSError as exc:
        refuse("the namespace directory does not exist", f"{path}: {exc.strerror}")
    if not stat.S_ISDIR(info.st_mode):
        refuse("the namespace is not a directory", path)
    if info.st_uid != os.getuid():
        refuse("the namespace is not owned by the invoking user", path)
    if info.st_mode & 0o077:
        refuse("the namespace must be mode 0700 (no group/other access)", f"{path}: {oct(info.st_mode & 0o777)}")
    if not os.path.basename(path).startswith(NAMESPACE_PREFIX):
        refuse(f"the namespace directory name must start with {NAMESPACE_PREFIX!r}", path)
    parent = os.path.dirname(path)
    if parent not in approved_bases():
        refuse("the namespace must live directly in an approved temporary base",
               f"{parent} not in {sorted(approved_bases())}")
    return path


def _remove_created_namespace(path):
    with contextlib.suppress(Exception):
        validate_namespace(path)
        shutil.rmtree(path, ignore_errors=True)


def provision_namespace(environ=None):
    """Return the validated namespace root, creating one unless the environment supplies it."""
    environ = os.environ if environ is None else environ
    supplied = environ.get(NAMESPACE_ENV)
    if supplied:
        path = validate_namespace(supplied)
    else:
        base = _real(tempfile.gettempdir())
        if base not in approved_bases():
            raise UnsafeTestEnvironmentError(
                CATEGORY_NAMESPACE, "the temporary base is not an approved location", detail=base)
        path = tempfile.mkdtemp(prefix=NAMESPACE_PREFIX, dir=base)
        os.chmod(path, 0o700)
        validate_namespace(path)
        environ[NAMESPACE_ENV] = path
        _created_here.append((os.getpid(), path))
        atexit.register(lambda p=path, pid=os.getpid(): os.getpid() == pid and _remove_created_namespace(p))
    for sub in PATH_SETTINGS.values():
        os.makedirs(os.path.join(path, sub), mode=0o700, exist_ok=True)
    return path


# ---------------------------------------------------------------------- invocation


def is_test_invocation(argv=None, environ=None):
    argv = sys.argv if argv is None else argv
    environ = os.environ if environ is None else environ
    return bool(environ.get(NAMESPACE_ENV)) or list(argv[1:2]) == ["test"]


def _station_markers():
    return [p for p in (STATION_CONFIG_PATH, PRODUCTION_ENVIRONMENT_FILE, PRODUCTION_APP_ROOT + "/manage.py")
            if os.path.lexists(p)]


def _database_problem(databases, environ):
    default = (databases or {}).get("default") or {}
    name, user = str(default.get("NAME") or ""), str(default.get("USER") or "")
    if name == DEV_DATABASE_NAME and user == DEV_DATABASE_USER:
        return None
    if environ.get(ISOLATED_CI_ENV) == "1" and not _station_markers():
        return None  # an isolated CI host: no installation, so no production role to confuse
    return (f"DB_NAME={name!r} DB_USER={user!r}; tests require DB_NAME={DEV_DATABASE_NAME} and "
            f"DB_USER={DEV_DATABASE_USER} (disposable test_{DEV_DATABASE_NAME})")


def effective_problems(get, namespace, databases=None, environ=None):
    """Every reason the effective configuration is unsafe; ``get(name)`` reads a setting."""
    environ = os.environ if environ is None else environ
    problems = []
    for name in PATH_SETTINGS:
        value = get(name)
        if not isinstance(value, (str, os.PathLike)) or not str(value):
            problems.append(f"{name} is unset")
            continue
        path = os.fspath(value)
        root = protected_match(path, ancestors=True)
        if root:
            problems.append(f"{name}={path!r} overlaps protected station storage {root!r}")
        elif not (_real(path) == namespace or _real(path).startswith(namespace + os.sep)) \
                or not (_lexical(path) == namespace or _lexical(path).startswith(namespace + os.sep)):
            problems.append(f"{name}={path!r} is not inside the test namespace {namespace!r}")
    for name in BLANK_SETTINGS:
        if get(name):
            problems.append(f"{name} must be blank in test runs (no network ingest)")
    if get("HOUR_REFILL_INSTALL_ARMED"):
        problems.append("HOUR_REFILL_INSTALL_ARMED must be false in test runs")
    for name in SOCKET_SETTINGS:
        value = get(name)
        if value and any(_within(_lexical(os.fspath(value)), d) for d in PRODUCTION_SOCKET_DIRS):
            problems.append(f"{name}={str(value)!r} names a production service socket")
    if databases is not None:
        problem = _database_problem(databases, environ)
        if problem:
            problems.append(problem)
    return problems


def configure_settings(namespace_settings, environ=None):
    """Called from settings.py at import: redirect roots, install the write boundary.

    ``namespace_settings`` is the settings module's ``globals()``.  Returns the state
    dictionary stored as ``ISADORAAIR_TEST_ISOLATION``.
    """
    environ = os.environ if environ is None else environ
    base_dir = str(namespace_settings["BASE_DIR"])
    root = protected_match(base_dir)
    if root:
        raise UnsafeTestEnvironmentError(
            CATEGORY_CHECKOUT, "tests must not run from the production checkout",
            detail=f"{base_dir} is inside protected {root}")
    problem = _database_problem(namespace_settings.get("DATABASES"), environ)
    if problem:
        raise UnsafeTestEnvironmentError(CATEGORY_DATABASE, "development database identity required", detail=problem)

    namespace = provision_namespace(environ)

    stale = {}
    for name, sub in PATH_SETTINGS.items():
        configured = namespace_settings.get(name)
        if configured and protected_match(str(configured), ancestors=True):
            stale[name] = str(configured)
        elif configured and not (_real(str(configured)) + os.sep).startswith(namespace + os.sep):
            stale[name] = str(configured)
        value = os.path.join(namespace, sub)
        namespace_settings[name] = value
        environ[name] = value  # child processes (subprocess manage.py, services) inherit the same roots
    for name in BLANK_SETTINGS:
        if namespace_settings.get(name):
            stale[name] = "<set; blanked>"
        namespace_settings[name] = ""
        environ[name] = ""
    namespace_settings["HOUR_REFILL_INSTALL_ARMED"] = False
    environ["HOUR_REFILL_INSTALL_ARMED"] = "false"
    for name, filename in SOCKET_SETTINGS.items():
        configured = namespace_settings.get(name)
        if not configured or any(_within(_lexical(str(configured)), d) for d in PRODUCTION_SOCKET_DIRS):
            if configured:
                stale[name] = str(configured)
            value = os.path.join(namespace, filename)  # unreachable unless the guarded runner supplies a socket
            namespace_settings[name] = value
            environ[name] = value

    problems = effective_problems(namespace_settings.get, namespace, namespace_settings.get("DATABASES"), environ)
    if problems:
        raise UnsafeTestEnvironmentError(CATEGORY_SETTINGS, "effective settings are not isolated",
                                         detail="; ".join(problems))
    install_write_guard()
    database = namespace_settings["DATABASES"]["default"]
    return {
        "namespace": namespace,
        "roots": {name: namespace_settings[name] for name in PATH_SETTINGS},
        "stale_configuration": stale,
        "database": {"NAME": database.get("NAME"), "USER": database.get("USER")},
        "created_by_pid": os.getpid() if _created_here else None,
    }


# ------------------------------------------------------------------ write boundary

_guard_installed = False
_hook_lock = threading.Lock()
_blocked = []

_O_WRITE = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

#: audit event -> (indexes of path arguments that are mutated, index(es) of dir_fd
#: arguments, indexes whose final symlink component is NOT followed by the operation)
_MUTATIONS = {
    "os.mkdir": ((0,), (2,), ()), "os.rmdir": ((0,), (1,), (0,)), "os.remove": ((0,), (1,), (0,)),
    "os.chmod": ((0,), (2,), ()), "os.chown": ((0,), (3,), ()), "os.utime": ((0,), (3,), ()),
    "os.truncate": ((0,), (), ()), "os.mkfifo": ((0,), (2,), ()), "os.mknod": ((0,), (3,), ()),
    "os.rename": ((0, 1), (2, 3), (0, 1)), "os.link": ((1,), (2, 3), (1,)), "os.symlink": ((1,), (2,), (1,)),
    "os.setxattr": ((0,), (), ()), "os.removexattr": ((0,), (), ()),
    "shutil.copyfile": ((1,), (), ()), "shutil.copymode": ((1,), (), ()), "shutil.copystat": ((1,), (), ()),
    "shutil.copytree": ((1,), (), ()), "shutil.move": ((0, 1), (), (0,)), "shutil.chown": ((0,), (), ()),
    "shutil.rmtree": ((0,), (1,), (0,)), "shutil.unpack_archive": ((1,), (), ()),
    "tempfile.mkstemp": ((0,), (), ()), "tempfile.mkdtemp": ((0,), (), ()),
}
#: Deleting an ancestor of protected storage is as destructive as deleting inside it.
_ANCESTOR_SENSITIVE = {"shutil.rmtree", "shutil.move", "os.rename", "os.rmdir", "os.remove"}


def _as_path(value):
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            return os.fsdecode(value)
        except (TypeError, ValueError):
            return None
    return None  # an int (file descriptor) or None: nothing to resolve


def _refuse(event, path, root):
    entry = (event, path, root, "".join(traceback.format_stack(limit=8)[:-2]))
    with _hook_lock:
        _blocked.append(entry)
    raise ProtectedPathWriteBlocked(event, path, root)


def _audit(event, args):
    if event == "open":
        path = _as_path(args[0]) if args else None
        if path is None:
            return
        mode, flags = (args[1] if len(args) > 1 else None), (args[2] if len(args) > 2 else None)
        intent = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                 (isinstance(flags, int) and flags & _O_WRITE)
        if intent:
            root = protected_match(path)
            if root:
                _refuse(event, path, root)
        return
    spec = _MUTATIONS.get(event)
    if spec is None:
        return
    indexes, dir_fds, nofollow = spec
    relative_to_fd = any(len(args) > i and isinstance(args[i], int) and args[i] >= 0 for i in dir_fds)
    for index in indexes:
        path = _as_path(args[index]) if len(args) > index else None
        if path is None or (relative_to_fd and not os.path.isabs(path)):
            continue
        root = protected_match(path, ancestors=event in _ANCESTOR_SENSITIVE, follow=index not in nofollow)
        if root:
            _refuse(event, path, root)


def install_write_guard():
    """Install the process-wide write boundary (idempotent; irreversible)."""
    global _guard_installed
    with _hook_lock:
        if _guard_installed:
            return
        sys.addaudithook(_audit)
        _guard_installed = True


def guard_installed():
    return _guard_installed


def blocked_writes():
    with _hook_lock:
        return list(_blocked)


def clear_blocked_writes():
    """For the guard's own regression tests, which provoke refusals on purpose."""
    with _hook_lock:
        del _blocked[:]


# ------------------------------------------------------------------------ engagement


def assert_engaged(settings=None):
    """Raise unless this process is provably isolated (called before any test runs)."""
    if settings is None:
        from django.conf import settings  # noqa: PLC0415 -- only meaningful inside a configured Django
    state = getattr(settings, "ISADORAAIR_TEST_ISOLATION", None)
    if not state:
        raise UnsafeTestEnvironmentError(
            CATEGORY_NOT_ENGAGED,
            "test isolation was not configured for this process (settings were not loaded by a "
            "test invocation)",
            detail="start tests with `manage.py test <labels>`; never from `manage.py shell` or a bare unittest runner")
    if not guard_installed():
        raise UnsafeTestEnvironmentError(CATEGORY_NOT_ENGAGED, "the write boundary is not installed")
    namespace = validate_namespace(state["namespace"])
    problems = effective_problems(lambda n: getattr(settings, n, None), namespace,
                                  getattr(settings, "DATABASES", None))
    if problems:
        raise UnsafeTestEnvironmentError(CATEGORY_SETTINGS, "effective settings are not isolated",
                                         detail="; ".join(problems))
    return state


def fail_on_blocked(failures, stream=None):
    """Add every refused write to ``failures`` (a runner's failure count) and report them.

    Even a refusal swallowed by the code under test fails the run.
    """
    blocked = blocked_writes()
    if not blocked:
        return failures
    stream = stream or sys.stderr
    print(f"\nISOLATION FAILURE: {len(blocked)} write(s) to protected station storage were refused:", file=stream)
    for event, path, root, _stack in blocked:
        print(f"  {event} {path}  (inside {root})", file=stream)
    return (failures or 0) + len(blocked)


def describe(state):
    """Human-readable preflight summary for the runner banner."""
    lines = [f"isadoraair test isolation ENGAGED  namespace={state['namespace']}"]
    for name, value in state["roots"].items():
        lines.append(f"  {name} = {value}")
    db = state["database"]
    lines.append(f"  database: DB_NAME={db['NAME']} DB_USER={db['USER']}")
    for name, configured in sorted(state["stale_configuration"].items()):
        lines.append(f"  NOTICE: configured {name}={configured} was NOT honoured (redirected to the namespace)")
    return "\n".join(lines)
