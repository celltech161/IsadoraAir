"""Summarize the bounded audio-gap ring around an operator timestamp."""

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from hardware.models import AudioPipeline
from library.services.engine import AUDIO_GAP_DIAG_PATH


def _nested(record, *keys):
    value = record
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _finite_numbers(values):
    return [value for value in values if isinstance(value, (int, float))]


def _load_records(path):
    records = []
    for candidate in (Path(f"{path}.1"), Path(path)):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, dict) and isinstance(record.get("wall_ts"), (int, float)):
                    records.append(record)
            except json.JSONDecodeError:
                continue
    records.sort(key=lambda item: item["wall_ts"])
    return records


def _maximum_zero_delta_run(records, *keys):
    longest = current = 0
    for record in records:
        if _nested(record, *keys) == 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


class Command(BaseCommand):
    help = "Summarize pre/ALSA/post-StereoTool diagnostic evidence around a heard gap."

    def add_arguments(self, parser):
        parser.add_argument(
            "timestamp",
            help="Local ISO timestamp, e.g. '2026-09-24 13:24:50'",
        )
        parser.add_argument("--window", type=float, default=15.0,
                            help="Seconds before/after timestamp (default: 15).")
        parser.add_argument("--path", default=str(AUDIO_GAP_DIAG_PATH),
                            help="Diagnostic JSONL path (normally leave unchanged).")
        parser.add_argument("--no-journal", action="store_true",
                            help="Do not print nearby journal activity.")

    def handle(self, *args, **options):
        try:
            target_dt = datetime.fromisoformat(options["timestamp"])
        except ValueError as exc:
            raise CommandError(f"Invalid ISO timestamp: {exc}") from exc
        if timezone.is_naive(target_dt):
            target_dt = timezone.make_aware(target_dt, timezone.get_current_timezone())
        target_epoch = target_dt.timestamp()
        window = max(0.1, float(options["window"]))

        all_records = _load_records(Path(options["path"]))
        try:
            diagnostics_enabled = bool(
                AudioPipeline.objects.filter(pk=1).values_list(
                    "audio_gap_diagnostics_enabled", flat=True).first())
        except Exception:
            diagnostics_enabled = None
        if diagnostics_enabled is False:
            self.stdout.write(
                "Audio-gap diagnostics are currently disabled; any samples "
                "shown below are retained data from an earlier enabled run.")
        records = [
            record for record in all_records
            if abs(record["wall_ts"] - target_epoch) <= window
        ]
        if not records:
            if diagnostics_enabled is False or not all_records:
                self.stdout.write(
                    "Audio-gap diagnostics are not currently enabled or no "
                    "retained diagnostic data is available.")
            coverage = "none"
            if all_records:
                coverage = (
                    f"{datetime.fromtimestamp(all_records[0]['wall_ts']).astimezone().isoformat()} .. "
                    f"{datetime.fromtimestamp(all_records[-1]['wall_ts']).astimezone().isoformat()}"
                )
            self.stdout.write(f"No diagnostic samples in window; retained coverage: {coverage}")
        else:
            self._summarize(records, target_epoch)

        if not options["no_journal"]:
            self._print_journal(target_dt, window)

    def _summarize(self, records, target_epoch):
        def values(*keys):
            return _finite_numbers(_nested(record, *keys) for record in records)

        def pre_event_deltas(record):
            """Return (arrival-advisory, media-continuity) deltas.

            Schema 2 records separate them directly. Schema 1 compatibility is
            retained for rings spanning an Engine restart: its combined latch
            is attributed to continuity only when the latched PTS/DISCONT
            fields say so; otherwise it is conservatively advisory.
            """
            if (record.get("schema") or 0) >= 2:
                return (
                    _nested(record, "pre", "arrival_jitter_delta") or 0,
                    _nested(record, "pre", "continuity_event_delta") or 0,
                )
            combined = _nested(record, "pre", "transient_delta") or 0
            pts_gap = _nested(record, "pre", "last_transient_pts_gap_ns")
            discont = _nested(record, "pre", "last_transient_discont")
            is_continuity = bool(discont) or (
                isinstance(pts_gap, (int, float)) and abs(pts_gap) >= 8_000_000
            )
            return (0 if is_continuity else combined,
                    combined if is_continuity else 0)

        intervals = values("sample_interval_ms")
        pre_level_age = values("pre", "level_age_ms")
        pre_buffer_age = values("pre", "buffer_age_ms")
        queue_time = values("queue", "level_time_ns")
        alsa_delay = values("alsa", "delay")
        post_age = values("post", "age_ms")
        post_frame_age = values("post", "frame_age_ms")
        post_interval = values("post", "frame_interval_ms")
        def post_arrival_jitter_delta(record):
            value = _nested(record, "post", "arrival_jitter_delta")
            if not isinstance(value, (int, float)):
                # Schema-2 compatibility for rings spanning the v5 restart.
                value = _nested(record, "post", "transient_delta")
            return value or 0

        post_arrival_jitter_deltas = [
            post_arrival_jitter_delta(record) for record in records]
        pre_deltas = [pre_event_deltas(record) for record in records]
        pre_arrival_jitter_count = sum(item[0] for item in pre_deltas)
        pre_continuity_count = sum(item[1] for item in pre_deltas)
        post_arrival_jitter_count = sum(post_arrival_jitter_deltas)

        alsa_starvation_records = [
            record for record in records
            if (
                (_nested(record, "alsa", "delay") is not None
                 and _nested(record, "alsa", "delay") < 1764)
                or (_nested(record, "alsa", "state") not in (None, "RUNNING"))
            )
        ]
        post_stall_records = [
            record for record in records
            if (_nested(record, "post", "frame_age_ms") or 0) >= 150
        ]

        start = datetime.fromtimestamp(records[0]["wall_ts"]).astimezone().isoformat()
        end = datetime.fromtimestamp(records[-1]["wall_ts"]).astimezone().isoformat()
        self.stdout.write(f"samples={len(records)} coverage={start} .. {end}")

        def range_text(name, vals, scale=1.0, unit=""):
            if vals:
                self.stdout.write(
                    f"{name}: min={min(vals) / scale:.3f}{unit} "
                    f"max={max(vals) / scale:.3f}{unit}")
            else:
                self.stdout.write(f"{name}: not retained")

        range_text("sampler interval", intervals, unit="ms")
        range_text("pre level age", pre_level_age, unit="ms")
        range_text("pre buffer age", pre_buffer_age, unit="ms")
        range_text("queue level", queue_time, scale=1_000_000, unit="ms")
        range_text("ALSA delay", alsa_delay, unit=" frames")
        range_text("post frame interval", post_interval, unit="ms")
        range_text("post sample age", post_age, unit="ms")
        range_text("post frame age", post_frame_age, unit="ms")
        self.stdout.write(
            "latched evidence in window: "
            f"pre_continuity={pre_continuity_count:.0f} "
            f"pre_arrival_jitter_advisory={pre_arrival_jitter_count:.0f} "
            f"post_arrival_jitter_advisory={post_arrival_jitter_count:.0f}")
        self.stdout.write(
            "longest zero-delta run: "
            f"pre_buffers={_maximum_zero_delta_run(records, 'pre', 'buffer_delta')} samples, "
            f"sink_rendered={_maximum_zero_delta_run(records, 'sink', 'rendered_delta')} samples")
        self.stdout.write(
            "boundary assessment: "
            f"pre-StereoTool={'EVIDENCE' if pre_continuity_count else 'none'}, "
            f"ALSA runway={'EVIDENCE' if alsa_starvation_records else 'none'}, "
            f"post-StereoTool={'ADVISORY' if post_arrival_jitter_count or post_stall_records else 'none'}")
        if pre_continuity_count or alsa_starvation_records:
            self.stdout.write(
                "conclusion: corroborating continuity/runway evidence is present; "
                "inspect the boundary-specific samples below")
        elif (pre_arrival_jitter_count or post_arrival_jitter_count
                or post_stall_records):
            self.stdout.write(
                "conclusion: no corroborated audio-gap evidence; raw pre/post "
                "arrival jitter is advisory scheduler context only")
        else:
            self.stdout.write(
                "conclusion: no corroborated audio-gap evidence in the retained window")

        notable = []
        for record in records:
            close = abs(record["wall_ts"] - target_epoch) <= 0.15
            anomalous = any((
                (_nested(record, "sample_interval_ms") or 0) >= 100,
                (_nested(record, "pre", "buffer_age_ms") or 0) >= 100,
                (_nested(record, "alsa", "delay") is not None
                 and _nested(record, "alsa", "delay") < 1764),
                (_nested(record, "post", "frame_interval_ms") or 0) >= 100,
                (_nested(record, "post", "frame_age_ms") or 0) >= 150,
                pre_event_deltas(record)[1] > 0,
                post_arrival_jitter_delta(record) > 0,
            ))
            if close or anomalous:
                notable.append(record)
        self.stdout.write("notable/nearest samples:")
        for record in notable[:80]:
            stamp = datetime.fromtimestamp(record["wall_ts"]).astimezone().isoformat(timespec="milliseconds")
            self.stdout.write(
                f"  {stamp} dt={record.get('sample_interval_ms')}ms "
                f"pre_age={_nested(record, 'pre', 'buffer_age_ms')}ms "
                f"pre_delta={_nested(record, 'pre', 'buffer_delta')} "
                f"pre_continuity={pre_event_deltas(record)[1]} "
                f"pre_pts_gap={_nested(record, 'pre', 'last_continuity_pts_gap_ns')}ns "
                f"pre_discont={_nested(record, 'pre', 'last_continuity_discont')} "
                f"arrival_jitter={pre_event_deltas(record)[0]} "
                f"arrival_late={_nested(record, 'pre', 'last_arrival_jitter_late_by_ns')}ns "
                f"q={_nested(record, 'queue', 'level_time_ns')}ns "
                f"render_delta={_nested(record, 'sink', 'rendered_delta')} "
                f"alsa_delay={_nested(record, 'alsa', 'delay')} "
                f"post_dt={_nested(record, 'post', 'frame_interval_ms')}ms "
                f"post_age={_nested(record, 'post', 'age_ms')}ms "
                f"post_frame_age={_nested(record, 'post', 'frame_age_ms')}ms "
                f"post_arrival_jitter={post_arrival_jitter_delta(record)} "
                f"post_arrival_late={_nested(record, 'post', 'last_arrival_jitter_late_ms')}ms")

    def _print_journal(self, target_dt, window):
        start = (target_dt - timedelta(seconds=window)).isoformat()
        end = (target_dt + timedelta(seconds=window)).isoformat()
        try:
            result = subprocess.run(
                ["journalctl", "--since", start, "--until", end,
                 "--no-pager", "-o", "short-iso-precise"],
                check=False, capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.stdout.write(f"nearby journal: unavailable ({exc})")
            return
        interesting = (
            "Starting ", "Finished ", "Deactivated ", "error", "Error",
            "warning", "Warning", "xrun", "underrun", "overrun",
            "Playing:", "Trigger:", "Re-opening output",
        )
        lines = [line for line in result.stdout.splitlines()
                 if any(token in line for token in interesting)]
        self.stdout.write("nearby journal activity:")
        if lines:
            for line in lines:
                self.stdout.write(f"  {line}")
        else:
            self.stdout.write("  none retained")
