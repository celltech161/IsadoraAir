"""r0123 corrective A1: append-only cue-marker approval history (every retired approval survives
repeated transitions; no conflict, refusal or failure appends anything). Per-test temporary
directories only; no station data is touched."""
import json

from django.test import Client

from library.models import Track
from library.services import cue_markers
from library.tests.test_waveform_integrity_package_b import _Base, _row

HOOK = "hook_in_seconds"


def _state(track_id, name=HOOK):
    return _row(track_id)["cue_marker_state"].get(name) or {}


# ------------------------------------------------------------------ A1: approval history

class ApprovalHistoryTests(_Base):
    def edit(self, track, revision, **kwargs):
        result = cue_markers.operator_edit(track.pk, expected_revision=revision, user=self.user, **kwargs)
        self.assertTrue(result.ok, result.error or result.conflict)
        return result.revision

    def test_every_retired_approval_survives_repeated_overrides_unlock_and_reapproval(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0})                      # approve A
        rev = self.edit(track, rev, values={HOOK: 1.2})                    # override with B
        rev = self.edit(track, rev, values={HOOK: 1.4})                    # override with C
        rev = self.edit(track, rev, unlock=[HOOK])                         # unlock (retires C)
        rev = self.edit(track, rev, values={HOOK: 1.6})                    # approve D
        entry = _state(track.pk)
        history = entry["approval_history"]
        self.assertEqual([(h["value"], h["retired_reason"]) for h in history],
                         [(1.0, "replaced_by_operator"), (1.2, "replaced_by_operator"), (1.4, "unlocked")])
        self.assertEqual(entry["previous_approval"], history[-1])          # one history, never two
        self.assertEqual((entry["protection"], _row(track.pk)[HOOK]), ("locked", 1.6))
        for record in history:
            self.assertNotIn("approval_history", record)                   # flat, never recursive
            self.assertNotIn("previous_approval", record)
            self.assertEqual(record["approved_by"], self.user.pk)

    def test_consecutive_media_replacements_retire_each_approval_once(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0}, audio_revision="a" * 64)          # A
        cue_markers.apply_calculation(track.pk, {"next_start_seconds": 2.5}, basis_revision=rev,
                                      media_replaced=True, audio_revision="b" * 64)   # A -> review
        rev = _row(track.pk)["cue_marker_revision"]
        self.assertEqual(len(_state(track.pk)["approval_history"]), 1)
        rev = self.edit(track, rev, approve=[HOOK], audio_revision="b" * 64)          # B (no duplicate)
        self.assertEqual(len(_state(track.pk)["approval_history"]), 1)
        cue_markers.apply_calculation(track.pk, {HOOK: 0.9}, basis_revision=rev,
                                      media_replaced=True, audio_revision="c" * 64)   # B superseded
        history = _state(track.pk)["approval_history"]
        self.assertEqual([(h["value"], h["audio_revision"], h["retired_reason"]) for h in history],
                         [(1.0, "a" * 64, "new_media"), (1.0, "b" * 64, "new_media")])
        self.assertEqual(_state(track.pk)["protection"], "unlocked")

    def test_unknown_audio_stays_unknown_in_history(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0})                     # no trusted cache: audio unknown
        self.edit(track, rev, values={HOOK: 1.1})
        record = _state(track.pk)["approval_history"][0]
        self.assertIsNone(record["audio_revision"])                         # never fabricated

    def test_a_legacy_single_previous_approval_becomes_the_first_history_record(self):
        track = self.track()
        legacy_record = {"value": 0.5, "approved_by": self.user.pk, "approved_at": "2026-10-01T00:00:00+00:00",
                         "audio_revision": None, "retired_reason": "new_media", "source": "operator"}
        with cue_markers._authorized():
            Track.objects.filter(pk=track.pk).update(
                hook_in_seconds=1.0, cue_marker_revision=7,
                cue_marker_state={HOOK: {"protection": "locked", "source": "operator", "approved_by": self.user.pk,
                                         "approved_at": "2026-10-20T00:00:00+00:00", "audio_revision": None,
                                         "previous_approval": legacy_record}})
        self.assertEqual(cue_markers.approval_history(_state(track.pk)), [legacy_record])
        self.edit(track, 7, values={HOOK: 1.3})
        history = _state(track.pk)["approval_history"]
        self.assertEqual([h["value"] for h in history], [0.5, 1.0])         # legacy record kept, first
        self.assertEqual(history[0], legacy_record)

    def test_conflicts_refusals_and_failed_validation_append_nothing(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0})
        rev = self.edit(track, rev, values={HOOK: 1.2})
        before = _row(track.pk)
        for kwargs in ({"expected_revision": rev - 1, "values": {HOOK: 1.5}},           # stale revision
                       {"expected_revision": rev, "values": {HOOK: 1.5}, "confirm_protected": []},  # unconfirmed
                       {"expected_revision": rev, "values": {HOOK: 999.0}},            # past the end: invalid
                       {"expected_revision": rev, "values": {HOOK: 1.5, "hook_out_seconds": 1.4}}):  # ordering
            with self.subTest(kwargs=sorted(kwargs)):
                result = cue_markers.operator_edit(track.pk, user=self.user, **kwargs)
                self.assertFalse(result.ok)
                self.assertEqual(_row(track.pk), before)

    def test_metadata_only_saves_and_repeated_approval_leave_history_alone(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0})
        rev = self.edit(track, rev, values={HOOK: 1.2})
        before = _row(track.pk)
        client = Client()
        client.force_login(self.user)
        self.assertEqual(client.patch(f"/api/tracks/{track.pk}/", data=json.dumps({"title": "Renamed"}),
                                      content_type="application/json").status_code, 200)
        self.assertEqual(_row(track.pk)["cue_marker_state"], before["cue_marker_state"])
        again = cue_markers.operator_edit(track.pk, expected_revision=rev, user=self.user, approve=[HOOK])
        self.assertEqual((again.skipped, _row(track.pk)["cue_marker_state"]),
                         ({HOOK: "already approved and locked"}, before["cue_marker_state"]))

    def test_an_override_recalculation_never_nests_history(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0})
        rev = self.edit(track, rev, values={HOOK: 1.2})
        cue_markers.apply_calculation(track.pk, {HOOK: 0.8}, basis_revision=rev, requested_by=self.user,
                                      override=[HOOK], override_confirmed=True)
        entry = _state(track.pk)
        self.assertNotIn("approval_history", entry["overridden_from"])
        self.assertEqual([h["value"] for h in entry["approval_history"]], [1.0, 1.2])

    def test_the_api_returns_the_whole_history_and_the_latest_record(self):
        track = self.track()
        rev = self.edit(track, 0, values={HOOK: 1.0})
        rev = self.edit(track, rev, values={HOOK: 1.2})
        self.edit(track, rev, unlock=[HOOK])
        client = Client()
        client.force_login(self.user)
        info = client.get(f"/api/tracks/{track.pk}/cue-markers/").json()["cue_marker_review"][HOOK]
        self.assertEqual([h["value"] for h in info["approval_history"]], [1.0, 1.2])
        self.assertEqual(info["previous_approval"]["value"], 1.2)
        self.assertEqual({h["approved_by_name"] for h in info["approval_history"]}, {self.user.username})
        self.assertEqual(info["display"], "unlocked_operator")
