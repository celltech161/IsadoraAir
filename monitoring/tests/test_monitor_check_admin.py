"""r0016 -- real production bug: enabling a transmitter_param MonitorCheck
(WRJE id 13, computed:vswr) through the Admin changelist's list-editable
checkbox raised HTTP 500, while editing the same row through its
individual change page worked fine.

Root cause, confirmed empirically against the pre-fix code: the
changelist's list_editable formset builds a restricted ModelForm with
only the list_editable fields (enabled, show_as_card, sort_order) --
NOT critical_threshold. MonitorCheck.clean() raised a ValidationError
keyed to critical_threshold for ANY transmitter_param row in the
CURRENTLY FILTERED QUERYSET with no threshold configured (e.g. the
already-enabled, already-production 'TX Reflected Power' style row),
not just the row actually being toggled. Django's ModelForm._post_clean
catches that ValidationError and tries to attach it to the form via
add_error(), which raises an unhandled
`ValueError: 'MonitorCheckForm' has no field named 'critical_threshold'`
because that field isn't part of the restricted list-editable form --
an uncaught ValueError that becomes a genuine HTTP 500 in production
(DEBUG=False), even though the row an operator was actually trying to
change was perfectly valid.

This exercises the real registered MonitorCheckAdmin via Django's test
client (a real changelist GET + a real list-editable formset POST),
per the task's explicit preference over merely unit-testing clean()."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.models import MonitorCheck


@override_settings(SECURE_SSL_REDIRECT=False)
class MonitorCheckChangelistListEditableTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            "monitorcheck-admin-staff", "mc-admin@example.invalid", "pw"
        )
        self.client.force_login(self.staff)

        # The row an operator is actually trying to enable -- mirrors
        # WRJE id 13 (computed:vswr), thresholded, starts disabled.
        self.target = MonitorCheck.objects.create(
            name="r0016 Test TX VSWR",
            kind="transmitter_param",
            transmitter_parameter="computed:vswr",
            warning_threshold=1.5,
            critical_threshold=1.65,
            threshold_direction="above",
            enabled=False,
            show_as_card=True,
            sort_order=513,
        )
        # An unrelated, ALREADY-ENABLED, informational (no threshold)
        # row -- mirrors WRJE id 20 (meters.parev), the row that
        # actually poisoned the whole formset pre-fix even though
        # nobody was touching it.
        self.informational_enabled = MonitorCheck.objects.create(
            name="r0016 Test TX Reflected Power",
            kind="transmitter_param",
            transmitter_parameter="meters.parev",
            enabled=True,
            show_as_card=True,
            sort_order=520,
        )
        # A second informational row, disabled -- mirrors WRJE id 25
        # (meters.fanspeed).
        self.informational_disabled = MonitorCheck.objects.create(
            name="r0016 Test TX Fan Speed",
            kind="transmitter_param",
            transmitter_parameter="meters.fanspeed",
            enabled=False,
            show_as_card=False,
            sort_order=525,
        )
        # A third, unrelated thresholded row that must stay untouched.
        self.other_thresholded = MonitorCheck.objects.create(
            name="r0016 Test TX Board Temperature High",
            kind="transmitter_param",
            transmitter_parameter="aio.temp.board",
            warning_threshold=55,
            critical_threshold=65,
            threshold_direction="above",
            enabled=False,
            show_as_card=True,
            sort_order=521,
        )

    def _changelist_formset_data(self):
        """Build the exact POST payload a real browser would submit for
        the current transmitter_param changelist page -- every row's
        current field values, not just the one being changed."""
        checks = list(
            MonitorCheck.objects.filter(kind="transmitter_param").order_by(
                "sort_order", "name"
            )
        )
        data = {
            "form-TOTAL_FORMS": str(len(checks)),
            "form-INITIAL_FORMS": str(len(checks)),
            "form-MIN_NUM_FORMS": "0",
            "form-MAX_NUM_FORMS": "1000",
            "_save": "Save",
        }
        for index, check in enumerate(checks):
            data[f"form-{index}-id"] = str(check.pk)
            if check.enabled:
                data[f"form-{index}-enabled"] = "on"
            if check.show_as_card:
                data[f"form-{index}-show_as_card"] = "on"
            data[f"form-{index}-sort_order"] = str(check.sort_order)
        return data, checks

    def _post_changelist(self, data):
        return self.client.post(
            reverse("admin:monitoring_monitorcheck_changelist")
            + "?kind__exact=transmitter_param",
            data=data,
        )

    def test_enabling_a_transmitter_row_through_the_changelist_does_not_500(self):
        data, checks = self._changelist_formset_data()
        target_index = checks.index(self.target)
        data[f"form-{target_index}-enabled"] = "on"

        response = self._post_changelist(data)

        self.assertNotEqual(response.status_code, 500)
        # A genuine save redirects back to the changelist (302) -- a
        # 200 here would mean the formset was rejected, which is also
        # a failure of this fix's intent even though it isn't a crash.
        self.assertEqual(response.status_code, 302)

    def test_enabling_a_transmitter_row_through_the_changelist_updates_only_that_row(self):
        data, checks = self._changelist_formset_data()
        target_index = checks.index(self.target)
        data[f"form-{target_index}-enabled"] = "on"

        self._post_changelist(data)

        self.target.refresh_from_db()
        self.assertTrue(self.target.enabled)

        self.informational_enabled.refresh_from_db()
        self.assertTrue(self.informational_enabled.enabled)  # unchanged

        self.informational_disabled.refresh_from_db()
        self.assertFalse(self.informational_disabled.enabled)  # unchanged

        self.other_thresholded.refresh_from_db()
        self.assertFalse(self.other_thresholded.enabled)  # unchanged

    def test_informational_transmitter_rows_do_not_invalidate_the_formset(self):
        """Even with no field of the target row changed at all, simply
        re-POSTing the current state of a queryset that includes
        thresholdless transmitter_param rows must not fail."""
        data, _checks = self._changelist_formset_data()

        response = self._post_changelist(data)

        self.assertEqual(response.status_code, 302)
        self.informational_enabled.refresh_from_db()
        self.assertTrue(self.informational_enabled.enabled)
        self.informational_disabled.refresh_from_db()
        self.assertFalse(self.informational_disabled.enabled)

    def test_changelist_get_renders_without_error(self):
        response = self.client.get(
            reverse("admin:monitoring_monitorcheck_changelist")
            + "?kind__exact=transmitter_param"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.target.name)
        self.assertContains(response, self.informational_enabled.name)
