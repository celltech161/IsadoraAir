"""Calibrated FLAC encode for generated Weather speech (lib/speech_audio.py): the gain table and the real
ffmpeg chain, measured -- EBU R128 integrated loudness and true peak, container format, duration and lead-in.

The audio is synthetic (modulated pink noise made by ffmpeg itself): no TTS, no station media."""
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import speech_audio  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _run(*argv):
    return subprocess.run(argv, check=True, capture_output=True, text=True)


def _synthetic_wav(path, amplitude, seconds=5):
    """24 kHz mono 16-bit WAV, the shape the TTS engine hands the producers."""
    _run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         f"anoisesrc=d={seconds}:c=pink:r=24000:a={amplitude}:seed=7,tremolo=f=3:d=0.7",
         "-ac", "1", "-sample_fmt", "s16", str(path))


def _loudness(path):
    """(integrated LUFS, true peak dBTP) of the decoded file."""
    text = _run("ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-af", "ebur128=peak=true",
                "-f", "null", "-").stderr
    summary = text[text.rindex("Summary:"):]
    integrated = float(re.search(r"I:\s+(-?[\d.]+) LUFS", summary).group(1))
    peak = float(re.search(r"Peak:\s+(-?[\d.]+) dBFS", summary).group(1))
    return integrated, peak


def _pcm(path, seconds=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path)]
    if seconds is not None:
        cmd += ["-t", str(seconds)]
    cmd += ["-f", "s16le", "-"]
    return subprocess.run(cmd, check=True, capture_output=True).stdout


class GainTableTests(unittest.TestCase):
    def test_every_product_has_a_calibrated_gain_for_both_announcers(self):
        for voice in ("Max_Weatherly", "Claira_Sky"):
            for product in speech_audio.PRODUCTS:
                with self.subTest(voice=voice, product=product):
                    gain = speech_audio.gain_db_for(product, {"logical_voice": voice})
                    self.assertGreater(gain, 0.0)
                    self.assertLess(gain, 12.0)

    def test_announcers_are_within_a_decibel_of_each_other(self):
        for product in speech_audio.PRODUCTS:
            a = speech_audio.gain_db_for(product, {"logical_voice": "Max_Weatherly"})
            b = speech_audio.gain_db_for(product, {"logical_voice": "Claira_Sky"})
            self.assertLessEqual(abs(a - b), 1.0)

    def test_temp_is_quieter_than_the_other_products(self):
        voice = {"logical_voice": "Max_Weatherly"}
        self.assertLess(speech_audio.gain_db_for("temp", voice), speech_audio.gain_db_for("obs", voice))

    def test_uncalibrated_voice_uses_the_default_row_and_warns(self):
        with self.assertLogs(speech_audio.log, level="WARNING") as captured:
            gain = speech_audio.gain_db_for("forecast", {"logical_voice": "Somebody_New"})
        self.assertEqual(gain, speech_audio.DEFAULT_GAIN_DB["forecast"])
        self.assertIn("Somebody_New", captured.output[0])

    def test_missing_voice_uses_the_default_row(self):
        with self.assertLogs(speech_audio.log, level="WARNING"):
            self.assertEqual(speech_audio.gain_db_for("alert", None), speech_audio.DEFAULT_GAIN_DB["alert"])

    def test_unknown_product_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            speech_audio.gain_db_for("traffic", {"logical_voice": "Max_Weatherly"})

    def test_every_table_row_is_complete_and_sane(self):
        for voice, row in speech_audio.VOICE_GAIN_DB.items():
            with self.subTest(voice=voice):
                self.assertEqual(set(row), set(speech_audio.PRODUCTS))
                for gain in row.values():
                    self.assertTrue(0.0 <= gain <= 12.0)

    def test_filter_chain_order_and_parameters(self):
        chain = speech_audio.filter_chain(6.0)
        stages = chain.split(",")
        self.assertEqual(stages[0], "aresample=44100")
        self.assertIn("channel_layouts=stereo", stages[1])
        self.assertEqual(stages[2], "volume=6.000dB")
        # latency=1 keeps the audio time-aligned; level=false stops the limiter's automatic make-up gain.
        self.assertEqual(stages[3], "alimiter=limit=0.84:level=false:latency=1")
        self.assertEqual(stages[4], "adelay=1000:all=1")

    def test_absurd_gain_is_refused(self):
        for gain in (-40, 25, float("nan")):
            with self.subTest(gain=gain), self.assertRaises(ValueError):
                speech_audio.filter_chain(gain)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg/ffprobe are required")
class MeasuredEncodeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _encode(self, wav, gain, name="out.flac"):
        out = self.dir / name
        speech_audio.encode_flac(["-i", str(wav)], out, gain, {"artist": "Oak Grove Radio", "title": "Test"})
        return out

    def test_container_is_44k_stereo_16_bit_flac_with_tags(self):
        wav = self.dir / "in.wav"
        _synthetic_wav(wav, 0.08)
        out = self._encode(wav, 6.0)
        info = _run("ffprobe", "-v", "error", "-show_entries", "stream=codec_name,sample_rate,channels,sample_fmt",
                    "-show_entries", "format_tags=title,artist", "-of", "default=nw=1", str(out)).stdout
        self.assertIn("codec_name=flac", info)
        self.assertIn("sample_rate=44100", info)
        self.assertIn("channels=2", info)
        self.assertIn("sample_fmt=s16", info)
        self.assertRegex(info.lower(), r"tag:title=test")
        self.assertRegex(info.lower(), r"tag:artist=oak grove radio")

    def test_gain_is_applied_exactly_when_nothing_is_limited(self):
        wav = self.dir / "in.wav"
        _synthetic_wav(wav, 0.08)
        base_lufs, base_tp = _loudness(self._encode(wav, 0.0, "g0.flac"))
        up_lufs, up_tp = _loudness(self._encode(wav, 6.0, "g6.flac"))
        self.assertAlmostEqual(up_lufs - base_lufs, 6.0, delta=0.1)
        self.assertLess(up_tp, -1.5)             # far from the ceiling: the limiter did nothing

    def test_hot_speech_is_limited_to_the_true_peak_ceiling(self):
        wav = self.dir / "hot.wav"
        _synthetic_wav(wav, 1.0)
        lufs, true_peak = _loudness(self._encode(wav, 6.0))
        self.assertLessEqual(true_peak, -1.0)    # the station's -1 dBTP target; the 0.84 ceiling is -1.5 dBFS
        self.assertGreater(true_peak, -2.0)      # it was actually limited down to the ceiling, not just quiet

    def test_duration_is_source_plus_exactly_the_lead_in(self):
        wav = self.dir / "in.wav"
        _synthetic_wav(wav, 0.08, seconds=5)
        out = self._encode(wav, 6.0)
        samples = len(_pcm(out)) // 4           # 2 channels * 2 bytes
        self.assertEqual(samples, 5 * 44100 + 44100)

    def test_lead_in_is_digital_silence_and_speech_follows(self):
        wav = self.dir / "in.wav"
        _synthetic_wav(wav, 0.08)
        pcm = _pcm(self._encode(wav, 6.0))
        lead = pcm[: 44100 * 4]
        self.assertEqual(lead.count(0), len(lead))
        self.assertTrue(any(pcm[44100 * 4: 44100 * 4 + 4410 * 4]))

    def test_mono_upmix_matches_the_previous_ffmpeg_minus_ac_2_level(self):
        """The calibration was measured on files the old chain upmixed with ``-ac 2``; the new chain must put the
        same signal in each channel, or every calibrated gain would be off."""
        wav = self.dir / "in.wav"
        _synthetic_wav(wav, 0.08)
        new = _pcm(self._encode(wav, 0.0))
        old_path = self.dir / "old.wav"
        _run("ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav), "-af", "adelay=1000:all=1",
             "-ar", "44100", "-ac", "2", "-sample_fmt", "s16", str(old_path))
        old = _pcm(old_path)
        self.assertEqual(len(new), len(old))
        n = len(new) // 2
        import struct
        a = struct.unpack(f"<{n}h", new)
        b = struct.unpack(f"<{n}h", old)
        # The old chain resampled after the delay, so only the few frames around the speech onset (resampler
        # edge ringing) differ; everywhere else the two are the same samples to rounding.
        onset = 2 * 44100
        keep = [i for i in range(n) if abs(i - onset) > 2 * 128]
        worst = max(abs(a[i] - b[i]) for i in keep)
        self.assertLessEqual(worst, 3, "same upmix gain and the same samples, to rounding")

    def test_concat_of_clips_gets_one_lead_in_and_one_calibration(self):
        clips = []
        for i in range(2):
            wav = self.dir / f"clip{i}.wav"
            _synthetic_wav(wav, 0.08, seconds=2)
            clips.append(wav)
        listing = self.dir / "list.txt"
        listing.write_text("".join(f"file '{c}'\n" for c in clips))
        out = self.dir / "alert.flac"
        speech_audio.encode_flac(["-f", "concat", "-safe", "0", "-i", str(listing)], out, 6.0,
                                 {"artist": "Weather Alert", "title": "Weather Alert"})
        self.assertEqual(len(_pcm(out)) // 4, 4 * 44100 + 44100)

    def test_failure_raises_and_leaves_no_complete_file(self):
        out = self.dir / "never.flac"
        with self.assertRaises(subprocess.CalledProcessError):
            speech_audio.encode_flac(["-i", str(self.dir / "missing.wav")], out, 6.0, {})
        self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
