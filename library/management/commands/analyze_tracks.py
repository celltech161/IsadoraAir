import json
import math
import re
import struct
import subprocess
import sys
from pathlib import Path

from django.core.management.base import BaseCommand

from library.models import AnalysisConfig, Track


def decode_audio_to_pcm(filepath, sample_rate):
    cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", str(filepath),
        "-ac", "1",
        "-ar", str(sample_rate),
        "-f", "s16le",
        "-",
    ]
    try:
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as exc:
        print(f"  ffmpeg failed for {filepath}: {exc}", file=sys.stderr)
        return b""


def compute_envelope_and_waveform(raw, sample_rate, window_seconds,
                                  target_points, floor_db):
    if not raw:
        return [], [], []

    sample_count = len(raw) // 2
    if sample_count == 0:
        return [], [], []

    window_size = max(1, int(sample_rate * window_seconds))
    max_int16 = 32768.0

    envelope_db = []
    times = []
    pos = 0

    while pos + window_size <= sample_count:
        window = raw[pos * 2 : (pos + window_size) * 2]
        acc = 0.0
        for i in range(0, len(window), 2):
            sample = struct.unpack_from("<h", window, i)[0]
            norm = sample / max_int16
            acc += norm * norm
        rms = math.sqrt(acc / window_size) if window_size > 0 else 0.0
        db = 20.0 * math.log10(rms) if rms > 0.0 else -120.0
        t = (pos + window_size / 2.0) / float(sample_rate)
        envelope_db.append(db)
        times.append(t)
        pos += window_size

    if not envelope_db:
        return times, envelope_db, []

    num_envelope = len(envelope_db)
    if num_envelope <= target_points:
        step = 1.0
        points = num_envelope
    else:
        step = num_envelope / float(target_points)
        points = target_points

    waveform = []
    for i in range(points):
        start = int(i * step)
        end = int((i + 1) * step)
        if start >= num_envelope:
            break
        end = max(end, start + 1)
        end = min(end, num_envelope)

        max_db = max(envelope_db[start:end])
        if max_db <= floor_db:
            level = 0
        else:
            span = 0.0 - floor_db
            norm = (max_db - floor_db) / span
            norm = max(0.0, min(1.0, norm))
            level = int(round(norm * 255.0))
        waveform.append(level)

    return times, envelope_db, waveform


def detect_next_start(times, envelope_db, duration, threshold_db):
    if not times or not envelope_db:
        return None

    last_loud = None
    for idx in range(len(envelope_db) - 1, -1, -1):
        if envelope_db[idx] > threshold_db:
            last_loud = idx
            break

    if last_loud is None:
        return float(duration) if duration is not None else None

    if last_loud + 1 < len(times):
        t = times[last_loud + 1]
    else:
        t = times[last_loud]

    if duration is not None and t > duration:
        t = float(duration)
    return float(t)


def detect_cue_in(times, envelope_db, duration, threshold_db, min_seconds):
    if not times or not envelope_db:
        return None

    first_loud = None
    for idx, db in enumerate(envelope_db):
        if db > threshold_db:
            first_loud = idx
            break

    if first_loud is None:
        return 0.0 if duration is not None else None

    t = times[first_loud]
    t = max(0.0, t)
    if duration is not None and t > duration:
        t = float(duration)
    if t < min_seconds:
        t = 0.0
    return float(t)


def extract_related_artists(artist, title, filename_stem):
    sources = []
    if artist:
        sources.append(str(artist))
    if title:
        sources.append(str(title))
    if filename_stem:
        sources.append(str(filename_stem))
    if not sources:
        return ""

    text = " ### ".join(sources)
    candidates = set()

    pattern = re.compile(
        r"(feat\.?|ft\.?|featuring|with)\s+(.+?)(?=$|\s*###|\)|\]|\}| - | -- | / )",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        tail = match.group(2).strip().strip(" .;:-_")
        parts = re.split(r"[,&/]| and |\+", tail)
        for part in parts:
            name = part.strip().strip(" '\"")
            name = re.sub(r"\s+", " ", name)
            if name:
                candidates.add(name)

    return ",".join(sorted(candidates)) if candidates else ""


class Command(BaseCommand):
    help = "Analyze tracks: compute waveforms, detect cue-in and next-start points, extract related artists."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-analyze all tracks, even those already analyzed.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=0,
            help="Limit the number of tracks to analyze (0 = no limit).",
        )

    def handle(self, *args, **options):
        force = options["force"]
        limit = options["limit"]

        cfg = AnalysisConfig.load()
        sample_rate = cfg.analysis_sample_rate
        window_seconds = cfg.analysis_window_seconds
        target_points = cfg.waveform_points
        next_start_db = cfg.next_start_threshold_db
        cue_in_db = cfg.cue_in_threshold_db
        cue_in_min = cfg.cue_in_min_seconds

        from django.conf import settings as django_settings
        wave_dir = Path(getattr(django_settings, "WAVEFORMS_DIR", "/srv/isadoraair/waveforms"))
        wave_dir.mkdir(parents=True, exist_ok=True)

        qs = Track.objects.filter(filepath__isnull=False)
        if not force:
            qs = qs.filter(next_start_seconds__isnull=True)
        qs = qs.order_by("id")
        if limit:
            qs = qs[:limit]

        tracks = list(qs.values_list(
            "id", "filepath", "filename", "duration_seconds",
            "title", "artist__name", "related_artists",
        ))

        total = len(tracks)
        self.stdout.write(f"Found {total} tracks to analyze.")

        analyzed = 0
        skipped = 0

        for row in tracks:
            track_id, filepath, filename, duration, title, artist_name, existing_related = row
            fp = Path(filepath)

            if not fp.is_file():
                self.stderr.write(f"  Missing file for track {track_id}: {filepath}")
                skipped += 1
                continue

            raw = decode_audio_to_pcm(fp, sample_rate)
            if not raw:
                skipped += 1
                continue

            times, envelope_db, waveform = compute_envelope_and_waveform(
                raw, sample_rate, window_seconds, target_points, next_start_db,
            )
            if not waveform:
                skipped += 1
                continue

            analysis_duration = times[-1] if times else None
            effective_duration = analysis_duration if analysis_duration else (
                float(duration) if duration else None
            )

            next_start = detect_next_start(
                times, envelope_db, effective_duration, next_start_db,
            )
            cue_in = detect_cue_in(
                times, envelope_db, effective_duration, cue_in_db, cue_in_min,
            )

            filename_stem = Path(filename).stem if filename else fp.stem
            if force or not existing_related:
                related = extract_related_artists(artist_name, title, filename_stem)
            else:
                related = existing_related

            out_path = wave_dir / f"{track_id}.json"
            payload = {
                "track_id": track_id,
                "samples": waveform,
                "next_start": next_start,
                "cue_in_seconds": cue_in,
                "analysis_duration": analysis_duration,
            }
            try:
                out_path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8",
                )
            except Exception as exc:
                self.stderr.write(f"  Failed to write waveform for track {track_id}: {exc}")

            update_fields = {
                "next_start_seconds": next_start,
                "cue_in_seconds": cue_in,
                "waveform_path": str(out_path),
                "related_artists": related,
            }
            if effective_duration and effective_duration != duration:
                update_fields["duration_seconds"] = effective_duration

            Track.objects.filter(id=track_id).update(**update_fields)
            analyzed += 1

            if analyzed % 50 == 0:
                self.stdout.write(
                    f"  ... analyzed={analyzed} skipped={skipped} "
                    f"remaining={total - analyzed - skipped}"
                )

        self.stdout.write(
            f"\nDone. Analyzed: {analyzed}, skipped: {skipped}"
        )
