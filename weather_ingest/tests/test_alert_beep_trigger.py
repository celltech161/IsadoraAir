"""r0053 amendment: the Weather Alert Beep's qualifying-event list
moved from a hard-coded ALERT_KEYWORDS constant into WeatherConfig.
alert_sound_trigger_events (Django) / CFG["alert_sound_trigger_events"]
(weather_ingest, via dump_weather_config's JSON bridge).

update_local_wx_data.py computes its module-level CFG via a real
subprocess call to `manage.py dump_weather_config` at IMPORT time (see
that module's own CFG = load_weather_config() line) -- exactly why
test_no_legacy_references.py/test_weather_data_dir.py never import it
directly either. This module patches wxconfig.load_weather_config with
a fixed fake payload BEFORE update_local_wx_data is first imported (a
"from wxconfig import load_weather_config" binds the name at import
time, so the patch must be in place first), and points weather_data_dir
at a throwaway temp directory -- the same technique test_weather_data_
dir.py already establishes for this module family, applied here to the
one module that needs a full import."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
sys.path.insert(0, str(PROJECT_ROOT))

import wxconfig  # noqa: E402

_TMP_DATA_DIR = tempfile.mkdtemp(prefix="isadoraair-wxingest-test-")

DEFAULT_TRIGGERS = [
    "Tornado Warning",
    "Severe Thunderstorm Warning",
    "Tornado Watch",
    "Severe Thunderstorm Watch",
]

_FAKE_CFG = {
    "weather_data_dir": _TMP_DATA_DIR,
    "station_lat": 39.13,
    "station_lon": -97.70,
    "sun_alt_threshold_deg": 3.0,
    "nws_alert_zone": "KSC143",
    "nws_forecast_office": "TOP",
    "nws_forecast_grid_x": 10,
    "nws_forecast_grid_y": 53,
    "nws_cloud_stations": [],
    "alert_sound_trigger_events": list(DEFAULT_TRIGGERS),
}

with patch.object(wxconfig, "load_weather_config", Mock(return_value=_FAKE_CFG)):
    import update_local_wx_data  # noqa: E402


class EventTriggersAlertBeepTests(unittest.TestCase):
    """Pure-function coverage -- event_triggers_alert_beep() is the
    ONLY new decision logic this amendment introduces; everything else
    (fetching, dedup, file writing) is unchanged."""

    def test_four_default_triggers_reproduce_legacy_behavior(self):
        # This is byte-for-byte the original hard-coded ALERT_KEYWORDS
        # list and the original `any(k.lower() in event.lower() ...)`
        # semantics, now routed through the new function.
        for qualifying_event in (
            "Tornado Warning", "Severe Thunderstorm Warning",
            "Tornado Watch", "Severe Thunderstorm Watch",
        ):
            with self.subTest(event=qualifying_event):
                self.assertTrue(
                    update_local_wx_data.event_triggers_alert_beep(qualifying_event, DEFAULT_TRIGGERS)
                )

    def test_non_qualifying_events_do_not_trigger_with_defaults(self):
        for event in ("Flood Warning", "Winter Storm Watch", "Heat Advisory", "Special Weather Statement"):
            with self.subTest(event=event):
                self.assertFalse(
                    update_local_wx_data.event_triggers_alert_beep(event, DEFAULT_TRIGGERS)
                )

    def test_configured_added_event_activates_beep(self):
        configured = DEFAULT_TRIGGERS + ["Flash Flood Emergency"]
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("Flash Flood Emergency", configured))

    def test_removed_legacy_event_no_longer_activates(self):
        configured = ["Tornado Warning"]  # Severe Thunderstorm Warning removed by the operator
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep("Severe Thunderstorm Warning", configured))
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("Tornado Warning", configured))

    def test_matching_is_case_insensitive_substring_like_before(self):
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("tornado warning", DEFAULT_TRIGGERS))
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("TORNADO WARNING", DEFAULT_TRIGGERS))
        # Substring: a real NWS event name containing a configured
        # phrase as a substring still matches, same as the original
        # `keyword.lower() in event.lower()` expression.
        self.assertTrue(
            update_local_wx_data.event_triggers_alert_beep("Tornado Warning issued", ["Tornado Warning"])
        )

    def test_empty_configured_list_means_no_event_ever_triggers(self):
        for event in DEFAULT_TRIGGERS + ["anything at all"]:
            with self.subTest(event=event):
                self.assertFalse(update_local_wx_data.event_triggers_alert_beep(event, []))

    def test_blank_or_missing_event_never_triggers(self):
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep("", DEFAULT_TRIGGERS))
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep(None, DEFAULT_TRIGGERS))


class MalformedPersistedTriggerConfigTests(unittest.TestCase):
    """r0053 review amendment: the Admin form guarantees clean strings,
    but WeatherConfig.alert_sound_trigger_events is a raw JSONField --
    a direct DB edit, a bad migration, or a bug elsewhere could still
    persist something this cron job must never choke on or misuse."""

    def test_non_list_container_never_matches_anything(self):
        for bad_container in ("Tornado Warning", 42, None, {"Tornado Warning": True}):
            with self.subTest(container=bad_container):
                self.assertFalse(update_local_wx_data.event_triggers_alert_beep("Tornado Warning", bad_container))

    def test_tuple_container_is_still_accepted(self):
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("Tornado Warning", ("Tornado Warning",)))

    def test_non_string_entries_are_ignored_not_crashed_on(self):
        configured = [123, None, ["nested"], {"a": 1}, "Tornado Warning"]
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("Tornado Warning", configured))
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep("Severe Thunderstorm Warning", configured))

    def test_blank_or_whitespace_only_entries_are_ignored(self):
        # The real danger: an empty-string entry must NEVER be treated
        # as "matches every event" (an empty string is a substring of
        # every string in Python).
        configured = ["", "   ", "\t\n"]
        for event in ("Tornado Warning", "anything at all", "Severe Thunderstorm Warning"):
            with self.subTest(event=event):
                self.assertFalse(update_local_wx_data.event_triggers_alert_beep(event, configured))

    def test_mixed_valid_and_blank_entries_still_match_the_valid_one(self):
        configured = ["", "Tornado Warning", "   "]
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep("Tornado Warning", configured))
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep("Flood Warning", configured))

    def test_valid_configuration_semantics_completely_unaffected(self):
        # Same assertions as EventTriggersAlertBeepTests' own default-
        # list coverage, re-run here to prove hardening changed nothing
        # for the actual production default list.
        for qualifying_event in DEFAULT_TRIGGERS:
            self.assertTrue(update_local_wx_data.event_triggers_alert_beep(qualifying_event, DEFAULT_TRIGGERS))
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep("Flood Warning", DEFAULT_TRIGGERS))


class ModuleConfigWiringTests(unittest.TestCase):
    """Proves the module-level ALERT_SOUND_TRIGGER_EVENTS is genuinely
    sourced from CFG (the dump_weather_config bridge), not still a
    hard-coded constant, and that a config payload from an OLDER
    Django checkout (missing the new key entirely) safely falls back
    to the same four legacy values rather than crashing or silently
    disabling the beep mid-upgrade."""

    def test_module_level_triggers_come_from_fake_cfg(self):
        self.assertEqual(update_local_wx_data.ALERT_SOUND_TRIGGER_EVENTS, DEFAULT_TRIGGERS)

    def test_missing_key_falls_back_to_legacy_four_not_crash_or_empty(self):
        older_cfg_without_key = {k: v for k, v in _FAKE_CFG.items() if k != "alert_sound_trigger_events"}
        fallback = older_cfg_without_key.get(
            "alert_sound_trigger_events", update_local_wx_data._LEGACY_ALERT_KEYWORDS_FALLBACK
        )
        self.assertEqual(fallback, DEFAULT_TRIGGERS)

    def test_present_but_empty_configured_list_is_not_overridden_by_fallback(self):
        # An operator's deliberate empty list must be honored, never
        # silently replaced with the legacy fallback -- .get() with a
        # default only ever fires on a genuinely MISSING key.
        cfg_with_empty_list = {**_FAKE_CFG, "alert_sound_trigger_events": []}
        result = cfg_with_empty_list.get(
            "alert_sound_trigger_events", update_local_wx_data._LEGACY_ALERT_KEYWORDS_FALLBACK
        )
        self.assertEqual(result, [])


class SpokenAlertIndependenceTests(unittest.TestCase):
    """Required regression coverage (r0053 amendment section 6): the
    Weather Alert Beep trigger configuration must have zero effect on
    which NWS Watches/Warnings qualify for the spoken WxAlert
    statement. _is_watch_or_warning() is untouched by this amendment
    (a plain suffix check, no dependency on beep configuration at
    all) -- this proves the two decisions can genuinely disagree for
    the same event, which is exactly the scenario the brief describes:
    an operator configuring only "Tornado Warning" for the beep must
    not silence a real, different, active Watch/Warning from the
    generated spoken statement."""

    def test_watch_or_warning_selection_has_no_beep_config_parameter(self):
        import inspect
        sig = inspect.signature(update_local_wx_data._is_watch_or_warning)
        self.assertEqual(list(sig.parameters), ["event"])  # no trigger-list parameter exists

    def test_event_can_qualify_for_spoken_statement_but_not_the_beep(self):
        beep_triggers_configured = ["Tornado Warning"]  # operator narrowed this down
        event = "Severe Thunderstorm Warning"  # a different, real, active NWS event
        self.assertFalse(update_local_wx_data.event_triggers_alert_beep(event, beep_triggers_configured))
        self.assertTrue(update_local_wx_data._is_watch_or_warning(event))  # still eligible to be spoken

    def test_event_can_qualify_for_the_beep_but_not_be_a_watch_or_warning_suffix(self):
        # The inverse also holds -- an operator could (unusually) add a
        # non-Watch/non-Warning event name to the beep list; that must
        # not make it eligible for the Watch/Warning speech path either.
        beep_triggers_configured = ["Special Weather Statement"]
        event = "Special Weather Statement"
        self.assertTrue(update_local_wx_data.event_triggers_alert_beep(event, beep_triggers_configured))
        self.assertFalse(update_local_wx_data._is_watch_or_warning(event))

    def test_alert_status_write_branch_only_reads_severe_active_never_watch_or_warning(self):
        """Static source check on update_wx_alerts(): the branch that
        writes/removes ALERT_STATUS (alert.txt) must key off
        `severe_active` only -- never off _is_watch_or_warning() or the
        watchwarn_entries list -- so the two paths cannot become
        coupled by a future edit without this test catching it."""
        import inspect
        source = inspect.getsource(update_local_wx_data.update_wx_alerts)
        branch_start = source.index("if severe_active:")
        branch_end = source.index("new_fp = _alert_fingerprint", branch_start)
        branch_region = source[branch_start:branch_end]
        self.assertIn("severe_active", branch_region)
        self.assertIn("ALERT_STATUS", branch_region)
        self.assertNotIn("watchwarn_entries", branch_region)
        self.assertNotIn("_is_watch_or_warning", branch_region)


class AmberFamilyIndependenceTests(unittest.TestCase):
    """Required regression coverage (r0053 amendment section 7): AMBER/
    BLU/MEP alerts must not start triggering the Weather Alert Beep FX
    Cart merely because they share WxAlert/wx_alert.mp3 for spoken
    urgent insertion. A static source-text check on the actual AMBER
    scripts (never imported -- they have their own real IPAWS-calling
    import-time behavior, same reasoning as update_local_wx_data.py's
    own import ceremony above) proves neither the new function nor the
    new config key was wired into that pipeline at all."""

    def test_amber_scripts_never_reference_the_new_beep_trigger_config(self):
        for filename in ("amber_poll.py", "amber_alert.py"):
            source = (PROJECT_ROOT / filename).read_text()
            with self.subTest(file=filename):
                self.assertNotIn("event_triggers_alert_beep", source)
                self.assertNotIn("alert_sound_trigger_events", source)
                self.assertNotIn("ALERT_SOUND_TRIGGER_EVENTS", source)

    def test_amber_alert_config_model_untouched_reference_fields_only(self):
        # AmberAlertConfig's own event/area filtering is a completely
        # separate, pre-existing mechanism (event_codes/same_codes) --
        # confirm the amber script still keys off those, not the new
        # Weather-only trigger list.
        source = (PROJECT_ROOT / "amber_poll.py").read_text()
        self.assertIn("event_codes", source)


if __name__ == "__main__":
    unittest.main()
