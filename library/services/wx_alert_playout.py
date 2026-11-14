"""Engine-side authority for the combined Weather/AMBER announcement (WxAlert/wx_alert.flac).

The announcement is ONE canonical file and Track, rebuilt in place (atomic rename) by the combined coordinator
``weather_ingest/wx_alert.py``, which records what should air in ``WEATHER_DATA_DIR/wx_alert_state.json``
(``isadoraair.wx_alert_state``).  This module is the engine's read side:

* ``audio_track()`` -- the exact canonical Track (never "first ready Track in the category", which could be a
  retired ``wx_alert.mp3``).
* ``authorize_open()`` -- the binding check at deck open: holds the publication's own lock SHARED (never
  blocking) through the file open, requires an active, unexpired generation whose recorded audio is the file on
  disk, refreshes the Track row (duration/next-start of the audio that will actually open) and refuses a
  generation another occurrence already claimed.
* ``generation_claimed()`` -- delivery truth for idempotent synchronization, from persisted playback claims.

Local file reads and bounded indexed queries only: never a network request or audio work on the engine thread.
"""
from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.utils import timezone

from isadoraair import wx_alert_state
from library.models import LogItem, Track

CATEGORY_CODE = wx_alert_state.CATEGORY_CODE


def audio_path() -> Path:
    root = Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")).resolve()
    return wx_alert_state.audio_path(root)


def state_path() -> Path:
    return wx_alert_state.state_path(getattr(settings, "WEATHER_DATA_DIR", "/var/lib/isadoraair/weather"))


def read_state():
    return wx_alert_state.read_state(state_path())


def audio_track():
    """The canonical wx_alert.flac Track if it is Ready to Air, else None."""
    return (
        Track.objects.filter(filepath=str(audio_path()), ready2air=True)
        .select_related("artist", "category")
        .first()
    )


def is_occurrence(log_item) -> bool:
    """Database-free: is this LogItem a Weather/AMBER announcement occurrence?"""
    category = getattr(log_item, "category", None) if getattr(log_item, "category_id", None) else None
    if getattr(category, "code", None) == CATEGORY_CODE:
        return True
    track = getattr(log_item, "track", None) if getattr(log_item, "track_id", None) else None
    return getattr(track, "filepath", None) == str(audio_path())


def current_decision(now=None):
    """(PlaybackDecision, state) for the file on disk right now (no lock; advisory for synchronization)."""
    state, error = read_state()
    if state is None:
        return wx_alert_state.PlaybackDecision(False, f"combined alert state {error}"), None
    identity = wx_alert_state.audio_identity(audio_path())
    return wx_alert_state.playback_decision(state, identity, now or timezone.now()), state


def generation_claimed(track_id, published_at, *, exclude_pk=None) -> bool:
    """Did any occurrence of the announcement Track claim playback at/after this generation's publication?

    Exact under the deck-open lock: a deck opens the file only while the state record names that file and while
    the publication lock is held shared, so an earlier generation's claim always precedes the next generation's
    ``published_at``.  Persisted, so it survives an engine restart."""
    qs = LogItem.objects.filter(track_id=track_id, playback_claimed_at__gte=published_at)
    if exclude_pk is not None:
        qs = qs.exclude(pk=exclude_pk)
    return qs.exists()


def authorize_open(log_item, scope):
    """The final WxAlert deck-open authority.  Returns None when the occurrence may open (``log_item.track`` is
    then the freshly read Track), else the refusal reason.  ``scope`` (an ExitStack) receives the shared
    publication lock and must stay open until the deck's filesrc has opened the file."""
    from weather.publication import destination_lock_path

    path = audio_path()
    if not path.parent.is_dir():
        return "the Weather/AMBER announcement has never been published"
    if not scope.enter_context(wx_alert_state.shared_publication_lock(destination_lock_path(path))):
        return "a Weather/AMBER announcement publication is in progress"
    state, error = read_state()
    if state is None:
        return f"combined alert state {error}"
    fresh = (
        Track._base_manager.select_related("artist", "album", "category", "category__kind")
        .filter(pk=log_item.track_id)
        .first()
    )
    if fresh is None:
        return "the announcement Track no longer exists"
    if fresh.filepath != str(path):
        return f"not the canonical announcement Track ({fresh.filepath})"
    decision = wx_alert_state.playback_decision(state, wx_alert_state.audio_identity(path), timezone.now())
    if not decision.allowed:
        return decision.reason
    claimed_at = getattr(log_item, "playback_claimed_at", None)
    if claimed_at is not None and claimed_at < decision.published_at:
        # A recreation (resume/seek) of an occurrence that began an EARLIER generation must never continue
        # into the newer audio now at the same path.
        return f"occurrence was claimed for an earlier announcement than generation {decision.generation}"
    if generation_claimed(fresh.pk, decision.published_at, exclude_pk=log_item.pk):
        return f"announcement generation {decision.generation} was already claimed by another occurrence"
    log_item.track = fresh
    return None
