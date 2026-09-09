"""Focused contracts for the three generated-announcement artifact modes."""

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase, override_settings

from isadoraair.announcements import (
    AnnouncementRenderError,
    AnnouncementRenderer,
    AnnouncementTrackMetadata,
    PreviewAnnouncement,
    RotationAssetAnnouncement,
    SpeechSpliceAnnouncement,
)
from isadoraair.announcements import renderer as renderer_module
from library.models import AnalysisConfig, Artist, Category, CategoryKind, Track


class AnnouncementRendererTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.waveforms = self.root / "waveforms"
        self.artist = Artist.objects.create(name="Generated Voice")
        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        self.category = Category.objects.create(
            code="Generated", name="Generated", kind=kind
        )
        self.renderer = AnnouncementRenderer()

    @staticmethod
    def _synthesize(text, *, voice, output_path, timeout_seconds, **_kwargs):
        Path(output_path).write_bytes(b"WAV:" + text.encode())
        return Path(output_path)

    @staticmethod
    def _convert(source, destination):
        Path(destination).write_bytes(b"FLAC:" + Path(source).read_bytes())

    def _metadata(self, **overrides):
        values = {
            "title": "Generated title",
            "artist": self.artist,
            "category_code": self.category.code,
            "ready2air": True,
        }
        values.update(overrides)
        return AnnouncementTrackMetadata(**values)

    def _speech_spec(self, destination=None, **metadata):
        return SpeechSpliceAnnouncement(
            text="A listener-facing announcement.",
            logical_voice="Station_Dave",
            destination=destination or self.root / "speech.flac",
            timeout_seconds=17,
            metadata=self._metadata(**metadata),
        )

    def _rotation_spec(self, destination=None, **metadata):
        return RotationAssetAnnouncement(
            text="A stable rotation announcement.",
            logical_voice="Station_Dave",
            destination=destination or self.root / "rotation.flac",
            timeout_seconds=29,
            metadata=self._metadata(**metadata),
        )

    def _assert_no_scratch(self):
        self.assertEqual(
            [path for path in self.root.iterdir() if path.name.startswith(".")],
            [],
        )

    def test_preview_uses_logical_voice_and_never_creates_library_rows(self):
        destination = self.root / "preview.wav"
        before_tracks = Track.objects.count()
        before_categories = Category.objects.count()
        spec = PreviewAnnouncement(
            text="Preview me",
            logical_voice="Preview_Alice",
            destination=destination,
            timeout_seconds=11,
        )

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ) as synthesize, patch.object(
            renderer_module, "_probe_duration", return_value=2.25
        ):
            result = self.renderer.render(spec)

        self.assertEqual(result.path, destination)
        self.assertEqual(result.duration_seconds, 2.25)
        self.assertIsNone(result.track)
        self.assertEqual(destination.read_bytes(), b"WAV:Preview me")
        self.assertEqual(Track.objects.count(), before_tracks)
        self.assertEqual(Category.objects.count(), before_categories)
        self.assertEqual(synthesize.call_args.kwargs["voice"], "Preview_Alice")
        self.assertEqual(synthesize.call_args.kwargs["timeout_seconds"], 11)
        self._assert_no_scratch()

    def test_preview_tts_failure_leaves_no_artifact_or_scratch(self):
        destination = self.root / "preview.wav"
        with patch.object(
            renderer_module,
            "synthesize_station_voice",
            side_effect=RuntimeError("provider unavailable"),
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(
                    PreviewAnnouncement(
                        text="Preview me",
                        logical_voice="Preview_Alice",
                        destination=destination,
                        timeout_seconds=11,
                    )
                )

        self.assertFalse(destination.exists())
        self._assert_no_scratch()

    def test_preview_probe_failure_preserves_existing_artifact(self):
        destination = self.root / "preview.wav"
        destination.write_bytes(b"LAST-GOOD")
        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_probe_duration", side_effect=ValueError("bad duration")
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(
                    PreviewAnnouncement(
                        text="Preview me",
                        logical_voice="Preview_Alice",
                        destination=destination,
                        timeout_seconds=11,
                    )
                )

        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self._assert_no_scratch()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_speech_splice_publishes_track_and_reasserts_cues_after_analysis(self):
        destination = self.root / "speech.flac"

        def analyze(track, wave_dir=None):
            Track.objects.filter(id=track.id).update(
                cue_in_seconds=0.75,
                next_start_seconds=1.5,
                waveform_path=str(self.waveforms / f"{track.id}.json"),
            )
            return True

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ) as synthesize, patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ) as convert, patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ) as probe, patch.object(
            renderer_module, "_analyze_track", side_effect=analyze
        ) as analyze_mock:
            result = self.renderer.render(self._speech_spec(destination))

        track = result.track
        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"FLAC:WAV:A listener-facing announcement.")
        self.assertEqual(track.filepath, str(destination))
        self.assertEqual(track.filename, destination.name)
        self.assertEqual(track.format, "flac")
        self.assertEqual(track.title, "Generated title")
        self.assertEqual(track.artist, self.artist)
        self.assertEqual(track.category, self.category)
        self.assertEqual(track.duration_seconds, 6.5)
        self.assertEqual(track.cue_in_seconds, 0)
        self.assertEqual(track.next_start_seconds, 6.5)
        self.assertTrue(track.ready2air)
        self.assertTrue(result.analysis_attempted)
        self.assertTrue(result.analysis_succeeded)
        self.assertEqual(synthesize.call_args.kwargs["voice"], "Station_Dave")
        self.assertEqual(synthesize.call_args.kwargs["timeout_seconds"], 17)
        convert.assert_called_once()
        probe.assert_called_once()
        analyze_mock.assert_called_once()
        self._assert_no_scratch()

    def test_speech_waveform_failure_is_non_fatal_and_cues_remain_safe(self):
        destination = self.root / "speech.flac"
        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=4.75
        ), patch.object(
            renderer_module, "_analyze_track", side_effect=RuntimeError("waveform failed")
        ):
            result = self.renderer.render(self._speech_spec(destination))

        result.track.refresh_from_db()
        self.assertTrue(destination.is_file())
        self.assertFalse(result.analysis_succeeded)
        self.assertEqual(result.track.cue_in_seconds, 0)
        self.assertEqual(result.track.next_start_seconds, 4.75)
        self.assertTrue(result.track.ready2air)
        self._assert_no_scratch()

    def test_missing_category_fails_before_tts_or_publication(self):
        destination = self.root / "speech.flac"
        spec = self._speech_spec(destination, category_code="Missing")
        with patch.object(renderer_module, "synthesize_station_voice") as synthesize:
            with self.assertRaisesRegex(AnnouncementRenderError, "Category 'Missing'"):
                self.renderer.render(spec)

        synthesize.assert_not_called()
        self.assertFalse(destination.exists())
        self._assert_no_scratch()

    def _existing_track(self, destination):
        destination.write_bytes(b"LAST-GOOD")
        return Track.objects.create(
            filepath=str(destination),
            filename=destination.name,
            format="flac",
            title="Old title",
            artist=self.artist,
            category=self.category,
            ready2air=False,
            duration_seconds=9.0,
            cue_in_seconds=1.0,
            next_start_seconds=8.0,
        )

    def test_tts_failure_leaves_old_artifact_and_track_untouched(self):
        destination = self.root / "speech.flac"
        track = self._existing_track(destination)
        with patch.object(
            renderer_module,
            "synthesize_station_voice",
            side_effect=RuntimeError("provider unavailable"),
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(
                    self._speech_spec(destination, title="New title")
                )

        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self.assertEqual(track.title, "Old title")
        self.assertEqual(track.duration_seconds, 9.0)
        self._assert_no_scratch()

    def test_conversion_failure_leaves_old_artifact_and_track_untouched(self):
        destination = self.root / "speech.flac"
        track = self._existing_track(destination)
        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=RuntimeError("ffmpeg failed")
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(self._speech_spec(destination, title="New title"))

        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self.assertEqual(track.title, "Old title")
        self.assertEqual(track.duration_seconds, 9.0)
        self._assert_no_scratch()

    def test_probe_failure_leaves_old_artifact_and_track_untouched(self):
        destination = self.root / "speech.flac"
        track = self._existing_track(destination)
        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", side_effect=ValueError("bad probe")
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(self._speech_spec(destination, title="New title"))

        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self.assertEqual(track.title, "Old title")
        self.assertEqual(track.duration_seconds, 9.0)
        self._assert_no_scratch()

    def test_track_failure_restores_old_file_and_database_row(self):
        destination = self.root / "speech.flac"
        track = self._existing_track(destination)
        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ), patch.object(
            renderer_module.Track.objects,
            "update_or_create",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaisesRegex(AnnouncementRenderError, "database unavailable"):
                self.renderer.render(self._speech_spec(destination, title="New title"))

        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self.assertEqual(track.title, "Old title")
        self.assertEqual(track.duration_seconds, 9.0)
        self._assert_no_scratch()

    def test_track_failure_for_new_asset_removes_published_file(self):
        destination = self.root / "speech.flac"
        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ), patch.object(
            renderer_module.Track.objects,
            "update_or_create",
            side_effect=RuntimeError("database unavailable"),
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(self._speech_spec(destination))

        self.assertFalse(destination.exists())
        self.assertFalse(Track.objects.filter(filepath=str(destination)).exists())
        self._assert_no_scratch()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_rotation_asset_has_stable_identity_and_analysis_owned_cues(self):
        destination = self.root / "rotation.flac"
        payload = {"bytes": b"ROTATION-ONE", "cue": 1.25, "next": 7.0}

        def convert(_source, output):
            Path(output).write_bytes(payload["bytes"])

        def analyze(track, wave_dir=None, category=None):
            Track.objects.filter(id=track.id).update(
                cue_in_seconds=payload["cue"], next_start_seconds=payload["next"]
            )
            return True

        patches = (
            patch.object(renderer_module, "synthesize_station_voice", side_effect=self._synthesize),
            patch.object(renderer_module, "_convert_wav_to_flac", side_effect=convert),
            patch.object(renderer_module, "_probe_duration", return_value=10.0),
            patch.object(renderer_module, "_analyze_track", side_effect=analyze),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            first = self.renderer.render(self._rotation_spec(destination))
            first_id = first.track.id
            payload.update(bytes=b"ROTATION-TWO", cue=2.5, next=8.25)
            second = self.renderer.render(
                self._rotation_spec(destination, title="Updated rotation")
            )

        second.track.refresh_from_db()
        self.assertEqual(second.track.id, first_id)
        self.assertEqual(second.track.title, "Updated rotation")
        self.assertEqual(second.track.cue_in_seconds, 2.5)
        self.assertEqual(second.track.next_start_seconds, 8.25)
        self.assertEqual(destination.read_bytes(), b"ROTATION-TWO")
        self.assertTrue(second.analysis_succeeded)
        self._assert_no_scratch()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_failed_rotation_regeneration_restores_last_known_good(self):
        destination = self.root / "rotation.flac"
        track = self._existing_track(destination)

        def failed_analysis(updated_track, wave_dir=None, category=None):
            Track.objects.filter(id=updated_track.id).update(
                title="Partially changed", cue_in_seconds=3.0, next_start_seconds=4.0
            )
            return False

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ), patch.object(
            renderer_module, "_analyze_track", side_effect=failed_analysis
        ):
            with self.assertRaisesRegex(AnnouncementRenderError, "analysis did not complete"):
                self.renderer.render(self._rotation_spec(destination, title="New title"))

        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self.assertEqual(track.title, "Old title")
        self.assertEqual(track.duration_seconds, 9.0)
        self.assertEqual(track.cue_in_seconds, 1.0)
        self.assertEqual(track.next_start_seconds, 8.0)
        self._assert_no_scratch()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_rotation_asset_analysis_uses_category_threshold_overrides(self):
        """Rotation Asset must resolve cue thresholds the same way an
        ordinary Track in the same Category would: global AnalysisConfig
        overlaid with that Category's explicit overrides via
        apply_category_thresholds(), not global values alone."""
        from library.management.commands.analyze_tracks import get_waveforms_dir

        global_cfg = AnalysisConfig.load()
        override_next_start = global_cfg.next_start_threshold_db - 5.0
        override_cue_in = global_cfg.cue_in_threshold_db - 7.0
        self.category.next_start_threshold_db_override = override_next_start
        self.category.cue_in_threshold_db_override = override_cue_in
        self.category.save()

        destination = self.root / "rotation.flac"
        wave_dir = get_waveforms_dir()
        self.addCleanup(shutil.rmtree, wave_dir, ignore_errors=True)

        captured = {}

        def fake_analyze_one_track(row, cfg_values, wave_dir, force, existing_artist_names_casefolded=None):
            captured["cfg_values"] = cfg_values
            Track.objects.filter(id=row[0]).update(cue_in_seconds=0.5, next_start_seconds=5.0)
            return True

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=5.0
        ), patch(
            "library.management.commands.analyze_tracks.analyze_one_track",
            side_effect=fake_analyze_one_track,
        ):
            self.renderer.render(self._rotation_spec(destination))

        (sample_rate, window_seconds, target_points,
         next_start_db, cue_in_db, cue_in_min) = captured["cfg_values"]
        self.assertEqual(next_start_db, override_next_start)
        self.assertEqual(cue_in_db, override_cue_in)
        self.assertNotEqual(next_start_db, global_cfg.next_start_threshold_db)
        self.assertNotEqual(cue_in_db, global_cfg.cue_in_threshold_db)
        # Fields the Category doesn't override still come straight from
        # the global AnalysisConfig, unaltered.
        self.assertEqual(sample_rate, global_cfg.analysis_sample_rate)
        self.assertEqual(window_seconds, global_cfg.analysis_window_seconds)
        self.assertEqual(target_points, global_cfg.waveform_points)
        self.assertEqual(cue_in_min, global_cfg.cue_in_min_seconds)
        self._assert_no_scratch()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_speech_splice_analysis_ignores_category_overrides(self):
        """The Category-override path is specific to Rotation Asset --
        Speech Splice reasserts cue=0/next_start=duration regardless, so
        it must keep resolving analysis thresholds from global
        AnalysisConfig only, exactly as before r0055's Rotation fix."""
        from library.management.commands.analyze_tracks import get_waveforms_dir

        self.addCleanup(shutil.rmtree, get_waveforms_dir(), ignore_errors=True)
        global_cfg = AnalysisConfig.load()
        self.category.next_start_threshold_db_override = global_cfg.next_start_threshold_db - 5.0
        self.category.cue_in_threshold_db_override = global_cfg.cue_in_threshold_db - 7.0
        self.category.save()

        destination = self.root / "speech.flac"
        captured = {}

        def fake_analyze_one_track(row, cfg_values, wave_dir, force, existing_artist_names_casefolded=None):
            captured["cfg_values"] = cfg_values
            Track.objects.filter(id=row[0]).update(cue_in_seconds=0, next_start_seconds=6.5)
            return True

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ), patch(
            "library.management.commands.analyze_tracks.analyze_one_track",
            side_effect=fake_analyze_one_track,
        ):
            self.renderer.render(self._speech_spec(destination))

        next_start_db, cue_in_db = captured["cfg_values"][3], captured["cfg_values"][4]
        self.assertEqual(next_start_db, global_cfg.next_start_threshold_db)
        self.assertEqual(cue_in_db, global_cfg.cue_in_threshold_db)
        self._assert_no_scratch()

    def test_mkdir_failure_before_synthesis_raises_typed_error_not_oserror(self):
        """A raw OSError/FileExistsError from the destination-parent mkdir
        happens before any mode helper's own try block. The public
        render() boundary must still convert it to AnnouncementRenderError
        -- callers must never see a raw filesystem exception."""
        blocking_parent = self.root / "blocked"
        blocking_parent.write_bytes(b"an ordinary file, not a directory")
        destination = blocking_parent / "rotation.flac"

        with patch.object(renderer_module, "synthesize_station_voice") as synthesize:
            with self.assertRaises(AnnouncementRenderError) as ctx:
                self.renderer.render(self._rotation_spec(destination))

        self.assertNotIsInstance(ctx.exception, OSError)
        synthesize.assert_not_called()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_failed_rotation_regeneration_restores_last_known_good_waveform_file(self):
        """The advertised LKG guarantee covers the physical waveform JSON,
        not just the FLAC and DB row -- prove the real file on disk is
        restored, not merely that the mocked analyzer's DB writes are
        rolled back."""
        from library.management.commands.analyze_tracks import get_waveforms_dir

        destination = self.root / "rotation.flac"
        track = self._existing_track(destination)
        wave_dir = get_waveforms_dir()
        self.addCleanup(shutil.rmtree, wave_dir, ignore_errors=True)
        waveform_path = wave_dir / f"{track.id}.json"
        waveform_path.write_bytes(b"LAST-GOOD-WAVEFORM")

        def failing_analysis_overwrites_waveform(updated_track, wave_dir=None, category=None):
            (wave_dir / f"{updated_track.id}.json").write_bytes(b"NEW-WAVEFORM")
            Track.objects.filter(id=updated_track.id).update(
                title="Partially changed", cue_in_seconds=3.0, next_start_seconds=4.0
            )
            return False

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ), patch.object(
            renderer_module, "_analyze_track", side_effect=failing_analysis_overwrites_waveform
        ):
            with self.assertRaisesRegex(AnnouncementRenderError, "analysis did not complete"):
                self.renderer.render(self._rotation_spec(destination, title="New title"))

        track.refresh_from_db()
        self.assertEqual(destination.read_bytes(), b"LAST-GOOD")
        self.assertEqual(track.title, "Old title")
        self.assertEqual(track.duration_seconds, 9.0)
        self.assertEqual(track.cue_in_seconds, 1.0)
        self.assertEqual(track.next_start_seconds, 8.0)
        self.assertEqual(waveform_path.read_bytes(), b"LAST-GOOD-WAVEFORM")
        self._assert_no_scratch()

    @override_settings(WAVEFORMS_DIR="/tmp/isadoraair-r0055-test-waveforms")
    def test_failed_rotation_regeneration_for_new_asset_removes_new_waveform(self):
        """A brand-new Rotation Asset has no preexisting waveform. If
        analysis fails after creating one, the renderer must remove that
        newly-created waveform on rollback rather than leaving an orphan
        file with no corresponding Track."""
        from library.management.commands.analyze_tracks import get_waveforms_dir

        destination = self.root / "rotation.flac"
        wave_dir = get_waveforms_dir()
        self.addCleanup(shutil.rmtree, wave_dir, ignore_errors=True)

        created_waveform_path = {}

        def failing_analysis_creates_waveform(updated_track, wave_dir=None, category=None):
            path = wave_dir / f"{updated_track.id}.json"
            path.write_bytes(b"NEW-WAVEFORM")
            created_waveform_path["path"] = path
            return False

        with patch.object(
            renderer_module, "synthesize_station_voice", side_effect=self._synthesize
        ), patch.object(
            renderer_module, "_convert_wav_to_flac", side_effect=self._convert
        ), patch.object(
            renderer_module, "_probe_duration", return_value=6.5
        ), patch.object(
            renderer_module, "_analyze_track", side_effect=failing_analysis_creates_waveform
        ):
            with self.assertRaises(AnnouncementRenderError):
                self.renderer.render(self._rotation_spec(destination))

        self.assertFalse(destination.exists())
        self.assertFalse(Track.objects.filter(filepath=str(destination)).exists())
        self.assertFalse(created_waveform_path["path"].exists())
        self._assert_no_scratch()
