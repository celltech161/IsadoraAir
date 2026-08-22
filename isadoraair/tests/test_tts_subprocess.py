"""Real temporary-process tests for the isolated TTS provider boundary."""

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from isadoraair.tts import (
    SynthesisRequest,
    TTSEngine,
    TTSRuntimeUnavailable,
    TTSSynthesisError,
    TTSTimeout,
    TTSVoiceUnavailable,
)
from isadoraair.tts.providers import SubprocessTTSProvider
from isadoraair.tts.service import TTSService
from isadoraair.tts.validation import KOKORO_WAV_REQUIREMENTS


class SubprocessProviderTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)

    def _script(self, name, source):
        path = self.root / name
        path.write_text(source, encoding="utf-8")
        return path

    def _request(self, *, output="final.wav", timeout=3):
        return SynthesisRequest(
            text="private source text",
            engine="kokoro",
            voice="test_voice",
            output_path=self.root / output,
            timeout_seconds=timeout,
        )

    def _provider(self, command_factory):
        return SubprocessTTSProvider(
            engine=TTSEngine.KOKORO,
            command_factory=command_factory,
            cwd=self.root,
            wav_requirements=KOKORO_WAV_REQUIREMENTS,
        )

    def test_dispatcher_uses_only_temporary_runtime_and_assets(self):
        model = self.root / "test-model.onnx"
        voices = self.root / "test-voices.bin"
        model.write_bytes(b"model placeholder")
        voices.write_bytes(b"voices placeholder")
        script = self._script(
            "fake_runtime.py",
            """import pathlib, sys, wave
output, model, voices = map(pathlib.Path, sys.argv[1:4])
assert model.is_file() and voices.is_file()
assert sys.stdin.read() == "private source text"
with wave.open(str(output), "wb") as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
    wav.writeframes(b"\\x00\\x00" * 8)
""",
        )
        provider = self._provider(
            lambda _request, output: [sys.executable, script, output, model, voices]
        )
        request = self._request()
        result = TTSService({TTSEngine.KOKORO: provider}).synthesize(request)
        self.assertEqual(result, request.output_path)
        self.assertTrue(result.is_file())

    def test_missing_runtime_fails_clearly(self):
        provider = self._provider(lambda _request, _output: [self.root / "missing-python"])
        with self.assertRaisesRegex(TTSRuntimeUnavailable, "runtime executable is unavailable"):
            TTSService({TTSEngine.KOKORO: provider}).synthesize(self._request())

    def test_nonzero_exit_is_synthesis_error_and_source_text_is_not_logged(self):
        script = self._script(
            "fail.py",
            "import sys; data=sys.stdin.read(); sys.stderr.write(data); raise SystemExit(7)\n",
        )
        provider = self._provider(lambda _request, _output: [sys.executable, script])
        with self.assertRaises(TTSSynthesisError) as raised:
            TTSService({TTSEngine.KOKORO: provider}).synthesize(self._request())
        message = str(raised.exception)
        self.assertIn("status 7", message)
        self.assertIn("diagnostic output suppressed", message)
        self.assertNotIn("private source text", message)

    def test_huge_error_output_is_drained_but_bounded_and_suppressed(self):
        script = self._script(
            "huge_error.py",
            "import sys; sys.stderr.write('SENSITIVE_PAYLOAD' * 100000); raise SystemExit(9)\n",
        )
        provider = self._provider(lambda _request, _output: [sys.executable, script])
        with self.assertRaises(TTSSynthesisError) as raised:
            TTSService({TTSEngine.KOKORO: provider}).synthesize(self._request())
        message = str(raised.exception)
        self.assertLess(len(message), 200)
        self.assertNotIn("SENSITIVE_PAYLOAD", message)
        self.assertRegex(message, r"suppressed \([1-9][0-9]+ bytes\)")

    def test_structured_safe_error_maps_to_voice_category(self):
        script = self._script(
            "structured_error.py",
            """import json, sys
payload=json.dumps({"category":"voice_unavailable","message":"logical voice is unavailable"})
sys.stderr.write("ISADORAAIR_TTS_ERROR:" + payload + "\\n")
raise SystemExit(12)
""",
        )
        provider = self._provider(lambda _request, _output: [sys.executable, script])
        with self.assertRaisesRegex(TTSVoiceUnavailable, "logical voice is unavailable"):
            TTSService({TTSEngine.KOKORO: provider}).synthesize(self._request())

    def test_structured_error_message_cannot_inject_log_lines(self):
        script = self._script(
            "structured_control_error.py",
            """import json, sys
payload=json.dumps({"category":"voice_unavailable","message":"first line\\nforged log\\tentry"})
sys.stderr.write("ISADORAAIR_TTS_ERROR:" + payload + "\\n")
raise SystemExit(12)
""",
        )
        provider = self._provider(lambda _request, _output: [sys.executable, script])
        with self.assertRaises(TTSVoiceUnavailable) as raised:
            TTSService({TTSEngine.KOKORO: provider}).synthesize(self._request())
        self.assertEqual(str(raised.exception), "first line forged log entry")

    def test_timeout_terminates_process_group_and_cleans_output(self):
        pid_file = self.root / "pids.txt"
        script = self._script(
            "hang.py",
            """import os, pathlib, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
pathlib.Path(sys.argv[1]).write_text(f"{os.getpid()} {child.pid}")
time.sleep(30)
""",
        )
        provider = self._provider(lambda _request, _output: [sys.executable, script, pid_file])
        request = self._request(timeout=0.2)
        with self.assertRaisesRegex(TTSTimeout, "exceeded"):
            TTSService({TTSEngine.KOKORO: provider}).synthesize(request)

        parent_pid, child_pid = (int(value) for value in pid_file.read_text().split())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            live_states = []
            for pid in (parent_pid, child_pid):
                stat_path = Path(f"/proc/{pid}/stat")
                if stat_path.exists():
                    live_states.append(stat_path.read_text().split()[2])
            if not live_states or all(state == "Z" for state in live_states):
                break
            time.sleep(0.05)
        else:
            self.fail("timed-out provider process group remained alive")
        self.assertFalse(request.output_path.exists())
        self.assertEqual(list(self.root.glob("*.tts.tmp.wav")), [])

    def test_provider_environment_does_not_inherit_arbitrary_secrets(self):
        observed = self.root / "observed.txt"
        script = self._script(
            "environment.py",
            """import os, pathlib, sys, wave
output, observed = map(pathlib.Path, sys.argv[1:3])
observed.write_text(str('TTS_TEST_SECRET' in os.environ))
with wave.open(str(output), "wb") as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(24000)
    wav.writeframes(b"\\x00\\x00" * 8)
""",
        )
        provider = self._provider(
            lambda _request, output: [sys.executable, script, output, observed]
        )
        with patch.dict(os.environ, {"TTS_TEST_SECRET": "do-not-pass"}):
            TTSService({TTSEngine.KOKORO: provider}).synthesize(self._request())
        self.assertEqual(observed.read_text(), "False")

    def test_provider_module_discovery_is_dispatcher_owned_from_arbitrary_cwd(self):
        provider = SubprocessTTSProvider(
            engine=TTSEngine.PIPER,
            command_factory=lambda request, output: (
                sys.executable,
                "-m",
                "isadoraair.tts.provider_cli",
                "--engine",
                "piper",
                "--voice",
                request.voice,
                "--output-file",
                output,
            ),
            cwd=self.root,
            module_root=Path(__file__).resolve().parents[2],
            wav_requirements=KOKORO_WAV_REQUIREMENTS,
        )
        request = SynthesisRequest(
            text="private source text",
            engine="piper",
            voice="test_voice",
            output_path=self.root / "final.wav",
        )

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(TTSRuntimeUnavailable, "not productized yet"):
                provider.synthesize(request, self.root / "provider-output.wav")

        self.assertFalse(request.output_path.exists())
