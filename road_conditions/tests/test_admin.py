"""Light admin smoke tests -- confirms the pages actually render (a
broken fieldset/readonly_fields reference is a runtime error Django
won't catch at `manage.py check` time) and that RoadEvent's admin is
genuinely read-only, not that every visual detail is pixel-checked."""
import json
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from road_conditions.models import RoadConditionsConfiguration, RoadEvent
from road_conditions.services import normalize_event

FIXTURES = Path(__file__).parent / "fixtures"


def _create_test_event():
    raw = json.loads((FIXTURES / "event_construction.json").read_text())
    normalized = normalize_event(raw)
    now = timezone.now()
    return RoadEvent.objects.create(**normalized, last_seen_at=now, last_changed_at=now,
                                     source_active=True, in_scope=True)


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class RoadConditionsAdminTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("admin", "admin@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_config_changelist_redirects_to_singleton(self):
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_changelist"))
        self.assertEqual(response.status_code, 302)

    def test_config_change_form_renders(self):
        RoadConditionsConfiguration.load()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Road Conditions Configuration")

    def test_config_change_form_includes_report_framing_fields(self):
        RoadConditionsConfiguration.load()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]))
        self.assertContains(response, "On-Air Report Framing")
        self.assertContains(response, 'name="report_preamble"')
        self.assertContains(response, 'name="report_postamble"')

    def test_config_change_form_includes_transition_sound_fields(self):
        RoadConditionsConfiguration.load()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]))
        self.assertContains(response, "Item Transition Sound")
        self.assertContains(response, 'name="transition_sound_enabled"')
        self.assertContains(response, 'name="transition_sound_path"')

    def test_config_change_form_includes_additional_route_coverage_field(self):
        RoadConditionsConfiguration.load()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]))
        self.assertContains(response, 'name="additional_route_coverage"')

    def test_config_change_form_shows_no_report_generation_yet_by_default(self):
        RoadConditionsConfiguration.load()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]))
        self.assertContains(response, "No successful report generation recorded yet.")
        # Read-only display -- never an editable input for either field.
        self.assertNotIn(b'name="last_report_fingerprint"', response.content)
        self.assertNotIn(b'name="last_report_generated_at"', response.content)

    def test_config_change_form_shows_shortened_fingerprint_when_present(self):
        obj = RoadConditionsConfiguration.load()
        obj.last_report_fingerprint = "a" * 64
        obj.last_report_generated_at = timezone.now()
        obj.save()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]))
        self.assertContains(response, "Last generated:")
        self.assertContains(response, "a" * 12)  # shortened prefix shown
        # The full 64-char value is what's stored, even though only a
        # shortened prefix is displayed.
        obj.refresh_from_db()
        self.assertEqual(obj.last_report_fingerprint, "a" * 64)

    def _config_post_data(self, obj, **overrides):
        """Minimal valid POST body for the singleton config change form --
        every non-readonly field needs SOME value or Django's own
        required-field validation fires first and masks whatever this
        test is actually trying to check."""
        data = {
            "enabled": "on" if obj.enabled else "",
            "report_preamble": obj.report_preamble,
            "report_postamble": obj.report_postamble,
            "transition_sound_enabled": "on" if obj.transition_sound_enabled else "",
            "transition_sound_path": obj.transition_sound_path,
            "api_base_url": obj.api_base_url,
            "request_timeout_seconds": obj.request_timeout_seconds,
            "poll_cadence_minutes": obj.poll_cadence_minutes,
            "event_classifications": obj.event_classifications,
            "counties": obj.counties,
            "routes": obj.routes,
            "additional_route_coverage": obj.additional_route_coverage,
            "min_priority": obj.min_priority,
            "max_event_age_days": obj.max_event_age_days,
            "lookahead_days": obj.lookahead_days,
            "stale_data_threshold_minutes": obj.stale_data_threshold_minutes,
            "debug_logging": "on" if obj.debug_logging else "",
        }
        data.update(overrides)
        return data

    def test_config_save_accepts_well_formed_additional_route_coverage(self):
        obj = RoadConditionsConfiguration.load()
        data = self._config_post_data(obj, additional_route_coverage="US 81: Saline,Cloud")
        response = self.client.post(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]), data)
        self.assertEqual(response.status_code, 302)  # redirect on successful save
        obj.refresh_from_db()
        self.assertEqual(obj.additional_route_coverage, "US 81: Saline,Cloud")

    def test_config_save_rejects_malformed_additional_route_coverage(self):
        obj = RoadConditionsConfiguration.load()
        original = obj.additional_route_coverage
        data = self._config_post_data(obj, additional_route_coverage="US 81")
        response = self.client.post(reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[1]), data)
        self.assertEqual(response.status_code, 200)  # re-renders the form with errors, no redirect
        self.assertContains(response, "errorlist")
        self.assertContains(response, "missing")  # from parse_additional_route_coverage's error text
        obj.refresh_from_db()
        self.assertEqual(obj.additional_route_coverage, original)  # nothing was saved

    def test_config_add_is_blocked_once_it_exists(self):
        RoadConditionsConfiguration.load()
        response = self.client.get(reverse("admin:road_conditions_roadconditionsconfiguration_add"))
        self.assertEqual(response.status_code, 403)

    def test_road_event_changelist_renders(self):
        response = self.client.get(reverse("admin:road_conditions_roadevent_changelist"))
        self.assertEqual(response.status_code, 200)

    def test_road_event_change_form_renders_with_raw_payload(self):
        obj = _create_test_event()
        response = self.client.get(reverse("admin:road_conditions_roadevent_change", args=[obj.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "US 81")

    def test_road_event_add_is_blocked(self):
        response = self.client.get(reverse("admin:road_conditions_roadevent_add"))
        self.assertEqual(response.status_code, 403)

    def test_road_event_fields_are_all_readonly(self):
        obj = _create_test_event()
        response = self.client.get(reverse("admin:road_conditions_roadevent_change", args=[obj.pk]))
        # A readonly-only change form has no <input>/<textarea> for
        # model fields (Django renders readonly values as plain <p>/<div>
        # text) -- the only inputs on the page should be CSRF/admin
        # chrome (search box etc.), not an editable model field. Assert
        # the specific fields we most want protected are not present as
        # editable widgets.
        content = response.content.decode()
        for field_name in ("external_id", "description", "primary_route", "raw_payload",
                            "source_active", "in_scope", "cause_categories"):
            self.assertNotIn(f'name="{field_name}"', content)

    def test_road_event_change_form_shows_cause_categories(self):
        raw = json.loads((FIXTURES / "event_device_status_with_cause.json").read_text())
        normalized = normalize_event(raw)
        now = timezone.now()
        obj = RoadEvent.objects.create(**normalized, last_seen_at=now, last_changed_at=now,
                                        source_active=True, in_scope=True)
        response = self.client.get(reverse("admin:road_conditions_roadevent_change", args=[obj.pk]))
        self.assertContains(response, "closure")
        self.assertContains(response, "roadwork")

    def test_road_event_has_no_action_that_writes_state(self):
        # RoadEvent is fully source-managed -- there is deliberately no
        # manual override action (an earlier revision had a
        # mark_inactive bulk action; removed with no demonstrated
        # operational need -- see admin.py's RoadEventAdmin docstring).
        # `actions` should be empty/absent, not just "mark_inactive is
        # gone" -- a regression guard against silently reintroducing
        # a similar write-capable action without deliberate review.
        from road_conditions.admin import RoadEventAdmin
        self.assertFalse(getattr(RoadEventAdmin, "actions", None))

    def test_is_current_display_reflects_source_active_and_in_scope(self):
        obj = _create_test_event()
        response = self.client.get(reverse("admin:road_conditions_roadevent_change", args=[obj.pk]))
        self.assertContains(response, "Currently relevant")
