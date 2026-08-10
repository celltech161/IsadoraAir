"""encoders/services/preflight.py -- Phase 2D/2E dependency + static
syntax preflight. subprocess.run and shutil.which are mocked
throughout for the failure-mode tests (missing binary, timeout,
nonzero exit) -- REAL `liquidsoap --check` is exercised separately in
LiquidsoapCheckRealSyntaxTests, skipped cleanly where liquidsoap isn't
installed, matching this project's established convention (see
BuildLiquidsoapScriptRealSyntaxTests in test_encoder_manager.py).
Nothing in this file ever opens a live ALSA device -- every real
liquidsoap invocation here uses --check only."""
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from encoders.models import Encoder
from encoders.services import preflight


def make_encoder(**overrides):
    defaults = dict(
        name="test", enabled=True, protocol="shoutcast2",
        host="192.168.1.112", port=8000, mount="/4", username="source",
        password="secret", format="mp3", bitrate_kbps=192,
        station_name="Test Station", genre="", url="", public=False,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


# ---------------------------------------------------------------------
# check_dependencies
# ---------------------------------------------------------------------
class CheckDependenciesTests(SimpleTestCase):
    def setUp(self):
        # Point candidate/LKG dirs at a real, writable temp location so
        # the writability checks pass by default -- individual tests
        # override this where they need the unwritable case.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        base = Path(self._tmpdir.name)
        for patcher in (
            patch("encoders.services.preflight.lkg.CANDIDATE_DIR", base / "candidate"),
            patch("encoders.services.preflight.lkg.LKG_DIR", base / "lkg"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_liquidsoap_present_mp3_only_ok(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight, "_is_executable", return_value=True):
            result = preflight.check_dependencies([make_encoder(format="mp3")])
        self.assertTrue(result.ok)

    def test_liquidsoap_missing_fails(self):
        with patch.object(preflight.shutil, "which", return_value=None):
            result = preflight.check_dependencies([make_encoder()])
        self.assertFalse(result.ok)
        self.assertIn("liquidsoap", result.reason)

    def test_liquidsoap_not_executable_fails(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight, "_is_executable", return_value=False):
            result = preflight.check_dependencies([make_encoder()])
        self.assertFalse(result.ok)
        self.assertIn("not executable", result.reason)

    def test_fdkaac_missing_with_aac_configured_fails(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight, "_is_executable", side_effect=lambda p: p == "/usr/bin/liquidsoap"), \
             patch.object(preflight.Path, "is_file", return_value=False):
            result = preflight.check_dependencies([make_encoder(format="aac")])
        self.assertFalse(result.ok)
        self.assertIn("fdkaac", result.reason)

    def test_fdkaac_missing_with_mp3_only_config_does_not_fail(self):
        """The critical distinction: a codec check must only fire for
        a codec the CANDIDATE actually uses -- an MP3-only candidate
        must never be rejected over fdkaac."""
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight, "_is_executable", return_value=True), \
             patch.object(preflight.Path, "is_file", return_value=False):
            result = preflight.check_dependencies([make_encoder(format="mp3")])
        self.assertTrue(result.ok)

    def test_fdkaac_present_and_executable_with_aac_ok(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight, "_is_executable", return_value=True), \
             patch.object(preflight.Path, "is_file", return_value=True):
            result = preflight.check_dependencies([make_encoder(format="aac")])
        self.assertTrue(result.ok)

    def test_candidate_directory_unwritable_fails(self):
        unwritable_root = Path(self._tmpdir.name) / "locked"
        unwritable_root.mkdir(mode=0o500)
        self.addCleanup(lambda: unwritable_root.chmod(0o700))  # so TemporaryDirectory cleanup can remove it
        with patch("encoders.services.preflight.lkg.CANDIDATE_DIR", unwritable_root / "candidate"), \
             patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight, "_is_executable", return_value=True):
            result = preflight.check_dependencies([make_encoder(format="mp3")])
        self.assertFalse(result.ok)
        self.assertIn("candidate directory", result.reason)

    def test_multiple_problems_all_listed(self):
        with patch.object(preflight.shutil, "which", return_value=None), \
             patch.object(preflight.Path, "is_file", return_value=False):
            result = preflight.check_dependencies([make_encoder(format="aac")])
        self.assertFalse(result.ok)
        self.assertGreaterEqual(len(result.detail["problems"]), 2)


# ---------------------------------------------------------------------
# check_liquidsoap_syntax -- mocked subprocess
# ---------------------------------------------------------------------
class CheckLiquidsoapSyntaxMockedTests(SimpleTestCase):
    def test_success_exit_zero(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [make_encoder()])
        self.assertTrue(result.ok)
        self.assertEqual(result.detail["exit_code"], 0)

    def test_syntax_failure_nonzero_exit(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", return_value=MagicMock(returncode=1, stdout="", stderr="Parse error")):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [make_encoder()])
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["exit_code"], 1)
        self.assertIn("Parse error", result.detail["stderr"])

    def test_timeout_treated_as_failure(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="liquidsoap", timeout=20)):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [make_encoder()])
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.reason)
        self.assertTrue(result.detail["timed_out"])

    def test_missing_binary_treated_as_failure(self):
        with patch.object(preflight.shutil, "which", return_value=None):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [make_encoder()])
        self.assertFalse(result.ok)
        self.assertIn("not found", result.reason)

    def test_oserror_on_launch_treated_as_failure(self):
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", side_effect=OSError("fork failed")):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [make_encoder()])
        self.assertFalse(result.ok)

    def test_check_uses_bounded_timeout(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", mock_run):
            preflight.check_liquidsoap_syntax("/tmp/x.liq", [make_encoder()])
        self.assertEqual(mock_run.call_args.kwargs["timeout"], preflight.LIQUIDSOAP_CHECK_TIMEOUT_SECONDS)

    def test_check_command_uses_check_flag_and_script_path(self):
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", mock_run):
            preflight.check_liquidsoap_syntax("/tmp/candidate.liq", [make_encoder()])
        called_cmd = mock_run.call_args.args[0]
        self.assertEqual(called_cmd, ["liquidsoap", "--check", "/tmp/candidate.liq"])

    def test_password_redacted_from_stderr(self):
        enc = make_encoder(password="s3cr3tPassw0rd")
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", return_value=MagicMock(
                 returncode=1, stdout="", stderr='Error near password="s3cr3tPassw0rd"',
             )):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [enc])
        self.assertNotIn("s3cr3tPassw0rd", result.detail["stderr"])
        self.assertIn("REDACTED", result.detail["stderr"])

    def test_password_redacted_from_stdout_too(self):
        enc = make_encoder(password="hunter2")
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", return_value=MagicMock(
                 returncode=0, stdout="debug: hunter2 was used", stderr="",
             )):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", [enc])
        self.assertNotIn("hunter2", result.detail["stdout"])

    def test_multiple_encoders_all_passwords_redacted(self):
        encs = [make_encoder(name="a", password="passA"), make_encoder(name="b", password="passB")]
        with patch.object(preflight.shutil, "which", return_value="/usr/bin/liquidsoap"), \
             patch.object(preflight.subprocess, "run", return_value=MagicMock(
                 returncode=1, stdout="", stderr="passA and passB both appear here",
             )):
            result = preflight.check_liquidsoap_syntax("/tmp/x.liq", encs)
        self.assertNotIn("passA", result.detail["stderr"])
        self.assertNotIn("passB", result.detail["stderr"])


# ---------------------------------------------------------------------
# run_preflight -- short-circuit ordering
# ---------------------------------------------------------------------
class RunPreflightTests(SimpleTestCase):
    def test_dependency_failure_short_circuits_before_syntax_check(self):
        mock_syntax_check = MagicMock()
        with patch.object(preflight, "check_dependencies", return_value=preflight.PreflightResult(ok=False, reason="no liquidsoap")), \
             patch.object(preflight, "check_liquidsoap_syntax", mock_syntax_check):
            result = preflight.run_preflight("/tmp/x.liq", [make_encoder()])
        self.assertFalse(result.ok)
        mock_syntax_check.assert_not_called()

    def test_dependency_success_proceeds_to_syntax_check(self):
        with patch.object(preflight, "check_dependencies", return_value=preflight.PreflightResult(ok=True)), \
             patch.object(preflight, "check_liquidsoap_syntax", return_value=preflight.PreflightResult(ok=True, reason="syntax ok")) as mock_syntax_check:
            result = preflight.run_preflight("/tmp/x.liq", [make_encoder()])
        self.assertTrue(result.ok)
        mock_syntax_check.assert_called_once()


# ---------------------------------------------------------------------
# Real liquidsoap --check -- skipped cleanly if not installed
# ---------------------------------------------------------------------
class LiquidsoapCheckRealSyntaxTests(SimpleTestCase):
    def test_real_valid_script_passes(self):
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "valid.liq"
            script_path.write_text('source = blank()\noutput.dummy(source)\n', encoding="utf-8")
            result = preflight.check_liquidsoap_syntax(str(script_path), [])
        self.assertTrue(result.ok, result.detail)

    def test_real_invalid_script_fails(self):
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "invalid.liq"
            script_path.write_text('this is not valid liquidsoap syntax @@@ ####\n', encoding="utf-8")
            result = preflight.check_liquidsoap_syntax(str(script_path), [])
        self.assertFalse(result.ok)
        self.assertEqual(result.detail["exit_code"], 1)

    def test_real_check_never_opens_alsa_device(self):
        """The absolute safety property this whole module exists to
        guarantee -- a script containing the REAL airtap input.alsa
        line must return quickly (not hang trying to open/compete for
        the device) regardless of whether that device is currently in
        use by the live encoder."""
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        with tempfile.TemporaryDirectory() as tmpdir:
            script_path = Path(tmpdir) / "airtap.liq"
            script_path.write_text(
                'source = input.alsa(buffer_size=1.0, device="airtap")\n'
                'source = blank.detect(threshold=-40.0, max_blank=20.0, min_noise=0.5, source)\n'
                'output.dummy(source)\n',
                encoding="utf-8",
            )
            import time
            start = time.time()
            result = preflight.check_liquidsoap_syntax(str(script_path), [])
            elapsed = time.time() - start
        self.assertTrue(result.ok, result.detail)
        self.assertLess(elapsed, 5.0, "liquidsoap --check took too long -- may have tried to actually open the device")


# ---------------------------------------------------------------------
# Real generated candidate script, real preflight, end to end
# ---------------------------------------------------------------------
class RealCandidatePreflightIntegrationTests(SimpleTestCase):
    def test_real_generated_script_passes_full_preflight(self):
        if shutil.which("liquidsoap") is None:
            self.skipTest("liquidsoap not installed on this box")
        from encoders.services.encoder_manager import build_liquidsoap_script

        with tempfile.TemporaryDirectory() as tmpdir:
            candidate_dir = Path(tmpdir) / "candidate"
            lkg_dir = Path(tmpdir) / "lkg"
            enc = make_encoder(format="mp3")
            script = build_liquidsoap_script("airtap", [enc], generation="preflighttest")
            script_path = candidate_dir / "airtap_test.liq"
            candidate_dir.mkdir(parents=True)
            lkg_dir.mkdir(parents=True)
            script_path.write_text(script, encoding="utf-8")
            with patch("encoders.services.preflight.lkg.CANDIDATE_DIR", candidate_dir), \
                 patch("encoders.services.preflight.lkg.LKG_DIR", lkg_dir):
                result = preflight.run_preflight(script_path, [enc])
        self.assertTrue(result.ok, result.detail)
