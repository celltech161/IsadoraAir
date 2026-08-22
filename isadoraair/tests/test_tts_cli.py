"""Stable external and internal-provider TTS CLI contract tests."""

import io
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from isadoraair.tts import TTSRuntimeUnavailable
from isadoraair.tts import cli, provider_cli
from isadoraair.tts.errors import TTSExitCode


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _RecordingService:
    def __init__(self, error=None):
        self.error = error
        self.requests = []

    def synthesize(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return request.output_path


class StableCliTests(SimpleTestCase):
    def test_repo_launcher_discovers_cli_from_arbitrary_working_directory(self):
        launcher = PROJECT_ROOT / "deploy" / "isadoraair-tts"
        with tempfile.TemporaryDirectory() as unrelated_cwd:
            environment = {
                name: os.environ[name]
                for name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TZ")
                if name in os.environ
            }
            result = subprocess.run(
                [launcher, "--help"],
                cwd=unrelated_cwd,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )

        self.assertEqual(result.returncode, TTSExitCode.SUCCESS, result.stderr)
        self.assertIn("usage: isadoraair-tts", result.stdout)
        self.assertNotIn("PYTHONPATH", environment)

    def test_reads_stdin_and_builds_engine_neutral_request(self):
        service = _RecordingService()
        stderr = io.StringIO()
        result = cli.main(
            [
                "--engine", "kokoro",
                "--voice", "test_voice",
                "--output-file", "/tmp/test-output.wav",
                "--speed", "1.2",
                "--language", "en-gb",
                "--timeout", "9",
            ],
            stdin=io.StringIO("Text from stdin"),
            stderr=stderr,
            service=service,
        )
        self.assertEqual(result, TTSExitCode.SUCCESS)
        self.assertEqual(stderr.getvalue(), "")
        request = service.requests[0]
        self.assertEqual(request.text, "Text from stdin")
        self.assertEqual(request.engine.value, "kokoro")
        self.assertEqual(request.voice, "test_voice")
        self.assertEqual(request.speed, 1.2)
        self.assertEqual(request.language, "en-gb")
        self.assertEqual(request.timeout_seconds, 9)

    def test_piper_compatible_voice_and_output_aliases(self):
        service = _RecordingService()
        result = cli.main(
            [
                "--engine", "kokoro",
                "--model", "test_voice",
                "--output_file", "/tmp/test-output.wav",
                "--lang", "en-us",
            ],
            stdin=io.StringIO("Text"),
            stderr=io.StringIO(),
            service=service,
        )
        self.assertEqual(result, TTSExitCode.SUCCESS)
        self.assertEqual(service.requests[0].voice, "test_voice")

    def test_empty_input_returns_stable_configuration_status(self):
        stderr = io.StringIO()
        result = cli.main(
            ["--engine", "kokoro", "--voice", "test_voice", "--output-file", "/tmp/out.wav"],
            stdin=io.StringIO(" \n"),
            stderr=stderr,
            service=_RecordingService(),
        )
        self.assertEqual(result, TTSExitCode.CONFIGURATION)
        self.assertEqual(stderr.getvalue(), "isadoraair-tts: configuration: input text is empty\n")

    def test_runtime_error_has_deterministic_status_and_safe_stderr(self):
        stderr = io.StringIO()
        result = cli.main(
            ["--engine", "kokoro", "--voice", "test_voice", "--output-file", "/tmp/out.wav"],
            stdin=io.StringIO("source text must not appear"),
            stderr=stderr,
            service=_RecordingService(TTSRuntimeUnavailable("runtime executable is unavailable")),
        )
        self.assertEqual(result, TTSExitCode.RUNTIME_UNAVAILABLE)
        self.assertEqual(
            stderr.getvalue(),
            "isadoraair-tts: runtime_unavailable: runtime executable is unavailable\n",
        )
        self.assertNotIn("source text", stderr.getvalue())

    def test_invalid_engine_is_usage_error(self):
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            cli.main(
                ["--engine", "unknown", "--voice", "voice", "--output-file", "/tmp/out.wav"],
                stdin=io.StringIO("Text"),
                stderr=stderr,
                service=_RecordingService(),
            )
        self.assertEqual(raised.exception.code, TTSExitCode.USAGE)
        self.assertIn("invalid choice", stderr.getvalue())


class ProviderCliTests(SimpleTestCase):
    def test_kokoro_worker_reuses_foundation_a_provider(self):
        stderr = io.StringIO()
        with patch("isadoraair.tts.provider_cli.KokoroSynthesizer") as synthesizer:
            result = provider_cli.main(
                [
                    "--engine", "kokoro",
                    "--voice", "test_voice",
                    "--output-file", "/tmp/provider-output.wav",
                    "--speed", "0.9",
                    "--language", "en-us",
                ],
                stdin=io.StringIO("  Text  "),
                stderr=stderr,
            )
        self.assertEqual(result, TTSExitCode.SUCCESS)
        synthesizer.return_value.synthesize.assert_called_once_with(
            "Text",
            voice="test_voice",
            output_path="/tmp/provider-output.wav",
            speed=0.9,
            lang="en-us",
        )
        source = Path(provider_cli.__file__).read_text(encoding="utf-8")
        self.assertNotIn("preprocess_text", source)
        self.assertNotIn("_DECIMAL_POINT_RE", source)

    def test_worker_empty_input_is_structured_and_deterministic(self):
        stderr = io.StringIO()
        result = provider_cli.main(
            ["--engine", "kokoro", "--voice", "voice", "--output-file", "/tmp/out.wav"],
            stdin=io.StringIO(""),
            stderr=stderr,
        )
        self.assertEqual(result, TTSExitCode.CONFIGURATION)
        self.assertIn('"category":"configuration"', stderr.getvalue())
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_worker_piper_path_is_deliberately_not_productized(self):
        stderr = io.StringIO()
        result = provider_cli.main(
            ["--engine", "piper", "--voice", "voice", "--output-file", "/tmp/out.wav"],
            stdin=io.StringIO("Text"),
            stderr=stderr,
        )
        self.assertEqual(result, TTSExitCode.RUNTIME_UNAVAILABLE)
        self.assertIn("not productized yet", stderr.getvalue())
