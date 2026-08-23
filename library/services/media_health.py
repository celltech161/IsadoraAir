"""Asynchronous, durable media attribution for deck-local playout failures.

The real-time engine only snapshots evidence, retires the deck, advances
playout, and then inserts a pending row.  All expensive probing happens here
on one serial daemon worker.  No result automatically changes Track state.
"""

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

from django.db import close_old_connections, transaction
from django.db.models import F
from django.utils import timezone

from library.models import MediaPlaybackIncident
from monitoring.models import NotificationConfig, emit_event
from monitoring.services.notify import send_operational_email


OUTPUT_LIMIT_BYTES = 32 * 1024
FFPROBE_TIMEOUT_SECONDS = 15.0
FFMPEG_TIMEOUT_SECONDS = 120.0
GSTREAMER_CHILD_TIMEOUT_SECONDS = 60.0
GSTREAMER_HARD_TIMEOUT_SECONDS = 65.0
TERMINATE_GRACE_SECONDS = 2.0


class ValidationInterrupted(Exception):
    """Engine shutdown interrupted validation; durable row must be retried."""


def _bounded_text(value, limit=8192):
    return str(value or "")[:limit]


def _file_identity(path):
    try:
        stat = os.stat(path)
    except (FileNotFoundError, OSError):
        return {"exists": False, "size": None, "mtime_ns": None}
    return {"exists": True, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def capture_deck_evidence(
    deck,
    *,
    trigger,
    runtime_commit="",
    position_seconds=None,
    observed_duration_seconds=None,
    gstreamer_error="",
    gstreamer_debug="",
):
    """Take a bounded, database-free snapshot while the exact deck exists."""
    eos = deck.milestone_snapshot()
    path = str(deck.track.filepath)
    # Avoid a lazy ORM fetch on the GLib path. Engine-loaded tracks normally
    # have artist cached; an empty artist snapshot is preferable to delaying
    # deck isolation if a test/alternate caller supplied a bare Track.
    artist = deck.track._state.fields_cache.get("artist") if hasattr(deck.track, "_state") else None
    return {
        "track": deck.track if getattr(deck.track, "pk", None) else None,
        "track_id_snapshot": getattr(deck.track, "pk", None),
        "track_title_snapshot": _bounded_text(getattr(deck.track, "title", ""), 255),
        "track_artist_snapshot": _bounded_text(artist, 255),
        "filepath_snapshot": _bounded_text(path, 1024),
        "slot": _bounded_text(deck.slot, 1),
        "deck_generation": deck.generation,
        "trigger": trigger,
        "runtime_commit": _bounded_text(runtime_commit, 64),
        "playback_position_seconds": position_seconds,
        "track_duration_seconds": getattr(deck.track, "duration_seconds", None),
        "observed_duration_seconds": observed_duration_seconds,
        "media_buffer_count": eos.get("media_buffers", 0),
        "last_media_buffer_age_seconds": eos.get("last_media_buffer_age_seconds"),
        "eos_snapshot": eos,
        "gstreamer_error": _bounded_text(gstreamer_error),
        "gstreamer_debug": _bounded_text(gstreamer_debug),
    }


def create_incident(evidence):
    """Persist captured evidence without ever propagating into playout."""
    try:
        evidence = dict(evidence)
        identity = _file_identity(evidence["filepath_snapshot"])
        evidence.update({
            "file_exists_snapshot": identity["exists"],
            "file_size_snapshot": identity["size"],
            "file_mtime_ns_snapshot": identity["mtime_ns"],
        })
        return MediaPlaybackIncident.objects.create(**evidence)
    except Exception as exc:
        detail = {"exception": type(exc).__name__, "error": _bounded_text(exc, 2048)}
        print(f"  [media-health] Failed to persist playback incident: {detail['exception']}: {detail['error']}")
        emit_event(
            category="media_health",
            level="error",
            title="Failed to persist media playback incident",
            detail=detail,
            dedupe_key="media-health|incident-persist-failed",
        )
        return None


class _BoundedCollector:
    def __init__(self, stream, limit):
        self.stream = stream
        self.limit = limit
        self.data = bytearray()
        self.truncated = False
        self.thread = threading.Thread(target=self._drain, daemon=True)

    def start(self):
        self.thread.start()

    def _drain(self):
        try:
            while True:
                chunk = self.stream.read(8192)
                if not chunk:
                    break
                remaining = self.limit - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        except Exception:
            self.truncated = True

    def finish(self):
        self.thread.join(timeout=1.0)
        return bytes(self.data).decode("utf-8", errors="replace")


def _terminate_process_group(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # Defensive only: SIGKILL should make a local child waitable.
        pass


def run_bounded_command(args, *, timeout_seconds, stop_event=None):
    """Run argv without a shell, bounding time, process tree, and output."""
    executable = shutil.which(args[0]) if not os.path.isabs(args[0]) else args[0]
    if not executable or not os.path.exists(executable):
        return {"status": "unavailable", "returncode": None, "stdout": "", "stderr": ""}
    argv = [executable, *args[1:]]
    nice = shutil.which("nice")
    if nice:
        argv = [nice, "-n", "10", *argv]
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except Exception as exc:
        return {
            "status": "infrastructure_error", "returncode": None,
            "stdout": "", "stderr": _bounded_text(exc),
        }
    stdout = _BoundedCollector(process.stdout, OUTPUT_LIMIT_BYTES)
    stderr = _BoundedCollector(process.stderr, OUTPUT_LIMIT_BYTES)
    stdout.start()
    stderr.start()
    status = "ok"
    deadline = started + timeout_seconds
    while process.poll() is None:
        if stop_event is not None and stop_event.is_set():
            status = "stopped"
            _terminate_process_group(process)
            break
        if time.monotonic() >= deadline:
            status = "timeout"
            _terminate_process_group(process)
            break
        try:
            process.wait(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        _terminate_process_group(process)
    out = stdout.finish()
    err = stderr.finish()
    if status == "ok" and process.returncode != 0:
        status = "failed"
    return {
        "status": status,
        "returncode": process.returncode,
        "stdout": out,
        "stderr": err,
        "stdout_truncated": stdout.truncated,
        "stderr_truncated": stderr.truncated,
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _ffprobe(path, stop_event):
    result = run_bounded_command([
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=index,codec_type,codec_name,sample_rate,channels,duration:format=format_name,duration",
        "-of", "json", path,
    ], timeout_seconds=FFPROBE_TIMEOUT_SECONDS, stop_event=stop_event)
    if result["status"] == "ok":
        try:
            payload = json.loads(result["stdout"])
            result["probe"] = payload
            if not payload.get("streams"):
                result["status"] = "failed"
                result["stderr"] = "no usable audio stream"
        except (ValueError, TypeError) as exc:
            result["status"] = "infrastructure_error"
            result["stderr"] = f"invalid ffprobe JSON: {exc}"
    return result


def _ffmpeg_decode(path, stop_event):
    return run_bounded_command([
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
        "-threads", "1", "-i", path, "-map", "0:a:0", "-f", "null", "-",
    ], timeout_seconds=FFMPEG_TIMEOUT_SECONDS, stop_event=stop_event)


def _gstreamer_decode(path, stop_event):
    child = str(Path(__file__).with_name("media_gst_probe.py"))
    result = run_bounded_command([
        sys.executable, child, "--timeout", str(GSTREAMER_CHILD_TIMEOUT_SECONDS), path,
    ], timeout_seconds=GSTREAMER_HARD_TIMEOUT_SECONDS, stop_event=stop_event)
    if result["status"] == "ok":
        try:
            result["probe"] = json.loads(result["stdout"])
        except (ValueError, TypeError) as exc:
            result["status"] = "infrastructure_error"
            result["stderr"] = f"invalid GStreamer validator JSON: {exc}"
    return result


def _identity_matches(incident, identity):
    return (
        bool(incident.file_exists_snapshot) == identity["exists"]
        and incident.file_size_snapshot == identity["size"]
        and incident.file_mtime_ns_snapshot == identity["mtime_ns"]
    )


def _eos_reached_decoder_and_real_leg(snapshot):
    milestones = (snapshot or {}).get("milestones", {})
    a = milestones.get("A_DECODER_AUDIO_EOS")
    b = milestones.get("B_REAL_LEG_EOS_BEFORE_CONCAT")
    if not a or not b or not a.get("count") or not b.get("count"):
        return False
    rejected = milestones.get("I_EOS_REJECTED_POST_SEEK")
    latest_media_eos = max(a.get("last_ms", -1), b.get("last_ms", -1))
    return not rejected or rejected.get("last_ms", -1) < latest_media_eos


def classify_result(incident, detail, final_identity):
    if _eos_reached_decoder_and_real_leg(incident.eos_snapshot):
        return MediaPlaybackIncident.CLASS_ENGINE_COMPLETION_PATH
    if not _identity_matches(incident, final_identity):
        return MediaPlaybackIncident.CLASS_FILE_MISSING_OR_CHANGED
    ffprobe = detail["ffprobe"]["status"]
    ffmpeg = detail["ffmpeg"]["status"]
    gst = detail["gstreamer"]
    gst_probe = gst.get("probe", {})
    if ffprobe == "failed" or ffmpeg == "failed":
        return MediaPlaybackIncident.CLASS_CONFIRMED_MEDIA_FAILURE
    if ffprobe == "ok" and ffmpeg == "ok":
        if gst["status"] == "timeout" or gst_probe.get("status") in ("error", "timeout"):
            return MediaPlaybackIncident.CLASS_GSTREAMER_COMPATIBILITY
        if gst["status"] == "ok" and gst_probe.get("status") == "eos" and gst_probe.get("eos"):
            return MediaPlaybackIncident.CLASS_VALIDATION_CLEAN
    return MediaPlaybackIncident.CLASS_INCONCLUSIVE


def _notification_identity(incident, classification, identity):
    material = "\0".join([
        incident.filepath_snapshot,
        str(identity.get("size")),
        str(identity.get("mtime_ns")),
        classification,
    ])
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _notify_incident(incident, identity):
    token = _notification_identity(incident, incident.classification, identity)
    try:
        config = NotificationConfig.load()
        if not config.enabled:
            return token, MediaPlaybackIncident.NOTIFY_DISABLED, None
        if not config.recipient_list():
            return token, MediaPlaybackIncident.NOTIFY_NO_RECIPIENTS, None
        cutoff = timezone.now() - timedelta(minutes=config.cooldown_minutes)
        duplicate = MediaPlaybackIncident.objects.filter(
            notification_identity=token,
            notification_status=MediaPlaybackIncident.NOTIFY_SENT,
            notified_at__gte=cutoff,
        ).exclude(pk=incident.pk).exists()
        if duplicate:
            return token, MediaPlaybackIncident.NOTIFY_SUPPRESSED, None
        labels = {
            MediaPlaybackIncident.CLASS_CONFIRMED_MEDIA_FAILURE: "Media failure",
            MediaPlaybackIncident.CLASS_GSTREAMER_COMPATIBILITY: "GStreamer media compatibility issue",
            MediaPlaybackIncident.CLASS_ENGINE_COMPLETION_PATH: "Engine completion path failure",
            MediaPlaybackIncident.CLASS_VALIDATION_CLEAN: "Media tests clean after playback incident",
            MediaPlaybackIncident.CLASS_FILE_MISSING_OR_CHANGED: "Media file missing or changed",
            MediaPlaybackIncident.CLASS_INCONCLUSIVE: "Media validation inconclusive",
        }
        subject = f"[IsadoraAir] {labels.get(incident.classification, 'Media validation')}: {incident.track_artist_snapshot} - {incident.track_title_snapshot}"
        validators = incident.validator_detail or {}
        body = "\n".join([
            f"Detected: {incident.detected_at.isoformat()}",
            f"Classification: {incident.classification}",
            f"Trigger: {incident.get_trigger_display()}",
            f"Track: {incident.track_artist_snapshot} -- {incident.track_title_snapshot}",
            f"Track ID: {incident.track_id_snapshot}",
            f"Path: {incident.filepath_snapshot}",
            f"Deck: {incident.slot} generation {incident.deck_generation}",
            f"Playback position: {incident.playback_position_seconds}",
            f"Stored/observed duration: {incident.track_duration_seconds} / {incident.observed_duration_seconds}",
            f"Media buffers / last age: {incident.media_buffer_count} / {incident.last_media_buffer_age_seconds}",
            f"ffprobe: {validators.get('ffprobe', {}).get('status', 'unknown')}",
            f"ffmpeg full decode: {validators.get('ffmpeg', {}).get('status', 'unknown')}",
            f"isolated GStreamer: {validators.get('gstreamer', {}).get('probe', {}).get('status', validators.get('gstreamer', {}).get('status', 'unknown'))}",
            f"EOS milestones: {json.dumps((incident.eos_snapshot or {}).get('milestones', {}), sort_keys=True)[:4096]}",
            f"Incident: {incident.pk}",
            "",
            "Recommendation: inspect this incident and replace/re-encode only when the evidence confirms a media failure; clean media or live A/B EOS evidence should be retained for engine/runtime investigation.",
            "Track.ready2air was NOT changed automatically.",
        ])
        status = send_operational_email(subject, body, config=config)
        notified_at = timezone.now() if status == MediaPlaybackIncident.NOTIFY_SENT else None
        return token, status, notified_at
    except Exception as exc:
        emit_event(
            category="media_health", level="warning",
            title="Media validation notification failed",
            detail={
                "incident_id": incident.pk,
                "exception": type(exc).__name__,
                "error": _bounded_text(exc, 2048),
            },
            dedupe_key=f"media-health|notification-failed|incident={incident.pk}",
        )
        return token, MediaPlaybackIncident.NOTIFY_FAILED, None


def validate_incident(incident, *, stop_event=None):
    stop_event = stop_event or threading.Event()
    initial = _file_identity(incident.filepath_snapshot)
    detail = {"identity_at_validation_start": initial}
    if not _identity_matches(incident, initial):
        detail.update({"ffprobe": {"status": "skipped"}, "ffmpeg": {"status": "skipped"}, "gstreamer": {"status": "skipped"}})
    else:
        detail["ffprobe"] = _ffprobe(incident.filepath_snapshot, stop_event)
        if stop_event.is_set():
            raise ValidationInterrupted()
        detail["ffmpeg"] = _ffmpeg_decode(incident.filepath_snapshot, stop_event)
        if stop_event.is_set():
            raise ValidationInterrupted()
        detail["gstreamer"] = _gstreamer_decode(incident.filepath_snapshot, stop_event)
        if stop_event.is_set():
            raise ValidationInterrupted()
    final = _file_identity(incident.filepath_snapshot)
    detail["identity_at_validation_end"] = final
    classification = classify_result(incident, detail, final)
    now = timezone.now()
    MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
        validation_state=MediaPlaybackIncident.STATE_COMPLETE,
        classification=classification,
        validator_detail=detail,
        validated_at=now,
    )
    incident.refresh_from_db()
    emit_event(
        category="media_health",
        level="error" if classification in (
            MediaPlaybackIncident.CLASS_CONFIRMED_MEDIA_FAILURE,
            MediaPlaybackIncident.CLASS_GSTREAMER_COMPATIBILITY,
        ) else "warning",
        title=f"Media validation completed: {classification}",
        detail={
            "incident_id": incident.pk,
            "trigger": incident.trigger,
            "slot": incident.slot,
            "generation": incident.deck_generation,
            "track_id": incident.track_id_snapshot,
            "track_artist": incident.track_artist_snapshot,
            "track_title": incident.track_title_snapshot,
            "filepath": incident.filepath_snapshot,
            "playback_position_seconds": incident.playback_position_seconds,
            "track_duration_seconds": incident.track_duration_seconds,
            "observed_duration_seconds": incident.observed_duration_seconds,
            "media_buffer_count": incident.media_buffer_count,
            "last_media_buffer_age_seconds": incident.last_media_buffer_age_seconds,
            "eos_milestones": (incident.eos_snapshot or {}).get("milestones", {}),
            "classification": classification,
            "ffprobe": detail["ffprobe"]["status"],
            "ffmpeg": detail["ffmpeg"]["status"],
            "gstreamer": detail["gstreamer"].get("probe", {}).get("status", detail["gstreamer"]["status"]),
            "recommended_action": (
                "Inspect/replace or re-encode only if media failure evidence is confirmed; "
                "Track.ready2air was not changed automatically."
            ),
        },
        dedupe_key=f"media-health|validation|incident={incident.pk}",
    )
    token, status, notified_at = _notify_incident(incident, final)
    MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
        notification_identity=token,
        notification_status=status,
        notified_at=notified_at,
    )
    return classification


class MediaValidationWorker:
    """One serial daemon worker; pending rows are the durable queue."""

    def __init__(self):
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._start_lock = threading.Lock()
        self._thread = None

    def start(self):
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="media-validation-worker", daemon=True,
            )
            self._thread.start()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _claim_next(self):
        with transaction.atomic():
            incident = (
                MediaPlaybackIncident.objects.select_for_update()
                .filter(validation_state=MediaPlaybackIncident.STATE_PENDING)
                .order_by("detected_at", "pk")
                .first()
            )
            if incident is None:
                return None
            MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
                validation_state=MediaPlaybackIncident.STATE_VALIDATING,
                validation_attempts=F("validation_attempts") + 1,
            )
            incident.validation_state = MediaPlaybackIncident.STATE_VALIDATING
            return incident

    def _recover_interrupted(self):
        MediaPlaybackIncident.objects.filter(
            validation_state=MediaPlaybackIncident.STATE_VALIDATING,
        ).update(validation_state=MediaPlaybackIncident.STATE_PENDING)

    def _process_once(self):
        incident = self._claim_next()
        if incident is None:
            return False
        try:
            validate_incident(incident, stop_event=self._stop)
        except ValidationInterrupted:
            MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
                validation_state=MediaPlaybackIncident.STATE_PENDING,
            )
        except Exception as exc:
            # validate_incident persists its classification before publishing
            # SystemEvent/email. A later notification bookkeeping failure must
            # never overwrite a truthful completed media result as generic
            # INCONCLUSIVE.
            try:
                incident.refresh_from_db()
            except Exception:
                pass
            if incident.validation_state == MediaPlaybackIncident.STATE_COMPLETE:
                emit_event(
                    category="media_health", level="warning",
                    title="Media validation result publication incomplete",
                    detail={
                        "incident_id": incident.pk,
                        "classification": incident.classification,
                        "exception": type(exc).__name__,
                        "error": _bounded_text(exc, 2048),
                    },
                    dedupe_key=f"media-health|publication-incomplete|incident={incident.pk}",
                )
                return True
            detail = {"exception": type(exc).__name__, "error": _bounded_text(exc, 4096)}
            now = timezone.now()
            MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
                validation_state=MediaPlaybackIncident.STATE_COMPLETE,
                classification=MediaPlaybackIncident.CLASS_INCONCLUSIVE,
                validator_detail={"worker_error": detail},
                validated_at=now,
                notification_status=MediaPlaybackIncident.NOTIFY_FAILED,
            )
            emit_event(
                category="media_health", level="error",
                title=f"Media validation completed: {MediaPlaybackIncident.CLASS_INCONCLUSIVE}",
                detail={
                    "incident_id": incident.pk,
                    "classification": MediaPlaybackIncident.CLASS_INCONCLUSIVE,
                    "recommended_action": "Validation infrastructure failed; retry/inspect. Track.ready2air was not changed automatically.",
                    **detail,
                },
                dedupe_key=f"media-health|validation|incident={incident.pk}",
            )
            incident.refresh_from_db()
            identity = _file_identity(incident.filepath_snapshot)
            token, status, notified_at = _notify_incident(incident, identity)
            MediaPlaybackIncident.objects.filter(pk=incident.pk).update(
                notification_identity=token,
                notification_status=status,
                notified_at=notified_at,
            )
        return True

    def _run(self):
        close_old_connections()
        try:
            recovered = False
            while not self._stop.is_set():
                try:
                    if not recovered:
                        self._recover_interrupted()
                        recovered = True
                    processed = self._process_once()
                except Exception as exc:
                    # Database outages around claiming/recovery are outside an
                    # individual incident. Keep the one worker alive and retry
                    # from durable rows after a bounded wait.
                    emit_event(
                        category="media_health", level="warning",
                        title="Media validation worker temporarily unavailable",
                        detail={
                            "exception": type(exc).__name__,
                            "error": _bounded_text(exc, 2048),
                        },
                        dedupe_key="media-health|worker-temporarily-unavailable",
                    )
                    close_old_connections()
                    self._wake.wait(timeout=30.0)
                    self._wake.clear()
                    continue
                if not processed:
                    self._wake.wait(timeout=30.0)
                    self._wake.clear()
        finally:
            close_old_connections()
