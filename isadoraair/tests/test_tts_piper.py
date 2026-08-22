"""Optional Piper provider tests using only tiny temporary fake assets."""

import hashlib
import json
import stat
import tempfile
import wave
from dataclasses import replace
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.tts import (
    SynthesisRequest,
    TTSEngine,
    TTSOutputValidationError,
    TTSRuntimeUnavailable,
    TTSService,
    TTSVoiceUnavailable,
)
from isadoraair.tts.providers import PiperTTSProvider, PiperVoiceSpec


def _sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PiperProviderTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = self.root / "test-medium.onnx"
        self.config = self.root / "test-medium.onnx.json"
        self.model.write_bytes(b"tiny fake model")
        self.config.write_text(
            json.dumps({"audio": {"sample_rate": 22050}, "language": {"code": "en_US"}}),
            encoding="utf-8",
        )
        self.runtime = self.root / "fake-piper"
        self.runtime.write_text(
            """#!/usr/bin/env python3
import argparse, json, wave
p = argparse.ArgumentParser()
p.add_argument('--model')
p.add_argument('--config')
p.add_argument('--output-file')
p.add_argument('--length-scale', type=float)
a = p.parse_args()
with open(a.config, encoding='utf-8') as source:
    rate = json.load(source)['audio']['sample_rate']
with wave.open(a.output_file, 'wb') as output:
    output.setnchannels(1)
    output.setsampwidth(2)
    output.setframerate(rate)
    output.writeframes(b'\\0\\0' * max(1, int(100 * a.length_scale)))
""",
            encoding="utf-8",
        )
        self.runtime.chmod(self.runtime.stat().st_mode | stat.S_IXUSR)
        self.spec = PiperVoiceSpec(
            model_id="test-model",
            model_filename=self.model.name,
            config_filename=self.config.name,
            model_sha256=_sha256(self.model),
            config_sha256=_sha256(self.config),
            language="en-us",
            sample_rate_hz=22050,
        )

    def _provider(self, spec=None, executable=None):
        return PiperTTSProvider(
            executable=str(executable or self.runtime),
            asset_root=self.root,
            voices=(spec or self.spec,),
        )

    def _request(self, *, voice="test-model", speed=1.0, language="en-us"):
        return SynthesisRequest(
            text="Fake Piper synthesis",
            engine="piper",
            voice=voice,
            output_path=self.root / "result.wav",
            speed=speed,
            language=language,
            timeout_seconds=5,
        )

    def test_optional_piper_absent_and_unselected_is_valid(self):
        class Kokoro:
            wav_requirements = type("Requirements", (), {
                "channels": 1, "sample_width_bytes": 2, "sample_rate_hz": 24000,
                "minimum_sample_rate_hz": 8000, "maximum_sample_rate_hz": 192000,
            })()

            def synthesize(inner_self, request, output_path):
                with wave.open(str(output_path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(24000)
                    output.writeframes(b"\0\0" * 10)

        request = SynthesisRequest(
            text="Kokoro only", engine="kokoro", voice="voice", output_path=self.root / "k.wav"
        )
        self.assertTrue(TTSService({TTSEngine.KOKORO: Kokoro()}).synthesize(request).is_file())

    def test_selected_piper_runtime_missing(self):
        with self.assertRaisesRegex(TTSRuntimeUnavailable, "runtime executable"):
            self._provider(executable=self.root / "absent").synthesize(
                self._request(), self.root / "temporary.wav"
            )

    def test_selected_piper_model_or_config_missing(self):
        for missing in (self.model, self.config):
            with self.subTest(missing=missing.name):
                original = missing.read_bytes()
                missing.unlink()
                with self.assertRaisesRegex(TTSVoiceUnavailable, "assets are unavailable"):
                    self._provider().synthesize(self._request(), self.root / "temporary.wav")
                missing.write_bytes(original)

    def test_model_config_pairing_rejects_path_and_wrong_sidecar(self):
        for spec in (
            replace(self.spec, model_filename="../test-medium.onnx"),
            replace(self.spec, config_filename="other.onnx.json"),
        ):
            with self.subTest(spec=spec):
                with self.assertRaisesRegex(TTSVoiceUnavailable, "invalid"):
                    self._provider(spec).synthesize(self._request(), self.root / "temporary.wav")

    def test_model_sha_mismatch(self):
        with self.assertRaisesRegex(TTSVoiceUnavailable, "model checksum"):
            self._provider(replace(self.spec, model_sha256="0" * 64)).synthesize(
                self._request(), self.root / "temporary.wav"
            )

    def test_config_sha_mismatch(self):
        with self.assertRaisesRegex(TTSVoiceUnavailable, "config checksum"):
            self._provider(replace(self.spec, config_sha256="0" * 64)).synthesize(
                self._request(), self.root / "temporary.wav"
            )

    def test_successful_fake_piper_synthesis_is_validated_at_native_rate(self):
        result = TTSService({TTSEngine.PIPER: self._provider()}).synthesize(self._request())
        with wave.open(str(result), "rb") as output:
            self.assertEqual(output.getframerate(), 22050)
            self.assertEqual(output.getnchannels(), 1)
            self.assertEqual(output.getsampwidth(), 2)

    def test_speed_multiplier_is_inverse_piper_length_scale(self):
        result = TTSService({TTSEngine.PIPER: self._provider()}).synthesize(
            self._request(speed=2.0)
        )
        with wave.open(str(result), "rb") as output:
            self.assertEqual(output.getnframes(), 50)

    def test_language_is_model_bound(self):
        with self.assertRaisesRegex(TTSVoiceUnavailable, "does not support language"):
            self._provider().synthesize(
                self._request(language="en-gb"), self.root / "temporary.wav"
            )

    def test_provider_appropriate_output_validation(self):
        self.config.write_text(
            json.dumps({"audio": {"sample_rate": 24000}, "language": {"code": "en_US"}}),
            encoding="utf-8",
        )
        spec = replace(self.spec, config_sha256=_sha256(self.config))
        with self.assertRaisesRegex(TTSVoiceUnavailable, "sample-rate metadata"):
            TTSService({TTSEngine.PIPER: self._provider(spec)}).synthesize(self._request())

    def test_invalid_generated_rate_is_rejected_without_forcing_kokoro_rate(self):
        bad_runtime = self.root / "bad-piper"
        bad_runtime.write_text(self.runtime.read_text().replace("rate = json.load(source)['audio']['sample_rate']", "rate = 24000"))
        bad_runtime.chmod(self.runtime.stat().st_mode)
        with self.assertRaisesRegex(TTSOutputValidationError, "expected 22050"):
            TTSService(
                {TTSEngine.PIPER: self._provider(executable=bad_runtime)}
            ).synthesize(self._request())

    def test_piper_failure_does_not_fail_over_to_kokoro(self):
        class NeverCalled:
            wav_requirements = None

            def __init__(inner_self):
                inner_self.called = False

            def synthesize(inner_self, request, output_path):
                inner_self.called = True

        kokoro = NeverCalled()
        provider = self._provider(replace(self.spec, model_sha256="0" * 64))
        with self.assertRaises(TTSVoiceUnavailable):
            TTSService({TTSEngine.KOKORO: kokoro, TTSEngine.PIPER: provider}).synthesize(
                self._request()
            )
        self.assertFalse(kokoro.called)
