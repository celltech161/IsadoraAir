"""Read-only git operations for Phase A update discovery -- [P0] 1.1.

Every function here either performs a genuinely read-only git
operation (`rev-parse`, `status`, `symbolic-ref`, `merge-base`, `log`,
`cat-file`, `remote get-url`), or `fetch` -- which DOES write
remote-tracking refs and objects under `.git/` (that's real, not
glossed over: it's the one operation in this module that mutates
anything on disk) but NEVER touches the working tree, the index, or
any tracked/untracked file outside `.git/`.

Nothing in this module ever runs checkout, reset, merge, pull, stash,
clean, branch, or submodule commands. This is enforced, not just
promised: `run_git()` whitelists the git subcommand against
ALLOWED_SUBCOMMANDS before ever invoking a subprocess, so even a future
call site that got the discipline wrong is refused at the boundary
rather than silently working-tree-mutating.

Subprocess discipline matches `isadoraair/version_info.py`'s own
(`_run_git`), extended for this module's broader operation set: fixed
argument list, never `shell=True`, bounded timeout, bounded captured
output, uniform fail-safe-to-None/False return on any error (missing
git, not a repo, timeout, non-zero exit, decode error) -- callers never
need to distinguish *why* a read failed, only that it did.

Phase A calls these functions directly from Django views (read-only,
non-privileged). Per the architecture-review correction, this module
is ALSO the thing the future Phase B privileged executor is expected
to reuse for its own read-side git logic (its own copy, running from
its own root-owned, application-unwritable install -- see
`docs/UPDATE_CENTER.md` -- never by importing this exact file out of
the Gunicorn-writable checkout). Keeping every function here provably
read-only-to-the-working-tree is what makes that reuse safe to
recommend at all."""
from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import threading
from pathlib import Path

GIT_TIMEOUT_SECONDS = 10
GIT_FETCH_TIMEOUT_SECONDS = 30  # network operation, needs more room
MAX_OUTPUT_BYTES = 1_000_000  # generous; a runaway/hostile output is truncated, never unbounded

# Read-only (or, for "fetch", write-only-to-.git-metadata) git
# subcommands this module ever invokes. Anything else -- in particular
# checkout, reset, merge, pull, stash, clean, branch, submodule, push,
# commit, rm, mv, apply -- is refused by run_git() itself before a
# subprocess is even spawned.
ALLOWED_SUBCOMMANDS = frozenset({
    "fetch", "rev-parse", "status", "symbolic-ref", "merge-base",
    "log", "cat-file", "remote", "diff", "show", "rev-list",
})
# Explicit, named, and tested (see tests/test_git_adapter.py) rather
# than left as "anything not in ALLOWED_SUBCOMMANDS" -- naming them
# individually makes the refusal message specific and makes the
# regression test read as a direct statement of the safety guarantee,
# not an inference from what's absent.
FORBIDDEN_SUBCOMMANDS = frozenset({
    "checkout", "reset", "merge", "pull", "stash", "clean", "branch",
    "submodule", "push", "commit", "rm", "mv", "apply", "worktree",
    "switch", "restore",
})


@dataclasses.dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str
    returncode: int | None


class _BoundedCollector:
    """Drain a pipe fully while retaining at most MAX_OUTPUT_BYTES."""

    def __init__(self, stream):
        self.stream = stream
        self.data = bytearray()
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def _drain(self):
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    return
                remaining = MAX_OUTPUT_BYTES - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
        except Exception:
            return


def _run_git_argv(argv: list[str], timeout: float) -> tuple[int | None, bytes]:
    """One bounded subprocess primitive for every git invocation."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            shell=False,
            start_new_session=True,
        )
    except Exception:
        return None, b""
    collector = _BoundedCollector(process.stdout)
    collector.thread.start()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            return None, bytes(collector.data)
        return None, bytes(collector.data)
    finally:
        collector.thread.join(timeout=1.0)
    return process.returncode, bytes(collector.data)


def run_git(args, cwd: Path, timeout: float = GIT_TIMEOUT_SECONDS) -> GitResult:
    """The one subprocess entry point in this module. `args` must be a
    list of plain strings (never a single string, never shell-joined);
    `args[0]` must be an allowed subcommand. Fails safe (`ok=False`,
    empty stdout) on literally any problem -- missing `git` binary,
    `cwd` not a repository, timeout, non-zero exit, or a decode
    error -- callers treat `ok=False` uniformly and never need to
    inspect why."""
    if not args or not isinstance(args, list) or not all(isinstance(a, str) for a in args):
        raise TypeError("run_git(args=...) must be a list[str], never a single string")
    subcommand = args[0]
    if subcommand in FORBIDDEN_SUBCOMMANDS:
        raise ValueError(
            f"git subcommand {subcommand!r} is forbidden in this read-only adapter -- "
            f"it can mutate the working tree/index, which this module never does"
        )
    if subcommand not in ALLOWED_SUBCOMMANDS:
        raise ValueError(f"git subcommand {subcommand!r} is not in ALLOWED_SUBCOMMANDS -- refusing")
    returncode, stdout_bytes = _run_git_argv(
        ["git", "-C", str(cwd), *args], timeout,
    )
    if returncode is None:
        return GitResult(ok=False, stdout="", returncode=None)
    try:
        stdout = stdout_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return GitResult(ok=False, stdout="", returncode=returncode)
    return GitResult(ok=(returncode == 0), stdout=stdout.strip(), returncode=returncode)


def get_current_branch(checkout_root: Path) -> str | None:
    """None means detached HEAD (or unreadable) -- callers must not
    conflate the two without checking is_detached_head separately if
    the distinction matters to them."""
    r = run_git(["symbolic-ref", "-q", "--short", "HEAD"], checkout_root)
    return r.stdout if r.ok and r.stdout else None


def is_detached_head(checkout_root: Path) -> bool | None:
    """True if HEAD is detached, False if on a branch, None if this
    couldn't be determined at all (not a repo, git missing, etc.) --
    None must never be treated as "not detached" by a caller deciding
    whether it's safe to plan an update."""
    r = run_git(["symbolic-ref", "-q", "HEAD"], checkout_root)
    if r.returncode is None:
        return None  # command itself failed to even run
    return r.returncode != 0


def get_worktree_dirty(checkout_root: Path) -> bool | None:
    """True/False, or None if the read itself failed (never coerced to
    either boolean -- see isadoraair/version_info.py's identical
    get_checkout_identity() precedent for why "unknown" must stay
    distinct from "known clean")."""
    r = run_git(["status", "--porcelain"], checkout_root)
    if not r.ok:
        return None
    return bool(r.stdout)


def get_origin_url(checkout_root: Path) -> str | None:
    r = run_git(["remote", "get-url", "origin"], checkout_root)
    return r.stdout if r.ok and r.stdout else None


def fetch_remote(checkout_root: Path, remote: str = "origin") -> bool:
    """The one operation in this module that writes anything to disk
    -- updates `origin`'s remote-tracking refs and fetches new objects
    into `.git/objects`. Never touches the working tree, the index, or
    any file `git status` would report. Returns False on ANY failure
    (network down, remote unreachable, timeout, no such remote) -- a
    failed fetch must read as "could not check for updates," never as
    "no update available" (a silent false negative would be worse than
    an honest failure -- see docs/UPDATE_CENTER.md)."""
    r = run_git(["fetch", "--quiet", remote], checkout_root, timeout=GIT_FETCH_TIMEOUT_SECONDS)
    return r.ok


def rev_parse(checkout_root: Path, ref: str) -> str | None:
    r = run_git(["rev-parse", "--verify", "-q", ref], checkout_root)
    if not r.ok or not r.stdout:
        return None
    sha = r.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        return None
    return sha


def commit_exists(checkout_root: Path, sha: str) -> bool:
    """Matches deploy/restore/20-application.sh's own proven pattern
    exactly: `git cat-file -e <sha>^{commit}` -- true iff `sha` is a
    real, reachable commit object in this checkout's object database
    (which, after a successful fetch_remote(), includes anything on
    the remote too). `sha` must already look like a real SHA (this
    function does not accept a symbolic ref) -- callers resolve
    symbolic refs via rev_parse() first if needed."""
    if not sha or not all(c in "0123456789abcdef" for c in sha.lower()) or len(sha) not in (40,):
        return False
    r = run_git(["cat-file", "-e", f"{sha}^{{commit}}"], checkout_root)
    return r.ok


def is_ancestor(checkout_root: Path, ancestor_sha: str, descendant_sha: str) -> bool | None:
    """None if the check itself couldn't be performed (missing object,
    not a repo, etc.) -- both True and False are meaningful, valid
    `merge-base --is-ancestor` outcomes (exit 0 / exit 1), so this
    function distinguishes "checked, answer is no" from "could not
    check" by using returncode directly rather than run_git's `ok`."""
    r = run_git(["merge-base", "--is-ancestor", ancestor_sha, descendant_sha], checkout_root)
    if r.returncode is None:
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None  # some other git-level error (bad object, etc.)


def ahead_behind(checkout_root: Path, local_ref: str, remote_ref: str) -> tuple[int, int] | None:
    """(ahead, behind) counts of `local_ref` relative to `remote_ref`,
    or None if either ref can't be resolved. `ahead` = commits on
    local_ref not on remote_ref; `behind` = commits on remote_ref not
    on local_ref -- either or both can be nonzero (a real divergence)."""
    r = run_git(["rev-list", "--left-right", "--count", f"{local_ref}...{remote_ref}"], checkout_root)
    if not r.ok or not r.stdout:
        return None
    parts = r.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def commit_summary(checkout_root: Path, sha: str) -> dict | None:
    """{"subject": str, "author_date_iso": str} for display purposes
    only (the "commit summary/date" §7 asks for) -- never used for any
    decision logic, so a truncated/odd subject line is cosmetic, not a
    correctness risk."""
    r = run_git(["show", "-s", "--format=%s%x1f%aI", sha], checkout_root)
    if not r.ok or "\x1f" not in r.stdout:
        return None
    subject, _, date_iso = r.stdout.partition("\x1f")
    return {"subject": subject.strip()[:300], "author_date_iso": date_iso.strip()}


def path_exists_at_commit(checkout_root: Path, sha: str, relative_path: str) -> bool | None:
    """Whether `relative_path` exists in the tree of `sha` -- checked
    via `git cat-file -e <sha>:<path>`, which never touches the
    working tree (unlike `git show`, no content is even read). None if
    `sha` itself isn't a valid/reachable commit. This is how Phase A
    cross-checks a manifest's claims against its TARGET commit's real
    content without ever checking that commit out -- the working tree
    stays exactly whatever the station currently has checked out the
    entire time."""
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise ValueError(f"relative_path must be repo-relative with no '..': {relative_path!r}")
    if not commit_exists(checkout_root, sha):
        return None
    r = run_git(["cat-file", "-e", f"{sha}:{relative_path}"], checkout_root)
    return r.ok


def read_bytes_at_commit(checkout_root: Path, sha: str, relative_path: str) -> bytes | None:
    """Raw bytes of `relative_path` as it exists in the tree of `sha`
    -- via `git show <sha>:<path>`, still never touching the working
    tree. Returns None if the path doesn't exist at that commit or the
    read otherwise fails. Uses the shared bounded byte-level process
    primitive directly (rather than run_git's UTF-8 text wrapper), so
    hashing cannot be changed by text decoding while the same fixed-
    argv/no-shell/timeout/output bounds still apply."""
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise ValueError(f"relative_path must be repo-relative with no '..': {relative_path!r}")
    returncode, stdout = _run_git_argv(
        ["git", "-C", str(checkout_root), "show", f"{sha}:{relative_path}"],
        GIT_TIMEOUT_SECONDS,
    )
    if returncode != 0:
        return None
    return stdout


def changed_paths_between(checkout_root: Path, before_sha: str, after_sha: str,
                          relative_dir: str) -> tuple[str, ...] | None:
    """Repo-relative changed paths below one directory, without checkout."""
    if relative_dir.startswith("/") or ".." in Path(relative_dir).parts:
        raise ValueError(f"relative_dir must be repo-relative with no '..': {relative_dir!r}")
    if not commit_exists(checkout_root, before_sha) or not commit_exists(checkout_root, after_sha):
        return None
    returncode, stdout = _run_git_argv(
        ["git", "-C", str(checkout_root), "diff", "--name-only", "-z",
         before_sha, after_sha, "--", f"{relative_dir}/"],
        GIT_TIMEOUT_SECONDS,
    )
    if returncode != 0:
        return None
    try:
        paths = tuple(path for path in stdout.decode("utf-8").split("\x00") if path)
    except UnicodeDecodeError:
        return None
    prefix = f"{relative_dir.rstrip('/')}/"
    if any(path.startswith("/") or ".." in Path(path).parts or not path.startswith(prefix)
           for path in paths):
        return None
    return paths


def list_files_at_commit(checkout_root: Path, sha: str, relative_dir: str) -> list[str] | None:
    """Filenames (basenames only, not recursive) directly under
    `relative_dir` in the tree of `sha` -- via `git ls-tree`, still
    never touching the working tree. This is how the release-chain
    reader (release_chain.load_manifest_files_at_ref) discovers
    releases that exist in a fetched commit's history but have never
    been checked out -- a station behind on releases legitimately does
    not have a newer release's manifest file on disk at all; only
    `git fetch` (never checkout) has happened. Returns None if `sha`
    isn't reachable or the directory doesn't exist at that commit
    (an empty list, distinctly, means the directory exists but is
    empty)."""
    if relative_dir.startswith("/") or ".." in Path(relative_dir).parts:
        raise ValueError(f"relative_dir must be repo-relative with no '..': {relative_dir!r}")
    if not commit_exists(checkout_root, sha):
        return None
    # `ls-tree` (not `show`, whose tree-listing format isn't meant for
    # parsing) -- uses the byte-level primitive because -z output must
    # stay exact until it is split.
    returncode, stdout = _run_git_argv(
        ["git", "-C", str(checkout_root), "ls-tree", "--name-only", "-z", sha, "--", f"{relative_dir}/"],
        GIT_TIMEOUT_SECONDS,
    )
    if returncode != 0:
        return None
    try:
        decoded = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    names = [Path(n).name for n in decoded.split("\x00") if n]
    return names


def find_introducing_commit(checkout_root: Path, relative_path: str) -> str | None:
    """The commit that first added `relative_path` (e.g.
    `deploy/releases/r0002.json`) to this repository's history -- the
    ONLY mechanism this codebase uses to associate a non-bootstrap
    release with a git commit (see manifest.py's top docstring for why
    there is deliberately no field for this inside the manifest
    itself). `relative_path` must be a repo-relative path with no
    leading `/` and no `..` component -- validated here, not just
    assumed safe, since a future caller could otherwise be tricked into
    asking git to inspect a path outside the intended `deploy/releases/`
    tree; git itself would just report "no such path" for anything
    outside the repo, but rejecting it explicitly here keeps the
    contract honest rather than relying on git's own leniency.

    Returns None if the path doesn't exist in history, was added more
    than once, OR has any reachable modification/deletion commit after
    introduction. Release manifests are immutable: resolving the
    introducing commit while silently accepting later edits would bind
    the release id to semantics that did not exist at that commit.
    A caller seeing None
    here should treat it as "this release's commit identity could not
    be established," not silently pick one of several candidates.

    Searches `--all` refs (every local branch AND every remote-tracking
    ref), not just HEAD's own ancestry -- a release several commits
    ahead of what's currently checked out is, correctly, only
    reachable via `origin/<branch>` after a fetch (checkout never
    happens in this module), not via bare HEAD. Using `--all` is what
    makes "a station several releases behind" actually resolvable at
    all; scoping to HEAD alone would silently fail to find any release
    newer than what's locally checked out."""
    if relative_path.startswith("/") or ".." in Path(relative_path).parts:
        raise ValueError(f"relative_path must be repo-relative with no '..': {relative_path!r}")
    additions = run_git(
        ["log", "--all", "--diff-filter=A", "--format=%H", "--", relative_path],
        checkout_root,
    )
    history = run_git(
        ["log", "--all", "--format=%H", "--", relative_path],
        checkout_root,
    )
    if not additions.ok or not additions.stdout or not history.ok or not history.stdout:
        return None
    added_shas = [line for line in additions.stdout.splitlines() if line]
    history_shas = [line for line in history.stdout.splitlines() if line]
    if len(added_shas) != 1 or history_shas != added_shas:
        return None
    return added_shas[0]
