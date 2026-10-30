"""Test support: give a Track fixture its markers through the cue-marker authority.

Fixtures describe a Track's audio and its markers; they set them as markers of (new) media, which
the authority records as generated and unprotected. Never a production code path."""
from library.models import Track
from library.services import cue_markers


def set_markers(track, **values):
    track_id = getattr(track, "pk", track)
    revision = Track.objects.filter(pk=track_id).values_list("cue_marker_revision", flat=True).get()
    result = cue_markers.apply_calculation(
        track_id, values, basis_revision=revision, source=cue_markers.SOURCE_GENERATED, media_replaced=True)
    if not result.ok:
        raise AssertionError(f"fixture markers refused: {result.conflict or result.error}")
    if hasattr(track, "refresh_from_db"):
        track.refresh_from_db()
    return result
