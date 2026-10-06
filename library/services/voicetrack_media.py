"""Evergreen VoiceTrack <-> ProductionMedia: THE binding service (2.22B B9).

Every change to ``VoiceTrack.media`` goes through ``bind_media`` -- the model
refuses any other writer (library.voicetrack_guard). The final transaction is
deliberately tiny and does nothing but row work, in this order:

  1. ``retention.lock_for_binding(media)`` -- the canonical Phase-A binding
     lock: the same row lock a purge takes, an authoritative re-read, and a
     refusal of purged / not-valid media;
  2. lock the Track row (serializes first creation of a (track, position));
  3. lock the existing VoiceTrack row, if any;
  4. compare the caller's revision token (optimistic concurrency: a stale
     editor session can never silently overwrite a newer binding);
  5. repoint the row -- same row, same (track, position) identity -- inside
     ``binding_scope()``;
  6. commit.

Nothing expensive ever happens inside it: upload, decode, validation,
waveform work, normalization and transcoding all finish BEFORE ``bind_media``
is called (intake creates the immutable ProductionMedia first).

Re-record / edit semantics: the previously bound media is left exactly as it
is (immutable, now unreferenced, reclaimable later by retention policy); a
legacy file is never deleted by a rebind.

Delete semantics (``remove_voicetrack``): the evergreen domain's existing
behavior -- the VoiceTrack row is deleted. ProductionMedia bytes are never
unlinked here (the media just becomes unreferenced); a LEGACY file the row
owned under the voice-track directory is removed after commit, as before.
"""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from django.db import IntegrityError, transaction
from django.utils import timezone

from production.errors import MediaInconsistent, MediaNotValidated, MediaPurged
from production.policy import MediaPolicy
from production.services import retention

from library import voicetrack_guard

POSITIONS = ("intro", "outro")
SOURCES = ("browser", "import", "studio")
ABSENT = "absent"

# What an evergreen VoiceTrack accepts. 10 minutes matches the proven
# OGRemote recorder cap; 128 MiB holds 10 minutes of 48 kHz stereo PCM WAV.
VOICETRACK_POLICY = MediaPolicy(
    max_bytes=128 * 1024 * 1024, min_duration_seconds=0.25, max_duration_seconds=600,
    require_engine_decode=True,
)
LEGACY_VOICETRACK_DIR = Path("/srv/isadoraair/voicetracks")


class VoiceTrackServiceError(Exception):
    status = 400

    def __init__(self, code, message, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


class VoiceTrackConflict(VoiceTrackServiceError):
    """The caller's revision is stale: someone committed in between."""
    status = 409


class VoiceTrackIneligible(VoiceTrackServiceError):
    """The track cannot carry a VoiceTrack in this position right now."""


class VoiceTrackMediaRefused(VoiceTrackServiceError):
    """The media cannot be bound (purged, not valid, or outside the VT policy)."""
    status = 409


def revision_of(voicetrack) -> str:
    """Opaque optimistic-concurrency token for the CURRENT binding of one
    (track, position). Changes on every commit/removal; ``"absent"`` when
    there is no VoiceTrack."""
    if voicetrack is None:
        return ABSENT
    material = "|".join((
        str(voicetrack.pk), str(voicetrack.media_id or ""), voicetrack.filepath or "",
        voicetrack.edited_at.isoformat() if voicetrack.edited_at else "",
        voicetrack.recorded_at.isoformat() if voicetrack.recorded_at else "",
    ))
    return hashlib.sha256(material.encode()).hexdigest()[:24]


def current(track_id, position):
    from library.models import VoiceTrack
    return VoiceTrack.objects.filter(track_id=track_id, position=position).select_related("media").first()


def eligibility_error(track, position):
    """The evergreen rule (unchanged from the pre-Phase-B upload endpoint):
    an outro VT needs outro_starts_seconds, an intro VT intro_until_seconds."""
    if position not in POSITIONS:
        return "position must be 'intro' or 'outro'"
    if position == "outro" and track.outro_starts_seconds is None:
        return ("Track has no outro_starts marker set. Set it on the track detail page (or during "
                "analysis) before recording an outro VT.")
    if position == "intro" and track.intro_until_seconds is None:
        return ("Track has no intro_until marker set. Set it on the track detail page (or during "
                "analysis) before recording an intro VT.")
    return None


def policy_error(media):
    """Re-check the VT policy on the locked, validated row (cheap: row facts only)."""
    if media.byte_size > VOICETRACK_POLICY.max_bytes:
        return "too_large"
    duration = media.decoded_duration_seconds
    if duration is None:
        return "no_duration"
    if float(duration) < VOICETRACK_POLICY.min_duration_seconds:
        return "too_short"
    if float(duration) > VOICETRACK_POLICY.max_duration_seconds:
        return "too_long"
    return None


def _audit(action, voicetrack_id, track, position, user, media_id, previous_media_id):
    from monitoring.models import emit_event
    emit_event(
        "voicetrack",
        f"Voice track {position} {action}: {track.title}"[:200],
        detail={
            "action": action, "track_id": track.pk, "position": position, "voicetrack_id": voicetrack_id,
            "media_id": str(media_id) if media_id else None,
            "previous_media_id": str(previous_media_id) if previous_media_id else None,
            "user": getattr(user, "username", None),
        },
        source="iportal",
    )


def bind_media(*, track_id, position, media_id, user, expected_revision, source="browser"):
    """Atomically point the evergreen VoiceTrack (track_id, position) at
    ``media_id``. Returns the VoiceTrack. Raises VoiceTrackConflict (stale
    revision), VoiceTrackIneligible, VoiceTrackMediaRefused."""
    from library.models import Track, VoiceTrack

    if position not in POSITIONS:
        raise VoiceTrackIneligible("bad_position", "position must be 'intro' or 'outro'")
    if source not in SOURCES:
        source = "browser"
    try:
        media_id = uuid.UUID(str(media_id))
    except (TypeError, ValueError, AttributeError):
        raise VoiceTrackMediaRefused("unknown_media", "no such take") from None
    try:
        with transaction.atomic():
            try:
                media = retention.lock_for_binding(media_id, require_valid=True)             # 1
            except MediaPurged as exc:
                raise VoiceTrackMediaRefused("media_purged", "this take has been purged") from exc
            except MediaNotValidated as exc:
                raise VoiceTrackMediaRefused("media_not_valid", "this take has not been validated") from exc
            except MediaInconsistent as exc:
                raise VoiceTrackMediaRefused("unknown_media", "no such take") from exc
            code = policy_error(media)
            if code is not None:
                raise VoiceTrackMediaRefused(code, f"this take does not fit a voice track ({code})")
            track = Track.objects.select_for_update().filter(pk=track_id).first()          # 2
            if track is None:
                raise VoiceTrackIneligible("no_track", "no such track")
            problem = eligibility_error(track, position)
            if problem:
                raise VoiceTrackIneligible("marker_missing", problem)
            vt = VoiceTrack.objects.select_for_update().filter(track=track, position=position).first()  # 3
            revision = revision_of(vt)
            if revision != expected_revision:                                               # 4
                raise VoiceTrackConflict(
                    "stale_revision", "this voice track changed since you opened it",
                    current_revision=revision,
                )
            previous = vt.media_id if vt is not None else None
            duration = float(media.decoded_duration_seconds)
            with voicetrack_guard.binding_scope():                                          # 5
                if vt is None:
                    vt = VoiceTrack(track=track, position=position, filepath="", media=media,
                                    duration_seconds=duration, source=source,
                                    recorded_by=user if getattr(user, "is_authenticated", False) else None)
                    vt.save()
                    action = "recorded"
                else:
                    vt.media = media
                    vt.duration_seconds = duration
                    vt.source = source
                    if getattr(user, "is_authenticated", False):
                        vt.recorded_by = user
                    vt.edited_at = timezone.now()
                    vt.save(update_fields=["media", "duration_seconds", "source", "recorded_by", "edited_at"])
                    action = "replaced" if previous != media.pk else "re-saved"
            vt_id = vt.pk
            transaction.on_commit(lambda: _audit(action, vt_id, track, position, user, media.pk, previous))
    except IntegrityError as exc:
        # A concurrent first creation of the same (track, position) that slipped
        # past the track lock (cannot happen under it; defensive).
        raise VoiceTrackConflict("stale_revision", "this voice track changed since you opened it",
                                 current_revision=revision_of(current(track_id, position))) from exc
    return vt                                                                               # 6


def _legacy_file_to_remove(filepath):
    """A legacy VT file is removed with its row only if it really lives under
    the legacy voice-track directory (never ProductionMedia, never elsewhere)."""
    if not filepath:
        return None
    try:
        real = os.path.realpath(filepath)
        base = os.path.realpath(LEGACY_VOICETRACK_DIR)
    except OSError:
        return None
    if os.path.commonpath([real, base]) != base or real == base:
        return None
    return real


def remove_voicetrack(*, track_id, position, user, expected_revision):
    """Delete the evergreen VoiceTrack (existing domain behavior) -- the ONLY
    direct VoiceTrack deletion (library.voicetrack_guard.removal_scope; the
    admin and generic deletes are refused). The bound ProductionMedia is NOT
    deleted or unlinked -- it simply becomes unreferenced; retention decides
    about its bytes later."""
    from library.models import VoiceTrack
    with transaction.atomic():
        vt = VoiceTrack.objects.select_for_update().filter(track_id=track_id, position=position) \
            .select_related("track").first()
        revision = revision_of(vt)
        if vt is None or revision != expected_revision:
            raise VoiceTrackConflict("stale_revision", "this voice track changed since you opened it",
                                     current_revision=revision)
        track, media_id = vt.track, vt.media_id
        legacy = _legacy_file_to_remove(vt.filepath)
        vt_id = vt.pk
        with voicetrack_guard.removal_scope():
            vt.delete()
        if legacy is not None:
            transaction.on_commit(lambda: _unlink_quiet(legacy))
        transaction.on_commit(lambda: _audit("deleted", vt_id, track, position, user, None, media_id))


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass
