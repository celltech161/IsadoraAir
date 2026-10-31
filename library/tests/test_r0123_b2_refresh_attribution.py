"""r0123 corrective B2: background waveform-refresh status is attributed to exactly one request
(its own lock, outcome record and cache token) -- never to a later or another Track's refresh, never
by timestamp. Per-test temporary directories only; no station data is touched."""
import json
import os
import time
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import Client

from library.models import Track
from library.services import waveform_cache, waveform_generation as wg
from library.tests.test_waveform_integrity_package_b import _Base


# ------------------------------------------------------------------ B2: exact request attribution

class RequestAttributionTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def started(self, track):
        """A background request whose child never ran (Popen mocked): its token, its lock file released."""
        Track.objects.filter(pk=track.pk).update(duration_seconds=wg.SYNC_MAX_SECONDS + 1)
        with patch.object(wg.subprocess, "Popen"):
            response = self.client.post(f"/api/tracks/{track.pk}/refresh-waveform/")
        Track.objects.filter(pk=track.pk).update(duration_seconds=3.0)
        self.assertEqual(response.status_code, 202, response.content)
        return response.json()["requested_ns"]

    def run_child(self, track, token, **patches):
        """What the detached child does, in-process: the same command, the same handed-over locks."""
        operation = waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock")
        slot = waveform_cache.try_operation_lock(self.waves, wg.BACKGROUND_SLOT_LOCK)
        name = wg.request_lock_name(track.pk, token)
        (waveform_cache.lock_dir() / name).unlink(missing_ok=True)
        request_lock = waveform_cache.create_request_lock(name)
        with patch("os.nice"):
            try:
                call_command("refresh_waveform", str(track.pk), "--background", "--operation-lock-fd", str(operation),
                             "--slot-lock-fd", str(slot), "--requested-ns", str(token),
                             "--request-lock-fd", str(request_lock), stdout=StringIO())
            except Exception:
                pass                                      # a failed refresh raises CommandError after recording

    def status(self, track, token):
        return wg.background_status(track.pk, token)

    def test_a_later_request_never_completes_an_earlier_one(self):
        track = self.track()
        token_a = self.started(track)                       # child died before doing anything
        self.assertEqual(self.status(track, token_a)["state"], "unknown")
        token_b = token_a + 1_000
        self.run_child(track, token_b)                      # request B succeeds
        self.assertEqual(json.loads(self.cache(track).read_text())["generation"]["request"], token_b)
        self.assertEqual(self.status(track, token_b)["state"], "completed")
        self.assertEqual(self.status(track, token_a)["state"], "unknown")   # B's cache is not A's result
        response = self.client.get(f"/api/tracks/{track.pk}/refresh-waveform/status/?requested_ns={token_a}")
        self.assertEqual((response.status_code, response.json()["state"]), (200, "unknown"))

    def test_running_means_this_request_is_running(self):
        track = self.track()
        token = self.started(track)
        held = os.open(waveform_cache.lock_dir() / wg.request_lock_name(track.pk, token), os.O_RDONLY)
        import fcntl
        fcntl.flock(held, fcntl.LOCK_EX)                    # its child is alive
        try:
            self.assertEqual(self.status(track, token)["state"], "running")
            other = token + 5                               # another request on the same Track
            self.assertEqual(self.status(track, other)["state"], "unknown")
            os.close(waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock"))
        finally:
            os.close(held)
        self.assertEqual(self.status(track, token)["state"], "unknown")    # child gone, nothing recorded

    def test_sequential_requests_each_report_their_own_outcome(self):
        track = self.track()
        first, second = self.started(track), None
        self.run_child(track, first)
        second = first + 7
        Path(track.filepath).unlink()                       # the second fails (source missing)
        self.run_child(track, second)
        self.assertEqual(self.status(track, first)["state"], "completed")
        failed = self.status(track, second)
        self.assertEqual((failed["state"], failed["status"]), ("failed", wg.SOURCE_MISSING))
        self.assertEqual(json.loads(self.cache(track).read_text())["generation"]["request"], first)  # cache kept

    def test_unrelated_malformed_negative_and_future_tokens_never_match(self):
        track = self.track()
        token = self.started(track)
        self.run_child(track, token)
        self.assertEqual(self.status(track, 1_000)["state"], "unknown")     # old, unrelated
        for bad in ("abc", "-5", "0", "1e18", "", f"{time.time_ns() + 3600 * 10 ** 9}", " 12"):
            with self.subTest(bad=bad):
                response = self.client.get(f"/api/tracks/{track.pk}/refresh-waveform/status/?requested_ns={bad}")
                self.assertEqual((response.status_code, response.json()["state"]), (400, "unknown"))
        self.assertEqual(wg.background_status(track.pk, -5)["state"], "unknown")

    def test_death_after_promotion_but_before_recording_is_attributed_exactly(self):
        track = self.track()
        token = self.started(track)
        self.assertEqual(wg.refresh(track.pk, request_token=token).status, wg.REPLACED)   # promoted, no event
        self.assertEqual(self.status(track, token)["state"], "completed")
        self.assertEqual(self.status(track, token - 1)["state"], "unknown")

    def test_death_before_promotion_keeps_the_previous_cache_and_reports_unknown(self):
        track = self.track()
        old = self.cache(track).read_bytes()
        token = self.started(track)
        self.assertEqual(self.status(track, token)["state"], "unknown")
        self.assertEqual(self.cache(track).read_bytes(), old)

    def test_another_tracks_refresh_never_completes_this_request(self):
        track, other = self.track(), self.track(name="other.flac")
        token = self.started(track)
        self.run_child(other, token)                        # same token value, different Track
        self.assertEqual(self.status(other, token)["state"], "completed")
        self.assertEqual(self.status(track, token)["state"], "unknown")

    def test_a_reloaded_page_polls_the_same_request(self):
        track = self.track()
        token = self.started(track)
        self.run_child(track, token)
        response = self.client.get(f"/api/tracks/{track.pk}/refresh-waveform/status/?requested_ns={token}")
        self.assertEqual((response.json()["state"], response.json()["requested_ns"]), ("completed", token))
        source = (Path(__file__).resolve().parents[2] / "library/templates/library/track_detail.html").read_text()
        self.assertIn("sessionStorage.setItem(REFRESH_KEY, String(data.requested_ns))", source)
        self.assertIn("if (resp.status === 400)", source)
