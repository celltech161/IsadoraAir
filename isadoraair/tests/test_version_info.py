"""isadoraair/version_info.py -- 1.7 release/version-skew visibility.

Every test that exercises the git-shelling paths mocks subprocess.run
directly -- nothing here depends on this checkout's own git history/
dirty state (which would make the suite flaky as the repo evolves), with
the single exception of RealRepoSmokeTests, which intentionally runs
against the REAL project checkout to confirm the plumbing actually works
end-to-end against a real `git` binary (read-only: rev-parse/status
only, never mutates anything)."""
import subprocess
import time
from unittest.mock import patch

from django.test import SimpleTestCase

from isadoraair import version_info


class _ResetModuleStateMixin:
    """Every public entry point in version_info.py reads or writes
    module-level state (the checkout cache, the web-runtime-commit
    holder) that must not leak between tests -- these are all imported
    once at process start, and a mutation in one test would otherwise
    silently affect every test that runs after it in the same process."""

    def setUp(self):
        super().setUp()
        self._orig_checkout_cache = dict(version_info._checkout_cache)
        self._orig_web_holder = dict(version_info._web_runtime_commit_holder)
        version_info._checkout_cache["value"] = None
        version_info._checkout_cache["computed_at"] = 0.0
        version_info._web_runtime_commit_holder["captured"] = False
        version_info._web_runtime_commit_holder["value"] = None

    def tearDown(self):
        version_info._checkout_cache.clear()
        version_info._checkout_cache.update(self._orig_checkout_cache)
        version_info._web_runtime_commit_holder.clear()
        version_info._web_runtime_commit_holder.update(self._orig_web_holder)
        super().tearDown()


def _completed(stdout="", returncode=0):
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr="")


class RunGitTests(_ResetModuleStateMixin, SimpleTestCase):
    def test_fixed_argument_list_never_shell(self):
        """Security requirement: -C PROJECT_ROOT, exact args, no
        shell=True, bounded timeout -- and never anything derived from
        caller input (there is none accepted here)."""
        with patch("isadoraair.version_info.subprocess.run", return_value=_completed("abc\n")) as mock_run:
            result = version_info._run_git("rev-parse", "HEAD")
        self.assertEqual(result, "abc")
        mock_run.assert_called_once_with(
            ["git", "-C", str(version_info.PROJECT_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=version_info.GIT_TIMEOUT_SECONDS, check=False,
        )

    def test_nonzero_exit_returns_none(self):
        with patch("isadoraair.version_info.subprocess.run", return_value=_completed("", returncode=128)):
            self.assertIsNone(version_info._run_git("rev-parse", "HEAD"))

    def test_oserror_returns_none(self):
        """Missing git binary, or any other OS-level launch failure."""
        with patch("isadoraair.version_info.subprocess.run", side_effect=OSError("not found")):
            self.assertIsNone(version_info._run_git("rev-parse", "HEAD"))

    def test_timeout_returns_none(self):
        with patch("isadoraair.version_info.subprocess.run",
                    side_effect=subprocess.TimeoutExpired(cmd="git", timeout=3)):
            self.assertIsNone(version_info._run_git("rev-parse", "HEAD"))

    def test_strips_stdout(self):
        with patch("isadoraair.version_info.subprocess.run", return_value=_completed("  abc123  \n")):
            self.assertEqual(version_info._run_git("rev-parse", "HEAD"), "abc123")

    def test_unexpected_subprocess_layer_exception_returns_none(self):
        """Regression test for a real failure hit during this feature's
        own full-suite validation: running many real git subprocess
        calls back-to-back (each manager class's __init__ now calls
        capture_runtime_commit() once per construction, and the
        existing test suite constructs several of them hundreds of
        times) intermittently raised a bare ValueError out of
        subprocess.run()'s own internals -- not OSError or
        TimeoutExpired, so it escaped the narrower except clause this
        function originally had, contradicting this module's own
        "fails safe to unknown EVERYWHERE" promise. Any subprocess-
        layer exception, not just the two originally anticipated types,
        must resolve to None."""
        with patch("isadoraair.version_info.subprocess.run",
                    side_effect=ValueError("not enough values to unpack (expected 2, got 0)")):
            self.assertIsNone(version_info._run_git("rev-parse", "HEAD"))


class ComputeCheckoutIdentityTests(_ResetModuleStateMixin, SimpleTestCase):
    def test_clean_tree(self):
        with patch("isadoraair.version_info._run_git", side_effect=["f" * 40, ""]):
            identity = version_info._compute_checkout_identity()
        self.assertEqual(identity, {"commit": "f" * 40, "short_commit": "f" * 7, "dirty": False})

    def test_dirty_tree(self):
        with patch("isadoraair.version_info._run_git", side_effect=["a" * 40, " M some/file.py\n"]):
            identity = version_info._compute_checkout_identity()
        self.assertEqual(identity["dirty"], True)
        self.assertEqual(identity["commit"], "a" * 40)

    def test_never_exposes_porcelain_output(self):
        """Only the derived boolean crosses out of this function -- the
        raw `git status --porcelain` text (filenames) must never appear
        anywhere in the returned dict."""
        with patch("isadoraair.version_info._run_git", side_effect=["a" * 40, " M secret/path.py\n"]):
            identity = version_info._compute_checkout_identity()
        self.assertNotIn("secret/path.py", str(identity))

    def test_git_unavailable_fails_safe(self):
        with patch("isadoraair.version_info._run_git", return_value=None):
            identity = version_info._compute_checkout_identity()
        self.assertEqual(identity, {"commit": None, "short_commit": None, "dirty": None})

    def test_status_failure_leaves_dirty_none_but_commit_populated(self):
        """dirty can independently be None (rev-parse worked, status
        didn't) while commit/short_commit are still populated -- callers
        must check dirty separately, never assume it's set just because
        commit is."""
        with patch("isadoraair.version_info._run_git", side_effect=["b" * 40, None]):
            identity = version_info._compute_checkout_identity()
        self.assertEqual(identity["commit"], "b" * 40)
        self.assertIsNone(identity["dirty"])


class GetCheckoutIdentityCachingTests(_ResetModuleStateMixin, SimpleTestCase):
    def test_second_call_within_window_uses_cache(self):
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": "x", "short_commit": "x", "dirty": False}) as mock_compute:
            first = version_info.get_checkout_identity()
            second = version_info.get_checkout_identity()
        self.assertEqual(mock_compute.call_count, 1)
        self.assertIs(first, second)

    def test_force_refresh_bypasses_cache(self):
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": "x", "short_commit": "x", "dirty": False}) as mock_compute:
            version_info.get_checkout_identity()
            version_info.get_checkout_identity(force_refresh=True)
        self.assertEqual(mock_compute.call_count, 2)

    def test_cache_expires_after_window(self):
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": "x", "short_commit": "x", "dirty": False}) as mock_compute:
            with patch("isadoraair.version_info.time.monotonic", return_value=1000.0):
                version_info.get_checkout_identity()
            with patch("isadoraair.version_info.time.monotonic",
                        return_value=1000.0 + version_info.CHECKOUT_CACHE_SECONDS + 1):
                version_info.get_checkout_identity()
        self.assertEqual(mock_compute.call_count, 2)

    def test_within_window_boundary_still_cached(self):
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": "x", "short_commit": "x", "dirty": False}) as mock_compute:
            with patch("isadoraair.version_info.time.monotonic", return_value=1000.0):
                version_info.get_checkout_identity()
            with patch("isadoraair.version_info.time.monotonic",
                        return_value=1000.0 + version_info.CHECKOUT_CACHE_SECONDS - 1):
                version_info.get_checkout_identity()
        self.assertEqual(mock_compute.call_count, 1)


class CaptureRuntimeCommitTests(_ResetModuleStateMixin, SimpleTestCase):
    def test_returns_commit_from_fresh_compute(self):
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": "deadbeef", "short_commit": "deadbee", "dirty": False}):
            self.assertEqual(version_info.capture_runtime_commit(), "deadbeef")

    def test_git_unavailable_returns_none(self):
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": None, "short_commit": None, "dirty": None}):
            self.assertIsNone(version_info.capture_runtime_commit())

    def test_does_not_participate_in_checkout_cache(self):
        """capture_runtime_commit() must NEVER read or write the
        module-level checkout cache -- each call re-shells to git fresh
        (the caller's own discipline of calling it exactly once is what
        gives the result its "fixed for this process's lifetime"
        meaning, not any caching here)."""
        with patch("isadoraair.version_info._compute_checkout_identity",
                    return_value={"commit": "c1", "short_commit": "c1", "dirty": False}) as mock_compute:
            version_info.capture_runtime_commit()
            version_info.capture_runtime_commit()
        self.assertEqual(mock_compute.call_count, 2)
        self.assertIsNone(version_info._checkout_cache["value"])


class CaptureWebRuntimeCommitTests(_ResetModuleStateMixin, SimpleTestCase):
    def test_first_call_captures(self):
        with patch("isadoraair.version_info.capture_runtime_commit", return_value="abc123"):
            result = version_info.capture_web_runtime_commit()
        self.assertEqual(result, "abc123")
        self.assertEqual(version_info.get_web_runtime_commit(), "abc123")

    def test_second_call_is_idempotent_stable_value(self):
        """Simulates the autoreloader-under-runserver scenario this
        idempotent guard exists for: a second call in the same process
        must NOT overwrite an already-fixed value with a later,
        different one."""
        with patch("isadoraair.version_info.capture_runtime_commit", side_effect=["first-sha", "second-sha"]):
            first = version_info.capture_web_runtime_commit()
            second = version_info.capture_web_runtime_commit()
        self.assertEqual(first, "first-sha")
        self.assertEqual(second, "first-sha")
        self.assertEqual(version_info.get_web_runtime_commit(), "first-sha")

    def test_get_web_runtime_commit_none_before_any_capture(self):
        """The realistic case for `manage.py runserver`/management
        commands/tests -- none of them import wsgi.py, so this must
        stay None rather than crash or fabricate a value."""
        self.assertIsNone(version_info.get_web_runtime_commit())


class ShortCommitTests(SimpleTestCase):
    def test_truncates_to_seven(self):
        self.assertEqual(version_info.short_commit("a" * 40), "a" * 7)

    def test_none_for_falsy(self):
        self.assertIsNone(version_info.short_commit(None))
        self.assertIsNone(version_info.short_commit(""))


class RealRepoSmokeTests(_ResetModuleStateMixin, SimpleTestCase):
    """No mocking -- runs the actual `git` binary against the real
    project checkout, read-only (rev-parse/status), to confirm the
    plumbing genuinely works end-to-end. Deliberately loose assertions
    (format/consistency only) since the real repo's exact commit/dirty
    state will vary run to run."""

    def test_get_checkout_identity_returns_well_formed_result(self):
        identity = version_info.get_checkout_identity(force_refresh=True)
        self.assertIsNotNone(identity["commit"])
        self.assertEqual(len(identity["commit"]), 40)
        self.assertEqual(identity["short_commit"], identity["commit"][:7])
        self.assertIn(identity["dirty"], (True, False))

    def test_capture_runtime_commit_matches_checkout(self):
        """Both read the same real HEAD at (nearly) the same instant --
        must agree in a real, unmocked run."""
        checkout = version_info.get_checkout_identity(force_refresh=True)
        runtime = version_info.capture_runtime_commit()
        self.assertEqual(runtime, checkout["commit"])
