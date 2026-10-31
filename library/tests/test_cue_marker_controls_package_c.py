"""r0123 Package C: Track Detail cue-marker controls -- the state/action endpoint, server-side
confirmation of protected replacements, approval history, waveform trust presentation, and the
Refresh Waveform Only status protocol.

Server-side behaviour only: the page's JavaScript is exercised in a browser at field acceptance
(no browser is available inside the test sandbox). Real audio is generated into per-test
temporary directories; no station media, waveform, database or service is touched."""
import json
import os
import re
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import Client

from library.models import Track
from library.services import audio_identity, cue_markers, waveform_cache, waveform_generation as wg
from library.tests.test_waveform_integrity_package_b import _audio, _Base, _row
from monitoring.models import SystemEvent, emit_event

M = cue_markers.MARKERS
REPO = Path(__file__).resolve().parents[2]


class _Controls(_Base):
    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.force_login(self.user)

    def state(self, track):
        response = self.client.get(f"/api/tracks/{track.pk}/cue-markers/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def act(self, track, body, status=200):
        response = self.client.post(f"/api/tracks/{track.pk}/cue-markers/", data=json.dumps(body),
                                    content_type="application/json")
        self.assertEqual(response.status_code, status, response.content)
        return response.json()

    def patch_track(self, track, body):
        return self.client.patch(f"/api/tracks/{track.pk}/", data=json.dumps(body), content_type="application/json")

    def displays(self, track):
        return {name: info["display"] for name, info in self.state(track)["cue_marker_review"].items()}


# ------------------------------------------------------------------ presentation

class PresentationTests(_Controls):
    def test_the_page_renders_eight_marker_rows_and_the_controls(self):
        track = self.track()
        html = self.client.get(f"/track/{track.pk}/").content.decode()
        self.assertEqual(re.findall(r'data-marker="([a-z_]+)"', html), list(M))
        for name in M:
            self.assertIn(f'<label for="f_{name}">', html)
        for element in ('id="lockAllBtn"', 'id="unlockAllBtn"', 'id="refreshWaveformBtn"', 'id="cueNotice"',
                        'role="status"', "without changing cue points, Track metadata, or the source audio"):
            self.assertIn(element, html)

    def test_every_display_state_is_reported_truthfully(self):
        track = self.track()                                          # historical values, legacy cache
        Track.objects.filter(pk=track.pk).update(waveform_path="")    # never analyzed: Cue In 0 is a default
        self.assertEqual(self.displays(track)["cue_in_seconds"], "default_zero")
        self.assertEqual(self.displays(track)["next_start_seconds"], "unset")
        track = self.track(name="b.flac")
        cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.5, "hook_in_seconds": 1.0}, basis_revision=0,
                                      source=cue_markers.SOURCE_GENERATED, media_replaced=True)
        cue_markers.operator_edit(track.pk, expected_revision=1, user=self.user, values={"intro_until_seconds": 0.8})
        cue_markers.operator_edit(track.pk, expected_revision=2, user=self.user, approve=["hook_in_seconds"])
        cue_markers.operator_edit(track.pk, expected_revision=3, user=self.user, unlock=["hook_in_seconds"])
        shown = self.displays(track)
        self.assertEqual((shown["next_start_seconds"], shown["hook_in_seconds"], shown["intro_until_seconds"],
                          shown["cue_in_seconds"]), ("auto", "unlocked_operator", "locked_unverified", "historical"))
        state = self.state(track)
        self.assertFalse(state["audio_verified"])                     # unknown audio is never shown as verified
        self.assertEqual(state["waveform"]["status"], "legacy")
        self.assertFalse(state["repick_available"])

    def test_current_other_and_replaced_audio_approvals_do_not_look_alike(self):
        track = self.track()
        wg.refresh(track.pk)
        state = self.act(track, {"cue_marker_revision": 0, "approve": ["cue_in_seconds"]})
        self.assertTrue(state["audio_verified"])
        self.assertEqual(state["cue_marker_review"]["cue_in_seconds"]["display"], "approved")
        self.assertEqual(state["cue_marker_review"]["cue_in_seconds"]["approved_by_name"], self.user.username)
        _audio(Path(track.filepath), freq=500)                        # replaced outside any writer...
        wg.refresh(track.pk)                                           # ...and re-identified
        info = self.state(track)["cue_marker_review"]["cue_in_seconds"]
        self.assertEqual((info["display"], info["needs_review"]), ("locked_other_audio", True))
        cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.0}, basis_revision=1, media_replaced=True,
                                      audio_revision="d" * 64)       # a writer publishing new media
        info = self.state(track)["cue_marker_review"]["cue_in_seconds"]
        self.assertEqual((info["display"], info["needs_review"]), ("review", True))
        self.assertEqual(info["previous_approval"]["approved_by_name"], self.user.username)
        self.assertTrue(Track.objects.get(pk=track.pk).ready2air)     # review never changes readiness


# ------------------------------------------------------------------ lock, unlock, approve

class ActionTests(_Controls):
    def test_approve_an_unchanged_value_and_unlock_keep_values_and_history(self):
        track = self.track()
        before = {name: getattr(track, name) for name in M}
        state = self.act(track, {"cue_marker_revision": 0, "approve": ["cue_in_seconds"]})
        self.assertEqual((state["cue_marker_revision"], state["result"]["approved"]), (1, ["cue_in_seconds"]))
        entry = _row(track.pk)["cue_marker_state"]["cue_in_seconds"]
        self.assertEqual((entry["protection"], entry["approved_by"]), ("locked", self.user.pk))
        state = self.act(track, {"cue_marker_revision": 1, "unlock": ["cue_in_seconds"]})
        self.assertEqual(state["cue_marker_review"]["cue_in_seconds"]["display"], "unlocked_operator")
        entry = _row(track.pk)["cue_marker_state"]["cue_in_seconds"]
        self.assertEqual(entry["previous_approval"]["approved_by"], self.user.pk)     # history kept
        self.assertEqual({name: _row(track.pk)[name] for name in M}, before)            # no value moved

    def test_nothing_is_approved_or_unlocked_without_a_value(self):
        track = self.track()
        state = self.act(track, {"cue_marker_revision": 0, "approve": ["hook_out_seconds"],
                                 "unlock": ["intro_until_seconds"]})
        self.assertEqual(state["result"]["skipped"], {"hook_out_seconds": "no value to approve",
                                                     "intro_until_seconds": "no value to unlock"})
        self.assertEqual((_row(track.pk)["cue_marker_revision"], _row(track.pk)["cue_marker_state"]), (0, {}))
        self.assertIsNone(_row(track.pk)["hook_out_seconds"])

    def test_lock_all_and_unlock_all_are_one_revision_step_each_and_skip_unset_markers(self):
        track = self.track()
        Track.objects.filter(pk=track.pk).update(waveform_path="")    # Cue In 0 = never-analyzed default
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user,
                                  values={"next_start_seconds": 2.0, "hook_in_seconds": 1.0})
        state = self.act(track, {"cue_marker_revision": 1, "action": "lock_all"})
        self.assertEqual(state["cue_marker_revision"], 1)            # both already locked: no change at all
        self.act(track, {"cue_marker_revision": 1, "action": "unlock_all"})
        row = _row(track.pk)
        self.assertEqual(row["cue_marker_revision"], 2)
        self.assertEqual({n for n, e in row["cue_marker_state"].items() if e.get("protection") == "unlocked"},
                         {"next_start_seconds", "hook_in_seconds", "cue_in_seconds"})
        state = self.act(track, {"cue_marker_revision": 2, "action": "lock_all"})
        self.assertEqual(state["cue_marker_revision"], 3)
        self.assertEqual(set(state["result"]["approved"]), {"next_start_seconds", "hook_in_seconds"})
        displays = self.displays(track)
        self.assertEqual(displays["cue_in_seconds"], "unlocked_operator")   # the unlocked default stays unlocked
        self.assertEqual((displays["hook_out_seconds"], displays["intro_until_seconds"]), ("unset", "unset"))
        self.assertFalse(state["audio_verified"])                    # no trusted cache: not approved "for this audio"
        self.assertIsNone(_row(track.pk)["cue_marker_state"]["next_start_seconds"]["audio_revision"])

    def test_a_stale_missing_or_invalid_request_changes_nothing(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, approve=["cue_in_seconds"])
        for body, status in (({"cue_marker_revision": 0, "action": "unlock_all"}, 409),
                             ({"action": "unlock_all"}, 400),
                             ({"cue_marker_revision": 1, "action": "erase"}, 400),
                             ({"cue_marker_revision": 1, "approve": ["volume"]}, 400),
                             ({"cue_marker_revision": 1, "approve": "cue_in_seconds"}, 400)):
            with self.subTest(body=body):
                self.act(track, body, status=status)
                self.assertEqual(_row(track.pk)["cue_marker_revision"], 1)
        self.assertEqual(self.act(track, {"cue_marker_revision": 0, "unlock": ["cue_in_seconds"]}, 409)["conflict"],
                         True)

    def test_unauthorized_users_cannot_change_protection_or_refresh(self):
        track = self.track()
        listener = Client()
        listener.force_login(User.objects.create_user("listener-c", password="pw"))
        for url, body in ((f"/api/tracks/{track.pk}/cue-markers/", {"cue_marker_revision": 0, "action": "lock_all"}),
                          (f"/api/tracks/{track.pk}/refresh-waveform/", {})):
            with self.subTest(url):
                response = listener.post(url, data=json.dumps(body), content_type="application/json")
                self.assertEqual(response.status_code, 403)
        self.assertEqual(listener.get(f"/api/tracks/{track.pk}/refresh-waveform/status/?requested_ns=1").status_code,
                         403)
        self.assertEqual(_row(track.pk)["cue_marker_revision"], 0)


# ------------------------------------------------------------------ editing with confirmation

class EditTests(_Controls):
    def test_editing_an_unprotected_marker_locks_only_that_marker(self):
        track = self.track()
        response = self.patch_track(track, {"hook_in_seconds": 1.25, "cue_marker_revision": 0})
        self.assertEqual(response.status_code, 200, response.content)
        state = _row(track.pk)["cue_marker_state"]
        self.assertEqual(set(state), {"hook_in_seconds"})
        self.assertEqual(state["hook_in_seconds"]["protection"], "locked")

    def test_replacing_a_protected_value_needs_server_side_confirmation(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, values={"next_start_seconds": 2.0})
        for body in ({"title": "Should Not Save", "next_start_seconds": 2.2, "cue_marker_revision": 1},
                     {"title": "Should Not Save", "next_start_seconds": 2.2, "cue_marker_revision": 1,
                      "confirm_override": ["hook_in_seconds"]}):
            with self.subTest(body=body):
                response = self.patch_track(track, body)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()["confirmation_required"], ["next_start_seconds"])
                row = Track.objects.filter(pk=track.pk).values("title", "next_start_seconds",
                                                                "cue_marker_revision").get()
                self.assertEqual((row["title"], row["next_start_seconds"], row["cue_marker_revision"]),
                                 ("song.flac", 2.0, 1))           # nothing at all was saved
        confirmed = self.patch_track(track, {"next_start_seconds": 2.2, "cue_marker_revision": 1,
                                             "confirm_override": ["next_start_seconds"]})
        self.assertEqual(confirmed.status_code, 200, confirmed.content)
        entry = _row(track.pk)["cue_marker_state"]["next_start_seconds"]
        self.assertEqual((_row(track.pk)["next_start_seconds"], entry["protection"]), (2.2, "locked"))
        self.assertEqual((entry["previous_approval"]["value"], entry["previous_approval"]["retired_reason"]),
                         (2.0, "replaced_by_operator"))

    def test_historical_values_need_confirmation_and_record_the_replaced_value(self):
        track = self.track()                                           # Cue In 0.0 with a cache: historical
        self.assertEqual(self.patch_track(track, {"cue_in_seconds": 0.4, "cue_marker_revision": 0}).status_code, 409)
        response = self.patch_track(track, {"cue_in_seconds": 0.4, "cue_marker_revision": 0,
                                            "confirm_override": ["cue_in_seconds"]})
        self.assertEqual(response.status_code, 200, response.content)
        entry = _row(track.pk)["cue_marker_state"]["cue_in_seconds"]
        self.assertEqual(entry["replaced_historical_value"]["value"], 0.0)

    def test_the_authority_enforces_confirmation_under_its_lock(self):
        track = self.track()
        result = cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user,
                                           values={"cue_in_seconds": 0.3}, confirm_protected=[])
        self.assertEqual((result.ok, result.confirmation_required), (False, ["cue_in_seconds"]))
        self.assertEqual(_row(track.pk)["cue_marker_revision"], 0)

    def test_metadata_only_and_unchanged_full_form_saves_touch_no_marker_state(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, approve=["cue_in_seconds"])
        before = _row(track.pk)
        unchanged = {name: before[name] for name in M}
        for body in ({"title": "Renamed"}, {"title": "Renamed Again", **unchanged, "cue_marker_revision": 1}):
            with self.subTest(body=sorted(body)):
                self.assertEqual(self.patch_track(track, body).status_code, 200)
                after = _row(track.pk)
                self.assertEqual((after["cue_marker_revision"], after["cue_marker_state"]),
                                 (before["cue_marker_revision"], before["cue_marker_state"]))

    def test_a_stale_page_cannot_overwrite_newer_values(self):
        track = self.track()
        self.act(track, {"cue_marker_revision": 0, "unlock": ["cue_in_seconds"]})
        response = self.patch_track(track, {"cue_in_seconds": 0.7, "cue_marker_revision": 0})
        self.assertEqual((response.status_code, response.json()["conflict"]), (409, True))
        self.assertEqual(_row(track.pk)["cue_in_seconds"], 0.0)


# ------------------------------------------------------------------ waveform trust and refresh

class TrustAndRefreshTests(_Controls):
    def test_trust_states_and_repick_availability_follow_the_backend(self):
        track = self.track()
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("legacy", False))
        wg.refresh(track.pk)
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("trusted", True))
        self.act(track, {"cue_marker_revision": 0, "unlock": ["cue_in_seconds"]})
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("trusted_stale_markers", True))
        moved = Path(track.filepath).with_name("moved.flac")
        os.rename(track.filepath, moved)
        Track.objects.filter(pk=track.pk).update(filepath=str(moved), filename=moved.name)
        self.assertEqual((self.state(track)["waveform"]["status"], self.state(track)["repick_available"]),
                         ("unverified_audio", False))
        self.cache(track).write_text("{\"track_id\"")
        state = self.state(track)
        self.assertEqual((state["waveform"]["status"], state["repick_available"]), ("malformed", False))
        self.assertIn("Refresh Waveform Only", state["waveform"]["explanation"])

    def test_a_short_refresh_reports_completion_and_changes_no_marker_state(self):
        track = self.track()
        cue_markers.operator_edit(track.pk, expected_revision=0, user=self.user, approve=["cue_in_seconds"])
        before = _row(track.pk)
        response = self.client.post(f"/api/tracks/{track.pk}/refresh-waveform/")
        self.assertEqual((response.status_code, response.json()["status"]), (200, "replaced"))
        self.assertEqual(_row(track.pk), before)
        self.assertTrue(self.state(track)["repick_available"])

    def long_form(self):
        track = self.track(name="programme.flac")
        Track.objects.filter(pk=track.pk).update(duration_seconds=wg.SYNC_MAX_SECONDS + 1)
        with patch.object(wg.subprocess, "Popen") as popen:
            response = self.client.post(f"/api/tracks/{track.pk}/refresh-waveform/")
        self.assertEqual((response.status_code, response.json()["status"]), (202, "started"))
        requested = response.json()["requested_ns"]
        self.assertIn(str(requested), popen.call_args.args[0])
        Track.objects.filter(pk=track.pk).update(duration_seconds=3.0)
        return track, requested

    def status(self, track, requested):
        response = self.client.get(f"/api/tracks/{track.pk}/refresh-waveform/status/?requested_ns={requested}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_a_started_refresh_is_never_reported_complete_until_it_is(self):
        track, requested = self.long_form()
        # The child would hold the Track's refresh lock while it works.
        fd = waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock")
        try:
            self.assertEqual(self.status(track, requested)["state"], "running")
        finally:
            os.close(fd)
        # It ended without recording anything (killed by a restart): uncertain, not success.
        unknown = self.status(track, requested)
        self.assertEqual((unknown["state"], unknown["status"]), ("unknown", None))
        self.assertEqual(self.client.get(f"/api/tracks/{track.pk}/refresh-waveform/status/").status_code, 400)

    def test_the_background_process_records_completion_failure_and_supersession(self):
        track, requested = self.long_form()
        operation = waveform_cache.try_operation_lock(self.waves, f"{track.pk}.refresh.lock")
        slot = waveform_cache.try_operation_lock(self.waves, wg.BACKGROUND_SLOT_LOCK)
        with patch("os.nice"):
            call_command("refresh_waveform", str(track.pk), "--background", "--operation-lock-fd", str(operation),
                         "--slot-lock-fd", str(slot), "--requested-ns", str(requested), stdout=StringIO())
        done = self.status(track, requested)
        self.assertEqual((done["state"], done["status"], done["new"]["trusted"]), ("completed", "replaced", True))
        self.assertTrue(SystemEvent.objects.filter(dedupe_key=wg.outcome_key(track.pk, requested)).exists())

        for status in ("superseded", "source_changed", "failed"):
            with self.subTest(status):
                token = requested + {"superseded": 1, "source_changed": 2, "failed": 3}[status]
                emit_event(category="library", level="warning", title="Waveform refresh did not complete",
                           dedupe_key=wg.outcome_key(track.pk, token),
                           detail={"status": status, "detail": "x", "requested_ns": token})
                outcome = self.status(track, token)
                self.assertEqual((outcome["state"], outcome["status"]), ("failed", status))

    def test_a_lost_outcome_record_falls_back_to_the_cache_this_refresh_promoted(self):
        track, requested = self.long_form()
        self.assertEqual(wg.refresh(track.pk).status, wg.REPLACED)    # promoted after the request, no event
        self.assertEqual(self.status(track, requested)["state"], "completed")
        self.assertEqual(self.status(track, 10 ** 19)["state"], "unknown")   # a request after that cache

    def test_the_ui_uses_only_the_waveform_only_endpoint_and_never_reanalyzes_as_a_fallback(self):
        source = (REPO / "library/templates/library/track_detail.html").read_text()
        refresh = source[source.index("async function refreshWaveformOnly"):source.index("async function pollRefresh")]
        self.assertIn("/refresh-waveform/", refresh)
        self.assertNotIn("reanalyze", refresh)
        click = source[source.index("function onWaveformClick"):source.index("async function loadWaveform")]
        self.assertNotIn("fetch(", click)                              # a click seeks/places; it never approves
