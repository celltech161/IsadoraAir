"""analyze_one_track's move-during-analysis recovery for the stereo
waveform decode pass (see the "Move-during-analysis recovery" comment
block in library/management/commands/analyze_tracks.py).

Regression coverage for the failure mode discovered 2026-08-14: a
category-change through the UI does an atomic shutil.move + DB filepath
rewrite between the mono decode (cue-point detection, persists
next_start_seconds and thereby marks the track "analyzed" per the
timer's default filter) and the stereo decode (waveform display data).
The stereo ffmpeg call then hits ENOENT and returns empty; without
recovery the display would stay empty until a manual reanalyze, since
the timer wouldn't re-pick this track. Fixed by re-reading the current
filepath from the DB when the decode returns empty and retrying once
with the new path.
"""
import json
import struct
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from library.management.commands import analyze_tracks
from library.models import Artist, Track


def _fake_mono_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    n = seconds * sample_rate
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _fake_stereo_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    # Interleaved L/R s16le, matches decode_audio_to_pcm_stereo output.
    n_frames = seconds * sample_rate
    return struct.pack(f"<{n_frames * 2}h", *([amplitude] * (n_frames * 2)))


def _make_track(filepath, filename="track.flac", title="Test Title", artist_name="Test Artist"):
    artist, _ = Artist.get_or_create_ci(artist_name)
    return Track.objects.create(
        filepath=filepath, filename=filename, title=title, artist=artist,
        related_artists="",
    )


class WaveformMoveRecoveryTests(TestCase):
    """The exact scenario the 2026-08-14 hardening fixes: file moves
    between the mono decode and the stereo decode, DB has the new path,
    retry the stereo decode against the new path so the waveform
    display still gets populated."""

    CFG = (8000, 0.05, 200, -26.0, -40.0, 0.1)

    def test_move_during_analysis_recovers_waveform(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old_path = tmp / "old_category" / "track.flac"
            new_path = tmp / "new_category" / "track.flac"
            old_path.parent.mkdir()
            new_path.parent.mkdir()
            # Both files exist -- in the real race the old path still
            # exists when analyze_one_track starts (mono decode uses
            # it), then the operator's UI-driven category change
            # atomically shutil.move's it to the new path in the
            # microsecond gap between mono and stereo. Simulating both
            # present is the closest deterministic proxy.
            old_path.write_bytes(b"pretend flac before move")
            new_path.write_bytes(b"pretend flac after move")
            wave_dir = tmp / "waveforms"
            wave_dir.mkdir()

            track = _make_track(filepath=str(new_path), filename=new_path.name)
            # The bulk analyze command reads filepath into `values_list`
            # BEFORE the move; the row passed to analyze_one_track
            # therefore carries the OLD path, while the DB has the new
            # one -- that's the whole race we're recovering from.
            row = (track.id, str(old_path), old_path.name, None, track.title, "Test Artist", "")

            calls = []
            def stereo_side_effect(fp, sr):
                calls.append(str(fp))
                # First call (old path) fails with empty bytes, second
                # (new path) succeeds. Matches how the real ffmpeg
                # subprocess wrapper degrades on ENOENT.
                if len(calls) == 1:
                    return b""
                return _fake_stereo_pcm()

            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", side_effect=stereo_side_effect), \
                 patch.object(analyze_tracks, "emit_event") as emit_mock:
                ok = analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True)

            self.assertTrue(ok)
            self.assertEqual(len(calls), 2, "stereo decode should have been retried exactly once")
            self.assertEqual(calls[0], str(old_path))
            self.assertEqual(calls[1], str(new_path))

            # Waveform payload actually contains stereo samples (the
            # bug's own symptom -- empty samples_left/samples_right
            # while envelope_times/envelope_db were populated -- would
            # regress here).
            payload = json.loads((wave_dir / f"{track.id}.json").read_text())
            self.assertGreater(len(payload["samples_left"]), 0)
            self.assertGreater(len(payload["samples_right"]), 0)
            self.assertEqual(len(payload["samples_left"]), len(payload["samples_right"]))

            # Recovery event surfaced so operators can see it happened
            # (the original 2026-08-14 incident's core symptom was
            # "nothing in /monitoring/ despite the empty waveform").
            emit_mock.assert_called_once()
            call = emit_mock.call_args
            self.assertEqual(call.kwargs["category"], "library")
            self.assertEqual(call.kwargs["level"], "info")
            self.assertEqual(call.kwargs["dedupe_key"], f"library|analyze-waveform-recover|{track.id}")
            self.assertEqual(call.kwargs["detail"]["track_id"], track.id)
            self.assertEqual(call.kwargs["detail"]["old_filepath"], str(old_path))
            self.assertEqual(call.kwargs["detail"]["new_filepath"], str(new_path))

    def test_stereo_decode_empty_but_db_filepath_unchanged_no_retry(self):
        """If the ffmpeg stereo decode returns empty for reasons other
        than a mid-analysis move (say, a genuinely broken file), the
        recovery path must not re-issue the decode -- there'd be no
        reason for it to succeed the second time, and pointlessly
        double-decoding a broken file is wasted work."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            audio_path = tmp / "track.flac"
            audio_path.write_bytes(b"pretend flac")
            wave_dir = tmp / "waveforms"
            wave_dir.mkdir()

            track = _make_track(filepath=str(audio_path), filename=audio_path.name)
            row = (track.id, str(audio_path), audio_path.name, None, track.title, "Test Artist", "")

            calls = []
            def stereo_side_effect(fp, sr):
                calls.append(str(fp))
                return b""

            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", side_effect=stereo_side_effect), \
                 patch.object(analyze_tracks, "emit_event") as emit_mock:
                ok = analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True)

            self.assertTrue(ok)  # mono side succeeded so overall analyze counts as done
            self.assertEqual(len(calls), 1, "no retry should occur when the DB filepath matches")
            payload = json.loads((wave_dir / f"{track.id}.json").read_text())
            self.assertEqual(payload["samples_left"], [])
            self.assertEqual(payload["samples_right"], [])
            emit_mock.assert_not_called()

    def test_db_filepath_changed_but_new_file_missing_no_retry(self):
        """A rare but real edge case: the DB says the file moved to a
        new path, but that path doesn't actually exist on disk (e.g.
        the move somehow crashed mid-flight). Don't blow up, don't
        emit a "we recovered!" event that isn't true -- just skip the
        retry."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old_path = tmp / "old" / "track.flac"
            missing_new_path = tmp / "new" / "track.flac"  # deliberately not created
            old_path.parent.mkdir()
            old_path.write_bytes(b"pretend flac still present at old path")
            wave_dir = tmp / "waveforms"
            wave_dir.mkdir()

            track = _make_track(filepath=str(missing_new_path), filename=missing_new_path.name)
            row = (track.id, str(old_path), old_path.name, None, track.title, "Test Artist", "")

            calls = []
            def stereo_side_effect(fp, sr):
                calls.append(str(fp))
                return b""

            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", side_effect=stereo_side_effect), \
                 patch.object(analyze_tracks, "emit_event") as emit_mock:
                ok = analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True)

            self.assertTrue(ok)
            self.assertEqual(len(calls), 1, "no retry when new path isn't a file")
            emit_mock.assert_not_called()

    def test_retry_also_fails_no_recovery_event(self):
        """If the second decode also returns empty (e.g. the file at
        the new path is itself broken), the waveform stays empty and
        no "recovered" event is emitted -- we mustn't claim success
        we didn't actually achieve."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            old_path = tmp / "old" / "track.flac"
            new_path = tmp / "new" / "track.flac"
            old_path.parent.mkdir()
            new_path.parent.mkdir()
            old_path.write_bytes(b"pretend flac still present at old path")
            new_path.write_bytes(b"pretend broken flac")
            wave_dir = tmp / "waveforms"
            wave_dir.mkdir()

            track = _make_track(filepath=str(new_path), filename=new_path.name)
            row = (track.id, str(old_path), old_path.name, None, track.title, "Test Artist", "")

            calls = []
            def stereo_side_effect(fp, sr):
                calls.append(str(fp))
                return b""  # both calls fail

            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", side_effect=stereo_side_effect), \
                 patch.object(analyze_tracks, "emit_event") as emit_mock:
                ok = analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True)

            self.assertTrue(ok)
            self.assertEqual(len(calls), 2, "retry attempted once")
            payload = json.loads((wave_dir / f"{track.id}.json").read_text())
            self.assertEqual(payload["samples_left"], [])
            self.assertEqual(payload["samples_right"], [])
            # We did not recover; do not emit a false-positive event.
            emit_mock.assert_not_called()

    def test_normal_path_no_retry_no_extra_db_lookup(self):
        """The happy path (stereo decode succeeds on the first try)
        must not trigger the recovery machinery at all -- no extra
        Track.objects.filter round-trip, no emit_event. Guards against
        the fix accidentally becoming a per-track overhead."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            audio_path = tmp / "track.flac"
            audio_path.write_bytes(b"pretend flac")
            wave_dir = tmp / "waveforms"
            wave_dir.mkdir()

            track = _make_track(filepath=str(audio_path), filename=audio_path.name)
            row = (track.id, str(audio_path), audio_path.name, None, track.title, "Test Artist", "")

            calls = []
            def stereo_side_effect(fp, sr):
                calls.append(str(fp))
                return _fake_stereo_pcm()

            with patch.object(analyze_tracks, "transcode_lossless_to_flac", side_effect=lambda tid, fp, fn: (fp, fn)), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
                 patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", side_effect=stereo_side_effect), \
                 patch.object(analyze_tracks, "emit_event") as emit_mock:
                ok = analyze_tracks.analyze_one_track(row, self.CFG, wave_dir, force=True)

            self.assertTrue(ok)
            self.assertEqual(len(calls), 1)
            payload = json.loads((wave_dir / f"{track.id}.json").read_text())
            self.assertGreater(len(payload["samples_left"]), 0)
            emit_mock.assert_not_called()
