"""D3E-C fixtures: real publications driven through the REAL domain path.

A READY aggregate is rendered and validated for real, published for real
(isolated LIBRARY_ROOT / WAVEFORMS_DIR, the native-airing gate opened ONLY by
the D2B test patch), dispatched, then taken through the engine-side services
the splice and the deck call -- ``playout.admit`` + ``record_insertion``
(ACCEPTED), ``playout.authorize_open`` (CLAIMED) -- and the deck's own
evidence (LogItem.played_at, PlayEvent and its segment) projected by
``playout.sync_evidence`` (STARTED / COMPLETED / INTERRUPTED). No state is
written raw; no GStreamer engine is needed.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from active_messages import playout, retention
from active_messages.models import (
    ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastMessageEvent, ActiveBroadcastPublication,
)
from library.models import LogItem, PlayEvent, PlayEventSegment, PlaylistLog, Track

from .d2b_support import Playout

ENGINE = "d3ec-test-engine"


class Lifecycle(Playout):
    def stored(self, execution):
        return ActiveBroadcastExecution.objects.get(pk=getattr(execution, "pk", execution))

    def accepted(self, **kwargs):
        execution, publication = self.published(**kwargs)
        with self.committing():
            playout.dispatch(execution)
        command = self.commands[-1]
        log = PlaylistLog.objects.create(date=date(2027, 9, 13), hour=self._next_hour(), status="approved")
        with transaction.atomic():
            admission = playout.admit(command)
            item = LogItem.objects.create(playlist_log=log, position=0, scheduled_time=timezone.now(),
                                          track=admission.track, track_title=admission.track.title,
                                          track_artist=admission.track.artist.name, category=admission.category)
            playout.record_insertion(admission, item, ENGINE)
        return self.stored(execution), ActiveBroadcastPublication.objects.get(pk=publication.pk), item

    def _next_hour(self):
        self._hour = getattr(self, "_hour", -1) + 1
        return self._hour % 24

    def claimed(self, **kwargs):
        execution, publication, item = self.accepted(**kwargs)
        item = LogItem.objects.select_related("track").get(pk=item.pk)
        LogItem.objects.filter(pk=item.pk).update(playback_claimed_at=timezone.now())
        decision = playout.authorize_open(item, ENGINE)
        assert decision is not None and decision.allowed, decision
        return self.stored(execution), publication, item

    def air(self, item, *, state="active", termination="", seconds=1.0):
        """The deck's own evidence for ``item`` (a PlayEvent and one segment)."""
        started = timezone.now() - timedelta(seconds=seconds + 1)
        ended = None if state == "active" else started + timedelta(seconds=seconds)
        LogItem.objects.filter(pk=item.pk).update(played_at=started)
        track = LogItem.objects.get(pk=item.pk).track
        event = PlayEvent.objects.create(track=track, log_item_id_snapshot=item.pk, track_title="Urgent PA",
                                         track_artist="Urgent PA", started_at=started, ended_at=ended,
                                         duration_played_seconds=seconds, duration_evidence_state=state)
        PlayEventSegment.objects.create(play_event=event, log_item_id_snapshot=item.pk, started_at=started,
                                        last_confirmed_at=ended or started, ended_at=ended,
                                        confirmed_duration_seconds=seconds, termination_reason=termination,
                                        evidence_state=state)
        return event

    def started(self, **kwargs):
        execution, publication, item = self.claimed(**kwargs)
        self.air(item, state="active")
        playout.sync_evidence(execution)
        return self.stored(execution), publication, item

    def completed(self, **kwargs):
        execution, publication, item = self.claimed(**kwargs)
        self.air(item, state="complete", termination="eos")
        playout.sync_evidence(execution)
        row = self.stored(execution)
        assert row.state == "completed", row.state
        return row, publication, item

    def interrupted(self, **kwargs):
        execution, publication, item = self.claimed(**kwargs)
        self.air(item, state="interrupted", termination="error")
        playout.sync_evidence(execution)
        row = self.stored(execution)
        assert row.state == "interrupted", row.state
        return row, publication, item

    # -- observation -------------------------------------------------------------------------
    def file(self, publication) -> Path:
        return self.library_root / publication.library_relpath

    def wave(self, publication) -> Path:
        return self.waveforms / f"{publication.track_id_snapshot}.json"

    def history(self):
        """Everything D3E-C must never delete or rewrite (except the SET_NULL of
        a retired Track's live references, which the snapshots record)."""
        def rows(model, *exclude):
            fields = [f.attname for f in model._meta.concrete_fields if f.attname not in exclude]
            return sorted(json.dumps(item, sort_keys=True, default=str)
                          for item in model.objects.values(*fields))
        return {
            "executions": rows(ActiveBroadcastExecution),
            "events": rows(ActiveBroadcastMessageEvent),
            "publications": rows(ActiveBroadcastPublication, "track_id"),
            "insertions": rows(ActiveBroadcastInsertion, "log_item_id"),
            "log_items": rows(LogItem, "track_id"),
            "play_events": rows(PlayEvent, "track_id"),
        }

    def disk(self):
        found = {}
        for root in (self.library_root, self.waveforms):
            for current, _dirs, names in os.walk(root):
                for name in names:
                    path = Path(current) / name
                    info = os.lstat(path)
                    found[str(path)] = (info.st_size, info.st_mtime_ns)
        return found

    def operator(self):
        return self.admin

    def retire(self, execution, *, apply=True, operator=None):
        with self.captureOnCommitCallbacks(execute=True) if hasattr(self, "captureOnCommitCallbacks") else _null():
            return retention.retire_publication(execution.pk, operator=operator or self.admin, apply=apply)

    def tracks(self, publication):
        return Track.objects.filter(pk=publication.track_id_snapshot)


class _null:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


__all__ = ["Lifecycle", "ENGINE"]
