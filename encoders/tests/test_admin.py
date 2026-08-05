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
import re
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.db import transaction
from django.http import HttpRequest
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from encoders import admin as encoders_admin
from encoders.admin import RUNTIME_AFFECTING_FIELDS, EncoderAdmin, dispatch_encoder_restart, mark_encoder_restart_needed
from encoders.models import Encoder

DEFAULT_FIELDS = dict(
    name="test-encoder", enabled=True, protocol="shoutcast2",
    host="192.168.1.112", port=8000, mount="/4", username="source",
    password="secret", format="mp3", bitrate_kbps=192, input_device="",
    station_name="", genre="", description="", url="", public=False,
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
