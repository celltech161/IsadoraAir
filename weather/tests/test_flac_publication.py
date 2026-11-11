"""Weather FLAC standardization (r0134): publishing .flac, and adopting the pre-existing .mp3 Track in place.

Why adoption and not "a new Track at the .flac path": the engine's urgent insert takes the first ready Track of
a category with no ordering, approved logs and rotations reference Track rows, and PlaylistItem.track is PROTECT.
A second Track would leave the old MP3 in service (stale, never regenerated), duplicate eligibility, and orphan
play history. The same Track row, re-pointed at the new file, keeps every one of those references working.
"""
import shutil
import threading
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.core.management import call_command
from django.test import TransactionTestCase
from django.utils import timezone

from library.management.commands.analyze_tracks import get_waveforms_dir
from library.management.commands.sync_track_file import sync_track_file as real_sync_track_file
from library.models import Category, LogItem, Playlist, PlaylistItem, PlaylistLog, Track
from weather.publication import PublicationError, publish_weather_asset

from .test_publication import PublicationTestCase, _generated_waveform


class FlacPublicationTests(PublicationTestCase):
    def _publish(self, name, filename, content=b"fLaC-bytes", **kwargs):
        candidate = self.make_candidate(name=name, content=content)
        with self._patch_analyze(succeed=True):
            return publish_weather_asset(str(candidate), "WxTemp", filename, **kwargs)

    # -- fresh install ------------------------------------------------------------------------------------------

    def test_first_publication_as_flac_creates_the_track_at_the_flac_path(self):
        result = self._publish("c.flac", "current_temp.flac", producer="current_temp.py", voice="Max")
        dest = self.library_root / "WxTemp" / "current_temp.flac"
        self.assertTrue(dest.is_file())
        self.assertTrue(result.created)
        self.assertIsNone(result.adopted_from)
        self.assertEqual(result.warnings, ())
        self.assertEqual(result.track.filepath, str(dest))
        self.assertEqual(result.track.filename, "current_temp.flac")
        self.assertEqual(result.track.format, "flac")
        self.assertEqual(Track.objects.filter(category=self.category).count(), 1)

    def test_an_mp3_candidate_is_still_accepted(self):
        result = self._publish("c.mp3", "current_temp.mp3")
        self.assertEqual(result.track.format, "mp3")

    # -- adoption -----------------------------------------------------------------------------------------------

    def test_flac_publication_adopts_the_existing_mp3_track_keeping_its_identity(self):
        old = self._publish("old.mp3", "current_temp.mp3", content=b"OLD-MP3")
        old_id = old.track.id
        old_path = self.library_root / "WxTemp" / "current_temp.mp3"

        result = self._publish("new.flac", "current_temp.flac", content=b"NEW-FLAC")

        new_path = self.library_root / "WxTemp" / "current_temp.flac"
        self.assertEqual(result.track.id, old_id, "adoption must keep the Track id (every reference follows it)")
        self.assertEqual(result.adopted_from, old_path)
        self.assertFalse(result.created)
        self.assertEqual(Track.objects.filter(category=self.category).count(), 1)
        track = Track.objects.get(id=old_id)
        self.assertEqual((track.filepath, track.filename, track.format), (str(new_path), "current_temp.flac", "flac"))
        self.assertTrue(track.ready2air)
        self.assertEqual(new_path.read_bytes(), b"NEW-FLAC")
        # the previous file is left exactly as it was -- never deleted by publication
        self.assertEqual(old_path.read_bytes(), b"OLD-MP3")
        # the waveform follows the same Track id
        self.assertEqual(track.waveform_path, str(get_waveforms_dir() / f"{old_id}.json"))
        self.assertTrue(Path(track.waveform_path).is_file())

    def test_references_to_the_track_survive_adoption(self):
        old = self._publish("old.mp3", "current_temp.mp3")
        track_id = old.track.id
        playlist = Playlist.objects.create(name="Weather pl")
        item = PlaylistItem.objects.create(playlist=playlist, position=0, track=old.track)
        log = PlaylistLog.objects.create(date=timezone.localdate(), hour=9)
        log_item = LogItem.objects.create(
            playlist_log=log, position=0, scheduled_time=timezone.now() + timedelta(hours=1),
            track=old.track, track_title="Temp", category=self.category,
        )

        self._publish("new.flac", "current_temp.flac")

        item.refresh_from_db()
        log_item.refresh_from_db()
        self.assertEqual(item.track_id, track_id)
        self.assertEqual(log_item.track_id, track_id)
        self.assertEqual(log_item.track.filepath, str(self.library_root / "WxTemp" / "current_temp.flac"))

    def test_the_engines_unordered_first_ready_track_is_the_adopted_one(self):
        """The urgent-insert shape (`ready Track in category .first()`): after adoption there is exactly one
        candidate and it points at the FLAC."""
        self._publish("old.mp3", "current_temp.mp3")
        self._publish("new.flac", "current_temp.flac")
        ready = list(Track.objects.filter(category__code="WxTemp", ready2air=True))
        self.assertEqual([t.filepath for t in ready], [str(self.library_root / "WxTemp" / "current_temp.flac")])

    def test_a_later_flac_publication_just_updates_the_adopted_track(self):
        self._publish("old.mp3", "current_temp.mp3")
        first = self._publish("a.flac", "current_temp.flac", content=b"A")
        second = self._publish("b.flac", "current_temp.flac", content=b"B")
        self.assertEqual(first.track.id, second.track.id)
        self.assertIsNone(second.adopted_from)
        self.assertEqual((self.library_root / "WxTemp" / "current_temp.flac").read_bytes(), b"B")

    def test_rollback_to_mp3_adopts_back_the_same_track(self):
        old = self._publish("old.mp3", "current_temp.mp3", content=b"OLD")
        self._publish("new.flac", "current_temp.flac", content=b"NEW")
        back = self._publish("again.mp3", "current_temp.mp3", content=b"AGAIN")
        self.assertEqual(back.track.id, old.track.id)
        self.assertEqual(back.adopted_from, self.library_root / "WxTemp" / "current_temp.flac")
        self.assertEqual(Track.objects.get(id=old.track.id).format, "mp3")

    def test_the_retained_mp3_can_be_published_in_place_to_roll_the_track_back(self):
        """The documented rollback: the old MP3 file is still on disk; publishing it (as its own candidate) re-points
        the same Track at the MP3 before a source rollback."""
        old = self._publish("old.mp3", "current_temp.mp3", content=b"RETAINED-MP3")
        self._publish("new.flac", "current_temp.flac", content=b"NEW")
        retained = self.library_root / "WxTemp" / "current_temp.mp3"
        with self._patch_analyze(succeed=True):
            back = publish_weather_asset(str(retained), "WxTemp", "current_temp.mp3")
        self.assertEqual(back.track.id, old.track.id)
        self.assertEqual(Track.objects.get(id=old.track.id).filepath, str(retained))
        self.assertEqual(retained.read_bytes(), b"RETAINED-MP3")

    def test_other_assets_tracks_are_never_adopted(self):
        other = Category.objects.create(code="WxObs", name="WxObs", kind=self.category.kind)
        (self.library_root / "WxObs").mkdir()
        obs_file = self.library_root / "WxObs" / "current_temp.mp3"      # same stem, different category
        obs_file.write_bytes(b"OBS")
        obs_track = Track.objects.create(filepath=str(obs_file), filename="current_temp.mp3", format="mp3",
                                         title="obs", artist=self.artist, category=other)
        result = self._publish("new.flac", "current_temp.flac")
        self.assertTrue(result.created)
        self.assertIsNone(result.adopted_from)
        obs_track.refresh_from_db()
        self.assertEqual(obs_track.filepath, str(obs_file))

    def test_tracks_at_both_extensions_adopt_nothing_and_warn(self):
        old = self._publish("old.mp3", "current_temp.mp3")
        flac_path = self.library_root / "WxTemp" / "current_temp.flac"
        flac_path.write_bytes(b"PRE-EXISTING")
        stray = Track.objects.create(filepath=str(flac_path), filename="current_temp.flac", format="flac",
                                     title="stray", artist=self.artist, category=self.category)
        result = self._publish("new.flac", "current_temp.flac")
        self.assertIsNone(result.adopted_from)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("both", result.warnings[0])
        self.assertEqual(result.track.id, stray.id)
        self.assertTrue(Track.objects.filter(id=old.track.id, format="mp3").exists())

    # -- failure ------------------------------------------------------------------------------------------------

    def test_failed_analysis_rolls_back_the_adoption_and_leaves_the_mp3_track_untouched(self):
        old = self._publish("old.mp3", "current_temp.mp3", content=b"OLD-MP3")
        old_path = self.library_root / "WxTemp" / "current_temp.mp3"
        candidate = self.make_candidate(name="new.flac", content=b"NEW-FLAC")
        with self._patch_analyze(succeed=False):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.flac")

        track = Track.objects.get(id=old.track.id)
        self.assertEqual((track.filepath, track.format), (str(old_path), "mp3"))
        self.assertEqual(Track.objects.filter(category=self.category).count(), 1)
        self.assertEqual(old_path.read_bytes(), b"OLD-MP3")
        self.assertFalse((self.library_root / "WxTemp" / "current_temp.flac").exists())
        self.assertTrue(Path(track.waveform_path).is_file())

    def test_failure_after_adoption_but_before_commit_rolls_the_track_back(self):
        old = self._publish("old.mp3", "current_temp.mp3")
        old_path = self.library_root / "WxTemp" / "current_temp.mp3"
        candidate = self.make_candidate(name="new.flac", content=b"NEW-FLAC")
        with self._patch_analyze(succeed=True), \
             patch("weather.publication.waveform_cache.install_file", side_effect=OSError("disk full")):
            with self.assertRaises(PublicationError):
                publish_weather_asset(str(candidate), "WxTemp", "current_temp.flac")
        track = Track.objects.get(id=old.track.id)
        self.assertEqual(track.filepath, str(old_path))
        self.assertFalse((self.library_root / "WxTemp" / "current_temp.flac").exists())

    # -- validation ---------------------------------------------------------------------------------------------

    def test_unsupported_candidate_formats_are_rejected(self):
        candidate = self.make_candidate(name="c.wav")
        with self.assertRaisesRegex(PublicationError, "FLAC"):
            publish_weather_asset(str(candidate), "WxTemp", "current_temp.wav")

    def test_destination_extension_must_match_the_candidate(self):
        candidate = self.make_candidate(name="c.flac")
        with self.assertRaisesRegex(PublicationError, "extension"):
            publish_weather_asset(str(candidate), "WxTemp", "current_temp.mp3")
        self.assertFalse((self.library_root / "WxTemp" / "current_temp.mp3").exists())

    def test_destination_filename_must_be_a_plain_name(self):
        candidate = self.make_candidate(name="c.flac")
        with self.assertRaisesRegex(PublicationError, "plain name"):
            publish_weather_asset(str(candidate), "WxTemp", "../escape.flac")

    # -- lock ---------------------------------------------------------------------------------------------------

    def test_both_extensions_share_one_asset_lock_file(self):
        self._publish("old.mp3", "current_temp.mp3")
        self._publish("new.flac", "current_temp.flac")
        locks = sorted(p.name for p in (self.library_root / "WxTemp").iterdir() if p.name.endswith(".lock"))
        self.assertEqual(locks, [".current_temp.asset.publish.lock"])

    # -- command ------------------------------------------------------------------------------------------------

    def test_command_reports_the_adoption(self):
        from io import StringIO
        self._publish("old.mp3", "current_temp.mp3")
        candidate = self.make_candidate(name="new.flac")
        out = StringIO()
        with self._patch_analyze(succeed=True):
            call_command("publish_weather_asset", str(candidate), "WxTemp", "current_temp.flac", stdout=out)
        text = out.getvalue()
        self.assertIn("Updated track", text)
        self.assertIn("Adopted track", text)
        self.assertIn("current_temp.mp3 -> current_temp.flac", text)


class AssetLockSerializationTests(TransactionTestCase):
    """The asset lock is keyed by category + stem, so a FLAC and an MP3 publication of the same asset (NWS vs AMBER
    during a cutover, or a producer on old code) can never interleave their file + adoption + Track sequence."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.library_root = Path(self.temporary.name) / "library"
        self.data_dir = Path(self.temporary.name) / "weather-data"
        self.library_root.mkdir()
        self.data_dir.mkdir()
        from django.test import override_settings
        patcher = override_settings(
            LIBRARY_ROOT=str(self.library_root), WEATHER_DATA_DIR=str(self.data_dir),
            WAVEFORMS_DIR=str(Path(self.temporary.name) / "waveforms"),
        )
        patcher.enable()
        self.addCleanup(patcher.disable)
        from library.models import Artist, CategoryKind
        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        self.category, _ = Category.objects.get_or_create(code="WxAlert", defaults={"name": "WxAlert", "kind": kind})
        Artist.get_or_create_ci("Oak Grove Radio")
        self.addCleanup(shutil.rmtree, get_waveforms_dir(), ignore_errors=True)

    def test_flac_and_mp3_publications_of_one_asset_never_overlap(self):
        active = {"count": 0}
        seen = []
        guard = threading.Lock()

        def instrumented_sync(path, wave_dir=None):
            with guard:
                active["count"] += 1
                seen.append(active["count"])
            try:
                threading.Event().wait(timeout=0.15)
                return real_sync_track_file(path, wave_dir=wave_dir)
            finally:
                with guard:
                    active["count"] -= 1

        def fake_analyze(row, cfg_values, wave_dir_arg, force, existing_artist_names_casefolded=None, **kwargs):
            scratch_path = Path(wave_dir_arg) / f"{row[0]}.json"
            scratch_path.write_text(_generated_waveform(row[0]))
            Track.objects.filter(id=row[0]).update(waveform_path=str(scratch_path))
            return True

        mp3 = Path(self.temporary.name) / "a.mp3"
        flac = Path(self.temporary.name) / "b.flac"
        mp3.write_bytes(b"MP3")
        flac.write_bytes(b"FLAC")
        errors = []

        def worker(candidate, filename):
            try:
                publish_weather_asset(str(candidate), "WxAlert", filename)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)
            finally:
                from django.db import connection
                connection.close()

        with patch("weather.publication.sync_track_file", side_effect=instrumented_sync), \
             patch("library.management.commands.sync_track_file.analyze_one_track", side_effect=fake_analyze):
            threads = [threading.Thread(target=worker, args=(mp3, "wx_alert.mp3")),
                       threading.Thread(target=worker, args=(flac, "wx_alert.flac"))]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=20)

        self.assertEqual(errors, [])
        self.assertEqual(max(seen), 1, "the two extensions were published concurrently")
        # one asset, one Track, whichever extension ran last
        tracks = list(Track.objects.filter(category=self.category))
        self.assertEqual(len(tracks), 1)
        self.assertIn(tracks[0].format, ("mp3", "flac"))
        self.assertTrue(Path(tracks[0].filepath).is_file())
