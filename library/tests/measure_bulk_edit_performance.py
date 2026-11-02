"""QA helper (not collected by default test discovery): time a 1,000-Track bulk edit's preview and apply
through the real endpoints in the guarded sandbox's disposable PostgreSQL, while a second connection
keeps making playback-style single-row updates to the same Tracks, and print the measurements.

    run_with_validation_service.sh <python> manage.py test library.tests.measure_bulk_edit_performance
"""
import contextlib
import json
import statistics
import threading
import time
import types
from unittest import mock

from django.contrib.sessions.models import Session
from django.db import close_old_connections, connections, transaction
from django.db.models import F
from django.test import TransactionTestCase

from library.models import Track
from library.services import track_bulk_edit
from library.tests.test_library_bulk_edit_r0125 import _FixtureMixin


class MeasureBulkEdit(_FixtureMixin, TransactionTestCase):
    def test_measure(self):
        n = track_bulk_edit.MAX_TRACKS
        through_c, through_h = Track.additional_categories.through, Track.holidays.through
        ids = []
        for start in range(0, n, 200):
            made = Track.objects.bulk_create([
                Track(filepath=str(self.root / "ROCK" / f"perf-{i:04d}.flac"), filename=f"perf-{i:04d}.flac",
                      title=f"Perf {i:04d}", artist=self.artist, category=self.rock, format="flac",
                      duration_seconds=180.0, energy="slow", composer="Old Composer")
                for i in range(start, min(start + 200, n))])
            ids += [t.pk for t in made]
        through_c.objects.bulk_create([through_c(track_id=pk, category_id=self.blues.pk) for pk in ids[::2]])
        through_h.objects.bulk_create([through_h(track_id=pk, holiday_id="halloween") for pk in ids[::2]])
        selection = {"mode": "filter", "filter": {"q": "Perf", "category": str(self.rock.pk), "ready2air": ""}}
        changes = {
            "artist": {"op": "set", "value": "Perf New Artist"}, "album": {"op": "set", "value": "Perf Album"},
            "genre": {"op": "set", "value": "Perf Genre"}, "year": {"op": "set", "value": 1987},
            "energy": {"op": "set", "value": "upbeat"}, "composer": {"op": "clear"},
            "category": {"op": "set", "value": self.pop.pk},
            "additional_categories": {"add": [self.rock.pk], "remove": [self.blues.pk]},
            "holidays": {"add": ["xmas"], "remove": ["halloween"]},
        }

        started = time.perf_counter()
        status, preview = self.preview(selection, changes)
        preview_s = time.perf_counter() - started
        self.assertEqual((status, preview["selection"]["count"]), (200, n), preview)
        session_bytes = max(len(s.session_data) for s in Session.objects.all())

        # Per-Track transaction (row-lock hold) durations, and a playback-style writer on the same rows.
        real_atomic, held = transaction.atomic, []

        @contextlib.contextmanager
        def timed_atomic(*args, **kwargs):
            t0 = time.perf_counter()
            with real_atomic(*args, **kwargs):
                yield
            held.append(time.perf_counter() - t0)

        stop, latencies = threading.Event(), []

        def playback_writer():
            close_old_connections()
            try:
                i = 0
                while not stop.is_set():
                    t0 = time.perf_counter()
                    Track.objects.filter(pk=ids[(i * 37) % n]).update(play_count=F("play_count") + 1)
                    latencies.append(time.perf_counter() - t0)
                    i += 1
            finally:
                connections.close_all()

        writer = threading.Thread(target=playback_writer)
        writer.start()
        try:
            with mock.patch.object(track_bulk_edit, "transaction", types.SimpleNamespace(atomic=timed_atomic)):
                started = time.perf_counter()
                status, applied = self.post("library:api-track-bulk-edit-apply",
                                            {"selection": selection, "changes": changes, "token": preview["token"]})
                apply_s = time.perf_counter() - started
        finally:
            stop.set()
            writer.join(30)
        self.assertEqual(status, 200, applied)
        per_track = sorted(held[1:])         # the first is the shared lookup-row preparation
        lat = sorted(latencies)
        result = {
            "tracks": n, "preview_seconds": round(preview_s, 3), "apply_seconds": round(apply_s, 3),
            "updated": applied["updated"], "unchanged": applied["unchanged"], "stale": len(applied["stale"]),
            "refused": len(applied["refused"]), "failed": len(applied["failed"]),
            "session_bytes_after_preview": session_bytes,
            "per_track_transaction_ms": {"mean": round(statistics.mean(per_track) * 1000, 2),
                                         "p99": round(per_track[int(len(per_track) * 0.99) - 1] * 1000, 2)
                                         if per_track else None,
                                         "max": round(max(per_track) * 1000, 2)},
            "playback_writer_updates": len(lat),
            "playback_writer_latency_ms": {"median": round(statistics.median(lat) * 1000, 2),
                                           "p99": round(lat[int(len(lat) * 0.99) - 1] * 1000, 2),
                                           "max": round(lat[-1] * 1000, 2)} if lat else None,
            "playback_play_count_total": sum(Track.objects.filter(pk__in=ids).values_list("play_count", flat=True)),
        }
        print("\n=====BEGIN-MEASURE=====\n" + json.dumps(result, indent=2) + "\n=====END-MEASURE=====")
        self.assertEqual(applied["updated"] + len(applied["stale"]), n)
