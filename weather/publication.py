"""Weather asset last-known-good publication -- P1 2.4 Pass G.

Weather's routine generated assets (WxTemp/current_temp.mp3, WxObs/
current_obs.mp3, WxForecast/forecast.mp3, WxAlert/wx_alert.mp3) are
rendered as MP3 by weather_ingest's isolated venv (current_temp.py,
wx_forecast.py, wx_alert.py, amber_alert.py), which then hands the
already-rendered candidate file to `manage.py publish_weather_asset`
in the main IsadoraAir app -- see weather_ingest/lib/delivery.py's
`deliver()`. This module is that command's actual implementation
(the command itself is a thin CLI wrapper -- see
weather/management/commands/publish_weather_asset.py).

Deliberately NOT built on isadoraair/announcements (the r0055 generic
renderer): Weather's asset is already fully rendered as MP3 before
this runs (no TTS/ffmpeg happens here), and the renderer's contract is
about producing an artifact from text, not about publishing an
already-produced file. Extending the renderer to "also accept a
pre-rendered file" would be a second, parallel API bolted onto a
contract that doesn't need one -- this narrow, Weather-specific
command mirrors the renderer's Rotation Asset last-known-good
filesystem/DB approach (scratch dir, backup-then-replace, roll back on
failure) without touching that module at all.

Publish sequence:
  1. Validate the candidate (exists, is a file, non-empty, .mp3) and
     resolve the destination Category (must already exist) -- neither
     step touches the destination itself, so neither needs the
     destination lock below.
  2. Acquire an exclusive, cross-process lock keyed by the canonical
     final destination path (see _destination_lock) -- held from here
     through the end of step 6. Two publish attempts for the SAME
     final destination (e.g. wx_alert.py and amber_alert.py, which
     have entirely separate producer-side lockfiles but both publish
     WxAlert/wx_alert.mp3) can never interleave their backup/replace/
     Track/waveform/provenance sequence; unrelated destinations are
     completely unaffected. The lock is an flock() on a hidden
     sidecar file next to the destination, released automatically on
     process exit/crash by the kernel.
  3. Stage: back up the current final file (if any) into scratch, copy
     the candidate into scratch, then replace() into place -- atomic
     on the destination filesystem.
  4. transaction.atomic(): sync_track_file(dest_path, wave_dir=<a
     scratch waveform directory private to this publish attempt>) --
     creates/updates the Track row (STABLE id/filepath across
     regenerations, same identity every time, exactly like today) and
     runs full analysis (waveform + cue points) into that SCRATCH
     directory, never the real WAVEFORMS_DIR, reusing the exact same
     analysis code path every other library ingestion pipeline uses.

     analyze_one_track() historically treats its own waveform-file
     WRITE failure as non-fatal (logs it, still updates Track fields,
     still returns True) -- callers other than Weather may genuinely
     rely on that, so this module does not change it. Weather's own
     publication contract is deliberately stronger: audio + Track +
     analysis + a REAL persisted waveform, together or not at all.  So
     once sync_track_file() returns without raising, this function
     itself REQUIRES the exact scratch <track_id>.json to exist --
     absence is treated as a publication failure (see step 5), never
     as a successful publish with a missing waveform.

     Once that scratch waveform is confirmed to exist, it is published
     to the real WAVEFORMS_DIR under that exact <track_id>.json name --
     still inside the same transaction, after backing up any real
     waveform this regeneration is about to replace -- and
     Track.waveform_path (which analyze_one_track persisted pointing at
     the SCRATCH copy, since it has no idea this caller will relocate
     it) is corrected to that same real path, with the in-memory Track
     object refreshed so the object this function returns matches
     exactly what was committed. A successful Weather publication must
     never retain any reference to its own temporary scratch directory,
     in the database or otherwise -- the invariant this function
     guarantees on success is exactly:

         Track.waveform_path == str(real_wave_dir / f"{track.id}.json")

     and that file exists on disk.
  5. On any failure in step 4 (including the missing-scratch-waveform
     case above, or the final waveform-publish move itself): restore
     the previous final file (or remove it, for a brand-new asset), let
     the DB transaction itself roll back the Track mutation (including
     any waveform_path rewrite), and restore/remove the real waveform
     file if step 4 had already reached the point of touching it. A
     failure that never gets that far -- either because sync_track_file
     raised before ever returning a Track id (analysis itself failed),
     or because the scratch waveform never materialized -- never
     touches the real WAVEFORMS_DIR at all: nothing there to restore or
     clean up, no directory-wide scan needed, and (deliberately) no
     risk of ever touching an unrelated waveform some OTHER, concurrent
     publish (a regular library upload, another Weather asset) may be
     writing at the same time under its own, different track id.
  6. Only once the file + Track + analysis + real waveform are all
     durably accepted: best-effort provenance write (weather.
     provenance) -- a failure here is recorded on the returned
     PublicationResult (surfaced by the management command as a
     stderr warning weather_ingest/lib/delivery.py forwards to the
     calling script's own log) but never undoes the successful publish
     above.

Scratch files (including the scratch waveform directory) are always
removed (the whole publish runs inside one tempfile.TemporaryDirectory).
Any failure raises PublicationError; callers (the management command)
surface that as a non-zero exit code, so weather_ingest's existing
per-script notify()/failure-email behavior is triggered exactly as it
is for any other delivery failure today.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from library.management.commands.analyze_tracks import get_waveforms_dir
from library.management.commands.sync_track_file import sync_track_file
from library.models import Category, Track

from . import provenance as provenance_mod


class PublicationError(RuntimeError):
    """A Weather asset could not be safely published. Always leaves
    the previous last-known-good artifact (if any) in place -- see
    publish_weather_asset()'s own docstring for the exact rollback
    sequence."""


@dataclass(frozen=True, slots=True)
class PublicationResult:
    track: Track
    created: bool
    final_path: Path
    provenance_path: Path | None
    provenance_error: str | None


def _restore_final_file(dest_path: Path, backup: Path | None) -> None:
    if backup is not None:
        Path(backup).replace(dest_path)
        return
    try:
        dest_path.unlink()
    except FileNotFoundError:
        pass


@contextmanager
def _destination_lock(dest_path: Path):
    """Exclusive, cross-process lock keyed by the canonical final
    destination path -- serializes every publish_weather_asset() call
    targeting the SAME LIBRARY_ROOT/<category>/<filename>, while
    leaving unrelated destinations completely independent. Two
    producers with entirely separate producer-side lockfiles (wx_alert.
    py and amber_alert.py both publish WxAlert/wx_alert.mp3) are
    exactly the case this exists to cover -- neither producer's own
    lock protects the shared destination they both write to.

    A plain flock() on a hidden sidecar file next to the destination:
    held for the whole backup -> replace -> Track/analysis -> waveform
    -> provenance sequence, and released automatically by the kernel
    the moment this process's file descriptor closes -- on a clean
    exit, an uncaught exception, or a crash alike. Not a Django/DB
    lock (this must work even before the destination Category/Track
    exist), and not per-producer (must work across processes with no
    shared Python state at all)."""
    lock_path = dest_path.parent / f".{dest_path.name}.publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def publish_weather_asset(
    candidate_path,
    category_code: str,
    filename: str,
    *,
    producer: str = "",
    voice: str = "",
    source_kind: str = "",
    source_age_seconds: float | None = None,
    used_fallback: bool = False,
    alert_family: str | None = None,
) -> PublicationResult:
    """Publish an already-rendered Weather MP3 with last-known-good
    rollback. Raises PublicationError on any failure that leaves the
    previous artifact untouched (or, for a first-generation asset,
    leaves nothing behind at all). See module docstring for the full
    sequence, including the per-destination lock."""
    candidate = Path(candidate_path)
    if not candidate.is_file():
        raise PublicationError(f"candidate is not a file: {candidate}")
    if candidate.suffix.lower() != ".mp3":
        raise PublicationError(
            f"candidate must be an MP3 (Weather's established audio format is unchanged "
            f"by this pass) -- got {candidate.suffix!r}: {candidate}"
        )
    try:
        candidate_size = candidate.stat().st_size
    except OSError as exc:
        raise PublicationError(f"could not stat candidate {candidate}: {exc}") from exc
    if candidate_size == 0:
        raise PublicationError(f"candidate is empty: {candidate}")

    category = Category.objects.filter(code=category_code).first()
    if category is None:
        raise PublicationError(f"no Category exists for {category_code!r}")

    library_root = Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")).resolve()
    dest_dir = library_root / category_code
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    with _destination_lock(dest_path):
        real_wave_dir = get_waveforms_dir()

        with tempfile.TemporaryDirectory(prefix=f".{filename}.publish-", dir=dest_dir) as scratch_name:
            scratch = Path(scratch_name)
            scratch_wave_dir = scratch / "waveforms"
            scratch_wave_dir.mkdir()
            backup: Path | None = None
            published = False
            track_committed = False
            real_waveform_committed = False
            real_waveform_existed = False
            real_waveform_backup: Path | None = None
            real_waveform_path: Path | None = None

            try:
                if dest_path.exists():
                    backup = scratch / "previous.mp3"
                    shutil.copy2(dest_path, backup)
                candidate_copy = scratch / "candidate.mp3"
                shutil.copy2(candidate, candidate_copy)
                candidate_copy.replace(dest_path)
                published = True

                try:
                    with transaction.atomic():
                        track, created = sync_track_file(str(dest_path), wave_dir=scratch_wave_dir)

                        scratch_waveform_path = scratch_wave_dir / f"{track.id}.json"
                        if not scratch_waveform_path.is_file():
                            # analyze_one_track() can return True having
                            # only logged a non-fatal write failure --
                            # Weather's own contract is stronger than
                            # that: no real persisted waveform, no
                            # publish. Raising here rolls back the
                            # Track mutation (including anything
                            # analysis wrote) via the enclosing
                            # transaction, exactly like any other
                            # failure in this block.
                            raise RuntimeError(
                                f"analysis reported success but no waveform was written for "
                                f"track {track.id} (expected {scratch_waveform_path}) -- Weather "
                                f"publication requires a real persisted waveform, not merely a "
                                f"successful analysis return value"
                            )
                        real_waveform_path = real_wave_dir / f"{track.id}.json"
                        real_waveform_existed = real_waveform_path.exists()
                        if real_waveform_existed:
                            real_waveform_backup = scratch / "previous-waveform.json"
                            shutil.copy2(real_waveform_path, real_waveform_backup)
                        # Publish exactly the known track-id waveform,
                        # still inside the transaction -- nothing was
                        # ever written to the real WAVEFORMS_DIR before
                        # this line, so a failure anywhere ABOVE this
                        # point leaves it completely untouched.
                        scratch_waveform_path.replace(real_waveform_path)
                        real_waveform_committed = True
                        # analyze_one_track() persisted Track.waveform_path
                        # pointing at the SCRATCH copy (now relocated) --
                        # correct it to the REAL path just published, and
                        # refresh the in-memory object (also picks up
                        # every other field analysis wrote via its own
                        # raw .update() call) so the Track this function
                        # returns matches exactly what was committed.
                        Track.objects.filter(id=track.id).update(waveform_path=str(real_waveform_path))
                        track.refresh_from_db()
                    track_committed = True
                except Exception as exc:
                    _restore_final_file(dest_path, backup)
                    published = False
                    if real_waveform_committed:
                        if real_waveform_existed and real_waveform_backup is not None:
                            Path(real_waveform_backup).replace(real_waveform_path)
                        elif not real_waveform_existed:
                            real_waveform_path.unlink(missing_ok=True)
                    raise PublicationError(f"publish failed for {dest_path}: {exc}") from exc

            except PublicationError:
                raise
            except Exception as exc:
                if published and not track_committed:
                    _restore_final_file(dest_path, backup)
                raise PublicationError(f"publish failed for {dest_path}: {exc}") from exc

        # Only reached once the file + Track + analysis + real waveform
        # are all durably accepted. Provenance is best-effort
        # diagnostics: a failure here is recorded on the result but
        # must never undo the successful publish above. Still inside
        # the destination lock, per the amendment requirement.
        provenance_written_path = None
        provenance_error = None
        try:
            data_dir = Path(getattr(settings, "WEATHER_DATA_DIR", "/var/lib/isadoraair/weather"))
            provenance_written_path = provenance_mod.write_provenance(
                data_dir,
                category_code=category_code,
                filename=filename,
                final_path=dest_path,
                producer=producer,
                generated_at=timezone.now().isoformat().replace("+00:00", "Z"),
                voice=voice,
                source_kind=source_kind,
                source_age_seconds=source_age_seconds,
                used_fallback=used_fallback,
                alert_family=alert_family,
            )
        except Exception as exc:
            provenance_error = str(exc)

        return PublicationResult(
            track=track, created=created, final_path=dest_path,
            provenance_path=provenance_written_path, provenance_error=provenance_error,
        )
