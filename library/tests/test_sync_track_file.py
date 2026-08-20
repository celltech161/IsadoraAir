"""Tests for library.management.commands.sync_track_file's core
sync_track_file() function (extracted from the command's own handle()
so aircheck.services.recorder can call it directly -- see
aircheck/tests/test_recorder.py's LibrarySyncTests for that side).

ffmpeg decode is mocked (same idiom as
test_analyze_waveform_move_recovery.py) so these tests don't need a
real playable audio file -- test fixtures use .mp3 (not .wav/.aif) so
transcode_lossless_to_flac's real, unmocked no-op path is exercised
correctly rather than needing its own mock.
"""
import struct
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from library.management.commands import analyze_tracks
from library.management.commands.sync_track_file import sync_track_file
from library.models import Category, CategoryKind, Track


def _fake_mono_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    n = seconds * sample_rate
    return struct.pack(f"<{n}h", *([amplitude] * n))


def _fake_stereo_pcm(seconds=1, sample_rate=8000, amplitude=3000):
    n_frames = seconds * sample_rate
    return struct.pack(f"<{n_frames * 2}h", *([amplitude] * (n_frames * 2)))


class SyncTrackFileTests(TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        override = override_settings(LIBRARY_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)

        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="Aircheck", name="Aircheck", kind=kind)

    def _write_file(self, subdir, name="clip.mp3"):
        d = self.root / subdir
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_bytes(b"not really an mp3 -- decode is mocked")
        return path

    def _sync(self, path, **kwargs):
        with patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
             patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_fake_stereo_pcm()):
            return sync_track_file(str(path), **kwargs)

    def test_creates_track_with_ready2air_true_by_default(self):
        path = self._write_file("Aircheck")
        track, created = self._sync(path)
        self.assertTrue(created)
        self.assertTrue(track.ready2air)
        self.assertEqual(track.category_id, self.category.id)
        self.assertEqual(track.filepath, str(path))

    def test_ready2air_false_when_requested(self):
        path = self._write_file("Aircheck", name="clip2.mp3")
        track, created = self._sync(path, ready2air=False)
        self.assertTrue(created)
        self.assertFalse(track.ready2air)

    def test_resync_updates_existing_track_and_ready2air(self):
        path = self._write_file("Aircheck", name="clip3.mp3")
        track1, created1 = self._sync(path, ready2air=True)
        self.assertTrue(created1)

        track2, created2 = self._sync(path, ready2air=False)
        self.assertFalse(created2)
        self.assertEqual(track1.id, track2.id)
        self.assertFalse(track2.ready2air)
        self.assertEqual(Track.objects.filter(filepath=str(path)).count(), 1)

    def test_not_a_file_raises(self):
        with self.assertRaises(CommandError):
            self._sync(self.root / "Aircheck" / "missing.mp3")

    def test_not_under_library_root_raises(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "clip.mp3"
            outside.write_bytes(b"x")
            with self.assertRaises(CommandError):
                self._sync(outside)

    def test_directly_in_library_root_raises(self):
        path = self.root / "clip.mp3"
        path.write_bytes(b"x")
        with self.assertRaises(CommandError):
            self._sync(path)

    def test_no_matching_category_raises(self):
        path = self._write_file("NoSuchCategory")
        with self.assertRaises(CommandError):
            self._sync(path)
        self.assertFalse(Track.objects.filter(filepath=str(path)).exists())

    def test_cli_wrapper_creates_track_and_reports(self):
        path = self._write_file("Aircheck", name="clip4.mp3")
        out = StringIO()
        with patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
             patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_fake_stereo_pcm()):
            call_command("sync_track_file", str(path), stdout=out)
        self.assertIn("Created track", out.getvalue())
        track = Track.objects.get(filepath=str(path))
        self.assertTrue(track.ready2air)  # CLI default unchanged by this refactor
