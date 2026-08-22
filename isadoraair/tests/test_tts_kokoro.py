"""Isolated tests for the repository-owned Kokoro provider."""

import io
import struct
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from isadoraair.tts import kokoro


class _FakeKokoroRuntime:
    def __init__(self, audio=None, sample_rate=24000):
        self.audio = audio if audio is not None else [-2.0, -0.5, 0.0, 0.5, 2.0]
        self.sample_rate = sample_rate
        self.calls = []

    def create(self, text, *, voice, speed, lang):
        self.calls.append({"text": text, "voice": voice, "speed": speed, "lang": lang})
        return self.audio, self.sample_rate


class KokoroSynthesizerTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.model = self.root / "model.onnx"
        self.voices = self.root / "voices.bin"
        self.model.write_bytes(b"test model placeholder")
        self.voices.write_bytes(b"test voices placeholder")

    def test_uses_explicit_runtime_and_asset_locations(self):
        runtime = _FakeKokoroRuntime()
        factory_calls = []

        def factory(model_path, voices_path):
            factory_calls.append((model_path, voices_path))
            return runtime

        output = self.root / "nested" / "speech.wav"
        synthesizer = kokoro.KokoroSynthesizer(
            model_path=self.model,
            voices_path=self.voices,
            runtime_factory=factory,
        )
        with patch("isadoraair.tts.kokoro._encode_pcm_s16", return_value=b"\x00\x00"):
            result = synthesizer.synthesize(
                "  Water is 20.2 feet. #DISPLAY_ONLY  ",
                voice="test_voice",
                output_path=output,
                speed=0.9,
                lang="en-us",
            )

        self.assertEqual(result, output.absolute())
        self.assertEqual(factory_calls, [(str(self.model), str(self.voices))])
        self.assertEqual(
            runtime.calls,
            [{
                "text": "Water is 20 point 2 feet.",
                "voice": "test_voice",
                "speed": 0.9,
                "lang": "en-us",
            }],
        )

    def test_wav_semantics_match_production(self):
        runtime = _FakeKokoroRuntime()
        output = self.root / "speech.wav"
        synthesizer = kokoro.KokoroSynthesizer(
            model_path=self.model,
            voices_path=self.voices,
            runtime_factory=lambda _model, _voices: runtime,
        )
        pcm = struct.pack("<5h", -32767, -16383, 0, 16383, 32767)
        with patch("isadoraair.tts.kokoro._encode_pcm_s16", return_value=pcm):
            synthesizer.synthesize("Test", voice="test_voice", output_path=output)

        with wave.open(str(output), "rb") as wav_file:
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.getframerate(), 24000)
            self.assertEqual(wav_file.getnframes(), 5)
            samples = struct.unpack("<5h", wav_file.readframes(5))
        self.assertEqual(samples, (-32767, -16383, 0, 16383, 32767))

    def test_missing_model_fails_before_runtime_creation(self):
        factory_called = False

        def factory(_model, _voices):
            nonlocal factory_called
            factory_called = True

        synthesizer = kokoro.KokoroSynthesizer(
            model_path=self.root / "missing.onnx",
            voices_path=self.voices,
            runtime_factory=factory,
        )
        with self.assertRaisesRegex(kokoro.KokoroSynthesisError, "model file is missing"):
            synthesizer.synthesize("Test", voice="test_voice", output_path=self.root / "out.wav")
        self.assertFalse(factory_called)

    def test_missing_voices_database_fails_before_runtime_creation(self):
        synthesizer = kokoro.KokoroSynthesizer(
            model_path=self.model,
            voices_path=self.root / "missing.bin",
            runtime_factory=lambda _model, _voices: self.fail("runtime must not be created"),
        )
        with self.assertRaisesRegex(kokoro.KokoroSynthesisError, "voices database file is missing"):
            synthesizer.synthesize("Test", voice="test_voice", output_path=self.root / "out.wav")

    def test_empty_text_fails_with_production_message(self):
        synthesizer = kokoro.KokoroSynthesizer(
            model_path=self.model,
            voices_path=self.voices,
            runtime_factory=lambda _model, _voices: self.fail("runtime must not be created"),
        )
        with self.assertRaisesRegex(kokoro.KokoroSynthesisError, "no text on stdin"):
            synthesizer.synthesize(" \n ", voice="test_voice", output_path=self.root / "out.wav")

    def test_text_removed_by_normalization_is_still_forwarded_like_production(self):
        runtime = _FakeKokoroRuntime()
        synthesizer = kokoro.KokoroSynthesizer(
            model_path=self.model,
            voices_path=self.voices,
            runtime_factory=lambda _model, _voices: runtime,
        )
        with patch("isadoraair.tts.kokoro._encode_pcm_s16", return_value=b"\x00\x00"):
            synthesizer.synthesize(
                "#DISPLAY_ONLY",
                voice="test_voice",
                output_path=self.root / "out.wav",
            )
        self.assertEqual(runtime.calls[0]["text"], "")

    def test_default_paths_come_from_component_contract(self):
        expected_model, expected_voices = kokoro.canonical_kokoro_paths()
        synthesizer = kokoro.KokoroSynthesizer()
        self.assertEqual(synthesizer.model_path, expected_model)
        self.assertEqual(synthesizer.voices_path, expected_voices)
        self.assertNotIn("/home/", str(expected_model))
        self.assertNotIn("/home/", str(expected_voices))

    def test_module_does_not_require_cpu_affinity_or_fixed_thread_count(self):
        source = Path(kokoro.__file__).read_text(encoding="utf-8")
        self.assertNotIn("taskset", source)
        self.assertNotIn("sched_setaffinity", source)
        self.assertNotIn("OMP_NUM_THREADS", source)
        self.assertNotIn("intra_op_num_threads = 4", source)


class KokoroCliTests(SimpleTestCase):
    def test_empty_stdin_preserves_production_cli_error(self):
        with self.assertRaises(SystemExit) as raised:
            kokoro.main(
                ["--model", "test_voice", "--output_file", "/tmp/not-created.wav"],
                stdin=io.StringIO(" \n"),
            )
        self.assertEqual(str(raised.exception), "kokoro_synth: no text on stdin")

    def test_cli_forwards_canonical_arguments_and_path_overrides(self):
        with patch("isadoraair.tts.kokoro.KokoroSynthesizer") as synthesizer_class:
            result = kokoro.main(
                [
                    "--model", "test_voice",
                    "--output_file", "/tmp/test-output.wav",
                    "--speed", "1.25",
                    "--lang", "en-gb",
                    "--model-path", "/staged/model.onnx",
                    "--voices-path", "/staged/voices.bin",
                ],
                stdin=io.StringIO("  Text to speak.  "),
            )

        self.assertEqual(result, 0)
        synthesizer_class.assert_called_once_with(
            model_path="/staged/model.onnx", voices_path="/staged/voices.bin"
        )
        synthesizer_class.return_value.synthesize.assert_called_once_with(
            "Text to speak.",
            voice="test_voice",
            output_path="/tmp/test-output.wav",
            speed=1.25,
            lang="en-gb",
        )
