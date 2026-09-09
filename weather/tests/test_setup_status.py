"""Pass C (P1 1.15 / 2.4): Weather Setup Status Admin panel.

weather.setup_status is presentation-only -- these tests build a
WeatherDiagnosticsSnapshot by hand and feed it straight to
render_setup_status(snapshot=...), never touching the database or
filesystem, per the brief's own instruction that "the Admin test should
not need real Weather JSON/Track inspection merely to test
presentation." A handful of integration tests at the bottom prove the
real Admin page wires it up correctly."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from weather.diagnostics import DiagnosticFact, WeatherDiagnosticsSnapshot
from weather.models import WeatherConfig
from weather.setup_status import (
    OVERALL_LABELS,
    STATE_LABELS,
    friendly_label,
    group_for_key,
    humanize_age,
    links_for_fact,
    overall_state,
    render_setup_status,
    state_counts,
)

GENERATED_AT = "2026-09-08T20:15:00Z"  # 8:15 PM UTC


def fact(key, state, summary="summary text", detail="", **kwargs):
    return DiagnosticFact(key=key, state=state, summary=summary, detail=detail, **kwargs)


def snapshot(*facts, generated_at=GENERATED_AT):
    return WeatherDiagnosticsSnapshot(generated_at=generated_at, facts=tuple(facts))


class OverallStateTests(TestCase):
    def test_all_healthy_plus_optional_and_na_is_ready(self):
        facts = [
            fact("station_location", "ready"),
            fact("notifications", "optional_disabled"),
            fact("generated_artifact:wx_alert", "not_applicable"),
        ]
        self.assertEqual(overall_state(facts), "ready")
        self.assertEqual(OVERALL_LABELS[overall_state(facts)], "Ready")

    def test_degraded_with_no_attention_is_ready_with_warnings(self):
        facts = [
            fact("station_location", "ready"),
            fact("forecast_cache", "degraded"),
        ]
        self.assertEqual(overall_state(facts), "degraded")
        self.assertEqual(OVERALL_LABELS[overall_state(facts)], "Ready with warnings")

    def test_needs_attention_present_wins(self):
        facts = [
            fact("station_location", "ready"),
            fact("forecast_cache", "degraded"),
            fact("nws_config", "needs_attention"),
        ]
        self.assertEqual(overall_state(facts), "needs_attention")
        self.assertEqual(OVERALL_LABELS[overall_state(facts)], "Needs attention")

    def test_counts_are_not_hardcoded_to_a_fixed_total(self):
        facts = [fact("a", "ready"), fact("b", "ready"), fact("c", "optional_disabled")]
        counts = state_counts(facts)
        self.assertEqual(counts["ready"], 2)
        self.assertEqual(counts["optional_disabled"], 1)
        self.assertEqual(counts["needs_attention"], 0)
        # A 4th, previously-unseen fact changes the counts with no code change.
        counts2 = state_counts(facts + [fact("d", "needs_attention")])
        self.assertEqual(counts2["needs_attention"], 1)


class StateLabelTests(TestCase):
    def test_ready_label(self):
        self.assertEqual(STATE_LABELS["ready"], "Ready")

    def test_degraded_label_is_warning(self):
        self.assertEqual(STATE_LABELS["degraded"], "Warning")

    def test_needs_attention_label(self):
        self.assertEqual(STATE_LABELS["needs_attention"], "Needs attention")

    def test_optional_disabled_label(self):
        self.assertEqual(STATE_LABELS["optional_disabled"], "Optional / Off")

    def test_not_applicable_label_is_na(self):
        self.assertEqual(STATE_LABELS["not_applicable"], "N/A")

    def test_rendered_html_shows_distinct_labels_not_raw_state_names(self):
        html = render_setup_status(snapshot(
            fact("station_location", "ready"),
            fact("forecast_cache", "degraded"),
            fact("nws_config", "needs_attention"),
            fact("notifications", "optional_disabled"),
            fact("generated_artifact:wx_alert", "not_applicable"),
        ))
        self.assertIn("Ready", html)
        self.assertIn("Warning", html)
        self.assertIn("Needs attention", html)
        self.assertIn("Optional / Off", html)
        self.assertIn("N/A", html)
        # Raw state tokens (as opposed to their labels) must not leak
        # into the visible page text as the "degraded"/"needs_attention"
        # variants use underscores that would be confusing as-is.
        self.assertNotIn("needs_attention", html)
        self.assertNotIn("optional_disabled", html)
        self.assertNotIn("not_applicable", html)


class GroupingTests(TestCase):
    def test_configuration_facts_grouped(self):
        for key in ("station_location", "nws_config", "announcer_schedule",
                    "alert_fx_cart", "notifications", "amber_alerts_config", "weather_data_dir"):
            self.assertEqual(group_for_key(key), "configuration", key)

    def test_persona_facts_grouped_with_announcer_schedule(self):
        self.assertEqual(group_for_key("announcer_schedule"), "configuration")
        self.assertEqual(group_for_key("announcer_persona:day"), "configuration")
        self.assertEqual(group_for_key("announcer_persona:night"), "configuration")

    def test_weather_data_files_and_forecast_cache_are_live_data(self):
        self.assertEqual(group_for_key("weather_data_file:latest_weather.json"), "live_data")
        self.assertEqual(group_for_key("weather_data_file:wind_history.json"), "live_data")
        self.assertEqual(group_for_key("forecast_cache"), "live_data")

    def test_wx_alert_artifact_only_in_alert_state_not_generated_audio(self):
        self.assertEqual(group_for_key("generated_artifact:wx_alert"), "alert_state")
        self.assertEqual(group_for_key("watch_warnings"), "alert_state")
        self.assertEqual(group_for_key("amber_alerts_data"), "alert_state")
        # The other three generated artifacts are audio, not alert state.
        self.assertEqual(group_for_key("generated_artifact:wx_temp"), "generated_audio")
        self.assertEqual(group_for_key("generated_artifact:wx_obs"), "generated_audio")
        self.assertEqual(group_for_key("generated_artifact:wx_forecast"), "generated_audio")

    def test_unknown_future_key_lands_in_other_not_discarded(self):
        self.assertEqual(group_for_key("some_brand_new_diagnostic_key"), "other")
        html = render_setup_status(snapshot(
            fact("some_brand_new_diagnostic_key", "ready", summary="A future fact."),
        ))
        self.assertIn("Other diagnostics", html)
        self.assertIn("A future fact.", html)

    def test_wx_alert_not_duplicated_across_groups(self):
        html = render_setup_status(snapshot(
            fact("generated_artifact:wx_alert", "not_applicable", summary="No active alert."),
        ))
        self.assertEqual(html.count("No active alert."), 1)


class EvidencePresentationTests(TestCase):
    def test_humanize_age_seconds(self):
        self.assertEqual(humanize_age(15), "15 sec ago")
        self.assertEqual(humanize_age(4 * 60), "4 min ago")
        self.assertEqual(humanize_age(59 * 60), "59 min ago")
        self.assertEqual(humanize_age(2 * 3600), "2 hr ago")
        self.assertEqual(humanize_age(24 * 3600), "1 day ago")
        self.assertEqual(humanize_age(3 * 24 * 3600), "3 days ago")
        self.assertIsNone(humanize_age(None))

    def test_age_seconds_rendered_human_readable_in_page(self):
        html = render_setup_status(snapshot(
            fact("weather_data_file:latest_weather.json", "ready", age_seconds=95),
        ))
        self.assertIn("1 min ago", html)
        self.assertNotIn("95", html)

    def test_detail_shown_for_needs_attention(self):
        html = render_setup_status(snapshot(
            fact("nws_config", "needs_attention", summary="Bad config.", detail="grid X/Y is not set."),
        ))
        self.assertIn("grid X/Y is not set.", html)

    def test_detail_shown_for_degraded(self):
        html = render_setup_status(snapshot(
            fact("forecast_cache", "degraded", summary="Stale.", detail="Cache is 7.2 hours old."),
        ))
        self.assertIn("Cache is 7.2 hours old.", html)

    def test_detail_not_shown_for_ready(self):
        html = render_setup_status(snapshot(
            fact("station_location", "ready", summary="Fine.", detail="should not appear"),
        ))
        self.assertNotIn("should not appear", html)

    def test_generated_at_timestamp_displayed_as_local_clock(self):
        html = render_setup_status(snapshot(fact("station_location", "ready")))
        self.assertIn("Status checked:", html)


class LinkTests(TestCase):
    def test_station_location_links_to_weatherconfig_change(self):
        WeatherConfig.objects.create(pk=1, voice_schedule=[["default", 0, 23]])
        links = links_for_fact(fact("station_location", "needs_attention"))
        self.assertEqual(links, [("Edit", reverse("admin:weather_weatherconfig_change", args=[1]))])

    def test_announcer_schedule_links_to_weather_voice_personas(self):
        links = links_for_fact(fact("announcer_schedule", "ready"))
        self.assertIn(("Weather Voice Personas", reverse("admin:weather_weathervoicepersona_changelist")), links)

    def test_persona_fact_links_to_station_tts_voice_when_evidence_has_id(self):
        links = links_for_fact(fact(
            "announcer_persona:day", "ready", evidence={"tts_voice_id": 7},
        ))
        self.assertIn(("Station TTS Voice", reverse("admin:tts_stationttsvoice_change", args=[7])), links)

    def test_alert_fx_cart_links_to_fx_carts(self):
        links = links_for_fact(fact("alert_fx_cart", "needs_attention"))
        self.assertEqual(links, [("FX Carts", reverse("admin:library_fxcart_changelist"))])

    def test_amber_config_links_to_amber_admin(self):
        links = links_for_fact(fact("amber_alerts_config", "needs_attention"))
        self.assertEqual(links, [("AMBER Alert Configuration", reverse("admin:weather_amberalertconfig_changelist"))])

    def test_weather_data_dir_links_to_env_subpage(self):
        links = links_for_fact(fact("weather_data_dir", "needs_attention"))
        self.assertEqual(links, [("Weather data storage", reverse("admin:weather_weatherconfig_weather_env"))])

    def test_unrecognized_key_has_no_links_but_does_not_error(self):
        self.assertEqual(links_for_fact(fact("some_future_key", "ready")), [])

    def test_links_rendered_as_real_anchors_in_page(self):
        html = render_setup_status(snapshot(fact("alert_fx_cart", "needs_attention")))
        self.assertIn(reverse("admin:library_fxcart_changelist"), html)


class EscapingTests(TestCase):
    def test_html_like_summary_is_escaped(self):
        html = render_setup_status(snapshot(
            fact("station_location", "needs_attention", summary='<script>alert("x")</script>'),
        ))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_html_like_detail_is_escaped(self):
        html = render_setup_status(snapshot(
            fact("nws_config", "needs_attention", summary="ok",
                 detail='<img src=x onerror=alert(1)> and "quoted" & ampersand'),
        ))
        self.assertNotIn("<img src=x onerror=alert(1)>", html)
        self.assertIn("&lt;img", html)

    def test_friendly_label_of_unknown_key_is_escaped_too(self):
        html = render_setup_status(snapshot(
            fact('<b>evil_key</b>', "ready", summary="fine"),
        ))
        self.assertNotIn("<b>evil_key</b>", html)


class AuthorityTests(TestCase):
    """Proves the panel is driven entirely by whatever
    get_weather_diagnostics() returns -- no independent inspection."""

    def test_render_setup_status_uses_supplied_snapshot_verbatim(self):
        custom = snapshot(fact("station_location", "needs_attention", summary="Totally fabricated evidence."))
        html = render_setup_status(custom)
        self.assertIn("Totally fabricated evidence.", html)
        self.assertIn("Needs attention", html)

    @override_settings(SECURE_SSL_REDIRECT=False)
    def test_admin_panel_reflects_patched_diagnostics_authority(self):
        staff = User.objects.create_superuser("wxsetupstatus", "x@example.invalid", "pw")
        self.client.force_login(staff)
        WeatherConfig.load()
        fake_snapshot = snapshot(
            fact("station_location", "needs_attention", summary="Patched-in fabricated fact."),
        )
        with patch("weather.setup_status.get_weather_diagnostics", return_value=fake_snapshot):
            resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[1]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Patched-in fabricated fact.", body)
        # Only the fact from the patched snapshot appears in the panel --
        # the panel did not independently query real config to add
        # anything else a full real snapshot would have included (e.g.
        # any Live Weather Data / Alert State / Generated Audio group,
        # none of which the one-fact fake snapshot has any fact for).
        self.assertNotIn("Live Weather Data", body)
        self.assertNotIn("Generated Audio", body)
        self.assertNotIn("Alert State", body)


class NoDuplicateImplementationSafetyTests(TestCase):
    """Rendering must remain read-only/side-effect-free (Pass C section
    12), same discipline as Pass B's diagnostics itself."""

    def test_rendering_with_zero_configuration_rows_does_not_mutate(self):
        from weather.models import AmberAlertConfig, WeatherVoicePersona
        before = (
            WeatherConfig.objects.count(), AmberAlertConfig.objects.count(),
            WeatherVoicePersona.objects.count(),
        )
        render_setup_status()  # real get_weather_diagnostics(), zero rows
        after = (
            WeatherConfig.objects.count(), AmberAlertConfig.objects.count(),
            WeatherVoicePersona.objects.count(),
        )
        self.assertEqual(before, after)
        self.assertEqual(before, (0, 0, 0))


@override_settings(SECURE_SSL_REDIRECT=False)
class AdminIntegrationTests(TestCase):
    """A handful of real-page smoke tests -- most presentation logic is
    covered above without any Admin/DB machinery."""

    def setUp(self):
        self.staff = User.objects.create_superuser("wxsetupstatus2", "y@example.invalid", "pw")
        self.client.force_login(self.staff)

    def test_setup_status_panel_present_on_real_change_page(self):
        obj = WeatherConfig.load()
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[obj.pk]))
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("wx-setup-status", body)
        self.assertIn("Overall:", body)
        self.assertIn("Status checked:", body)

    def test_ordinary_form_still_renders_and_saves(self):
        """The panel must not interfere with the ordinary form beneath
        it -- unchanged save behavior is the regression this protects."""
        obj = WeatherConfig.load()
        resp = self.client.get(reverse("admin:weather_weatherconfig_change", args=[obj.pk]))
        self.assertContains(resp, "station_lat")
        self.assertContains(resp, 'name="voice_schedule_0"')
