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
import json
import shutil
import struct
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from isadoraair import test_isolation
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
        # A private waveform directory next to the private library root. Analysis
        # writes <track id>.json into WAVEFORMS_DIR, and test databases reuse small
        # Track ids that collide with a station's real ones, so overriding only
        # LIBRARY_ROOT overwrote real waveforms (2026-10-29 incident). This
        # suite must own both.
        self.waveforms = self.root.parent / (self.root.name + "-waveforms")
        self.waveforms.mkdir()
        self.addCleanup(shutil.rmtree, self.waveforms, True)
        override = override_settings(LIBRARY_ROOT=str(self.root), WAVEFORMS_DIR=str(self.waveforms))
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

    def assert_waveform_written_under_scratch(self, track):
        """The analysis-produced waveform exists, only under this test's scratch directory."""
        track.refresh_from_db()
        expected = self.waveforms / f"{track.id}.json"
        self.assertEqual(track.waveform_path, str(expected))
        self.assertTrue(expected.is_file())
        payload = json.loads(expected.read_text(encoding="utf-8"))
        self.assertEqual(payload["track_id"], track.id)
        self.assertGreater(len(payload["samples_left"]), 0)
        self.assertEqual(len(payload["samples_left"]), len(payload["samples_right"]))
        self.assertEqual(len(payload["envelope_times"]), len(payload["envelope_db"]))
        self.assertEqual(sorted(p.name for p in self.waveforms.iterdir()), [expected.name])

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
        self.assert_waveform_written_under_scratch(track)

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

    def test_cli_wrapper_writes_its_waveform_under_the_scratch_directory(self):
        path = self._write_file("Aircheck", name="clip5.mp3")
        with patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
             patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_fake_stereo_pcm()):
            call_command("sync_track_file", str(path), stdout=StringIO())
        self.assert_waveform_written_under_scratch(Track.objects.get(filepath=str(path)))


class ProtectedWaveformCollisionTests(TestCase):
    """Regression for the 2026-10-29 incident: a colliding Track id must never reach a
    station's waveform directory. A simulated protected directory stands in for
    /srv/isadoraair/waveforms -- these tests never write under /srv."""

    SENTINEL = b'{"track_id": "sentinel -- a real station waveform"}'

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        base = Path(self._tmpdir.name)
        self.library = base / "library"
        self.scratch = base / "scratch-waveforms"
        self.protected = base / "station-waveforms"
        for directory in (self.library / "Aircheck", self.scratch, self.protected):
            directory.mkdir(parents=True)
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        Category.objects.create(code="Aircheck", name="Aircheck", kind=kind)
        self.clip = self.library / "Aircheck" / "collide.mp3"
        self.clip.write_bytes(b"not really an mp3 -- decode is mocked")
        # Deliberate refusals below must not be counted as isolation failures by the runner.
        self.addCleanup(test_isolation.clear_blocked_writes)

    def _station_protected(self):
        """Declare the simulated station directory protected (after the sentinel is planted)."""
        return test_isolation.protect_for_test(self.protected)

    def _sync(self, **overrides):
        settings_override = override_settings(LIBRARY_ROOT=str(self.library), **overrides)
        with settings_override, \
             patch.object(analyze_tracks, "decode_audio_to_pcm", return_value=_fake_mono_pcm()), \
             patch.object(analyze_tracks, "decode_audio_to_pcm_stereo", return_value=_fake_stereo_pcm()):
            return sync_track_file(str(self.clip), ready2air=False)

    def _plant_sentinel(self, track_id):
        sentinel = self.protected / f"{track_id}.json"
        sentinel.write_bytes(self.SENTINEL)
        return sentinel

    def test_colliding_id_with_a_scratch_waveform_directory_leaves_the_station_file_untouched(self):
        track, _ = self._sync(WAVEFORMS_DIR=str(self.scratch))
        sentinel = self._plant_sentinel(track.id)            # same file name the incident overwrote
        with self._station_protected():
            track, created = self._sync(WAVEFORMS_DIR=str(self.scratch))   # re-analysis of the same Track id
        self.assertFalse(created)
        self.assertEqual(sentinel.read_bytes(), self.SENTINEL)
        written = self.scratch / f"{track.id}.json"
        self.assertTrue(written.is_file())
        self.assertEqual(json.loads(written.read_text())["track_id"], track.id)
        self.assertEqual(test_isolation.blocked_writes(), [])

    def test_a_waveform_directory_misconfigured_to_station_storage_is_refused_not_overwritten(self):
        track, _ = self._sync(WAVEFORMS_DIR=str(self.scratch))
        sentinel = self._plant_sentinel(track.id)
        # The stale-.env / forgotten-override scenario: analysis is pointed at the station.
        with self._station_protected(), self.assertRaises(test_isolation.ProtectedPathWriteBlocked):
            self._sync(WAVEFORMS_DIR=str(self.protected))
        self.assertEqual(sentinel.read_bytes(), self.SENTINEL)
        self.assertEqual(sorted(p.name for p in self.protected.iterdir()), [sentinel.name])
        blocked = test_isolation.blocked_writes()
        self.assertEqual([entry[0] for entry in blocked][:1], ["open"])
        self.assertEqual(Path(blocked[0][1]), sentinel)

    def test_a_symlink_alias_of_station_storage_is_refused_too(self):
        track, _ = self._sync(WAVEFORMS_DIR=str(self.scratch))
        sentinel = self._plant_sentinel(track.id)
        alias = self.scratch.parent / "alias-to-station"
        alias.symlink_to(self.protected, target_is_directory=True)
        with self._station_protected(), self.assertRaises(test_isolation.ProtectedPathWriteBlocked):
            self._sync(WAVEFORMS_DIR=str(alias))
        self.assertEqual(sentinel.read_bytes(), self.SENTINEL)
