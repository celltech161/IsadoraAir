"""Optional peak ceiling for generated announcements (Dedication intros): the default conversion is untouched,
the opt-in limiter changes peaks only (native mono/rate preserved, no gain, no time shift), and only the Web
Requests dedication feature opts in. Real ffmpeg on synthetic audio; no TTS and no station media."""
import re
import shutil
import struct
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import skipUnless
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from isadoraair.announcements import (
    AnnouncementRenderer,
    AnnouncementTrackMetadata,
    RotationAssetAnnouncement,
    SpeechSpliceAnnouncement,
)
from isadoraair.announcements import renderer as renderer_module
from library.models import Artist, Category, CategoryKind

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True)


def _hot_mono_wav(path, seconds=4, amplitude=1.0, boost_db=3):
    """24 kHz mono 16-bit WAV with peaks driven to full scale -- what an over-peaking TTS intro looks like."""
    boost = f",volume={boost_db}dB,alimiter=limit=0.999:level=false:latency=1" if boost_db else ""
    _run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"anoisesrc=d={seconds}:c=pink:r=24000:a={amplitude}:seed=3,tremolo=f=2:d=0.6{boost}",
         "-ac", "1", "-sample_fmt", "s16", str(path))


def _measure(path):
    """Integrated LUFS and true peak of the file as the pipeline plays it: a mono file is duplicated at unity
    into both channels, so measure ``pan=stereo|c0=c0|c1=c0`` (not ffmpeg's power-preserving upmix)."""
    text = _run("ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af",
                "aresample=44100,pan=stereo|c0=c0|c1=c0,ebur128=peak=true", "-f", "null", "-").stderr
    summary = text[text.rindex("Summary:"):]
    return (float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1)),
            float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1)))


def _samples(path):
    raw = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path), "-f", "s16le", "-"],
                         check=True, capture_output=True).stdout
    return struct.unpack(f"<{len(raw) // 2}h", raw)


class ConvertCommandTests(SimpleTestCase):
    def test_default_conversion_command_is_exactly_what_it_always_was(self):
        with patch.object(renderer_module.subprocess, "run") as run:
            renderer_module._convert_wav_to_flac(Path("/tmp/in.wav"), Path("/tmp/out.flac"))
        self.assertEqual(run.call_args.args[0], ["ffmpeg", "-y", "-i", "/tmp/in.wav", "/tmp/out.flac"])
        self.assertEqual(run.call_args.kwargs, {"check": True, "timeout": 30, "capture_output": True})

    def test_ceiling_adds_only_a_limiter_with_no_gain_and_no_time_shift(self):
        with patch.object(renderer_module.subprocess, "run") as run:
            renderer_module._convert_wav_to_flac(Path("/tmp/in.wav"), Path("/tmp/out.flac"), peak_ceiling=0.84)
        argv = run.call_args.args[0]
        chain = argv[argv.index("-af") + 1]
        self.assertEqual(chain, "alimiter=limit=0.84:level=false:latency=1")
        self.assertNotIn("volume", chain)
        self.assertNotIn("-ar", argv)       # native rate
        self.assertNotIn("-ac", argv)       # native channels (mono stays mono)
        self.assertEqual(argv[argv.index("-sample_fmt") + 1], "s16")

    def test_invalid_ceilings_are_refused_before_ffmpeg_runs(self):
        for bad in (0, -0.5, 1.5, float("nan"), "0.84", True):
            with self.subTest(ceiling=bad), patch.object(renderer_module.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    renderer_module._convert_wav_to_flac(Path("/tmp/in.wav"), Path("/tmp/out.flac"), peak_ceiling=bad)
                run.assert_not_called()


@skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe are required")
class MeasuredLimiterTests(SimpleTestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.wav = self.dir / "in.wav"
        _hot_mono_wav(self.wav)

    def test_limiter_brings_peaks_under_the_ceiling_and_keeps_level_and_format(self):
        plain, limited = self.dir / "plain.flac", self.dir / "limited.flac"
        renderer_module._convert_wav_to_flac(self.wav, plain)
        renderer_module._convert_wav_to_flac(self.wav, limited, peak_ceiling=0.84)

        plain_lufs, plain_tp = _measure(plain)
        limited_lufs, limited_tp = _measure(limited)
        self.assertGreaterEqual(plain_tp - limited_tp, 0.5)    # the limiter took the peaks down at least 0.5 dB
        self.assertLessEqual(limited_tp, -1.0)                 # the station's -1 dBTP target
        self.assertLess(abs(limited_lufs - plain_lufs), 1.5)   # a peak safeguard, not a level change

        info = _run("ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels,sample_fmt",
                    "-of", "default=nw=1", str(limited)).stdout
        for expected in ("codec_name=flac", "sample_rate=24000", "channels=1", "sample_fmt=s16"):
            self.assertIn(expected, info)

    def test_limiter_does_not_change_duration_or_alignment(self):
        plain, limited = self.dir / "plain.flac", self.dir / "limited.flac"
        renderer_module._convert_wav_to_flac(self.wav, plain)
        renderer_module._convert_wav_to_flac(self.wav, limited, peak_ceiling=0.84)
        a, b = _samples(plain), _samples(limited)
        self.assertEqual(len(a), len(b))                       # cue markers come from this duration
        # time-aligned (latency=1): a one-sample shift would show here
        n = len(a)
        best = min(range(-2, 3), key=lambda s: sum((a[i] - b[i + s]) ** 2 for i in range(4000, n - 4000, 7)))
        self.assertEqual(best, 0)

    def test_quiet_speech_is_not_touched_at_all(self):
        quiet = self.dir / "quiet.wav"
        _hot_mono_wav(quiet, amplitude=0.1, boost_db=0)
        plain, limited = self.dir / "qp.flac", self.dir / "ql.flac"
        renderer_module._convert_wav_to_flac(quiet, plain)
        renderer_module._convert_wav_to_flac(quiet, limited, peak_ceiling=0.84)
        a, b = _samples(plain), _samples(limited)
        self.assertLessEqual(max(abs(x - y) for x, y in zip(a, b)), 1)


class SpecWiringTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artist = Artist.objects.create(name="Generated Voice")
        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        self.category = Category.objects.create(code="Generated", name="Generated", kind=kind)

    def _spec(self, cls, **extra):
        return cls(
            text="A listener-facing announcement.", logical_voice="Station_Dave",
            destination=self.root / "x.flac", timeout_seconds=17,
            metadata=AnnouncementTrackMetadata(title="T", artist=self.artist, category_code="Generated", ready2air=True),
            **extra,
        )

    def _render(self, spec):
        calls = []

        def convert(source, destination, **kwargs):
            calls.append(kwargs)
            Path(destination).write_bytes(b"FLAC")

        with patch.object(renderer_module, "synthesize_station_voice",
                          side_effect=lambda text, *, voice, output_path, timeout_seconds, **_: Path(output_path).write_bytes(b"W")), \
             patch.object(renderer_module, "_convert_wav_to_flac", side_effect=convert), \
             patch.object(renderer_module, "_probe_duration", return_value=3.0), \
             patch.object(renderer_module, "_analyze_track", return_value=True), \
             patch.object(renderer_module, "_set_speech_markers"):
            AnnouncementRenderer().render(spec)
        return calls

    def test_default_specs_never_pass_a_ceiling(self):
        for cls in (SpeechSpliceAnnouncement, RotationAssetAnnouncement):
            with self.subTest(cls=cls.__name__):
                self.assertEqual(self._render(self._spec(cls)), [{}])
                self.assertIsNone(self._spec(cls).peak_ceiling)

    def test_a_ceiling_reaches_the_conversion(self):
        self.assertEqual(self._render(self._spec(SpeechSpliceAnnouncement, peak_ceiling=0.84)),
                         [{"peak_ceiling": 0.84}])
