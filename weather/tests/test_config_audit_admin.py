from types import SimpleNamespace
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import User
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import FXCart
from monitoring.models import SystemEvent
from weather.admin import (
    AmberAlertConfigAdmin,
    WeatherConfigAdmin,
    WeatherVoicePersonaAdmin,
    _ALERT_EVENT_AUDIT_MAX_ITEMS,
    _AMBER_CONFIG_AUDIT_FIELDS,
    _NWS_APPLY_FIELDS,
    _WEATHER_CONFIG_AUDIT_FIELDS,
)
from weather.models import AmberAlertConfig, WeatherConfig, WeatherVoicePersona
from weather.nws_discovery import NWSDiscoveryError


PRIVATE_EMAIL = "private-weather-operator@example.invalid"
PRIVATE_IPAWS_URL = (
    "https://operator:private-password@ipaws.example.invalid/"
    "service?token=private-query-token"
)


def _request():
    return SimpleNamespace(
        user=SimpleNamespace(get_username=lambda: "weather-audit-operator")
    )


def _audit_events(object_type=None):
    events = [
        event
        for event in SystemEvent.objects.order_by("pk")
        if event.detail.get("event_type") == "configuration_change"
    ]
    if object_type is not None:
        events = [
            event
            for event in events
            if event.detail.get("object_type") == object_type
        ]
    return events


class WeatherConfigAuditTests(TestCase):
    object_type = "weather.WeatherConfig"

    def setUp(self):
        self.admin = WeatherConfigAdmin(
            WeatherConfig, django_admin.AdminSite()
        )
        self.config = WeatherConfig.load()

    def _save(self, updates=None, *, change=True, execute=True):
        updates = updates or {}
        for field, value in updates.items():
            setattr(self.config, field, value)
        callbacks_context = self.captureOnCommitCallbacks(execute=execute)
        with callbacks_context as callbacks:
            self.admin.save_model(
                _request(),
                self.config,
                SimpleNamespace(changed_data=list(updates)),
                change=change,
            )
        return callbacks

    def test_allowlist_covers_all_database_config_fields_only(self):
        model_fields = {
            field.name
            for field in WeatherConfig._meta.concrete_fields
            if field.name != "id"
        }
        self.assertEqual(set(_WEATHER_CONFIG_AUDIT_FIELDS), model_fields)
        self.assertNotIn("WEATHER_DATA_DIR", _WEATHER_CONFIG_AUDIT_FIELDS)

    def test_location_and_weather_ingest_fields_have_cycle_metadata(self):
        cases = (
            ("station_lat", 40.123),
            ("station_lon", -98.456),
            ("sun_alt_threshold_deg", 4.5),
            ("nws_alert_zone", "KSC169"),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self._save({field: value})
                detail = _audit_events(self.object_type)[0].detail
                self.assertEqual(detail["changed_fields"], [field])
                self.assertEqual(
                    detail["apply_modes"][field], "next_weather_ingest_cycle"
                )
                SystemEvent.objects.all().delete()

    def test_normal_admin_nws_field_uses_forecast_cycle(self):
        self._save({"nws_forecast_office": "ICT"})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(detail["changed_fields"], ["nws_forecast_office"])
        self.assertEqual(
            detail["apply_modes"],
            {"nws_forecast_office": "next_forecast_cycle"},
        )

    def test_metar_station_change_retains_normalized_station_list(self):
        self._save({"nws_cloud_stations": " KICT, KHUT "})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(detail["changed_fields"], ["nws_cloud_stations"])
        self.assertEqual(
            detail["changes"]["nws_cloud_stations"]["new"],
            ["KICT", "KHUT"],
        )
        self.assertEqual(
            detail["apply_modes"]["nws_cloud_stations"],
            "next_weather_ingest_cycle",
        )

    def test_voice_schedule_change_uses_bounded_validated_shape(self):
        WeatherVoicePersona.objects.create(slot="night", display_name="Night")
        schedule = [["default", 0, 11], ["night", 12, 23]]
        self._save({"voice_schedule": schedule})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(
            detail["changes"]["voice_schedule"]["new"], schedule
        )
        self.assertEqual(
            detail["apply_modes"]["voice_schedule"],
            "next_voice_schedule_evaluation",
        )

    def test_alert_beep_master_interval_and_apply_modes(self):
        cases = (
            ("alert_sound_enabled", False),
            ("alert_sound_interval_seconds", 900),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self._save({field: value})
                detail = _audit_events(self.object_type)[0].detail
                self.assertEqual(detail["changed_fields"], [field])
                self.assertEqual(
                    detail["apply_modes"][field],
                    "next_alert_beep_evaluation",
                )
                SystemEvent.objects.all().delete()

    def test_alert_beep_cart_uses_only_stable_id_and_name(self):
        cart = FXCart.objects.create(
            name="Severe Weather Beep",
            filepath="/private/runtime/path/never-audit.wav",
            gain_db=-4.0,
        )
        self._save({"alert_sound_cart": cart})
        event = _audit_events(self.object_type)[0]
        self.assertEqual(
            event.detail["changes"]["alert_sound_cart"]["new"],
            {"id": cart.pk, "name": "Severe Weather Beep"},
        )
        retained = repr((event.detail, event.dedupe_key))
        self.assertNotIn(cart.filepath, retained)
        self.assertNotIn("gain_db", retained)

    def test_alert_event_names_use_bounded_normalized_summary(self):
        events = [
            f"Qualifying Weather Event {index:02d} " + ("X" * 150)
            for index in range(_ALERT_EVENT_AUDIT_MAX_ITEMS + 7)
        ]
        self._save({"alert_sound_trigger_events": events})
        event = _audit_events(self.object_type)[0]
        summary = event.detail["changes"]["alert_sound_trigger_events"]["new"]
        self.assertEqual(summary["count"], len(events))
        self.assertEqual(len(summary["events"]), _ALERT_EVENT_AUDIT_MAX_ITEMS)
        self.assertTrue(summary["truncated"])
        self.assertTrue(all(len(value) <= 120 for value in summary["events"]))
        self.assertNotIn(events[-1], repr(event.detail))
        self.assertEqual(
            event.detail["apply_modes"]["alert_sound_trigger_events"],
            "runtime_adoption_not_confirmed",
        )

    def test_notify_email_is_fully_redacted(self):
        self._save({"notify_email": PRIVATE_EMAIL})
        event = _audit_events(self.object_type)[0]
        detail = event.detail
        self.assertEqual(detail["changed_fields"], ["notify_email"])
        self.assertEqual(detail["redacted_fields"], ["notify_email"])
        self.assertNotIn("notify_email", detail["changes"])
        self.assertNotIn(PRIVATE_EMAIL, repr((detail, event.dedupe_key)))

    def test_multiple_field_save_is_one_event_with_grouped_apply_modes(self):
        self._save({
            "station_lat": 38.5,
            "nws_forecast_grid_x": 22,
            "alert_sound_enabled": False,
        })
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["station_lat", "nws_forecast_grid_x", "alert_sound_enabled"],
        )
        self.assertEqual(
            events[0].detail["apply_modes"],
            {
                "station_lat": "next_weather_ingest_cycle",
                "nws_forecast_grid_x": "next_forecast_cycle",
                "alert_sound_enabled": "next_alert_beep_evaluation",
            },
        )

    def test_noop_and_effectively_equivalent_values_emit_none(self):
        self._save()
        self.assertEqual(_audit_events(self.object_type), [])
        self._save({"nws_cloud_stations": " KCNK , KSLN "})
        self.assertEqual(_audit_events(self.object_type), [])
        self._save({
            "alert_sound_trigger_events": [
                "TORNADO WARNING",
                "Severe Thunderstorm Warning",
                "Tornado Watch",
                "Severe Thunderstorm Watch",
                "tornado warning",
            ],
        })
        self.assertEqual(_audit_events(self.object_type), [])

    def test_event_is_absent_before_commit_callback(self):
        callbacks = self._save({"station_lat": 39.5}, execute=False)
        self.assertEqual(_audit_events(self.object_type), [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(_audit_events(self.object_type)), 1)

    def test_rollback_restores_config_and_emits_none(self):
        original = self.config.station_lon
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.config.station_lon = -100.0
                    self.admin.save_model(
                        _request(), self.config,
                        SimpleNamespace(changed_data=["station_lon"]),
                        change=True,
                    )
                    self.assertEqual(_audit_events(self.object_type), [])
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(self.config.station_lon, original)
        self.assertEqual(_audit_events(self.object_type), [])

    def test_rapid_saves_are_distinct_rows(self):
        self._save({"alert_sound_enabled": False})
        self._save({"alert_sound_enabled": True})
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)
        self.assertEqual([event.repeat_count for event in events], [1, 1])

    def test_emitter_failure_does_not_break_persistence(self):
        self.config.station_lat = 37.75
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                _request(), self.config,
                SimpleNamespace(changed_data=["station_lat"]), change=True,
            )
        self.config.refresh_from_db()
        self.assertEqual(self.config.station_lat, 37.75)

    def test_event_claims_persistence_only_not_runtime_success(self):
        self._save({"alert_sound_enabled": False})
        retained = repr(_audit_events(self.object_type)[0].detail).lower()
        for false_claim in (
            "fetch succeeded", "tts succeeded", "cart played",
            "alert poll succeeded", "email sent",
        ):
            self.assertNotIn(false_claim, retained)

    def test_lazy_creation_is_not_audited_and_explicit_creation_is(self):
        self.config.delete()
        SystemEvent.objects.all().delete()
        WeatherConfig.load()
        self.assertEqual(_audit_events(self.object_type), [])

        WeatherConfig.objects.all().delete()
        self.config = WeatherConfig(voice_schedule=[["default", 0, 23]])
        self._save(change=False)
        event = _audit_events(self.object_type)[0]
        self.assertEqual(event.detail["action"], "create")
        self.assertEqual(
            event.detail["changed_fields"], list(_WEATHER_CONFIG_AUDIT_FIELDS)
        )

    def test_weather_voice_persona_remains_outside_pass(self):
        persona = WeatherVoicePersona(slot="not-audited", display_name="No audit")
        WeatherVoicePersonaAdmin(
            WeatherVoicePersona, django_admin.AdminSite()
        ).save_model(
            _request(), persona,
            SimpleNamespace(changed_data=["display_name"]), change=False,
        )
        self.assertEqual(_audit_events(), [])


@override_settings(SECURE_SSL_REDIRECT=False)
class NWSDiscoveryApplyAuditTests(TestCase):
    object_type = "weather.WeatherConfig"

    def setUp(self):
        self.staff = User.objects.create_superuser(
            "nws-audit-operator", "nws-audit@example.invalid", "pw"
        )
        self.client.force_login(self.staff)
        self.config = WeatherConfig.load()
        self.config.nws_forecast_office = "OLD"
        self.config.nws_forecast_grid_x = 1
        self.config.nws_forecast_grid_y = 2
        self.config.nws_alert_zone = "KSC999"
        self.config.save(update_fields=_NWS_APPLY_FIELDS)
        self.url = reverse("admin:weather_weatherconfig_nws_discovery")

    def _payload(self, **overrides):
        data = {
            "action": "apply",
            "grid_id": "TOP",
            "grid_x": "10",
            "grid_y": "53",
            "county_ugc": "KSC143",
        }
        data.update(overrides)
        return data

    def _apply(self, **overrides):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, self._payload(**overrides))
        self.assertEqual(response.status_code, 302)
        return response

    def test_apply_all_fields_emits_exactly_one_custom_source_event(self):
        self._apply()
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.title, "Weather NWS configuration updated")
        self.assertEqual(event.source, "weather_nws_discovery_apply")
        self.assertEqual(
            event.detail["change_source"], "weather_nws_discovery_apply"
        )
        self.assertEqual(event.detail["changed_by"], "nws-audit-operator")
        self.assertEqual(event.detail["changed_fields"], _NWS_APPLY_FIELDS)
        self.assertEqual(
            event.detail["changes"]["nws_forecast_office"],
            {"old": "OLD", "new": "TOP"},
        )
        self.assertNotEqual(event.title, "Weather configuration updated")

    def test_apply_only_actual_subset_and_noop_emits_none(self):
        self._apply(
            grid_id="OLD", grid_x="1", grid_y="2", county_ugc="KSC143"
        )
        event = _audit_events(self.object_type)[0]
        self.assertEqual(event.detail["changed_fields"], ["nws_alert_zone"])
        SystemEvent.objects.all().delete()

        self._apply(
            grid_id="OLD", grid_x="1", grid_y="2", county_ugc="KSC143"
        )
        self.assertEqual(_audit_events(self.object_type), [])

    def test_apply_is_absent_before_commit_and_rollback_safe(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            response = self.client.post(self.url, self._payload())
            self.assertEqual(response.status_code, 302)
            self.assertEqual(_audit_events(self.object_type), [])
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(_audit_events(self.object_type)), 1)

        SystemEvent.objects.all().delete()
        self.config.refresh_from_db()
        prior = (
            self.config.nws_forecast_office,
            self.config.nws_forecast_grid_x,
            self.config.nws_forecast_grid_y,
            self.config.nws_alert_zone,
        )
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.client.post(
                        self.url,
                        self._payload(
                            grid_id="ICT", grid_x="20", grid_y="30",
                            county_ugc="KSC001",
                        ),
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(
            (
                self.config.nws_forecast_office,
                self.config.nws_forecast_grid_x,
                self.config.nws_forecast_grid_y,
                self.config.nws_alert_zone,
            ),
            prior,
        )
        self.assertEqual(_audit_events(self.object_type), [])

    def test_rapid_apply_actions_are_distinct(self):
        self._apply()
        self._apply(
            grid_id="ICT", grid_x="20", grid_y="30", county_ugc="KSC001"
        )
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)

    def test_emitter_failure_does_not_break_apply_persistence(self):
        with patch(
            "monitoring.services.config_audit.emit_event",
            side_effect=RuntimeError("audit unavailable"),
        ):
            self._apply()
        self.config.refresh_from_db()
        self.assertEqual(self.config.nws_forecast_office, "TOP")
        self.assertEqual(self.config.nws_forecast_grid_x, 10)
        self.assertEqual(self.config.nws_forecast_grid_y, 53)
        self.assertEqual(self.config.nws_alert_zone, "KSC143")
        self.assertEqual(_audit_events(self.object_type), [])

    def test_discovery_preview_and_failure_do_not_audit(self):
        with patch("weather.admin.fetch_nws_point", return_value={
            "grid_id": "TOP", "grid_x": 10, "grid_y": 53,
            "county_ugc": "KSC143", "forecast_zone_ugc": "KSZ004",
        }), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {"action": "discover"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_audit_events(self.object_type), [])

        with patch(
            "weather.admin.fetch_nws_point",
            side_effect=NWSDiscoveryError("discovery failed"),
        ), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(self.url, {"action": "discover"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_audit_events(self.object_type), [])

    def test_invalid_apply_saves_nothing_and_emits_nothing(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url,
                self._payload(grid_id="", grid_x="bad", county_ugc=""),
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(_audit_events(self.object_type), [])


class AmberAlertConfigAuditTests(TestCase):
    object_type = "weather.AmberAlertConfig"

    def setUp(self):
        self.admin = AmberAlertConfigAdmin(
            AmberAlertConfig, django_admin.AdminSite()
        )
        self.config = AmberAlertConfig.load()

    def _save(self, updates=None, *, change=True):
        updates = updates or {}
        for field, value in updates.items():
            setattr(self.config, field, value)
        with self.captureOnCommitCallbacks(execute=True):
            self.admin.save_model(
                _request(), self.config,
                SimpleNamespace(changed_data=list(updates)), change=change,
            )

    def test_allowlist_covers_all_amber_config_fields(self):
        model_fields = {
            field.name
            for field in AmberAlertConfig._meta.concrete_fields
            if field.name != "id"
        }
        self.assertEqual(set(_AMBER_CONFIG_AUDIT_FIELDS), model_fields)

    def test_enabled_cadence_and_instruction_fields_apply_next_poll(self):
        cases = (
            ("enabled", True),
            ("poll_cadence_minutes", 7),
            ("include_instruction_in_forecast", False),
        )
        for field, value in cases:
            with self.subTest(field=field):
                self._save({field: value})
                detail = _audit_events(self.object_type)[0].detail
                self.assertEqual(detail["changed_fields"], [field])
                self.assertEqual(
                    detail["apply_modes"][field], "next_amber_alert_poll"
                )
                SystemEvent.objects.all().delete()

    def test_ipaws_url_is_fully_redacted(self):
        self._save({"ipaws_base_url": PRIVATE_IPAWS_URL})
        event = _audit_events(self.object_type)[0]
        self.assertEqual(event.detail["changed_fields"], ["ipaws_base_url"])
        self.assertEqual(event.detail["redacted_fields"], ["ipaws_base_url"])
        self.assertNotIn("ipaws_base_url", event.detail["changes"])
        self.assertNotIn(
            PRIVATE_IPAWS_URL, repr((event.detail, event.dedupe_key))
        )

    def test_event_codes_are_normalized_bounded_and_useful(self):
        self._save({"event_codes": " cae, EVI,cae "})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(
            detail["changes"]["event_codes"]["new"],
            {"count": 2, "codes": ["CAE", "EVI"]},
        )

    def test_same_codes_are_normalized_bounded_and_useful(self):
        self._save({"same_codes": "020169, 020000,020169"})
        detail = _audit_events(self.object_type)[0].detail
        self.assertEqual(
            detail["changes"]["same_codes"]["new"],
            {"count": 2, "codes": ["020000", "020169"]},
        )

    def test_multiple_fields_emit_one_event(self):
        self._save({
            "enabled": True,
            "event_codes": "CAE",
            "poll_cadence_minutes": 9,
        })
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            events[0].detail["changed_fields"],
            ["enabled", "event_codes", "poll_cadence_minutes"],
        )
        self.assertEqual(
            set(events[0].detail["apply_modes"].values()),
            {"next_amber_alert_poll"},
        )

    def test_noop_and_equivalent_code_reordering_emit_none(self):
        self._save()
        self.assertEqual(_audit_events(self.object_type), [])
        self._save({"event_codes": "MEP, CAE, BLU"})
        self.assertEqual(_audit_events(self.object_type), [])

    def test_rollback_and_rapid_saves(self):
        original = self.config.poll_cadence_minutes
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    self.config.poll_cadence_minutes = 11
                    self.admin.save_model(
                        _request(), self.config,
                        SimpleNamespace(changed_data=["poll_cadence_minutes"]),
                        change=True,
                    )
                    raise RuntimeError("force rollback")
            except RuntimeError:
                pass
        self.config.refresh_from_db()
        self.assertEqual(self.config.poll_cadence_minutes, original)
        self.assertEqual(_audit_events(self.object_type), [])

        self._save({"poll_cadence_minutes": 6})
        self._save({"poll_cadence_minutes": 8})
        events = _audit_events(self.object_type)
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0].dedupe_key, events[1].dedupe_key)

    def test_event_never_claims_upstream_or_alert_success(self):
        self._save({"enabled": True})
        retained = repr(_audit_events(self.object_type)[0].detail).lower()
        for false_claim in (
            "ipaws reachable", "cap accepted", "amber alert found",
            "speech succeeded", "instruction aired",
        ):
            self.assertNotIn(false_claim, retained)

    def test_lazy_creation_is_not_audited(self):
        self.config.delete()
        AmberAlertConfig.load()
        self.assertEqual(_audit_events(self.object_type), [])

    def test_explicit_admin_creation_is_audited_and_url_is_redacted(self):
        self.config.delete()
        self.config = AmberAlertConfig(ipaws_base_url=PRIVATE_IPAWS_URL)
        self._save(change=False)
        event = _audit_events(self.object_type)[0]
        self.assertEqual(event.detail["action"], "create")
        self.assertEqual(
            event.detail["changed_fields"], list(_AMBER_CONFIG_AUDIT_FIELDS)
        )
        self.assertEqual(event.detail["redacted_fields"], ["ipaws_base_url"])
        self.assertNotIn(
            PRIVATE_IPAWS_URL, repr((event.detail, event.dedupe_key))
        )
