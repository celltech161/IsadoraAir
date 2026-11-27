"""1.24-B: content-only regeneration, published-audio provenance, readiness
ownership and publication integrity.

Control paths run the real command against real files and rows, with only
TTS and ffmpeg faked (KanDriveSynthesisFixtureMixin). RealMediaPublicationTests
at the end uses real ffmpeg and real Track analysis; only the speech itself is
a generated tone. A "killed" publication is simulated by also disabling the
cleanup handlers, since a signal would not run them."""
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import psycopg2
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone as dj_timezone

from isadoraair.tts.models import StationTTSVoice
from library.models import Category, CategoryKind, Track
from library.services import cue_markers
from monitoring.models import SystemEvent
from road_conditions import synthesis as synthesis_module
from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.synthesis import (
    HELD, READY, RETIRED_BY_FRESHNESS, UNATTRIBUTED, SynthesisError, hash_file_sha256,
    inspect_published_report, read_provenance, road_report_path, road_report_previous_path,
    road_report_provenance_path, road_report_retirement_path, road_report_text_path,
)
from road_conditions.tests.test_generate_road_condition_audio import CMD, make_fresh_config
from road_conditions.tests.test_kandrive_report import make_road_event
from road_conditions.tests.test_kandrive_synthesis import (
    KanDriveSynthesisFixtureMixin, ensure_default_weather_voice_personas,
)
from road_conditions.voice import VoiceResolutionError
from weather.models import WeatherConfig, WeatherVoicePersona

SKIPPED = "unchanged since last successful generation"
REARMED = "re-armed without synthesis"


class SimulatedKill(BaseException):
    pass


def use_voice_all_day(slot):
    config = WeatherConfig.load()
    config.voice_schedule = [[slot, 0, 23]]
    config.save(update_fields=["voice_schedule"])


def generate(*args):
    out = StringIO()
    call_command("generate_road_condition_audio", *args, stdout=out)
    return out.getvalue()


def set_config(**fields):
    RoadConditionsConfiguration.load()
    RoadConditionsConfiguration.objects.update(**fields)


def feed_fails():
    set_config(last_error="CARS API returned HTTP 404")


def feed_recovers():
    set_config(last_error="", last_fetch_succeeded_at=dj_timezone.now())


def track():
    return Track.objects.get(filepath=str(road_report_path()))


def analysis_writes_markers(row):
    """Analysis reaching the cue-marker authority, as the real one does."""
    result = cue_markers.apply_calculation(
        row.pk, {"next_start_seconds": 250.0}, basis_revision=row.cue_marker_revision,
        source=cue_markers.SOURCE_ANALYSIS, media_replaced=True, duration=row.duration_seconds)
    assert result.ok, (result.error, result.conflict)
    assert Track.objects.get(pk=row.pk).cue_marker_revision != row.cue_marker_revision


def set_ready(value):
    """What an operator's ready2air change does to the row."""
    Track.objects.filter(filepath=str(road_report_path())).update(ready2air=value)


class PublishedReportMixin(KanDriveSynthesisFixtureMixin):
    """One road event, rendered once by the day voice."""

    def setUp(self):
        super().setUp()
        make_fresh_config()
        use_voice_all_day("day")
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        self._flac_payload = b"FLAC-ONE"
        generate()
        assert self._kokoro_call_count == 1

    def renders(self):
        return self._kokoro_call_count

    def provenance(self):
        record, problem = read_provenance()
        self.assertEqual(problem, "")
        return record

    def fingerprint(self):
        return RoadConditionsConfiguration.load().last_report_fingerprint

    def assert_skips(self, *args):
        before = self.renders()
        out = generate(*args)
        self.assertEqual(self.renders(), before, out)
        self.assertIn(SKIPPED, out)
        return out

    def assert_renders(self, reason, *args):
        before = self.renders()
        self._flac_payload = f"FLAC-{before + 1}".encode()
        out = generate(*args)
        self.assertEqual(self.renders(), before + 1, out)
        self.assertIn(f"reason={reason}", out)
        return out

    def change_content(self, text="Both lanes are closed."):
        RoadEvent.objects.filter(external_id="A").update(description=text)


class VoiceChangesDoNotRegenerateTests(PublishedReportMixin, TransactionTestCase):
    def assert_unchanged_and_still_identified_as_claira(self):
        before = road_report_provenance_path().read_bytes()
        self.assert_skips()
        self.assertEqual(road_report_provenance_path().read_bytes(), before)
        record = self.provenance()
        self.assertEqual(record["announcer"], {"slot": "day", "name": "Claira", "full_name": "Claira Sky"})
        self.assertEqual(record["voice"]["provider_voice"], "af_jessica")
        self.assertIn("Claira Sky", road_report_text_path().read_text(encoding="utf-8"))
        self.assertEqual(track().title, "KanDrive Road Report (Claira)")
        self.assertTrue(track().ready2air)

    def test_every_hour_of_a_real_shift_schedule_skips(self):
        config = WeatherConfig.load()
        config.voice_schedule = [["day", 3, 8], ["night", 9, 14], ["day", 15, 20], ["night", 21, 2]]
        config.save(update_fields=["voice_schedule"])
        base = dj_timezone.localtime().replace(minute=2, second=0, microsecond=0)
        for hour in range(24):
            moment = base.replace(hour=hour)
            with patch("django.utils.timezone.now", return_value=moment):
                set_config(last_fetch_succeeded_at=moment)
                self.assert_skips()
        self.assertEqual(self.renders(), 1)

    def test_shift_to_the_other_announcer_skips(self):
        use_voice_all_day("night")
        self.assert_unchanged_and_still_identified_as_claira()

    def test_persona_rename_skips(self):
        WeatherVoicePersona.objects.filter(slot="day").update(display_name="Clare", full_name="Clare Skye")
        self.assert_unchanged_and_still_identified_as_claira()

    def test_voice_remap_and_model_change_skip(self):
        StationTTSVoice.objects.filter(name="Claira_Sky").update(provider_voice="af_bella", speed=1.1)
        self.assert_unchanged_and_still_identified_as_claira()

    def test_previously_used_voice_disabled_skips(self):
        StationTTSVoice.objects.filter(name="Claira_Sky").update(enabled=False)
        self.assert_unchanged_and_still_identified_as_claira()

    def test_previously_used_persona_removed_skips(self):
        use_voice_all_day("night")
        WeatherVoicePersona.objects.filter(slot="day").delete()
        self.assert_unchanged_and_still_identified_as_claira()

    def test_unchanged_report_never_resolves_a_voice(self):
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")) as resolve:
            self.assert_skips()
        resolve.assert_not_called()

    def test_changed_report_with_broken_voice_fails_and_keeps_the_old_report(self):
        self.change_content()
        old = self.fingerprint()
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")):
            with self.assertRaises(CommandError):
                generate()
        self.assertEqual(self.fingerprint(), old)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.assertTrue(track().ready2air)

    def test_changed_report_is_rendered_with_the_current_voice(self):
        use_voice_all_day("night")
        self.change_content()
        self.assert_renders("content_changed")
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")
        self.assertIn("{announcer_name}", RoadConditionsConfiguration.load().report_postamble)
        self.assertIn("Max Weatherly", road_report_text_path().read_text(encoding="utf-8"))
        self.assertIn("Max Weatherly", self.provenance()["script"])
        self.assertEqual(track().title, "KanDrive Road Report (Max)")


class ContentIdentityTests(PublishedReportMixin, TransactionTestCase):
    def test_new_changed_and_cleared_events_each_render_once(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.")
        self.assert_renders("content_changed")
        self.assert_skips()
        self.change_content()
        self.assert_renders("content_changed")
        self.assert_skips()
        RoadEvent.objects.filter(external_id="B").update(source_active=False)
        self.assert_renders("content_changed")
        RoadEvent.objects.update(source_active=False)
        self.assert_renders("content_changed")
        self.assertIn("not reporting any significant road conditions",
                      road_report_text_path().read_text(encoding="utf-8"))
        self.assert_skips()

    def test_priority_change_that_reorders_the_spoken_report_renders(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.", priority=3)
        make_road_event(external_id="C", headline_category="roadwork", description="Paving work.", priority=2)
        self.assert_renders("content_changed")
        RoadEvent.objects.filter(external_id="C").update(priority=4)
        self.assert_renders("content_changed")

    def test_priority_change_that_keeps_the_order_does_not_render(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.", priority=3)
        self.assert_renders("content_changed")
        RoadEvent.objects.filter(external_id="B").update(priority=4)
        self.assert_skips()

    def test_incidental_record_fields_and_whitespace_do_not_render(self):
        now = dj_timezone.now()
        RoadEvent.objects.update(source_update_time=now, source_next_update_time=now + timedelta(days=14),
                                 update_number=99, payload_checksum="f" * 64, last_seen_at=now,
                                 last_changed_at=now, description="  Road   closed.  ")
        config = RoadConditionsConfiguration.load()
        set_config(report_preamble=f"  {config.report_preamble}  ")
        self.assert_skips()

    def test_upstream_record_order_does_not_matter(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.")
        make_road_event(external_id="C", headline_category="roadwork", description="Paving work.")
        self.assert_renders("content_changed")
        rows = list(RoadEvent.objects.order_by("-external_id").values())
        RoadEvent.objects.all().delete()
        for row in rows:
            row.pop("id")
            RoadEvent.objects.create(**row)
        self.assert_skips()

    def test_template_and_transition_sound_changes_render(self):
        make_road_event(external_id="B", headline_category="roadwork", description="Bridge work.")
        self.assert_renders("content_changed")
        set_config(report_postamble="That is the report. I'm {announcer_name}.")
        self.assert_renders("content_changed")
        sound = Path(self._tmpdir.name) / "horn.bin"
        sound.write_bytes(b"horn-one")
        set_config(transition_sound_enabled=True, transition_sound_path=str(sound))
        before = self.renders()
        generate()
        self.assertEqual(self.renders(), before + 2)        # two segments, one report
        self.assertEqual(self.provenance()["rendering"]["transition"],
                         {"active": True, "sound_sha256": hash_file_sha256(sound), "segment_count": 2})
        self.assert_skips()
        sound.write_bytes(b"horn-two")
        out = generate()
        self.assertIn("reason=content_changed", out)


class ProvenanceTests(PublishedReportMixin, TransactionTestCase):
    def test_records_what_was_actually_published(self):
        record = self.provenance()
        script = road_report_text_path().read_text(encoding="utf-8")
        self.assertEqual(record["semantic_fingerprint"], self.fingerprint())
        self.assertEqual(record["script"], script)
        self.assertEqual(record["flac_sha256"], hash_file_sha256(road_report_path()))
        self.assertEqual(record["voice"], {"logical_voice_name": "Claira_Sky", "engine": "kokoro",
                                           "provider_voice": "af_jessica", "language": "en-us", "speed": 1.0})
        self.assertEqual(record["model"]["filename"], "kokoro-v1.0.onnx")
        self.assertEqual(len(record["model"]["contract_sha256"]), 64)
        self.assertEqual(record["rendering"]["audio_format_version"], 1)
        self.assertEqual(record["track"]["id"], track().id)
        self.assertTrue(record["published_at"])
        self.assertFalse(road_report_previous_path().exists())

    def test_each_damaged_artifact_has_a_defined_recovery_render(self):
        def corrupt_audio():
            road_report_path().write_bytes(b"garbage")

        def remove_audio():
            road_report_path().unlink()

        def edit_text():
            road_report_text_path().write_text("something else", encoding="utf-8")

        def remove_text():
            road_report_text_path().unlink()

        def remove_provenance():
            road_report_provenance_path().unlink()

        def garble_provenance():
            road_report_provenance_path().write_text("{not json", encoding="utf-8")

        def edit_record(**changes):
            record = json.loads(road_report_provenance_path().read_text(encoding="utf-8"))
            record.update(changes)
            road_report_provenance_path().write_text(json.dumps(record), encoding="utf-8")

        cases = [
            (corrupt_audio, "flac_mismatch"),
            (remove_audio, "flac_missing"),
            (edit_text, "text_mismatch"),
            (remove_text, "text_missing"),
            (remove_provenance, "provenance_missing"),
            (garble_provenance, "provenance_invalid"),
            (lambda: edit_record(provenance_version=99), "provenance_invalid"),
            (lambda: edit_record(flac_sha256="0" * 64), "flac_mismatch"),
            (lambda: edit_record(script="not what was published"), "text_mismatch"),
            (lambda: edit_record(semantic_fingerprint="0" * 64), "provenance_content_mismatch"),
            (lambda: edit_record(track={"id": 10**9, "duration_seconds": 1.0, "cue_marker_revision": 0}),
             "track_mismatch"),
            (lambda: Track.objects.filter(filepath=str(road_report_path())).delete(), "track_missing"),
        ]
        for damage, reason in cases:
            with self.subTest(reason=reason):
                damage()
                self.assert_renders(reason)
                self.assert_skips()

    def test_regenerate_renders_an_unchanged_report_in_the_current_voice(self):
        use_voice_all_day("night")
        self.assert_renders("operator_regenerate", "--regenerate")
        self.assertEqual(self.provenance()["announcer"]["name"], "Max")

    def test_one_time_transition_from_the_version_one_fingerprint(self):
        # r0149 state: audio, Track, text and a v1 fingerprint; no sidecar.
        road_report_provenance_path().unlink()
        set_config(last_report_fingerprint="1" * 64)
        self.assert_renders("provenance_missing")
        use_voice_all_day("night")
        self.assert_skips()
        self.assertEqual(self.renders(), 2)

    def test_report_retired_by_the_older_release_recovers(self):
        road_report_provenance_path().unlink()
        set_config(last_report_fingerprint="1" * 64)
        set_ready(False)
        self.assert_renders("provenance_missing")
        self.assertTrue(track().ready2air)

    def test_dry_run_writes_nothing(self):
        self.change_content()
        def snapshot():
            return sorted((p.name, p.read_bytes()) for p in Path(self._tmpdir.name).iterdir())
        before = snapshot()
        out = generate("--dry-run")
        self.assertIn("Would skip generation: no", out)
        self.assertIn("Render reason: content_changed", out)
        self.assertEqual(snapshot(), before)


class RetirementRecoveryTests(PublishedReportMixin, TransactionTestCase):
    def retire(self):
        feed_fails()
        self.assertIn("Retired the existing KanDrive report", generate())
        self.assertFalse(track().ready2air)

    def test_rearms_identical_content_without_rendering(self):
        generated_at = RoadConditionsConfiguration.load().last_report_generated_at
        self.retire()
        self.assertEqual(inspect_published_report(self.fingerprint()).readiness, RETIRED_BY_FRESHNESS)
        self.assertIn("No existing KanDrive report to retire", generate())  # still failed: unchanged
        use_voice_all_day("night")
        feed_recovers()
        with patch(f"{CMD}.resolve_voice", side_effect=VoiceResolutionError("broken")):
            out = generate()
        self.assertIn(REARMED, out)
        self.assertEqual(self.renders(), 1)
        self.assertTrue(track().ready2air)
        self.assertFalse(road_report_retirement_path().exists())
        self.assertEqual(RoadConditionsConfiguration.load().last_report_generated_at, generated_at)
        self.assert_skips()

    def test_stale_data_retirement_also_rearms(self):
        set_config(last_fetch_succeeded_at=dj_timezone.now() - timedelta(days=1))
        generate()
        feed_recovers()
        self.assertIn(REARMED, generate())
        self.assertEqual(self.renders(), 1)

    def test_repeated_retire_and_rearm_cycles(self):
        for _ in range(3):
            self.retire()
            feed_recovers()
            self.assertIn(REARMED, generate())
        self.assertEqual(self.renders(), 1)

    def test_force_on_a_stale_feed_never_rearms(self):
        self.retire()
        before = self.renders()
        out = generate("--force")
        self.assertNotIn(REARMED, out)
        self.assertEqual(self.renders(), before + 1)

    def test_content_changed_during_the_outage_renders_and_airs(self):
        self.retire()
        feed_recovers()
        self.change_content()
        self.assert_renders("content_changed")
        self.assertTrue(track().ready2air)
        self.assertFalse(road_report_retirement_path().exists())

    def test_audio_damaged_during_the_outage_is_not_rearmed(self):
        self.retire()
        road_report_path().write_bytes(b"damaged")
        feed_recovers()
        before = self.renders()
        self.assertNotIn(REARMED, generate())
        self.assertEqual(self.renders(), before + 1)

    def test_row_touched_after_retirement_falls_back_to_the_recovery_render(self):
        self.retire()
        set_ready(False)            # same value, but the row was written
        self.assertEqual(inspect_published_report(self.fingerprint()).readiness, UNATTRIBUTED)
        feed_recovers()
        self.assert_renders("readiness_unattributed")
        self.assertTrue(track().ready2air)

    def test_track_no_longer_in_the_report_category_is_not_rearmed(self):
        self.retire()
        other, _ = Category.objects.get_or_create(
            code="Elsewhere", defaults={"name": "Elsewhere", "kind": CategoryKind.objects.first()})
        record = json.loads(road_report_retirement_path().read_text(encoding="utf-8"))
        Track.objects.filter(filepath=str(road_report_path())).update(category=other)
        record["row_version"] = synthesis_module._track_row_version(track().id)
        road_report_retirement_path().write_text(json.dumps(record), encoding="utf-8")
        feed_recovers()
        self.assert_renders("retired_report_not_rearmable")

    def test_unwritable_retirement_record_still_retires_and_recovers_by_rendering(self):
        real = synthesis_module._write_json_atomically

        def fail_record(path, payload):
            if path == road_report_retirement_path():
                raise OSError("disk full")
            return real(path, payload)

        with patch.object(synthesis_module, "_write_json_atomically", side_effect=fail_record):
            self.retire()
        feed_recovers()
        self.assert_renders("provenance_missing")
        self.assertTrue(track().ready2air)

    def test_retirement_interrupted_before_its_record_is_complete(self):
        real = synthesis_module._write_json_atomically
        calls = []

        def only_intent(path, payload):
            if path == road_report_retirement_path():
                calls.append(payload)
                if payload["row_version"] is not None:
                    raise OSError("interrupted")
            return real(path, payload)

        with patch.object(synthesis_module, "_write_json_atomically", side_effect=only_intent):
            self.retire()
        self.assertEqual(len(calls), 2)
        feed_recovers()
        self.assert_renders("readiness_unattributed")


class UnrelatedReadinessRestrictionTests(PublishedReportMixin, TransactionTestCase):
    def test_hold_is_preserved_and_release_restores_normal_skipping(self):
        set_ready(False)
        self.assertEqual(inspect_published_report(self.fingerprint()).readiness, HELD)
        for _ in range(3):
            self.assertIn("held off air", generate())
        self.assertEqual(self.renders(), 1)
        self.assertFalse(track().ready2air)
        set_ready(True)
        self.assert_skips()

    def test_hold_placed_before_an_outage_survives_it(self):
        set_ready(False)
        feed_fails()
        self.assertIn("No existing KanDrive report to retire", generate())
        feed_recovers()
        self.assertIn("held off air", generate())
        self.assertFalse(track().ready2air)
        self.assertEqual(self.renders(), 1)

    def test_hold_after_a_completed_rearm_is_still_a_hold(self):
        feed_fails()
        generate()
        feed_recovers()
        generate()
        set_ready(False)
        self.assertIn("held off air", generate())
        self.assertFalse(track().ready2air)

    def test_new_content_is_published_but_stays_held_and_is_reported(self):
        set_ready(False)
        self.change_content()
        out = self.assert_renders("content_changed")
        self.assertIn("left held off air", out)
        self.assertFalse(track().ready2air)
        self.assertTrue(SystemEvent.objects.filter(dedupe_key="road_conditions|kandrive-held").exists())
        self.assertIn("held off air", generate())
        self.assertEqual(self.renders(), 2)

    def test_regenerate_does_not_release_a_hold(self):
        set_ready(False)
        self.assert_renders("operator_regenerate", "--regenerate")
        self.assertFalse(track().ready2air)

    def test_hold_placed_while_rendering_is_honored(self):
        real = synthesis_module.synthesize_station_voice.side_effect

        def hold_during_synthesis(*args, **kwargs):
            set_ready(False)
            return real(*args, **kwargs)

        synthesis_module.synthesize_station_voice.side_effect = hold_during_synthesis
        self.change_content()
        self.assert_renders("content_changed")
        self.assertFalse(track().ready2air)

    def test_hold_survives_a_failed_publication_and_the_retry(self):
        set_ready(False)
        self.change_content()
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                generate()
        self.assertFalse(track().ready2air)
        self.assert_renders("content_changed")
        self.assertFalse(track().ready2air)

    def test_hold_survives_a_publication_that_could_not_be_recorded(self):
        set_ready(False)
        self.change_content()
        self._flac_payload = b"FLAC-UNRECORDED"
        with patch.object(synthesis_module, "write_road_report_text", side_effect=OSError("disk full")):
            with self.assertRaises(CommandError):
                generate()
        self.assert_renders("content_changed")
        self.assertFalse(track().ready2air)

    def test_a_stale_record_from_other_audio_is_not_evidence_of_a_hold(self):
        # An older release rewrites the audio without updating the record,
        # then retires it: that must never be mistaken for a hold.
        road_report_path().write_bytes(b"written-by-an-older-release")
        set_ready(False)
        self.assertEqual(inspect_published_report(self.fingerprint()).readiness, UNATTRIBUTED)
        before = self.renders()
        generate()
        self.assertEqual(self.renders(), before + 1)
        self.assertTrue(track().ready2air)


class PublicationFailureTests(PublishedReportMixin, TransactionTestCase):
    def snapshot(self):
        row = track()
        return (road_report_path().read_bytes(), road_report_text_path().read_text(encoding="utf-8"),
                row.title, row.duration_seconds, row.ready2air, row.cue_marker_revision, self.fingerprint())

    def fail_new_content(self, patcher):
        before = self.snapshot()
        use_voice_all_day("night")
        self.change_content()
        self._flac_payload = b"FLAC-NEW"
        with patcher:
            with self.assertRaises(CommandError):
                generate()
        use_voice_all_day("day")
        return before

    def revert_and_expect_render(self):
        """The content goes back to what was last published successfully:
        the failed attempt must still not be skipped over."""
        self.change_content("Road closed.")
        before = self.renders()
        self._flac_payload = b"FLAC-REVERTED"
        out = generate()
        self.assertEqual(self.renders(), before + 1, out)
        self.assertNotIn(SKIPPED, out)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-REVERTED")
        self.assert_skips()

    def assert_previous_restored(self, before):
        self.assertEqual(self.snapshot(), before)
        self.assertEqual([p.name for p in Path(self._tmpdir.name).iterdir() if p.name.startswith(".")], [])
        self.assertEqual(self.provenance()["semantic_fingerprint"], "")

    def test_validation_failure_changes_nothing_and_old_report_stays_healthy(self):
        before = self.snapshot()
        record = road_report_provenance_path().read_bytes()
        with patch.object(synthesis_module, "_validate_candidate", side_effect=ValueError("bad decode")):
            with self.assertRaises(CommandError):
                generate("--regenerate")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(road_report_provenance_path().read_bytes(), record)
        self.assert_skips()

    def test_replace_failure_changes_nothing(self):
        before = self.snapshot()
        self.change_content()
        with patch.object(synthesis_module.os, "link", side_effect=OSError("no space")):
            with self.assertRaises(CommandError):
                generate()
        self.assertEqual(self.snapshot(), before)

    def test_track_update_failure_restores_previous_audio(self):
        before = self.fail_new_content(
            patch.object(Track.objects, "update_or_create", side_effect=RuntimeError("db down")))
        self.assert_previous_restored(before)
        self.revert_and_expect_render()

    def test_analysis_failure_restores_previous_audio_and_track(self):
        before = self.fail_new_content(
            patch.object(synthesis_module, "_run_full_analysis", return_value=False))
        self.assert_previous_restored(before)
        self.revert_and_expect_render()

    def test_analysis_exception_after_partial_writes_is_not_accepted_as_healthy(self):
        reached = []

        def partial(row):
            analysis_writes_markers(row)
            reached.append(True)
            raise RuntimeError("analysis crashed")

        self.fail_new_content(patch.object(synthesis_module, "_run_full_analysis", side_effect=partial))
        self.assertEqual(reached, [True])
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.revert_and_expect_render()

    def test_failed_regenerate_of_unchanged_content_is_retried(self):
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                generate("--regenerate")
        self.assert_renders("previous_publication_incomplete")

    def test_text_write_failure_keeps_new_audio_unrecorded(self):
        self.fail_new_content(
            patch.object(synthesis_module, "write_road_report_text", side_effect=OSError("disk full")))
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")
        self.assertEqual(road_report_previous_path().read_bytes(), b"FLAC-ONE")   # still recoverable
        self.assertEqual(self.provenance()["semantic_fingerprint"], "")
        self.revert_and_expect_render()

    def test_provenance_write_failure_keeps_new_audio_unrecorded(self):
        real = synthesis_module._write_json_atomically

        def fail_final_record(path, payload):
            if "published_at" in payload:
                raise OSError("disk full")
            return real(path, payload)

        old = self.fingerprint()
        self.fail_new_content(
            patch.object(synthesis_module, "_write_json_atomically", side_effect=fail_final_record))
        self.assertEqual(self.fingerprint(), old)
        self.revert_and_expect_render()

    def test_first_ever_publication_failure_leaves_nothing_airable(self):
        Track.objects.filter(filepath=str(road_report_path())).delete()
        for path in Path(self._tmpdir.name).iterdir():
            path.unlink()
        set_config(last_report_fingerprint="")
        with patch.object(synthesis_module, "_run_full_analysis", return_value=False):
            with self.assertRaises(CommandError):
                generate()
        self.assertFalse(road_report_path().exists())
        self.assertFalse(track().ready2air)
        self.assertFalse(road_report_provenance_path().exists())
        generate()
        self.assertTrue(track().ready2air)
        self.assert_skips()

    def test_row_is_not_locked_while_the_audio_is_analyzed(self):
        # The engine updates play_count on this row from its main loop
        # when the report starts to air; it must never wait on analysis.
        seen = []

        def engine_air_start(row):
            conn = psycopg2.connect(**connections["default"].get_connection_params())
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET lock_timeout = '1s'")
                    cur.execute("UPDATE library_track SET play_count = play_count + 1 WHERE id = %s", [row.pk])
                    seen.append(cur.rowcount)
            finally:
                conn.close()
            return True

        self.change_content()
        with patch.object(synthesis_module, "_run_full_analysis", side_effect=engine_air_start):
            generate()
        self.assertEqual(seen, [1])


class InterruptedPublicationTests(PublishedReportMixin, TransactionTestCase):
    def kill_during(self, target, name):
        use_voice_all_day("night")
        self.change_content()
        self._flac_payload = b"FLAC-NEW"
        with patch.object(synthesis_module, "_roll_back_publication"), \
             patch.object(synthesis_module, "_invalidate_provenance"), \
             patch.object(target, name, side_effect=SimulatedKill()):
            with self.assertRaises(SimulatedKill):
                generate()
        use_voice_all_day("day")
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")
        self.assertEqual(road_report_previous_path().read_bytes(), b"FLAC-ONE")
        self.assertTrue(inspect_published_report(self.fingerprint()).problem)

    def test_killed_before_the_track_update_restores_the_recorded_audio(self):
        self.kill_during(Track.objects, "update_or_create")
        self.change_content("Road closed.")         # content reverts
        out = self.assert_skips()
        self.assertIn("Restored the previous report audio", out)
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-ONE")
        self.assertFalse(road_report_previous_path().exists())

    def test_killed_before_the_track_update_then_renders_new_content(self):
        self.kill_during(Track.objects, "update_or_create")
        self.assert_renders("content_changed")
        self.assert_skips()

    def test_killed_during_analysis_is_never_healthy_even_if_content_reverts(self):
        # The row already moved on (new duration), so nothing is restored.
        with patch.object(synthesis_module, "_probe_duration", return_value=301.0):
            self.kill_during(synthesis_module, "_run_full_analysis")
        self.change_content("Road closed.")
        self.assert_renders("flac_mismatch")
        self.assert_skips()
        self.assertFalse(road_report_previous_path().exists())

    def test_killed_before_the_record_is_never_healthy(self):
        def analyzed(row):
            analysis_writes_markers(row)
            return True

        with patch.object(synthesis_module, "_run_full_analysis", side_effect=analyzed):
            self.kill_during(synthesis_module, "write_road_report_text")
        self.change_content("Road closed.")
        self.assert_renders("flac_mismatch")

    def test_dry_run_does_not_restore(self):
        self.kill_during(Track.objects, "update_or_create")
        generate("--dry-run")
        self.assertEqual(road_report_path().read_bytes(), b"FLAC-NEW")

    def test_leftover_copy_after_a_recorded_publication_is_removed(self):
        road_report_previous_path().write_bytes(b"leftover")
        self.assert_skips()
        self.assertFalse(road_report_previous_path().exists())

    def test_restart_with_a_valid_publication_skips(self):
        for _ in range(3):
            connections["default"].close()
            self.assert_skips()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg is required")
class RealMediaPublicationTests(TransactionTestCase):
    """Real ffmpeg encode, strict decode, Track analysis and waveform; only
    the speech is replaced by a short generated tone."""

    def setUp(self):
        kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        Category.objects.get_or_create(code="KanDrive", defaults={"name": "KanDrive", "kind": kind})
        ensure_default_weather_voice_personas()
        make_fresh_config()
        use_voice_all_day("day")
        make_road_event(external_id="A", headline_category="closure", description="Road closed.")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        waveforms = override_settings(WAVEFORMS_DIR=str(self.root / "waveforms"))
        waveforms.enable()
        self.addCleanup(waveforms.disable)
        for name, value in (("KANDRIVE_ROOT", self.root / "KanDrive"), ("TMP_AUDIO_ROOT", self.root / "tmp")):
            patcher = patch.object(synthesis_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.frequency = 440
        self.renders = 0

        def tone(text, *, voice, output_path, timeout_seconds=None, **kwargs):
            self.renders += 1
            subprocess.run(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency={self.frequency}:sample_rate=24000:duration=3",
                 "-ac", "1", "-c:a", "pcm_s16le", str(output_path)], check=True, timeout=30)

        speech = patch.object(synthesis_module, "synthesize_station_voice", side_effect=tone)
        speech.start()
        self.addCleanup(speech.stop)

    def test_publish_verify_rollback_and_republish(self):
        generate()
        row = track()
        record, problem = read_provenance()
        self.assertEqual(problem, "")
        self.assertTrue(row.ready2air)
        self.assertEqual(row.cue_marker_revision, record["track"]["cue_marker_revision"])
        self.assertIsNotNone(row.next_start_seconds)
        waveform = Path(row.waveform_path)
        self.assertTrue(waveform.is_file())
        self.assertTrue(str(waveform).startswith(str(self.root)))
        self.assertIn(SKIPPED, generate())
        self.assertEqual(self.renders, 1)
        first_audio = road_report_path().read_bytes()
        first_waveform = waveform.read_bytes()

        # Real analysis fails (every decode fails) after the replacement.
        self.frequency = 880
        RoadEvent.objects.update(description="Both lanes are closed.")
        with patch("library.management.commands.analyze_tracks.decode_audio_to_pcm", return_value=b""):
            with self.assertRaises(CommandError):
                generate()
        self.assertEqual(road_report_path().read_bytes(), first_audio)
        self.assertEqual(waveform.read_bytes(), first_waveform)
        self.assertEqual(track().duration_seconds, row.duration_seconds)
        self.assertTrue(track().ready2air)

        # The next ordinary run completes, and the waveform lock was released.
        generate()
        self.assertNotEqual(road_report_path().read_bytes(), first_audio)
        self.assertNotEqual(waveform.read_bytes(), first_waveform)
        self.assertEqual(inspect_published_report(
            RoadConditionsConfiguration.load().last_report_fingerprint).readiness, READY)
        self.assertFalse(road_report_previous_path().exists())
        self.assertEqual(list((self.root / "waveforms").glob(".*previous*")), [])
        self.assertIn(SKIPPED, generate())

    def test_flipped_bytes_in_the_published_audio_are_detected(self):
        generate()
        data = bytearray(road_report_path().read_bytes())
        data[len(data) // 2] ^= 0xFF
        road_report_path().write_bytes(bytes(data))
        self.assertIn("reason=flac_mismatch", generate())
        self.assertEqual(self.renders, 2)

    def test_strict_decode_gate_rejects_truncated_and_non_audio_candidates(self):
        generate()
        good = road_report_path().read_bytes()
        duration = synthesis_module._probe_duration(road_report_path())
        synthesis_module._validate_candidate(road_report_path(), duration)
        for name, data in (("truncated", good[:len(good) // 2]), ("noise", b"not audio" * 1000)):
            with self.subTest(name=name):
                candidate = self.root / f"{name}.flac"
                candidate.write_bytes(data)
                with self.assertRaises(Exception):
                    synthesis_module._validate_candidate(candidate, duration)

    def test_truncated_encode_never_replaces_the_published_report(self):
        generate()
        good = road_report_path().read_bytes()
        real = synthesis_module._run_loudnorm_two_pass

        def truncate_after_encode(build, tmp_flac):
            real(build, tmp_flac)
            tmp_flac.write_bytes(tmp_flac.read_bytes()[:tmp_flac.stat().st_size // 2])

        with patch.object(synthesis_module, "_run_loudnorm_two_pass", side_effect=truncate_after_encode):
            with self.assertRaises(CommandError):
                generate("--regenerate")
        self.assertEqual(road_report_path().read_bytes(), good)
        self.assertIn(SKIPPED, generate())
