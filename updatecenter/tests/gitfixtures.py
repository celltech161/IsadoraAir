"""Shared test fixture: real, throwaway git repositories in a temp
directory -- matches ARCHITECTURE_REPORT.md's own test-strategy
recommendation ("a throwaway local bare repo + working checkout...
proves the fetch/plan/checkout logic without touching GitHub or the
real checkout at all"). Deliberately NOT named test_*.py so Django's
test discovery never tries to collect this as a test module itself."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


def _run(args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


class FakeRepo:
    """A real bare 'origin' repo plus a real working clone, both under
    one temp directory, torn down automatically. `.work` is the
    checkout tests point planner/git_adapter functions at; `.origin`
    is what `git fetch` in tests actually talks to (a plain local
    filesystem path, no network involved)."""

    def __init__(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="updatecenter-fakerepo-")
        base = Path(self._tmpdir.name)
        self.origin = base / "origin.git"
        self.work = base / "work"

        _run(["init", "--bare", "-q", "-b", "main", str(self.origin)], base)
        _run(["clone", "-q", str(self.origin), str(self.work)], base)
        _run(["config", "user.email", "test@example.invalid"], self.work)
        _run(["config", "user.name", "Test"], self.work)
        # An empty repo has no HEAD to check out / commit onto in some
        # git versions' clone-of-empty-bare behavior -- make one real
        # commit immediately so every fixture starts from a known-good
        # "clean, on main, up to date with origin" state.
        (self.work / "README.md").write_text("fixture\n", encoding="utf-8")
        self.commit("initial", push=True)

    def close(self):
        self._tmpdir.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, relative_path: str, content: str):
        path = self.work / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def commit(self, message: str, push: bool = True) -> str:
        _run(["add", "-A"], self.work)
        _run(["commit", "-q", "-m", message, "--allow-empty"], self.work)
        if push:
            _run(["push", "-q", "origin", "HEAD:main"], self.work)
        return self.rev_parse("HEAD")

    def rev_parse(self, ref: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.work), "rev-parse", ref],
            check=True, capture_output=True, text=True,
        )
        return result.stdout.strip()

    def checkout_detached(self, ref: str):
        _run(["checkout", "-q", "--detach", ref], self.work)

    def checkout_branch(self, branch: str):
        _run(["checkout", "-q", branch], self.work)

    def reset_local_to(self, sha: str):
        """Moves `self.work`'s local HEAD (and working tree) back to
        an earlier commit WITHOUT touching origin -- simulates "this
        station's checkout hasn't caught up to a release that already
        exists in history" (e.g. was already pushed by a prior
        `commit(push=True)` call). Deliberately named differently from
        anything git_adapter.py exposes: this is test-fixture-only
        history manipulation of a THROWAWAY repo, not something the
        read-only production adapter (which forbids `reset` outright)
        would ever be asked to do."""
        _run(["reset", "-q", "--hard", sha], self.work)

    def dirty_untracked(self, relative_path: str = "untracked.txt"):
        (self.work / relative_path).write_text("dirty\n", encoding="utf-8")

    def diverge_origin(self, message: str = "origin-only commit"):
        """Push a commit to origin from a SECOND clone, without ever
        pulling it into self.work -- gives self.work a genuine
        ahead/behind divergence against origin/main once combined with
        a local-only commit in self.work itself."""
        second = self.origin.parent / "second-clone"
        _run(["clone", "-q", str(self.origin), str(second)], self.origin.parent)
        _run(["config", "user.email", "test@example.invalid"], second)
        _run(["config", "user.name", "Test"], second)
        (second / "origin-only.txt").write_text("x\n", encoding="utf-8")
        _run(["add", "-A"], second)
        _run(["commit", "-q", "-m", message], second)
        _run(["push", "-q", "origin", "HEAD:main"], second)
