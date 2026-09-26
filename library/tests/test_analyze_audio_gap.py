import json
import tempfile
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from hardware.models import AudioPipeline


class AnalyzeAudioGapCommandTests(TestCase):
    def _run(self, records):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "audio-gap.jsonl"
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            target = datetime.fromtimestamp(records[0]["wall_ts"]).astimezone()
            output = StringIO()
            call_command(
                "analyze_audio_gap", target.isoformat(), path=str(path),
                no_journal=True, stdout=output,
            )
            return output.getvalue()

    def _record(self, **overrides):
        record = {
            "schema": 3,
            "wall_ts": time.time(),
            "sample_interval_ms": 50.0,
            "pre": {
                "buffer_age_ms": 2.0,
                "buffer_delta": 10,
                "arrival_jitter_delta": 0,
                "continuity_event_delta": 0,
            },
            "queue": {"level_time_ns": 40_000_000},
            "sink": {"rendered_delta": 10},
            "alsa": {"state": "RUNNING", "delay": 7800},
            "post": {
                "age_ms": 4.0,
                "frame_age_ms": 3.0,
                "frame_interval_ms": 20.0,
                "arrival_jitter_delta": 0,
            },
        }
        for key, value in overrides.items():
            record[key] = value
        return record

    def test_raw_arrival_jitter_alone_is_advisory_not_gap_evidence(self):
        record = self._record()
        record["pre"]["arrival_jitter_delta"] = 3
        record["pre"]["last_arrival_jitter_late_by_ns"] = 15_000_000

        output = self._run([record])

        self.assertIn("pre_arrival_jitter_advisory=3", output)
        self.assertIn(
            "boundary assessment: pre-StereoTool=none, ALSA runway=none, "
            "post-StereoTool=none", output)
        self.assertIn("no corroborated audio-gap evidence", output)
        self.assertIn("advisory scheduler context only", output)

    def test_boundary_evidence_is_reported_separately(self):
        record = self._record()
        record["pre"]["continuity_event_delta"] = 1
        record["pre"]["last_continuity_pts_gap_ns"] = 10_000_000
        record["alsa"]["delay"] = 1000
        record["post"]["arrival_jitter_delta"] = 1

        output = self._run([record])

        self.assertIn(
            "boundary assessment: pre-StereoTool=EVIDENCE, ALSA runway=EVIDENCE, "
            "post-StereoTool=ADVISORY", output)
        self.assertIn("corroborating continuity/runway evidence is present", output)

    def test_post_frame_age_not_writer_age_identifies_current_stall(self):
        healthy_frames = self._record()
        healthy_frames["post"]["age_ms"] = 500.0
        healthy_frames["post"]["frame_age_ms"] = 5.0
        output = self._run([healthy_frames])
        self.assertIn("post-StereoTool=none", output)

        stalled_frames = self._record()
        stalled_frames["post"]["age_ms"] = 5.0
        stalled_frames["post"]["frame_age_ms"] = 500.0
        output = self._run([stalled_frames])
        self.assertIn("post-StereoTool=ADVISORY", output)
        self.assertIn("no corroborated audio-gap evidence", output)

    def test_legacy_combined_latch_with_only_arrival_delay_is_advisory(self):
        record = self._record()
        record["schema"] = 1
        record["pre"] = {
            "buffer_age_ms": 2.0,
            "buffer_delta": 10,
            "transient_delta": 2,
            "last_transient_pts_gap_ns": 0,
            "last_transient_discont": False,
            "last_transient_arrival_late_by_ns": 20_000_000,
        }

        output = self._run([record])

        self.assertIn("pre_continuity=0", output)
        self.assertIn("pre_arrival_jitter_advisory=2", output)
        self.assertIn("no corroborated audio-gap evidence", output)

    def test_disabled_without_ring_reports_telemetry_unavailable(self):
        pipeline = AudioPipeline.load()
        pipeline.audio_gap_diagnostics_enabled = False
        pipeline.save(update_fields=["audio_gap_diagnostics_enabled"])
        with tempfile.TemporaryDirectory() as tmpdir:
            output = StringIO()
            call_command(
                "analyze_audio_gap", datetime.now().astimezone().isoformat(),
                path=str(Path(tmpdir) / "missing.jsonl"), no_journal=True,
                stdout=output,
            )

        self.assertIn(
            "Audio-gap diagnostics are not currently enabled or no retained "
            "diagnostic data is available.", output.getvalue())
