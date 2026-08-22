"""Stable external and internal-provider TTS CLI contract tests."""

import io
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TransactionTestCase

from isadoraair.tts import TTSConfigurationError, TTSRuntimeUnavailable
from isadoraair.tts import cli, provider_cli
from isadoraair.tts.errors import TTSExitCode
from isadoraair.tts.models import PiperVoiceModel, StationTTSVoice


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _RecordingStationService:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def synthesize(self, text, **kwargs):
        self.calls.append((text, kwargs))
        if self.error is not None:
            raise self.error
        return Path(kwargs["output_path"])


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
        self.assertIn("--voice", result.stdout)
        self.assertNotIn("--engine", result.stdout)
        self.assertNotIn("--model", result.stdout)
        self.assertNotIn("PYTHONPATH", environment)

    def test_logical_voice_mode_does_not_require_engine_or_provider_paths(self):
        station_service = _RecordingStationService()
        result = cli.main(
            ["--voice", "weather-day", "--output-file", "/tmp/logical.wav", "--timeout", "45"],
            stdin=io.StringIO("Weather text"),
            stderr=io.StringIO(),
            station_service=station_service,
        )
        self.assertEqual(result, TTSExitCode.SUCCESS)
        text, options = station_service.calls[0]
        self.assertEqual(text, "Weather text")
        self.assertEqual(options["voice"], "weather-day")
        self.assertEqual(options["timeout_seconds"], 45)
        self.assertIsNone(options["speed"])
        self.assertIsNone(options["language"])

    def test_reads_stdin_and_forwards_logical_request_options(self):
        station_service = _RecordingStationService()
        stderr = io.StringIO()
        result = cli.main(
            [
                "--voice", "logical-voice",
                "--output-file", "/tmp/test-output.wav",
                "--speed", "1.2",
                "--language", "en-gb",
                "--timeout", "9",
            ],
            stdin=io.StringIO("Text from stdin"),
            stderr=stderr,
            station_service=station_service,
        )
        self.assertEqual(result, TTSExitCode.SUCCESS)
        self.assertEqual(stderr.getvalue(), "")
        text, options = station_service.calls[0]
        self.assertEqual(text, "Text from stdin")
        self.assertEqual(options["voice"], "logical-voice")
        self.assertEqual(options["speed"], 1.2)
        self.assertEqual(options["language"], "en-gb")
        self.assertEqual(options["timeout_seconds"], 9)

    def test_provider_native_and_legacy_alias_flags_are_not_public(self):
        invocations = (
            ["--engine", "kokoro", "--voice", "logical", "--output-file", "/tmp/out.wav"],
            ["--model", "native", "--output-file", "/tmp/out.wav"],
            ["--voice", "logical", "--output_file", "/tmp/out.wav"],
            ["--voice", "logical", "--output-file", "/tmp/out.wav", "--lang", "en-us"],
        )
        for invocation in invocations:
            with self.subTest(invocation=invocation):
                with self.assertRaises(SystemExit) as raised:
                    cli.main(invocation, stdin=io.StringIO("Text"), stderr=io.StringIO())
                self.assertEqual(raised.exception.code, TTSExitCode.USAGE)

    def test_empty_input_returns_stable_configuration_status(self):
        stderr = io.StringIO()
        result = cli.main(
            ["--voice", "test-voice", "--output-file", "/tmp/out.wav"],
            stdin=io.StringIO(" \n"),
            stderr=stderr,
            station_service=_RecordingStationService(TTSConfigurationError("input text is empty")),
        )
        self.assertEqual(result, TTSExitCode.CONFIGURATION)
        self.assertEqual(stderr.getvalue(), "isadoraair-tts: configuration: input text is empty\n")

    def test_runtime_error_has_deterministic_status_and_safe_stderr(self):
        stderr = io.StringIO()
        result = cli.main(
            ["--voice", "test-voice", "--output-file", "/tmp/out.wav"],
            stdin=io.StringIO("source text must not appear"),
            stderr=stderr,
            station_service=_RecordingStationService(
                TTSRuntimeUnavailable("runtime executable is unavailable")
            ),
        )
        self.assertEqual(result, TTSExitCode.RUNTIME_UNAVAILABLE)
        self.assertEqual(
            stderr.getvalue(),
            "isadoraair-tts: runtime_unavailable: runtime executable is unavailable\n",
        )
        self.assertNotIn("source text", stderr.getvalue())



class ExternalLogicalCliIntegrationTests(TransactionTestCase):
    """Exercise the real launcher, Django setup, and test database from another CWD."""

    def setUp(self):
        super().setUp()
        StationTTSVoice.objects.create(
            name="cli-kokoro",
            enabled=True,
            engine="kokoro",
            provider_voice="native-kokoro",
        )
        piper_model = PiperVoiceModel.objects.create(
            model_id="cli-piper-model",
            model_filename="cli-piper.onnx",
            config_filename="cli-piper.onnx.json",
            model_sha256="1" * 64,
            config_sha256="2" * 64,
            language="en-us",
            sample_rate_hz=22050,
        )
        StationTTSVoice.objects.create(
            name="cli-piper",
            enabled=True,
            engine="piper",
            piper_model=piper_model,
        )
        StationTTSVoice.objects.create(
            name="cli-disabled",
            enabled=False,
            engine="kokoro",
            provider_voice="native-disabled",
        )

    def _environment(self):
        database = connection.settings_dict
        environment = {
            name: os.environ[name]
            for name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TZ")
            if name in os.environ
        }
        environment.update({
            "DEBUG": "True",
            "DB_NAME": str(database["NAME"]),
            "DB_USER": str(database.get("USER") or ""),
            "DB_PASSWORD": str(database.get("PASSWORD") or ""),
            "DB_HOST": str(database.get("HOST") or ""),
            "DB_PORT": str(database.get("PORT") or ""),
        })
        environment.pop("PYTHONPATH", None)
        return environment

    def _invoke(self, voice, cwd):
        return subprocess.run(
            [
                PROJECT_ROOT / "deploy" / "isadoraair-tts",
                "--voice", voice,
                "--output-file", str(Path(cwd) / f"{voice}.wav"),
            ],
            input="External logical CLI test",
            cwd=cwd,
            env=self._environment(),
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_enabled_logical_kokoro_and_piper_reach_their_canonical_providers(self):
        with tempfile.TemporaryDirectory() as unrelated_cwd:
            for voice in ("cli-kokoro", "cli-piper"):
                with self.subTest(voice=voice):
                    result = self._invoke(voice, unrelated_cwd)
                    self.assertEqual(result.returncode, TTSExitCode.RUNTIME_UNAVAILABLE, result.stderr)
                    self.assertIn("runtime_unavailable", result.stderr)
                    self.assertNotIn("not configured", result.stderr)
                    self.assertNotIn("PYTHONPATH", self._environment())

    def test_missing_and_disabled_logical_voices_fail_before_provider_dispatch(self):
        with tempfile.TemporaryDirectory() as unrelated_cwd:
            missing = self._invoke("cli-missing", unrelated_cwd)
            disabled = self._invoke("cli-disabled", unrelated_cwd)
        self.assertEqual(missing.returncode, TTSExitCode.VOICE_UNAVAILABLE)
        self.assertIn("not configured", missing.stderr)
        self.assertEqual(disabled.returncode, TTSExitCode.VOICE_UNAVAILABLE)
        self.assertIn("disabled", disabled.stderr)


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
