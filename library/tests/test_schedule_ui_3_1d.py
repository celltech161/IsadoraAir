"""Roadmap 3.1D -- Schedule UI optimization and refinement.

The backend resolver remains the authority.  These tests cover the complete
server payload needed by the vertical Date Override view and guard the page's
rendering/interaction contract without reimplementing schedule precedence in
the browser.
"""
from datetime import time

from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import ScheduleBlock
from library.tests.test_schedule_profiles_3_1b import ApiMixin, MONDAY


def row(profile, hour, minute, *, rotation, dow=None, on=None):
    return ScheduleBlock.objects.create(
        profile=profile,
        day_of_week=dow,
        specific_date=on,
        start_time=time(hour, minute),
        end_time=time((hour + 1) % 24, 0),
        rotation=rotation,
    )


@override_settings(SECURE_SSL_REDIRECT=False)
class DateOverridePayloadTests(ApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.other = self.create_profile("3.1D Other")
        self.url = reverse("library:api-schedule-list")

    def date_payload(self, profile=None):
        return self.client.get(self.url, {
            "profile": (profile or self.default).uuid,
            "date": MONDAY.isoformat(),
        }).json()

    def hour_detail(self, hour, profile=None):
        return self.client.get(reverse("library:api-schedule-hour-detail"), {
            "profile": (profile or self.default).uuid,
            "date": MONDAY.isoformat(),
            "hour": hour,
        }).json()

    def test_vertical_day_payload_distinguishes_all_states_and_details(self):
        # Weekly base only.
        row(self.default, 1, 0, rotation=self.rotation_a, dow=0)
        # Explicit dated base.
        row(self.default, 2, 0, rotation=self.rotation_b, on=MONDAY)
        # 03:00 intentionally empty.
        # Inherited weekly transition.
        row(self.default, 4, 0, rotation=self.rotation_a, dow=0)
        row(self.default, 4, 20, rotation=self.rotation_b, dow=0)
        # Explicit dated transition over a weekly base.
        row(self.default, 5, 0, rotation=self.rotation_a, dow=0)
        row(self.default, 5, 20, rotation=self.rotation_b, on=MONDAY)
        # Dated transition shadows a later weekly transition.
        row(self.default, 6, 0, rotation=self.rotation_a, dow=0)
        row(self.default, 6, 30, rotation=self.rotation_b, dow=0)
        row(self.default, 6, 20, rotation=self.rotation_b, on=MONDAY)
        # Several effective transitions in one detailed hour.
        row(self.default, 7, 0, rotation=self.rotation_a, dow=0)
        row(self.default, 7, 10, rotation=self.rotation_b, dow=0)
        row(self.default, 7, 20, rotation=self.rotation_a, on=MONDAY)
        row(self.default, 7, 50, rotation=self.rotation_b, on=MONDAY)

        cells = {cell["hour"]: cell for cell in self.date_payload()["cells"]}
        self.assertEqual((cells[1]["origin"], cells[1]["detail_count"]), ("weekly", 0))
        self.assertEqual((cells[2]["origin"], cells[2]["detail_count"]), ("date_override", 0))
        self.assertEqual((cells[3]["origin"], cells[3]["effective_block"]), ("none", None))
        self.assertEqual((cells[4]["origin"], cells[4]["detail_count"]), ("weekly", 1))
        self.assertEqual((cells[5]["origin"], cells[5]["detail_count"]), ("weekly", 1))
        self.assertEqual(cells[6]["detail_count"], 1)
        self.assertEqual(cells[7]["detail_count"], 3)

        inherited = {entry["minute"]: entry for entry in self.hour_detail(4)["minutes"]}
        self.assertTrue(inherited[20]["inherited_transition"])
        explicit = {entry["minute"]: entry for entry in self.hour_detail(5)["minutes"]}
        self.assertTrue(explicit[20]["explicit_block_id"])
        self.assertEqual(explicit[20]["origin"], "date_override")
        shadowed = {entry["minute"]: entry for entry in self.hour_detail(6)["minutes"]}
        self.assertFalse(shadowed[30]["inherited_transition"])
        self.assertEqual(shadowed[30]["origin"], "date_override")

    def test_same_date_hour_is_isolated_by_selected_profile(self):
        row(self.default, 9, 0, rotation=self.rotation_a, dow=0)
        row(self.other, 9, 0, rotation=self.rotation_b, on=MONDAY)
        default_cell = self.date_payload(self.default)["cells"][9]
        other_cell = self.date_payload(self.other)["cells"][9]
        self.assertEqual(default_cell["origin"], "weekly")
        self.assertEqual(default_cell["effective_block"]["content_id"], self.rotation_a.pk)
        self.assertEqual(other_cell["origin"], "date_override")
        self.assertEqual(other_cell["effective_block"]["content_id"], self.rotation_b.pk)

    def test_revert_conflict_preserves_related_rows_and_exact_delete_is_narrow(self):
        base = row(self.default, 8, 0, rotation=self.rotation_a, on=MONDAY)
        transition = row(self.default, 8, 20, rotation=self.rotation_b, on=MONDAY)
        base_url = reverse("library:api-schedule-delete", args=[base.pk])
        blocked = self.client.delete(
            f"{base_url}?profile={self.default.uuid}&date={MONDAY.isoformat()}"
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(ScheduleBlock.objects.filter(pk__in=[base.pk, transition.pk]).count(), 2)

        transition_url = reverse("library:api-schedule-delete", args=[transition.pk])
        removed = self.client.delete(
            f"{transition_url}?profile={self.default.uuid}&date={MONDAY.isoformat()}"
        )
        self.assertEqual(removed.status_code, 200)
        self.assertTrue(ScheduleBlock.objects.filter(pk=base.pk).exists())
        self.assertFalse(ScheduleBlock.objects.filter(pk=transition.pk).exists())


@override_settings(SECURE_SSL_REDIRECT=False)
class SchedulePage31DMarkupTests(ApiMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.html = self.client.get(reverse("library:schedule")).content.decode()

    def test_date_override_is_one_semantic_vertical_24_hour_surface(self):
        for marker in (
            'id="dateHourList" class="date-hour-list"',
            'aria-label="24-hour Date Override schedule"',
            "date-hour-row", "date-hour-main", "date-origin-badge",
            "'OVERRIDE'", "'WEEKLY'", "'EMPTY'", "DATE_HOUR_LABELS",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)
        self.assertNotIn('class="date-grid"', self.html)
        self.assertNotIn("grid-template-columns: repeat(auto-fill, minmax(145px", self.html)

    def test_date_rows_render_only_server_derived_state(self):
        script = self.html[
            self.html.index("async function loadDateSchedule("):
            self.html.index("function escapeHtml(")
        ]
        for field in ("cell.origin", "cell.effective_block", "cell.detail_count", "cell.explicit_block_id"):
            self.assertIn(field, script)
        for forbidden in ("resolve_schedule_segments", "Math.max(", ".sort(", ".reduce("):
            self.assertNotIn(forbidden, script)

    def test_profile_and_date_changes_cannot_render_a_stale_response(self):
        for marker in (
            "const requestedProfileUuid = selectedProfile.uuid;",
            "const requestedDate = date;",
            "selectedProfile.uuid !== requestedProfileUuid",
            "document.getElementById('overrideDate').value !== requestedDate",
        ):
            self.assertIn(marker, self.html)

    def test_date_navigation_and_selected_date_heading_are_exposed(self):
        for marker in (
            'id="previousDateButton"', 'id="nextDateButton"', 'id="todayButton"',
            'id="selectedDateTitle"', "function shiftOverrideDate(days)",
            "function selectToday()", "formatSelectedDate(data.date)",
        ):
            self.assertIn(marker, self.html)

    def test_shared_picker_is_single_selectable_and_visible_before_both_modes(self):
        self.assertEqual(self.html.count('id="contentPicker"'), 1)
        self.assertEqual(self.html.count('id="selectedContentStatus"'), 1)
        self.assertIn("b.setAttribute('aria-pressed', 'false')", self.html)
        self.assertIn("btn.setAttribute('aria-pressed', 'true')", self.html)
        self.assertLess(self.html.index('id="contentPicker"'), self.html.index('id="weeklyDesktop"'))
        self.assertLess(self.html.index('id="contentPicker"'), self.html.index('id="dateSchedule"'))

    def test_archived_date_rows_disable_mutation_but_not_hour_detail(self):
        self.assertIn("assignButton.disabled = selectedProfile.is_archived", self.html)
        self.assertIn("button.disabled = selectedProfile.is_archived", self.html)
        self.assertNotIn("detailButton.disabled = selectedProfile.is_archived", self.html)

    def test_responsive_contract_has_no_date_list_horizontal_scroller(self):
        for marker in (
            "@media (max-width: 1100px)", "@media (max-width: 900px)",
            "@media (max-width: 768px)", "@media (max-width: 620px)",
            "@media (max-width: 480px)",
            ".minute-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }",
            ".date-hour-row { grid-template-columns: minmax(0, 1fr); }",
        ):
            self.assertIn(marker, self.html)
        self.assertNotIn(".date-hour-list { overflow-x:", self.html)
