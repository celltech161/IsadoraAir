"""Evergreen VoiceTrack as the first iPortal recorder consumer (2.22B).

This adapter is the ONLY place the shared recorder/editor meets VoiceTrack:
the recorder core (production.recorder) never imports library code, and no
VoiceTrack field appears in it.

* subject      (Track, position) -- the evergreen identity, unchanged;
* authorize    the existing ``voicetrack.record`` capability (roadmap 2.5C),
               for every operation, exactly as the pre-Phase-B endpoints;
* commit       library.services.voicetrack_media.bind_media (the canonical
               locked binding; optimistic revision; immutable takes);
* remove       library.services.voicetrack_media.remove_voicetrack;
* media access a user may preview/derive from their OWN takes, and from the
               take currently bound to this (track, position) -- nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse

from authz.evaluator import authorize as authz_authorize
from production.recorder.contracts import (
    Conflict, CurrentAudio, RecorderError, RecordingAdapter, RecordingContext, SubjectNotFound,
)
from production.recorder.views import url_name

from library.services import voicetrack_media as vtm

ADAPTER_KEY = "evergreen-voicetrack"


@dataclass(frozen=True)
class VoiceTrackSubject:
    track: object
    position: str


class EvergreenVoiceTrackAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__(
            key=ADAPTER_KEY, label="Voice Tracking",
            description="Record and edit evergreen intro/outro voice tracks that air with a song every time it plays.",
            entry_url="/voicetracks/",
        )

    # -- authorization ----------------------------------------------------------
    def authorize(self, user, operation):
        return bool(getattr(user, "is_authenticated", False) and authz_authorize(user, "voicetrack.record"))

    # -- subject ------------------------------------------------------------------
    def resolve(self, params):
        from library.models import Track
        try:
            track_id = int(str(params.get("track", "")))
        except (TypeError, ValueError):
            raise SubjectNotFound("bad_subject", "track is required") from None
        position = str(params.get("position", ""))
        if position not in vtm.POSITIONS:
            raise SubjectNotFound("bad_subject", "position must be 'intro' or 'outro'")
        track = Track.objects.select_related("artist").filter(pk=track_id).first()
        if track is None:
            raise SubjectNotFound("bad_subject", "no such track")
        return VoiceTrackSubject(track, position)

    def subject_params(self, subject):
        return {"track": subject.track.pk, "position": subject.position}

    def media_policy(self, subject):
        return vtm.VOICETRACK_POLICY

    def _media_url(self, subject, media_id):
        base = reverse(f"library:{url_name(ADAPTER_KEY, 'media')}", kwargs={"media_id": media_id})
        return f"{base}?track={subject.track.pk}&position={subject.position}"

    def context(self, request, subject):
        track, position = subject.track, subject.position
        vt = vtm.current(track.pk, position)
        revision = vtm.revision_of(vt)
        current = None
        if vt is not None:
            audio = vt.playable_audio()
            if audio is not None and audio.origin == "production_media":
                current = CurrentAudio(origin="production_media", label="On-air take (iPortal)",
                                       duration_seconds=audio.duration_seconds, media_id=str(vt.media_id),
                                       preview_url=self._media_url(subject, vt.media_id))
            elif audio is not None:
                current = CurrentAudio(origin="legacy", label="On-air take (legacy recording)",
                                       duration_seconds=audio.duration_seconds,
                                       preview_url=reverse("library:api-voicetrack-audio", args=[vt.pk]))
        blocked = vtm.eligibility_error(track, position) or ""
        operations = ["open", "record", "import", "save", "export"]
        if current is not None:
            operations.append("edit")
        if vt is not None:
            operations.append("remove")
        marker = track.intro_until_seconds if position == "intro" else track.outro_starts_seconds
        marker_label = "Intro ends at" if position == "intro" else "Outro starts at"
        display = [
            ("Song", f"{track.title} — {track.artist.name if track.artist_id else ''}".strip(" —")),
            ("Position", "Intro (before the song's vocal entry)" if position == "intro"
             else "Outro (over the song's ending)"),
            (marker_label, f"{marker:.1f} s" if marker is not None else "not set"),
            ("Current audio", current.label if current else "none yet"),
        ]
        if vt is not None and vt.recorded_by_id:
            display.append(("Last saved by", vt.recorded_by.get_username()))
        return RecordingContext(
            adapter=ADAPTER_KEY, subject=self.subject_params(subject),
            title=f"{position.title()} voice track — {track.title}",
            purpose="Evergreen voice track: it airs with this song every time the song plays.",
            allowed_operations=tuple(operations),
            max_duration_seconds=float(vtm.VOICETRACK_POLICY.max_duration_seconds),
            max_bytes=vtm.VOICETRACK_POLICY.max_bytes,
            revision=revision, current=current, display=tuple(display),
            return_url=reverse("library:track-detail", args=[track.pk]),
            blocked_reason=blocked, air_label="On air with this song",
        )

    # -- media --------------------------------------------------------------------
    def can_access_media(self, user, subject, media):
        from library.models import VoiceTrack
        if media.owner_id is not None and media.owner_id == getattr(user, "pk", None):
            return True
        return VoiceTrack.objects.filter(track=subject.track, position=subject.position, media=media).exists()

    def source(self, subject):
        vt = vtm.current(subject.track.pk, subject.position)
        if vt is None:
            return None
        audio = vt.playable_audio()
        if audio is None:
            return None
        if audio.origin == "production_media":
            return ("production_media", vt.media)
        return ("legacy", audio.path)

    # -- lifecycle ----------------------------------------------------------------
    @staticmethod
    def _translate(exc):
        if isinstance(exc, vtm.VoiceTrackConflict):
            return Conflict(exc.code, str(exc), **exc.detail)
        return RecorderError(exc.code, str(exc), status=exc.status, **exc.detail)

    def commit(self, user, subject, media, expected_revision):
        source = "import" if media.kind == media.KIND_UPLOAD else "browser"
        try:
            vtm.bind_media(track_id=subject.track.pk, position=subject.position, media_id=media.pk, user=user,
                           expected_revision=expected_revision, source=source)
        except vtm.VoiceTrackServiceError as exc:
            raise self._translate(exc) from exc

    def remove(self, user, subject, expected_revision):
        try:
            vtm.remove_voicetrack(track_id=subject.track.pk, position=subject.position, user=user,
                                  expected_revision=expected_revision)
        except vtm.VoiceTrackServiceError as exc:
            raise self._translate(exc) from exc


ADAPTER = EvergreenVoiceTrackAdapter()
