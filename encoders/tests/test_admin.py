"""EncoderAdmin restart-hardening tests (Encoder hardening Item #1):
transaction-safe, coalesced (one restart per logical admin operation),
limited to runtime-affecting field changes, and observable on
immediate dispatch failure. See encoders/admin.py's module docstring-
level comments for the design this verifies.

dispatch_encoder_restart (or, at the subprocess.Popen layer, for the
dispatch-behavior tests) is patched in every test -- nothing here ever
touches real sudo/systemctl.

Uses TestCase.captureOnCommitCallbacks() throughout: Django's TestCase
wraps each test in a transaction that is rolled back at the end, so
transaction.on_commit() callbacks registered by the admin (or directly,
in the coalescing unit tests) do not fire on their own without it.
"""
import json
import re
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from encoders import admin as encoders_admin
from encoders.admin import RUNTIME_AFFECTING_FIELDS, EncoderAdmin, dispatch_encoder_restart, mark_encoder_restart_needed
from encoders.models import Encoder
from encoders.services import encoder_manager as em
from encoders.services import lkg as lkg_module
from encoders.services import preflight as preflight_module

DEFAULT_FIELDS = dict(
    name="test-encoder", enabled=True, protocol="shoutcast2",
    host="192.168.1.112", port=8000, mount="/4", username="source",
    password="secret", format="mp3", bitrate_kbps=192, input_device="",
    # station_name non-blank: Phase 2 hardening's centralized validation
    # (encoders/services/validation.py) now enforces this for Shoutcast
    # rows -- matches the real, already-documented production incident
    # in encoder_manager.py's own module docstring (SC2's validator
    # rejecting an empty icy-name header). These tests are about admin
    # restart-dispatch behavior, not station_name policy, so the
    # fixture just needs to be a VALID row, same as test_encoder_manager
    # .py's own make_encoder() already supplies.
    station_name="Test Station", genre="", description="", url="", public=False,
    sort_order=0,
)
FORM_FIELDS = [
    "name", "protocol", "sort_order", "host", "port", "mount", "username",
    "password", "format", "bitrate_kbps", "input_device", "station_name",
    "genre", "description", "url",
]
BOOL_FIELDS = ["enabled", "public"]


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
# 1-3: transaction behavior (direct, no HTTP round trip needed)
# ---------------------------------------------------------------------
class CoalescingTransactionTests(TestCase):
    def test_restart_registered_does_not_execute_before_commit(self):
        request = HttpRequest()
        with patch.object(encoders_admin, "dispatch_encoder_restart") as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    mark_encoder_restart_needed(request)
                    mock_dispatch.assert_not_called()

    def test_restart_executes_after_successful_commit(self):
        request = HttpRequest()
        with patch.object(encoders_admin, "dispatch_encoder_restart") as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    mark_encoder_restart_needed(request)
            mock_dispatch.assert_called_once()

    def test_rolled_back_transaction_causes_no_restart(self):
        request = HttpRequest()
        with patch.object(encoders_admin, "dispatch_encoder_restart") as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                try:
                    with transaction.atomic():
                        mark_encoder_restart_needed(request)
                        raise RuntimeError("force rollback")
                except RuntimeError:
                    pass
            mock_dispatch.assert_not_called()

    def test_repeated_registration_within_one_request_coalesces(self):
        """Directly exercises the coalescing guard itself: calling
        mark_encoder_restart_needed() more than once on the same request
        must not register more than one on_commit callback -- registering
        the same callback repeatedly is not automatically deduplicated by
        transaction.on_commit() itself, so this is the behavior the
        request-attribute guard exists to provide."""
        request = HttpRequest()
        with patch.object(encoders_admin, "dispatch_encoder_restart") as mock_dispatch:
            with self.captureOnCommitCallbacks(execute=True):
                with transaction.atomic():
                    mark_encoder_restart_needed(request)
                    mark_encoder_restart_needed(request)
                    mark_encoder_restart_needed(request)
            mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------
# 4-13: admin HTTP behavior -- field changes, delete, changelist bulk edit
# ---------------------------------------------------------------------
@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class EncoderAdminHttpRestartTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("admin", "admin@example.invalid", "password")
        self.client.force_login(self.staff)
        # These tests are about restart-dispatch coalescing, not the
        # pre-dispatch preflight gate itself (see PredispatchPreflight*
        # test classes below) -- without this, every test here that
        # actually saves an enabled Encoder row would hit the REAL
        # /run and /var/lib paths and spawn a real `liquidsoap --check`
        # subprocess as an unintended side effect of the new gate.
        patcher = patch.object(encoders_admin, "_run_predispatch_preflight", return_value=(True, []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _add_url(self):
        return reverse("admin:encoders_encoder_add")

    def _change_url(self, obj):
        return reverse("admin:encoders_encoder_change", args=[obj.pk])

    def _delete_url(self, obj):
        return reverse("admin:encoders_encoder_delete", args=[obj.pk])

    def _changelist_url(self):
        return reverse("admin:encoders_encoder_changelist")

    def _post(self, url, data, **kwargs):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, data, **kwargs)

    # 4. Adding an enabled encoder requests one restart.
    def test_add_enabled_encoder_requests_one_restart(self):
        data = encoder_post_data(name="new-encoder")
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            response = self._post(self._add_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Encoder.objects.filter(name="new-encoder").exists())
        mock_dispatch.assert_called_once()

    # 5. Changing a runtime-affecting field requests one restart.
    def test_changing_runtime_affecting_field_requests_one_restart(self):
        obj = make_encoder()
        data = encoder_post_data_from(obj, host="192.168.1.200")
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            response = self._post(self._change_url(obj), data)
        self.assertEqual(response.status_code, 302)
        obj.refresh_from_db()
        self.assertEqual(obj.host, "192.168.1.200")
        mock_dispatch.assert_called_once()

    # 6. Enabling or disabling an encoder requests one restart.
    def test_disabling_encoder_requests_one_restart(self):
        obj = make_encoder(enabled=True)
        data = encoder_post_data_from(obj, enabled=False)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._change_url(obj), data)
        obj.refresh_from_db()
        self.assertFalse(obj.enabled)
        mock_dispatch.assert_called_once()

    # 7. Changing only sort_order requests no restart.
    def test_sort_order_only_change_requests_no_restart(self):
        obj = make_encoder(sort_order=0)
        data = encoder_post_data_from(obj, sort_order=5)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._change_url(obj), data)
        obj.refresh_from_db()
        self.assertEqual(obj.sort_order, 5)
        mock_dispatch.assert_not_called()

    # 8. Saving with no effective change requests no restart.
    def test_no_effective_change_requests_no_restart(self):
        obj = make_encoder()
        data = encoder_post_data_from(obj)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._change_url(obj), data)
        mock_dispatch.assert_not_called()

    # 9. Deleting one encoder requests one restart after commit.
    def test_deleting_one_encoder_requests_one_restart(self):
        obj = make_encoder()
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._delete_url(obj), {"post": "yes"})
        self.assertFalse(Encoder.objects.filter(pk=obj.pk).exists())
        mock_dispatch.assert_called_once()

    # 10. Bulk deleting multiple encoders requests exactly one restart.
    def test_bulk_delete_multiple_encoders_requests_one_restart(self):
        a = make_encoder(name="a")
        b = make_encoder(name="b")
        data = {"action": "delete_selected", "_selected_action": [a.pk, b.pk], "post": "yes"}
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        self.assertEqual(Encoder.objects.count(), 0)
        mock_dispatch.assert_called_once()

    # 11. Editing multiple runtime-affecting rows in one changelist
    # request requests exactly one restart.
    def test_editing_multiple_runtime_affecting_rows_requests_one_restart(self):
        a = make_encoder(name="a", enabled=True)
        b = make_encoder(name="b", enabled=True)
        data = changelist_formset_data([a, b], {a.pk: {"enabled": False}, b.pk: {"enabled": False}})
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertFalse(a.enabled)
        self.assertFalse(b.enabled)
        mock_dispatch.assert_called_once()

    # 12. Editing only sort_order values in one changelist request
    # requests no restart.
    def test_editing_only_sort_order_in_changelist_requests_no_restart(self):
        a = make_encoder(name="a", sort_order=0)
        b = make_encoder(name="b", sort_order=1)
        data = changelist_formset_data([a, b], {a.pk: {"sort_order": 5}, b.pk: {"sort_order": 6}})
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.sort_order, 5)
        self.assertEqual(b.sort_order, 6)
        mock_dispatch.assert_not_called()

    # 13. A mixed changelist update (sort_order + at least one
    # runtime-affecting change) requests exactly one restart.
    def test_mixed_changelist_update_requests_one_restart(self):
        a = make_encoder(name="a", sort_order=0, enabled=True)
        b = make_encoder(name="b", sort_order=1, enabled=True)
        data = changelist_formset_data([a, b], {a.pk: {"sort_order": 9}, b.pk: {"enabled": False}})
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.sort_order, 9)
        self.assertFalse(b.enabled)
        mock_dispatch.assert_called_once()

    # --- Enabled-state gating (review round 2) -------------------------
    # A disabled row was never part of the running Liquidsoap topology,
    # so an operation that only ever touches a disabled row -- add,
    # delete, or an edit that leaves it disabled -- must not restart
    # anything, even if a runtime-affecting field is involved.

    def test_adding_disabled_encoder_requests_no_restart(self):
        data = encoder_post_data(name="new-disabled-encoder", enabled=False)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            response = self._post(self._add_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Encoder.objects.filter(name="new-disabled-encoder", enabled=False).exists())
        mock_dispatch.assert_not_called()

    def test_deleting_disabled_encoder_requests_no_restart(self):
        obj = make_encoder(enabled=False)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._delete_url(obj), {"post": "yes"})
        self.assertFalse(Encoder.objects.filter(pk=obj.pk).exists())
        mock_dispatch.assert_not_called()

    def test_editing_runtime_fields_on_disabled_encoder_remains_disabled_requests_no_restart(self):
        obj = make_encoder(enabled=False, host="192.168.1.112", port=8000, bitrate_kbps=192)
        data = encoder_post_data_from(obj, host="192.168.1.250", port=8010, bitrate_kbps=256)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._change_url(obj), data)
        obj.refresh_from_db()
        self.assertEqual(obj.host, "192.168.1.250")
        self.assertEqual(obj.port, 8010)
        self.assertEqual(obj.bitrate_kbps, 256)
        self.assertFalse(obj.enabled)
        mock_dispatch.assert_not_called()

    def test_changing_only_description_requests_no_restart(self):
        # description was removed from RUNTIME_AFFECTING_FIELDS --
        # encoder_manager.py doesn't read it, so it can't affect the
        # running script (see admin.py's RUNTIME_AFFECTING_FIELDS comment).
        obj = make_encoder(enabled=True, description="old description")
        data = encoder_post_data_from(obj, description="new description")
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._change_url(obj), data)
        obj.refresh_from_db()
        self.assertEqual(obj.description, "new description")
        mock_dispatch.assert_not_called()

    def test_enabling_disabled_encoder_requests_one_restart(self):
        obj = make_encoder(enabled=False)
        data = encoder_post_data_from(obj, enabled=True)
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._change_url(obj), data)
        obj.refresh_from_db()
        self.assertTrue(obj.enabled)
        mock_dispatch.assert_called_once()

    def test_bulk_delete_only_disabled_encoders_requests_no_restart(self):
        a = make_encoder(name="a", enabled=False)
        b = make_encoder(name="b", enabled=False)
        data = {"action": "delete_selected", "_selected_action": [a.pk, b.pk], "post": "yes"}
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        self.assertEqual(Encoder.objects.count(), 0)
        mock_dispatch.assert_not_called()

    def test_bulk_delete_mixture_with_one_enabled_requests_one_restart(self):
        a = make_encoder(name="a", enabled=False)
        b = make_encoder(name="b", enabled=True)
        c = make_encoder(name="c", enabled=False)
        data = {"action": "delete_selected", "_selected_action": [a.pk, b.pk, c.pk], "post": "yes"}
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        self.assertEqual(Encoder.objects.count(), 0)
        mock_dispatch.assert_called_once()

    def test_changelist_edit_of_disabled_rows_only_requests_no_restart(self):
        a = make_encoder(name="a", enabled=False, sort_order=0)
        b = make_encoder(name="b", enabled=False, sort_order=1)
        # sort_order changes; each row's "enabled" is resubmitted
        # unchanged (still False) -- no runtime-affecting delta at all.
        data = changelist_formset_data([a, b], {a.pk: {"sort_order": 5}, b.pk: {"sort_order": 6}})
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.sort_order, 5)
        self.assertEqual(b.sort_order, 6)
        self.assertFalse(a.enabled)
        self.assertFalse(b.enabled)
        mock_dispatch.assert_not_called()

    def test_mixed_changelist_disabled_and_active_row_change_requests_one_restart(self):
        a = make_encoder(name="a", enabled=False, sort_order=0)
        b = make_encoder(name="b", enabled=True, sort_order=1)
        # a: stays disabled, only sort_order changes -- no restart on its own.
        # b: enabled -> disabled -- a genuine runtime change on a
        # previously-enabled row, so exactly one restart overall.
        data = changelist_formset_data([a, b], {a.pk: {"sort_order": 9}, b.pk: {"enabled": False}})
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
            self._post(self._changelist_url(), data)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.sort_order, 9)
        self.assertFalse(b.enabled)
        mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------
# 14-15, 17: dispatch helper behavior (pure function, no admin/HTTP)
# ---------------------------------------------------------------------
class DispatchEncoderRestartTests(TestCase):
    # 14. Invokes exactly the restart command, no shell=True.
    def test_invokes_exact_restart_command_without_shell(self):
        with patch.object(encoders_admin.subprocess, "Popen") as mock_popen:
            result = dispatch_encoder_restart()
        mock_popen.assert_called_once_with(["sudo", "systemctl", "restart", "isadoraair-encoders"])
        self.assertNotIn("shell", mock_popen.call_args.kwargs)
        self.assertTrue(result)

    # 15. An immediate Popen() failure is caught, not propagated.
    def test_popen_oserror_is_caught_not_raised(self):
        with patch.object(encoders_admin.subprocess, "Popen", side_effect=OSError("no such file")):
            result = dispatch_encoder_restart()  # must not raise
        self.assertFalse(result)

    # 17. No password or other encoder credentials appear in the
    # error/event detail.
    def test_dispatch_failure_event_detail_has_no_credentials(self):
        with patch.object(encoders_admin.subprocess, "Popen", side_effect=OSError("boom")):
            with patch.object(encoders_admin, "emit_event") as mock_emit:
                dispatch_encoder_restart()
        mock_emit.assert_called_once()
        detail = mock_emit.call_args.kwargs["detail"]
        self.assertEqual(detail, {"error": "boom"})


# ---------------------------------------------------------------------
# 16: immediate dispatch failure -> admin-visible error.
#
# Deliberately a TransactionTestCase, not TestCase+captureOnCommitCallbacks:
# captureOnCommitCallbacks only runs queued on_commit callbacks at its
# `with` block's __exit__, which is AFTER self.client.post() has already
# built and returned its response -- messages.error() added that late
# can't land in that response's rendered HTML even though, in real
# production request handling, the admin's transaction.atomic() commits
# (firing on_commit synchronously) before response_change() builds the
# response at all. TransactionTestCase performs a real commit during
# request handling, matching production ordering, so this is the correct
# tool for this specific assertion.
# ---------------------------------------------------------------------
@override_settings(SECURE_SSL_REDIRECT=False)
class EncoderAdminDispatchFailureMessageTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("admin", "admin@example.invalid", "password")
        self.client.force_login(self.staff)
        patcher = patch.object(encoders_admin, "_run_predispatch_preflight", return_value=(True, []))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dispatch_failure_shows_admin_visible_error_and_keeps_db_change(self):
        obj = make_encoder()
        data = encoder_post_data_from(obj, host="192.168.1.201")
        with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=False):
            response = self.client.post(
                reverse("admin:encoders_encoder_change", args=[obj.pk]), data, follow=True,
            )
        self.assertContains(response, "unable to launch")
        obj.refresh_from_db()
        self.assertEqual(obj.host, "192.168.1.201")


# ---------------------------------------------------------------------
# Phase 2M: desired (database) vs accepted (last-known-good) surfacing.
# encoders/admin.py._describe_group_status reads two on-disk artifacts
# the encoder manager (a separate systemd-managed process) writes --
# the group-state JSON and the LKG metadata sidecar -- rather than any
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
# Phase 2 review fix #1: the admin used to dispatch `systemctl restart
# isadoraair-encoders` unconditionally on any runtime-affecting save --
# meaning a bad admin edit would kill the currently-healthy process
# BEFORE the manager's own Phase 2 validation/preflight ever got a
# chance to reject it (the LKG would still recover the stream, but
# only after an unnecessary interruption). _run_predispatch_preflight
# runs the same validate -> render -> preflight sequence the manager
# uses, BEFORE dispatch, so the admin can refuse to restart at all
# when it already knows the change would fail.
# ---------------------------------------------------------------------
class PredispatchPreflightUnitTests(GroupStatusFixtureMixin, TestCase):
    def test_no_enabled_encoders_passes_trivially(self):
        ok, failures = encoders_admin._run_predispatch_preflight()
        self.assertTrue(ok)
        self.assertEqual(failures, [])

    def test_candidate_write_failure_is_reported_not_raised(self):
        """Phase 2 review-fix pass 2, Issue 3: a filesystem failure
        while writing the candidate script (ENOSPC, EACCES, ...) must
        become an ordinary rejection -- restart not dispatched, current
        stream untouched, admin error displayed -- never an exception
        escaping this function (it runs inside transaction.on_commit(),
        with nothing above it to catch a stray exception before it
        reaches the response cycle)."""
        make_encoder(input_device="airtap", enabled=True)
        with patch.object(lkg_module, "write_candidate", side_effect=OSError(28, "No space left on device")):
            ok, failures = encoders_admin._run_predispatch_preflight()  # must not raise
        self.assertFalse(ok)
        self.assertEqual(len(failures), 1)
        self.assertIn("failed to write candidate script", failures[0][1])

    def test_valid_group_passes_with_mocked_preflight(self):
        make_encoder(input_device="airtap", enabled=True)
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok, failures = encoders_admin._run_predispatch_preflight()
        self.assertTrue(ok)
        self.assertEqual(failures, [])

    def test_validation_failure_reported_without_calling_preflight(self):
        """protocol=shoutcast1 + format=vorbis passes Django's own
        per-row model field choices (both are individually legal
        FORMAT_CHOICES/PROTOCOL_CHOICES values) but fails Phase 2A's
        protocol/format compatibility matrix -- validate_full_
        configuration must catch it before preflight (liquidsoap) is
        ever invoked."""
        make_encoder(input_device="airtap", enabled=True, protocol="shoutcast1", format="vorbis")
        mock_preflight = MagicMock()
        with patch.object(preflight_module, "run_preflight", mock_preflight):
            ok, failures = encoders_admin._run_predispatch_preflight()
        self.assertFalse(ok)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "airtap")
        mock_preflight.assert_not_called()

    def test_preflight_failure_reported(self):
        make_encoder(input_device="airtap", enabled=True)
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=False, reason="liquidsoap --check failed")):
            ok, failures = encoders_admin._run_predispatch_preflight()
        self.assertFalse(ok)
        self.assertIn("liquidsoap --check failed", failures[0][1])

    def test_predispatch_leaves_no_stray_candidate_files(self):
        make_encoder(input_device="airtap", enabled=True)
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            encoders_admin._run_predispatch_preflight()
        leftovers = list(lkg_module.CANDIDATE_DIR.glob("*.liq")) if lkg_module.CANDIDATE_DIR.exists() else []
        self.assertEqual(leftovers, [])

    def test_one_bad_group_is_reported_even_though_another_group_is_fine(self):
        """Whole-service gate, not per-group: a bad group still shows
        up in `failures` on its own -- _restart_encoders_and_report is
        what turns "any failures at all" into "no restart dispatched,"
        covered separately below."""
        make_encoder(name="good", input_device="airtap", enabled=True)
        make_encoder(name="bad", input_device="other-device", enabled=True, protocol="shoutcast1", format="vorbis")
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            ok, failures = encoders_admin._run_predispatch_preflight()
        self.assertFalse(ok)
        failed_devices = [f[0] for f in failures]
        self.assertIn("other-device", failed_devices)
        self.assertNotIn("airtap", failed_devices)


@override_settings(SECURE_SSL_REDIRECT=False)
class PredispatchPreflightHttpTests(GroupStatusFixtureMixin, TransactionTestCase):
    # TransactionTestCase, matching EncoderAdminDispatchFailureMessageTests
    # above: messages.error() added inside the on_commit callback needs a
    # REAL commit during request handling to land in this response.
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser("admin-predispatch", "admin-pd@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_group_level_duplicate_destination_blocks_restart(self):
        """A duplicate destination across two DIFFERENT rows is a
        group-level failure Django's own per-row form validation
        cannot catch (each row is individually fine) -- exactly the
        kind of check this admin-level gate exists to add."""
        make_encoder(name="existing")  # DEFAULT_FIELDS: shoutcast2, host/port/mount all default
        data = encoder_post_data(name="duplicate")  # same host/port/mount -> same destination
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
                response = self.client.post(reverse("admin:encoders_encoder_add"), data, follow=True)
        mock_dispatch.assert_not_called()
        self.assertContains(response, "failed pre-restart checks")
        # The DB write itself still happened -- desired config stays
        # visible for diagnosis, never silently discarded.
        self.assertTrue(Encoder.objects.filter(name="duplicate").exists())

    def test_valid_change_still_dispatches_restart(self):
        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
                self.client.post(reverse("admin:encoders_encoder_add"), encoder_post_data(name="fine"), follow=True)
        mock_dispatch.assert_called_once()

    def test_candidate_write_failure_shows_error_no_restart_no_exception(self):
        """Phase 2 review-fix pass 2, Issue 3, at the full HTTP level:
        a filesystem failure writing the candidate script must not
        dispatch a restart, must not raise (500), and must show the
        operator a clear error -- exactly like an ordinary validation/
        preflight rejection."""
        with patch.object(lkg_module, "write_candidate", side_effect=OSError(28, "No space left on device")):
            with patch.object(encoders_admin, "dispatch_encoder_restart", return_value=True) as mock_dispatch:
                response = self.client.post(reverse("admin:encoders_encoder_add"), encoder_post_data(name="fine"), follow=True)
        mock_dispatch.assert_not_called()
        self.assertContains(response, "failed pre-restart checks")

    def test_rejected_predispatch_does_not_touch_currently_running_process(self):
        """The whole point of this fix: no restart command is ever
        launched for a rejected desired config -- dispatch_encoder_
        restart (the only thing that would touch the live process) is
        never called at all.

        Popen is only intercepted for the RESTART command specifically
        -- the add-view's own get_form() also shells out (`arecord -l`,
        via hardware.devices.list_input_devices) to populate the
        input_device dropdown, and that real call must be left alone."""
        make_encoder(name="existing")
        data = encoder_post_data(name="duplicate")
        restart_calls = []
        real_popen = encoders_admin.subprocess.Popen

        def selective_popen(cmd, *args, **kwargs):
            if cmd == encoders_admin.RESTART_COMMAND:
                restart_calls.append(cmd)
                return MagicMock()
            return real_popen(cmd, *args, **kwargs)

        with patch.object(preflight_module, "run_preflight", return_value=preflight_module.PreflightResult(ok=True)):
            with patch.object(encoders_admin.subprocess, "Popen", side_effect=selective_popen):
                self.client.post(reverse("admin:encoders_encoder_add"), data, follow=True)
        self.assertEqual(restart_calls, [])


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

    def test_encoder_admin_still_includes_confirm_js(self):
        self.assertIn("encoders/js/encoder_confirm.js", EncoderAdmin.Media.js)
