"""weather_ingest/lib/delivery.py's deliver() cross-venv boundary --
P1 2.4 Pass G correction: a non-fatal provenance-write warning from
publish_weather_asset (surfaced on stderr, see that command's own
docstring) must be visibly logged by the Weather-side caller, without
turning a successful-but-degraded publish into a raised exception.
delivery.py has no Django/config-bridge dependency of its own -- it's
plain stdlib + subprocess -- so this needs none of the import-time
config-patching ceremony other weather_ingest test modules require."""
import sys
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import delivery  # noqa: E402


def _completed(returncode=0, stdout="", stderr=""):
    return CompletedProcess(
        args=["manage.py", "publish_weather_asset"], returncode=returncode, stdout=stdout, stderr=stderr,
    )


class ProvenanceWarningVisibilityTests(unittest.TestCase):
    def test_successful_publish_with_provenance_warning_is_logged_not_raised(self):
        completed = _completed(
            stdout="Created track 1 (WxTemp/current_temp.mp3).\n",
            stderr="WARNING: provenance write failed (publish still succeeded): disk full\n",
        )
        with patch.object(delivery.subprocess, "run", return_value=completed) as mock_run:
            with self.assertLogs("delivery", level="WARNING") as captured:
                dest = delivery.deliver(
                    "/tmp/candidate.mp3", "WxTemp", "current_temp.mp3",
                    producer="current_temp.py", voice="Claira_Sky",
                )

        mock_run.assert_called_once()
        self.assertEqual(dest, delivery.LIBRARY_ROOT / "WxTemp" / "current_temp.mp3")
        self.assertTrue(any("provenance" in message.lower() for message in captured.output))
        self.assertTrue(any("disk full" in message for message in captured.output))

    def test_clean_success_with_no_stderr_logs_nothing(self):
        completed = _completed(stdout="Created track 1 (WxTemp/current_temp.mp3).\n", stderr="")
        with patch.object(delivery.subprocess, "run", return_value=completed):
            with self.assertNoLogs("delivery", level="WARNING"):
                dest = delivery.deliver("/tmp/candidate.mp3", "WxTemp", "current_temp.mp3")

        self.assertEqual(dest, delivery.LIBRARY_ROOT / "WxTemp" / "current_temp.mp3")

    def test_publication_failure_still_raises_and_does_not_swallow_the_error(self):
        """Provenance warnings are forwarded non-fatally, but an actual
        publish FAILURE (non-zero exit) must still raise -- this
        correction must not accidentally widen non-fatal handling to
        real failures."""
        import subprocess as _subprocess

        error = _subprocess.CalledProcessError(
            returncode=1, cmd=["manage.py", "publish_weather_asset"],
            output="", stderr="CommandError: candidate is not a file\n",
        )
        with patch.object(delivery.subprocess, "run", side_effect=error):
            with self.assertRaises(_subprocess.CalledProcessError):
                delivery.deliver("/tmp/missing.mp3", "WxTemp", "current_temp.mp3")

    def test_non_weather_caller_omits_provenance_kwargs_without_error(self):
        completed = _completed(stdout="Created track 1 (Something/file.mp3).\n", stderr="")
        with patch.object(delivery.subprocess, "run", return_value=completed) as mock_run:
            dest = delivery.deliver("/tmp/candidate.mp3", "Something", "file.mp3")
        self.assertEqual(dest, delivery.LIBRARY_ROOT / "Something" / "file.mp3")
        args = mock_run.call_args.args[0]
        self.assertNotIn("--producer", args)
        self.assertNotIn("--voice", args)
        self.assertNotIn("--alert-family", args)


if __name__ == "__main__":
    unittest.main()
