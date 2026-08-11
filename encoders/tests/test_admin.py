"""EncoderAdmin Phase 3A tests: the admin no longer dispatches
`systemctl restart isadoraair-encoders` (or anything else privileged)
for a routine Encoder edit -- it commits the desired configuration and
shows an informational message; the running EncoderManager discovers
the change itself on its own reconciliation cadence (see
encoders/services/encoder_manager.py's _reconcile). This file verifies:

  * the old restart-dispatch surface (RESTART_COMMAND,
    dispatch_encoder_restart, _run_predispatch_preflight,
    _restart_encoders_and_report, mark_encoder_restart_needed) is
    genuinely gone, not just unused;
  * admin.py imports no subprocess/sudo-adjacent machinery at all;
  * a save always commits the desired configuration regardless of
    whether it's runtime-affecting;
  * the "will reconcile automatically" info message appears exactly
    under the same runtime-affecting-field-and-enabled conditions the
    old restart dispatch used to, coalesced once per logical admin
    operation (not once per row in a bulk edit);
  * _describe_group_status's desired-vs-running-vs-accepted banner
    (Phase 2M, extended for Phase 3O's reconcile_status/last_reconcile_
    * fields).

Group-level duplicate-destination / cross-group-collision rejection is
no longer something this admin can pre-check synchronously (that
required a whole-service restart-dispatch gate that no longer exists)
-- it's now caught by the encoder manager's own reconciliation
(encoders/services/encoder_manager.py's _reconcile_changed_group /
_static_check_candidate), covered in encoders/tests/
test_encoder_manager.py's reconciliation test suite, not here.
"""
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from encoders import admin as encoders_admin
from encoders.admin import RUNTIME_AFFECTING_FIELDS, EncoderAdmin
from encoders.models import Encoder
from encoders.services import encoder_manager as em
from encoders.services import lkg as lkg_module

DEFAULT_FIELDS = dict(
    name="test-encoder", enabled=True, protocol="shoutcast2",
    host="192.168.1.112", port=8000, mount="/4", username="source",
    password="secret", format="mp3", bitrate_kbps=192, input_device="",
    # station_name non-blank: Phase 2 hardening's centralized validation
    # (encoders/services/validation.py) now enforces this for Shoutcast
    # rows -- matches the real, already-documented production incident
    # in encoder_manager.py's own module docstring (SC2's validator
    # rejecting an empty icy-name header). These tests are about admin
    # save/notification behavior, not station_name policy, so the
    # fixture just needs to be a VALID row.
    station_name="Test Station", genre="", description="", url="", public=False,
    sort_order=0,
)
FORM_FIELDS = [
    "name", "protocol", "sort_order", "host", "port", "mount", "username",
    "password", "format", "bitrate_kbps", "input_device", "station_name",
    "genre", "description", "url",
]
BOOL_FIELDS = ["enabled", "public"]

RECONCILE_MESSAGE_SNIPPET = b"encoder manager will reconcile"


def make_encoder(**overrides):
    values = dict(DEFAULT_FIELDS)
    values.update(overrides)
    return Encoder.objects.create(**values)


def _build_post_data(values):
    data = {f: values[f] for f in FORM_FIELDS}
    for f in BOOL_FIELDS:
        if values[f]:
            data[f] = "on"
    data["_save"] = "Save"
    return data


def encoder_post_data(**overrides):
    """POST body for the add form (or a from-scratch change form)."""
    values = dict(DEFAULT_FIELDS)
    values.update(overrides)
    return _build_post_data(values)


def encoder_post_data_from(obj, **overrides):
    """POST body that resubmits `obj`'s current values, with `overrides`
    applied -- i.e. a change-form submission."""
    values = {f: getattr(obj, f) for f in FORM_FIELDS + BOOL_FIELDS}
    values.update(overrides)
    return _build_post_data(values)


def changelist_formset_data(objs, row_overrides=None):
    """POST body for a list_editable changelist bulk-save. row_overrides
    is {obj.pk: {"enabled": bool, "sort_order": int}}."""
    row_overrides = row_overrides or {}
    data = {
        "form-TOTAL_FORMS": str(len(objs)),
        "form-INITIAL_FORMS": str(len(objs)),
        "form-MIN_NUM_FORMS": "0",
        "form-MAX_NUM_FORMS": "1000",
        "_save": "Save",
    }
    for i, obj in enumerate(objs):
        overrides = row_overrides.get(obj.pk, {})
        enabled = overrides.get("enabled", obj.enabled)
        sort_order = overrides.get("sort_order", obj.sort_order)
        data[f"form-{i}-id"] = str(obj.pk)
        if enabled:
            data[f"form-{i}-enabled"] = "on"
        data[f"form-{i}-sort_order"] = str(sort_order)
    return data


# ---------------------------------------------------------------------
# The old restart-dispatch surface is genuinely gone, not just unused.
# ---------------------------------------------------------------------
class NoPrivilegedDispatchTests(TestCase):
    def test_restart_dispatch_functions_no_longer_exist(self):
        for name in (
            "RESTART_COMMAND", "dispatch_encoder_restart", "_run_predispatch_preflight",
            "_restart_encoders_and_report", "mark_encoder_restart_needed",
        ):
            self.assertFalse(hasattr(encoders_admin, name), f"encoders.admin.{name} still exists")

    def test_admin_module_imports_no_subprocess_or_transaction(self):
        # subprocess: nothing in this module ever shells out anymore.
        # transaction: nothing here defers to on_commit() anymore --
        # there's no dispatch left to race the DB write becoming
        # visible; the manager only ever reads fully-committed state on
        # its own independent schedule.
        self.assertFalse(hasattr(encoders_admin, "subprocess"))
        self.assertFalse(hasattr(encoders_admin, "transaction"))


# ---------------------------------------------------------------------
# Admin save behavior: always commits; shows the reconciliation-pending
# info message under exactly the same runtime-affecting-field-and-
# enabled conditions the old restart dispatch used to gate on.
# ---------------------------------------------------------------------
@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class EncoderAdminSaveTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("admin", "admin@example.invalid", "password")
        self.client.force_login(self.staff)

    def _add_url(self):
        return reverse("admin:encoders_encoder_add")

    def _change_url(self, obj):
        return reverse("admin:encoders_encoder_change", args=[obj.pk])

    def _delete_url(self, obj):
        return reverse("admin:encoders_encoder_delete", args=[obj.pk])

    def _changelist_url(self):
        return reverse("admin:encoders_encoder_changelist")

    def _post(self, url, data, **kwargs):
        return self.client.post(url, data, follow=True, **kwargs)

    def test_add_enabled_encoder_commits_and_notifies(self):
        response = self._post(self._add_url(), encoder_post_data(name="new-encoder"))
        self.assertTrue(Encoder.objects.filter(name="new-encoder").exists())
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_changing_runtime_affecting_field_notifies(self):
        obj = make_encoder()
        response = self._post(self._change_url(obj), encoder_post_data_from(obj, host="192.168.1.200"))
        obj.refresh_from_db()
        self.assertEqual(obj.host, "192.168.1.200")
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_disabling_encoder_notifies(self):
        obj = make_encoder(enabled=True)
        response = self._post(self._change_url(obj), encoder_post_data_from(obj, enabled=False))
        obj.refresh_from_db()
        self.assertFalse(obj.enabled)
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_sort_order_only_change_commits_no_notification(self):
        obj = make_encoder(sort_order=0)
        response = self._post(self._change_url(obj), encoder_post_data_from(obj, sort_order=5))
        obj.refresh_from_db()
        self.assertEqual(obj.sort_order, 5)
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_no_effective_change_no_notification(self):
        obj = make_encoder()
        response = self._post(self._change_url(obj), encoder_post_data_from(obj))
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_deleting_one_encoder_notifies(self):
        obj = make_encoder()
        response = self._post(self._delete_url(obj), {"post": "yes"})
        self.assertFalse(Encoder.objects.filter(pk=obj.pk).exists())
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_bulk_delete_multiple_encoders_notifies_once(self):
        a = make_encoder(name="a")
        b = make_encoder(name="b")
        data = {"action": "delete_selected", "_selected_action": [a.pk, b.pk], "post": "yes"}
        response = self._post(self._changelist_url(), data)
        self.assertEqual(Encoder.objects.count(), 0)
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_editing_multiple_runtime_affecting_rows_notifies_once(self):
        """Coalescing: a changelist bulk-save calls save_model() once
        per changed row -- must still show the reconciliation-pending
        message exactly once for the whole logical operation, not once
        per row."""
        a = make_encoder(name="a", enabled=True)
        b = make_encoder(name="b", enabled=True)
        data = changelist_formset_data([a, b], {a.pk: {"enabled": False}, b.pk: {"enabled": False}})
        response = self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.enabled)
        self.assertFalse(b.enabled)
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_editing_only_sort_order_in_changelist_no_notification(self):
        a = make_encoder(name="a", sort_order=0)
        b = make_encoder(name="b", sort_order=1)
        data = changelist_formset_data([a, b], {a.pk: {"sort_order": 5}, b.pk: {"sort_order": 6}})
        response = self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.sort_order, 5)
        self.assertEqual(b.sort_order, 6)
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_mixed_changelist_update_notifies_once(self):
        a = make_encoder(name="a", sort_order=0, enabled=True)
        b = make_encoder(name="b", sort_order=1, enabled=True)
        data = changelist_formset_data([a, b], {a.pk: {"sort_order": 9}, b.pk: {"enabled": False}})
        response = self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.sort_order, 9)
        self.assertFalse(b.enabled)
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    # --- Enabled-state gating (unchanged invariant from Phase 2) -------
    # A disabled row was never part of the running Liquidsoap topology,
    # so an operation that only ever touches a disabled row -- add,
    # delete, or an edit that leaves it disabled -- must not notify,
    # even if a runtime-affecting field is involved.

    def test_adding_disabled_encoder_no_notification(self):
        response = self._post(self._add_url(), encoder_post_data(name="new-disabled-encoder", enabled=False))
        self.assertTrue(Encoder.objects.filter(name="new-disabled-encoder", enabled=False).exists())
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_deleting_disabled_encoder_no_notification(self):
        obj = make_encoder(enabled=False)
        response = self._post(self._delete_url(obj), {"post": "yes"})
        self.assertFalse(Encoder.objects.filter(pk=obj.pk).exists())
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_editing_runtime_fields_on_disabled_encoder_remains_disabled_no_notification(self):
        obj = make_encoder(enabled=False, host="192.168.1.112", port=8000, bitrate_kbps=192)
        data = encoder_post_data_from(obj, host="192.168.1.250", port=8010, bitrate_kbps=256)
        response = self._post(self._change_url(obj), data)
        obj.refresh_from_db()
        self.assertEqual(obj.host, "192.168.1.250")
        self.assertEqual(obj.port, 8010)
        self.assertEqual(obj.bitrate_kbps, 256)
        self.assertFalse(obj.enabled)
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_changing_only_description_no_notification(self):
        # description was removed from RUNTIME_AFFECTING_FIELDS --
        # encoder_manager.py doesn't read it, so it can't affect the
        # rendered script (see admin.py's RUNTIME_AFFECTING_FIELDS comment).
        obj = make_encoder(enabled=True, description="old description")
        response = self._post(self._change_url(obj), encoder_post_data_from(obj, description="new description"))
        obj.refresh_from_db()
        self.assertEqual(obj.description, "new description")
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_enabling_disabled_encoder_notifies(self):
        obj = make_encoder(enabled=False)
        response = self._post(self._change_url(obj), encoder_post_data_from(obj, enabled=True))
        obj.refresh_from_db()
        self.assertTrue(obj.enabled)
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)

    def test_bulk_delete_only_disabled_encoders_no_notification(self):
        a = make_encoder(name="a", enabled=False)
        b = make_encoder(name="b", enabled=False)
        data = {"action": "delete_selected", "_selected_action": [a.pk, b.pk], "post": "yes"}
        response = self._post(self._changelist_url(), data)
        self.assertEqual(Encoder.objects.count(), 0)
        self.assertNotIn(RECONCILE_MESSAGE_SNIPPET, response.content)

    def test_bulk_delete_mixture_with_one_enabled_notifies_once(self):
        a = make_encoder(name="a", enabled=False)
        b = make_encoder(name="b", enabled=True)
        c = make_encoder(name="c", enabled=False)
        data = {"action": "delete_selected", "_selected_action": [a.pk, b.pk, c.pk], "post": "yes"}
        response = self._post(self._changelist_url(), data)
        self.assertEqual(Encoder.objects.count(), 0)
        self.assertEqual(response.content.count(RECONCILE_MESSAGE_SNIPPET), 1)


# ---------------------------------------------------------------------
# Phase 2M, extended for Phase 3O: desired (database) vs running vs
# accepted (last-known-good) surfacing. encoders/admin.py.
# _describe_group_status reads two on-disk artifacts the encoder
# manager (a separate systemd-managed process) writes -- the
# group-state JSON and the LKG metadata sidecar -- rather than any
# in-process link, since gunicorn (this admin) and isadoraair-encoders
# never share memory. Patches STATE_DIR/CANDIDATE_DIR/LKG_DIR to temp
# dirs so nothing here touches real /run or /var/lib paths.
# ---------------------------------------------------------------------
class GroupStatusFixtureMixin:
    def setUp(self):
        super().setUp()
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        base = Path(tmpdir.name)
        for patcher in (
            patch.object(em, "STATE_DIR", base / "run"),
            patch.object(lkg_module, "CANDIDATE_DIR", base / "candidate"),
            patch.object(lkg_module, "LKG_DIR", base / "lkg"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_group_state(self, input_device, **fields):
        """Writes a group-state JSON matching what EncoderManager.
        _write_group_state would (only the fields _describe_group_status
        actually reads are required, defaulted to the "healthy, nothing
        to report" shape)."""
        path = em._group_state_path_for_slug(em._slug(input_device))
        path.parent.mkdir(parents=True, exist_ok=True)
        state = {"input_device": input_device, "launch_kind": "accepted", "critical_stopped": False}
        state.update(fields)
        path.write_text(json.dumps(state), encoding="utf-8")


class DescribeGroupStatusTests(GroupStatusFixtureMixin, TestCase):
    def test_no_lkg_reports_bootstrap_info(self):
        obj = make_encoder(input_device="airtap")
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "info")
        self.assertIn("no last-known-good", status["message"])

    def test_matching_fingerprint_reports_nothing(self):
        obj = make_encoder(input_device="airtap")
        fp = lkg_module.compute_fingerprint("airtap", [obj])
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": fp})
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertIsNone(status["level"])

    def test_mismatched_fingerprint_reports_warning(self):
        obj = make_encoder(input_device="airtap")
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": "different-fp"})
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "warning")
        self.assertIn("does not match", status["message"])
        self.assertIn("automatically", status["message"])

    def test_mismatched_fingerprint_with_sticky_rejection_reports_error(self):
        """Phase 3O: a mismatch caused by a sticky rejection (won't be
        auto-retried) is a materially different situation from "still
        being reconciled" -- the message must say so distinctly."""
        obj = make_encoder(input_device="airtap")
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": "different-fp"})
        self.write_group_state(
            "airtap", last_reconcile_result="static_validation_rejected",
            last_reconcile_error="Host is required.",
        )
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "error")
        self.assertIn("rejected", status["message"])
        self.assertIn("Host is required.", status["message"])

    def test_mismatched_fingerprint_with_cross_group_collision_reports_warning(self):
        obj = make_encoder(input_device="airtap")
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": "different-fp"})
        self.write_group_state(
            "airtap", last_reconcile_result="blocked_cross_group_collision",
            last_reconcile_error="('icecast', '1.2.3.4', 8000, '/x') already in use by group 'other' ('e')",
        )
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "warning")
        self.assertIn("already in use by another encoder group", status["message"])

    def test_candidate_launch_kind_reports_probation_warning(self):
        obj = make_encoder(input_device="airtap")
        self.write_group_state("airtap", launch_kind="candidate")
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "warning")
        self.assertIn("PROBATION", status["message"])

    def test_rollback_launch_kind_reports_warning(self):
        obj = make_encoder(input_device="airtap")
        self.write_group_state("airtap", launch_kind="rollback")
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "warning")
        self.assertIn("rolled back", status["message"])

    def test_critical_stopped_reports_error_even_when_launch_kind_accepted(self):
        obj = make_encoder(input_device="airtap")
        self.write_group_state("airtap", launch_kind="accepted", critical_stopped=True)
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "error")
        self.assertIn("STOPPED", status["message"])

    def test_critical_stopped_takes_priority_over_candidate_kind(self):
        """Defensive: critical_stopped should never leave launch_kind
        as "candidate"/"rollback" in practice (_on_rollback_failed
        always resets it to "accepted" first) -- but the priority order
        itself is still worth pinning down explicitly."""
        obj = make_encoder(input_device="airtap")
        self.write_group_state("airtap", launch_kind="candidate", critical_stopped=True)
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertEqual(status["level"], "error")

    def test_corrupt_group_state_file_falls_back_to_accepted_not_a_crash(self):
        obj = make_encoder(input_device="airtap")
        fp = lkg_module.compute_fingerprint("airtap", [obj])
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": fp})
        path = em._group_state_path_for_slug(em._slug("airtap"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertIsNone(status["level"])  # falls back to "accepted", matches -> nothing to report

    def test_missing_group_state_file_falls_back_to_accepted(self):
        """Fresh install / group has never run yet -- no group-state
        file exists at all. Must not be mistaken for candidate/
        rollback/critical-stopped."""
        obj = make_encoder(input_device="airtap")
        fp = lkg_module.compute_fingerprint("airtap", [obj])
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": fp})
        status = encoders_admin._describe_group_status(em._slug("airtap"), "airtap", [obj])
        self.assertIsNone(status["level"])


@override_settings(SECURE_SSL_REDIRECT=False)
class ChangelistGroupStatusMessageTests(GroupStatusFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser("admin-groupstatus", "admin-gs@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_mismatched_group_shows_warning_banner_on_changelist(self):
        make_encoder(input_device="airtap", enabled=True)
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": "some-other-fp"})
        response = self.client.get(reverse("admin:encoders_encoder_changelist"))
        self.assertContains(response, "does not match")

    def test_matching_group_shows_no_banner(self):
        obj = make_encoder(input_device="airtap", enabled=True)
        fp = lkg_module.compute_fingerprint("airtap", [obj])
        lkg_module.write_lkg(em._slug("airtap"), 'generation = "g"\n', {"fingerprint": fp})
        response = self.client.get(reverse("admin:encoders_encoder_changelist"))
        self.assertNotContains(response, "does not match")

    def test_disabled_only_group_produces_no_banner(self):
        make_encoder(input_device="airtap", enabled=False)
        response = self.client.get(reverse("admin:encoders_encoder_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "last-known-good")

    def test_no_encoders_at_all_does_not_crash_changelist(self):
        response = self.client.get(reverse("admin:encoders_encoder_changelist"))
        self.assertEqual(response.status_code, 200)

    def test_critical_stopped_shows_error_banner(self):
        make_encoder(input_device="airtap", enabled=True)
        self.write_group_state("airtap", launch_kind="accepted", critical_stopped=True)
        response = self.client.get(reverse("admin:encoders_encoder_changelist"))
        self.assertContains(response, "STOPPED")


# ---------------------------------------------------------------------
# JS confirmation: static-content regression guard. No browser-testing
# framework exists in this project (Python/Django tests only) -- per
# the task's own allowance, this checks the file's content directly
# rather than building one for this change.
# ---------------------------------------------------------------------
class EncoderConfirmJsTests(TestCase):
    def setUp(self):
        js_path = Path(__file__).resolve().parent.parent / "static" / "encoders" / "js" / "encoder_confirm.js"
        self.js_source = js_path.read_text()

    def test_js_runtime_fields_list_matches_server_set(self):
        match = re.search(r"RUNTIME_AFFECTING_FIELDS\s*=\s*\[(.*?)\]", self.js_source, re.S)
        self.assertIsNotNone(match, "RUNTIME_AFFECTING_FIELDS array not found in encoder_confirm.js")
        js_fields = set(re.findall(r"'([a-z_]+)'", match.group(1)))
        self.assertEqual(js_fields, RUNTIME_AFFECTING_FIELDS)

    def test_js_no_longer_unconditionally_claims_every_save_restarts(self):
        self.assertNotIn("Saving this will restart the IsadoraAir encoders service, ", self.js_source)
        self.assertNotIn("briefly dropping all active streams", self.js_source)

    def test_js_describes_per_group_reconciliation_not_whole_service_restart(self):
        self.assertIn("encoder manager", self.js_source)
        self.assertIn("not affected", self.js_source)

    def test_encoder_admin_still_includes_confirm_js(self):
        self.assertIn("encoders/js/encoder_confirm.js", EncoderAdmin.Media.js)
