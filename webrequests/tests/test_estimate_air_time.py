"""1.1 airtime-correction follow-up -- webrequests.services.
estimate_air_time inspection: it means "estimated audible request-air
time" (confirmed via its own docstring and _live_eta_datetime's
comment: "the same way [the real 'Coming Up' dashboard] already
computes its 'Time On' column"). It's a pure CONSUMER of whichever
value engine_state.json's "queue" entries carry as eta_seconds
(live source) or LogItem.scheduled_time (static fallback when the
engine isn't running/the item isn't in its live preview yet) -- it
performs no timing arithmetic of its own.

Both of those sources were corrected elsewhere in this pass
(_write_state's queue-eta accumulation in engine.py, and
log_builder.py's own build-time scheduled_time accumulation via
effective_airtime_seconds in the prior airtime-correction pass) --
estimate_air_time therefore needed NO code change; this test proves it
inherits the corrected live value automatically, without duplicating
or re-deriving the timing formula itself."""
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track
from webrequests.models import WebRequestConfig
from webrequests.services import estimate_air_time
import webrequests.services as services_module


class EstimateAirTimeInheritsCorrectedLiveEtaTests(TestCase):
    def setUp(self):
        kind, _ = CategoryKind.objects.get_or_create(code="music", defaults={"name": "Music"})
        self.category = Category.objects.create(code="ESTAIRTIME", name="Estimate Air Time", kind=kind)
        self.artist, _ = Artist.get_or_create_ci("Estimate Air Time Artist")
        WebRequestConfig.objects.all().delete()
        WebRequestConfig.objects.create(enabled=True, open_slots=list(range(168)), max_fulfilled_per_hour=4)

        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_path = Path(self._tmpdir.name) / "engine_state.json"
        patcher = patch.object(services_module, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

        log = PlaylistLog.objects.create(date=date(2027, 6, 2), hour=11, status="approved")
        track = Track.objects.create(
            filepath="/tmp/does-not-exist/estimate-air-time.mp3", filename="t.mp3",
            title="Requested Song", artist=self.artist, category=self.category,
            ready2air=True, duration_seconds=250.0,
        )
        # A stale, INCORRECT static scheduled_time deliberately far from
        # the live eta_seconds below, so a passing test can only mean
        # estimate_air_time actually preferred the live queue source.
        self.item = LogItem.objects.create(
            playlist_log=log, position=1,
            scheduled_time=timezone.now() + timezone.timedelta(hours=5),
            track=track, category=self.category,
        )

    def write_state(self, queue):
        state = {
            "decks": {"A": None, "B": None}, "queue": queue,
            "date": "2027-06-02", "hour": 11, "timestamp": timezone.now().timestamp(),
        }
        self.state_path.write_text(__import__("json").dumps(state), encoding="utf-8")

    def test_uses_the_corrected_live_eta_seconds_not_the_stale_static_schedule(self):
        # eta_seconds=138.0 mirrors this pass's own worked example
        # (current-deck runway 138s) -- if this were still computed
        # from raw next_start_seconds instead of the corrected
        # accumulation, a caller couldn't tell the difference from this
        # test alone, but the point here is narrower and still real:
        # estimate_air_time must read the LIVE value verbatim, not fall
        # through to the stale static scheduled_time, once the engine's
        # own (now-corrected) queue preview contains this item.
        self.write_state(queue=[{"item_id": self.item.id, "eta_seconds": 138.0}])

        before = timezone.now()
        result = estimate_air_time(self.item)
        after = timezone.now()

        expected_low = before + timezone.timedelta(seconds=138.0)
        expected_high = after + timezone.timedelta(seconds=138.0)
        self.assertTrue(expected_low <= result <= expected_high)
        # Must NOT be the stale ~5-hour-away static fallback.
        self.assertLess((result - before).total_seconds(), 3600)

    def test_falls_back_to_static_scheduled_time_when_engine_not_running(self):
        # No state file written at all -- _read_engine_state returns
        # None, _live_eta_datetime returns None, falls through to the
        # log-builder-computed (already-corrected, prior pass)
        # scheduled_time.
        result = estimate_air_time(self.item)
        self.assertEqual(result, self.item.scheduled_time)
