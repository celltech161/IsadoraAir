"""Small, policy-explicit renderer for generated station announcements.

The renderer sits above the shared logical-voice TTS contract. Features own
words, voice choice, destination identity, library metadata, scheduling, and
monitoring. This module owns artifact mechanics and the three deliberately
different publication policies represented by the request types in types.py.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.db import transaction

from isadoraair.tts.station import synthesize_station_voice
from library.models import AnalysisConfig, Category, Track

from .errors import AnnouncementRenderError
from .types import (
    AnnouncementRenderResult,
    AnnouncementSpec,
    AnnouncementTrackMetadata,
    PreviewAnnouncement,
    RotationAssetAnnouncement,
    SpeechSpliceAnnouncement,
)


def _probe_duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    duration = float(completed.stdout.strip())
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be a positive finite number")
    return duration


def _convert_wav_to_flac(source: Path, destination: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(source), str(destination)],
        check=True,
        timeout=30,
        capture_output=True,
    )


def _analyze_track(
    track: Track, wave_dir: Path | None = None, category: Category | None = None
) -> bool:
    """Run ordinary library analysis on `track`. When `category` is given,
    overlays that Category's threshold overrides on top of the global
    AnalysisConfig first -- via the same `apply_category_thresholds()`
    normal Track analysis uses (library/management/commands/
    analyze_tracks.py), so Rotation Asset cue points are resolved with
    identical semantics to an ordinary Track in the same Category.
    Callers that must NOT pick up Category overrides (Speech Splice,
    which reasserts fixed cue points afterwards regardless) simply omit
    `category`, leaving global AnalysisConfig values in effect."""
    from library.management.commands.analyze_tracks import (
        analyze_one_track,
        apply_category_thresholds,
        get_waveforms_dir,
    )

    cfg = AnalysisConfig.load()
    cfg_values = (
        cfg.analysis_sample_rate,
        cfg.analysis_window_seconds,
        cfg.waveform_points,
        cfg.next_start_threshold_db,
        cfg.cue_in_threshold_db,
        cfg.cue_in_min_seconds,
    )
    if category is not None:
        cfg_values = apply_category_thresholds(
            cfg_values,
            category.next_start_threshold_db_override,
            category.cue_in_threshold_db_override,
        )
    row = (
        track.id,
        track.filepath,
        track.filename,
        track.duration_seconds,
        track.title,
        track.artist.name if track.artist_id else "",
        track.related_artists,
    )
    return bool(analyze_one_track(row, cfg_values, wave_dir or get_waveforms_dir(), force=True))


def _validate_common(spec: AnnouncementSpec) -> Path:
    if not isinstance(spec.text, str) or not spec.text.strip():
        raise AnnouncementRenderError("configuration", "announcement text is empty")
    if not isinstance(spec.logical_voice, str) or not spec.logical_voice.strip():
        raise AnnouncementRenderError("configuration", "logical station voice is empty")
    destination = Path(spec.destination).absolute()
    if destination.exists() and not destination.is_file():
        raise AnnouncementRenderError("configuration", "destination is not a regular file")
    return destination


def _validate_track_metadata(metadata: AnnouncementTrackMetadata) -> Category:
    if not metadata.title:
        raise AnnouncementRenderError("configuration", "Track title is empty")
    if getattr(metadata.artist, "pk", None) is None:
        raise AnnouncementRenderError("configuration", "Track artist must be a saved Artist")
    try:
        return Category.objects.get(code=metadata.category_code)
    except Category.DoesNotExist as exc:
        raise AnnouncementRenderError(
            "configuration", f"library Category {metadata.category_code!r} does not exist"
        ) from exc


def _restore_published_file(destination: Path, backup: Path | None) -> None:
    if backup is not None:
        os.replace(backup, destination)
        return
    try:
        destination.unlink()
    except FileNotFoundError:
        pass


class AnnouncementRenderer:
    """Render one explicit announcement spec into a structured result."""

    def render(self, spec: AnnouncementSpec) -> AnnouncementRenderResult:
        """Public entry point. This is the one place the generic-layer
        contract -- "public renderer failures are always
        AnnouncementRenderError" -- is guaranteed, regardless of which
        mode-specific helper below the exception originated in. Mode
        helpers still raise their own more specific stages
        (configuration/preview/analysis/track/rollback) where that's
        deliberate; this only catches what would otherwise escape as a
        raw exception (e.g. OSError from a parent-directory mkdir that
        runs before any mode helper's own try block)."""
        try:
            if isinstance(spec, PreviewAnnouncement):
                return self._render_preview(spec)
            if isinstance(spec, (SpeechSpliceAnnouncement, RotationAssetAnnouncement)):
                return self._render_library_asset(spec)
            raise AnnouncementRenderError(
                "configuration", f"unsupported announcement spec: {type(spec)!r}"
            )
        except AnnouncementRenderError:
            raise
        except Exception as exc:
            raise AnnouncementRenderError("render", str(exc)) from exc

    def _render_preview(self, spec: PreviewAnnouncement) -> AnnouncementRenderResult:
        destination = _validate_common(spec)
        if destination.suffix.lower() != ".wav":
            raise AnnouncementRenderError("configuration", "Preview destination must use .wav")
        destination.parent.mkdir(parents=True, exist_ok=True)

        try:
            with tempfile.TemporaryDirectory(
                prefix=f".{destination.name}.render-", dir=destination.parent
            ) as scratch_name:
                candidate = Path(scratch_name) / "preview.wav"
                synthesize_station_voice(
                    spec.text,
                    voice=spec.logical_voice,
                    output_path=candidate,
                    timeout_seconds=spec.timeout_seconds,
                )
                duration = _probe_duration(candidate)
                os.replace(candidate, destination)
        except AnnouncementRenderError:
            raise
        except Exception as exc:
            raise AnnouncementRenderError("preview", str(exc)) from exc

        return AnnouncementRenderResult(
            path=destination,
            duration_seconds=duration,
            track=None,
            mode=spec.mode,
        )

    def _render_library_asset(
        self, spec: SpeechSpliceAnnouncement | RotationAssetAnnouncement
    ) -> AnnouncementRenderResult:
        destination = _validate_common(spec)
        if destination.suffix.lower() != ".flac":
            raise AnnouncementRenderError("configuration", "Library announcement destination must use .flac")
        category = _validate_track_metadata(spec.metadata)
        destination.parent.mkdir(parents=True, exist_ok=True)

        backup: Path | None = None
        published = False
        rotation_waveform: Path | None = None
        track_committed = False
        waveform_backup: Path | None = None
        waveform_existed = False
        analysis_attempted = False
        analysis_succeeded = False
        analysis_error: str | None = None

        try:
            with tempfile.TemporaryDirectory(
                prefix=f".{destination.name}.render-", dir=destination.parent
            ) as scratch_name:
                scratch = Path(scratch_name)
                wav_path = scratch / "synthesis.wav"
                candidate = scratch / "candidate.flac"

                synthesize_station_voice(
                    spec.text,
                    voice=spec.logical_voice,
                    output_path=wav_path,
                    timeout_seconds=spec.timeout_seconds,
                )
                _convert_wav_to_flac(wav_path, candidate)
                duration = _probe_duration(candidate)

                if destination.exists():
                    backup = scratch / "previous.flac"
                    shutil.copy2(destination, backup)
                os.replace(candidate, destination)
                published = True

                try:
                    with transaction.atomic():
                        defaults = {
                            "filename": destination.name,
                            "format": "flac",
                            "title": spec.metadata.title,
                            "artist": spec.metadata.artist,
                            "duration_seconds": duration,
                            "category": category,
                            "ready2air": spec.metadata.ready2air,
                        }
                        if isinstance(spec, SpeechSpliceAnnouncement):
                            defaults.update(cue_in_seconds=0, next_start_seconds=duration)

                        track, _ = Track.objects.update_or_create(
                            filepath=str(destination), defaults=defaults
                        )

                        if isinstance(spec, RotationAssetAnnouncement):
                            from library.management.commands.analyze_tracks import get_waveforms_dir

                            wave_dir = get_waveforms_dir()
                            rotation_waveform = wave_dir / f"{track.id}.json"
                            waveform_existed = rotation_waveform.exists()
                            if waveform_existed:
                                waveform_backup = scratch / "previous-waveform.json"
                                shutil.copy2(rotation_waveform, waveform_backup)
                            analysis_attempted = True
                            analysis_succeeded = _analyze_track(
                                track, wave_dir=wave_dir, category=category
                            )
                            if not analysis_succeeded:
                                raise AnnouncementRenderError(
                                    "analysis", "rotation Track analysis did not complete"
                                )
                            track.refresh_from_db()
                    track_committed = True
                except Exception as exc:
                    try:
                        _restore_published_file(destination, backup)
                        published = False
                        if rotation_waveform is not None:
                            if waveform_existed and waveform_backup is not None:
                                os.replace(waveform_backup, rotation_waveform)
                            elif not waveform_existed:
                                try:
                                    rotation_waveform.unlink()
                                except FileNotFoundError:
                                    pass
                    except Exception as rollback_exc:
                        raise AnnouncementRenderError(
                            "rollback",
                            f"Track persistence failed and artifact restoration also failed: {rollback_exc}",
                        ) from exc
                    if isinstance(exc, AnnouncementRenderError):
                        raise
                    raise AnnouncementRenderError("track", str(exc)) from exc

                if isinstance(spec, SpeechSpliceAnnouncement):
                    analysis_attempted = True
                    try:
                        with transaction.atomic():
                            analysis_succeeded = _analyze_track(track)
                            Track.objects.filter(id=track.id).update(
                                cue_in_seconds=0, next_start_seconds=duration
                            )
                        if not analysis_succeeded:
                            analysis_error = "analysis did not complete"
                    except Exception as exc:
                        analysis_succeeded = False
                        analysis_error = str(exc)
                    try:
                        track.refresh_from_db()
                    except Exception:
                        pass

                return AnnouncementRenderResult(
                    path=destination,
                    duration_seconds=duration,
                    track=track,
                    mode=spec.mode,
                    analysis_attempted=analysis_attempted,
                    analysis_succeeded=analysis_succeeded,
                    analysis_error=analysis_error,
                )
        except AnnouncementRenderError:
            raise
        except Exception as exc:
            if published and not track_committed:
                try:
                    _restore_published_file(destination, backup)
                except Exception as rollback_exc:
                    raise AnnouncementRenderError(
                        "rollback", f"render failed and artifact restoration also failed: {rollback_exc}"
                    ) from exc
            raise AnnouncementRenderError("render", str(exc)) from exc


def render_announcement(spec: AnnouncementSpec) -> AnnouncementRenderResult:
    return AnnouncementRenderer().render(spec)
