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

    def test_revert_base_preserves_later_partial_row_and_exact_delete_is_narrow(self):
        base = row(self.default, 8, 0, rotation=self.rotation_a, on=MONDAY)
        transition = row(self.default, 8, 20, rotation=self.rotation_b, on=MONDAY)
        base_url = reverse("library:api-schedule-delete", args=[base.pk])
        removed_base = self.client.delete(
            f"{base_url}?profile={self.default.uuid}&date={MONDAY.isoformat()}"
        )
        self.assertEqual(removed_base.status_code, 200)
        self.assertFalse(ScheduleBlock.objects.filter(pk=base.pk).exists())
        self.assertTrue(ScheduleBlock.objects.filter(pk=transition.pk).exists())

        transition_url = reverse("library:api-schedule-delete", args=[transition.pk])
        removed = self.client.delete(
            f"{transition_url}?profile={self.default.uuid}&date={MONDAY.isoformat()}"
        )
        self.assertEqual(removed.status_code, 200)
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

    def test_hour_detail_renders_server_derived_partial_continuation_state(self):
        for marker in (
            "entry.continuation", "data.is_partial_hour", "data.takeover_minute",
            "Previous program continues", "minute-cell.continuation",
        ):
            self.assertIn(marker, self.html)
        # The browser consumes the semantic field; it does not infer the
        # continuation prefix from the first segment itself.
        detail_script = self.html[
            self.html.index("function renderHourDetail("):
            self.html.index("async function onMinuteClick(")
        ]
        self.assertNotIn("Math.min", detail_script)
        self.assertNotIn("segments[0].start_minute", detail_script)

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


def js_function(html, signature, until):
    """The source text of one script function (up to the next marker)."""
    start = html.index(signature)
    return html[start:html.index(until, start)]


@override_settings(SECURE_SSL_REDIRECT=False)
class ScheduleAsyncContextContractTests(ApiMixin, TestCase):
    """Static contract for the async hardening: request identity + context.

    The behavioral proof (real overlapping/out-of-order responses in Chromium)
    lives in test_schedule_ui_async_3_1d.py; these guard the code shape.
    """

    def setUp(self):
        super().setUp()
        self.html = self.client.get(reverse("library:schedule")).content.decode()
        self.load_date = js_function(self.html, "async function loadDateSchedule(", "function escapeHtml(")

    # 1 -- the Date Override surface is invalidated synchronously
    def test_date_surface_is_cleared_and_marked_busy_before_the_request_is_awaited(self):
        body = self.load_date
        self.assertLess(body.index("++dateScheduleRequestGeneration"), body.index("await apiRequest"))
        self.assertLess(body.index("showDateScheduleLoading()"), body.index("await apiRequest"))
        loading = js_function(self.html, "function showDateScheduleLoading(", "function dateScheduleContextIsLive(")
        self.assertIn("setAttribute('aria-busy', 'true')", loading)
        self.assertIn("list.replaceChildren(", loading)  # rows removed, not just hidden
        self.assertIn("Loading schedule", loading)
        self.assertIn("role', 'status'", self.html)
        # Busy is cleared only by the request that is still current.
        self.assertIn("setAttribute('aria-busy', 'false')", body)
        self.assertLess(body.index("showDateScheduleLoading()"), body.index("formatSelectedDate(ctx.date)"))
        self.assertLess(body.index("formatSelectedDate(ctx.date)"), body.index("await apiRequest"))

    def test_loading_state_holds_height_instead_of_collapsing_the_page(self):
        loading = js_function(self.html, "function showDateScheduleLoading(", "function dateScheduleContextIsLive(")
        self.assertIn("style.minHeight", loading)
        self.assertNotIn("overflow-x", loading)

    # 2 -- same profile/date overlapping requests
    def test_date_requests_are_ordered_by_a_monotonic_generation_not_just_profile_and_date(self):
        self.assertIn("let dateScheduleRequestGeneration = 0;", self.html)
        live = js_function(self.html, "function dateScheduleContextIsLive(", "async function loadDateSchedule(")
        self.assertIn("ctx.generation === dateScheduleRequestGeneration", live)
        self.assertIn("selectedProfile.uuid === ctx.profileUuid", live)
        self.assertIn("document.getElementById('overrideDate').value === ctx.date", live)
        self.assertIn("scheduleMode === 'date'", live)
        body = self.load_date
        self.assertEqual(body.count("if (!dateScheduleContextIsLive(ctx)) return;"), 2)  # render path AND error path
        self.assertIn("Object.freeze({generation, profileUuid: selectedProfile.uuid, date})", body)
        # Even a bail-out (no profile/date) supersedes earlier requests.
        self.assertLess(body.index("++dateScheduleRequestGeneration"), body.index("if (!selectedProfile || !date)"))

    def test_stale_failures_cannot_overwrite_a_newer_context(self):
        catch = self.load_date[self.load_date.index("} catch (error) {"):]
        self.assertLess(catch.index("dateScheduleContextIsLive(ctx)"), catch.index("showError(error.message)"))
        refresh = js_function(self.html, "async function refreshHourDetail(", "function renderHourDetail(")
        catch = refresh[refresh.index("} catch (error) {"):]
        self.assertLess(catch.index("if (!isCurrent()) return;"), catch.index("showError(error.message)"))

    def test_date_rows_and_actions_are_bound_to_the_context_that_rendered_them(self):
        self.assertIn("assignDateCell(cell.hour, ctx)", self.html)
        self.assertIn("revertDateCell(cell.explicit_block_id, ctx)", self.html)
        for signature, until in (
            ("async function assignDateCell(", "async function revertDateCell("),
            ("async function revertDateCell(", "// --- Hour Detail (3.1C) ---"),
        ):
            body = js_function(self.html, signature, until)
            self.assertIn("if (!dateScheduleContextIsLive(ctx)", body)
            self.assertIn("ctx.profileUuid", body)
            self.assertIn("ctx.date", body)
        assign = js_function(self.html, "async function assignDateCell(", "async function revertDateCell(")
        self.assertNotIn("document.getElementById('overrideDate')", assign)  # no live re-read at write time

    # 3 -- Hour Detail request identity
    def test_hour_detail_requests_carry_an_immutable_full_context_and_a_generation(self):
        opened = js_function(self.html, "async function openHourDetail(", "function closeHourDetail(")
        self.assertIn("Object.freeze({", opened)
        for field in ("mode", "day", "hour", "profileUuid", "date"):
            self.assertIn(field, opened)
        self.assertIn("let hourDetailRequestGeneration = 0;", self.html)
        refresh = js_function(self.html, "async function refreshHourDetail(", "function renderHourDetail(")
        self.assertIn("++hourDetailRequestGeneration", refresh)
        self.assertLess(refresh.index("++hourDetailRequestGeneration"), refresh.index("await apiRequest"))
        self.assertIn("generation === hourDetailRequestGeneration && hourDetailContextIsLive(ctx)", refresh)
        self.assertLess(refresh.index("await apiRequest"), refresh.index("if (!isCurrent()) return;"))
        self.assertLess(refresh.index("if (!isCurrent()) return;"), refresh.index("renderHourDetail(data, ctx)"))
        live = js_function(self.html, "function hourDetailContextIsLive(", "function hourDetailTitle(")
        for clause in (
            "hourDetail === ctx", "selectedProfile.uuid === ctx.profileUuid",
            "scheduleMode === ctx.mode", "document.getElementById('overrideDate').value === ctx.date",
        ):
            self.assertIn(clause, live)

    def test_hour_detail_rendering_and_writes_never_read_the_mutable_global(self):
        for signature, until in (
            ("function renderHourDetail(", "async function onMinuteClick("),
            ("async function onMinuteClick(", "async function afterMinuteWrite("),
            ("function hourDetailQuery(", "function hourDetailContextIsLive("),
        ):
            body = js_function(self.html, signature, until)
            self.assertNotIn("hourDetail.", body, signature)
        minute = js_function(self.html, "async function onMinuteClick(", "async function afterMinuteWrite(")
        self.assertIn("if (!hourDetailContextIsLive(ctx)", minute)
        self.assertIn("onMinuteClick(entry, ctx)", self.html)
        self.assertNotIn("document.getElementById('overrideDate')", minute)

    def test_opening_a_context_removes_the_previous_minutes_before_loading(self):
        opened = js_function(self.html, "async function openHourDetail(", "function closeHourDetail(")
        self.assertLess(opened.index("showHourDetailLoading(ctx)"), opened.index("await refreshHourDetail()"))
        loading = js_function(self.html, "function showHourDetailLoading(", "async function openHourDetail(")
        self.assertIn("grid.replaceChildren()", loading)
        self.assertIn("setAttribute('aria-busy', 'true')", loading)

    # 4 -- close / context change invalidation
    def test_closing_hour_detail_invalidates_the_in_flight_generation(self):
        close = js_function(self.html, "function closeHourDetail(", "async function refreshHourDetail(")
        self.assertIn("hourDetail = null;", close)
        self.assertIn("hourDetailRequestGeneration++", close)
        self.assertIn("hidden = true", close)

    def test_every_context_change_closes_hour_detail_before_anything_loads(self):
        for signature, until in (
            ("async function setMode(", "function selectContent("),
            ("function setOverrideDate(", "function shiftOverrideDate("),
        ):
            body = js_function(self.html, signature, until)
            self.assertIn("closeHourDetail();", body)
        self.assertIn("dateScheduleRequestGeneration++;", js_function(self.html, "async function setMode(", "function selectContent("))
        picker = js_function(self.html, "function buildDayPicker(", "function renderMobileHours(")
        self.assertIn("closeHourDetail();", picker)
        select = js_function(self.html, "getElementById('profileSelect').addEventListener('change'", "loadProfiles();")
        self.assertLess(select.index("closeHourDetail();"), select.index("loadDateSchedule()"))

    def test_server_derived_rendering_is_untouched_by_the_hardening(self):
        for field in ("cell.origin", "cell.effective_block", "cell.detail_count", "cell.explicit_block_id"):
            self.assertIn(field, self.load_date)
        for forbidden in ("resolve_schedule_segments", ".sort(", ".reduce("):
            self.assertNotIn(forbidden, self.load_date)
