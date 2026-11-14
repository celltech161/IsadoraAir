"""r0137: NWS and AMBER source snapshots feeding the combined announcement.

No network: the NWS session and the IPAWS fetch functions are replaced by doubles, and every snapshot lives in
a private temporary directory.  The coordinator itself is stubbed (covered by test_wx_alert_coordinator)."""
import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))
sys.path.insert(0, str(PROJECT_ROOT))

import wxconfig  # noqa: E402
# Same import ceremony as test_alert_beep_trigger (module-level CFG/DATA_DIR are read at import time).
from weather_ingest.tests.test_alert_beep_trigger import update_local_wx_data  # noqa: E402

with patch.object(wxconfig, "resolve_weather_data_dir",
                  return_value=Path(tempfile.mkdtemp(prefix="isadoraair-amber-import-"))):
    import amber_poll  # noqa: E402
import ipaws  # noqa: E402


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


NOW = datetime.now(timezone.utc)


def feature(event="Tornado Warning", *, etn="0042", phen="TO", sig="W", action="NEW", product_class="O",
            sent=None, expires=None, ends=None, status="Actual", message_type="Alert",
            what="Tornado.", where="Saline County.", instruction="Take shelter now.", headline=None):
    description = f"* WHAT...{what}\n\n* WHERE...{where}"
    return {"properties": {
        "id": f"urn:oid:{event}:{etn}:{sent}",
        "event": event,
        "headline": headline or f"{event} issued November 13 at 4:00PM CST until 6:00PM CST by NWS Topeka KS",
        "description": description,
        "instruction": instruction,
        "sent": iso(sent or NOW - timedelta(minutes=5)),
        "expires": iso(expires or NOW + timedelta(hours=1)),
        "ends": iso(ends) if ends else None,
        "status": status,
        "messageType": message_type,
        "areaDesc": "Saline",
        "parameters": {"VTEC": [f"/{product_class}.{action}.KTOP.{phen}.{sig}.{etn}.261113T2200Z-261113T2345Z/"]},
    }}


class NwsHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-nws-sources-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.coordinator_runs = 0
        self.current_temp_runs = 0
        self.response = None
        targets = {
            "WX_WATCHWARN_FILE": self.tmp / "active_watches_warnings.json",
            "ALERT_STATUS": self.tmp / "alert.txt",
            "ALERT_DESCRIPTION": self.tmp / "warn.txt",
            "ALERT_FINGERPRINT_FILE": self.tmp / "alert_fingerprint.txt",
        }
        patches = [patch.object(update_local_wx_data, name, value) for name, value in targets.items()]
        patches += [
            patch.object(update_local_wx_data.session, "get", side_effect=lambda *a, **k: self.response),
            patch.object(update_local_wx_data, "_run_alert_coordinator", side_effect=self._coordinator),
            patch.object(update_local_wx_data, "_trigger_current_temp", side_effect=self._current_temp),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _coordinator(self):
        self.coordinator_runs += 1

    def _current_temp(self):
        self.current_temp_runs += 1

    def feed(self, *features, status_code=200):
        self.response = Mock(status_code=status_code)
        self.response.json.return_value = {"features": list(features)}

    def cycle(self, *features):
        self.feed(*features)
        update_local_wx_data.update_wx_alerts()
        return self.snapshot()

    def snapshot(self):
        return json.loads((self.tmp / "active_watches_warnings.json").read_text())


class NwsSnapshotTests(NwsHarness):
    def test_entries_carry_identity_and_authoritative_validity_additively(self):
        ends = NOW + timedelta(hours=3)
        entry, = self.cycle(feature(ends=ends))
        self.assertEqual(entry["key"], "KTOP.TO.W.0042")
        self.assertEqual(entry["ends"], iso(ends))
        self.assertTrue(entry["expires"])
        for key in ("event", "text", "text_core"):
            self.assertIn(key, entry)
        self.assertNotIn("Take shelter now.", entry["text_core"])   # text_core semantics unchanged
        self.assertIn("Take shelter now.", entry["text"])

    def test_a_description_change_under_an_unchanged_headline_changes_the_spoken_text(self):
        first, = self.cycle(feature(what="A confirmed tornado."))
        second, = self.cycle(feature(what="A large and extremely dangerous tornado.", sent=NOW))
        self.assertNotEqual(first["text"], second["text"])
        self.assertEqual(self.current_temp_runs, 0)                   # headline fingerprint unchanged

    def test_an_advisory_only_change_leaves_the_spoken_snapshot_byte_identical(self):
        warning = feature()
        self.cycle(warning)
        before = (self.tmp / "active_watches_warnings.json").read_bytes()
        advisory = feature("Wind Advisory", phen="WI", sig="Y", etn="0007")
        self.cycle(warning, advisory)
        self.assertEqual((self.tmp / "active_watches_warnings.json").read_bytes(), before)
        self.assertEqual(self.current_temp_runs, 1)                   # current_temp behavior is preserved

    def test_a_cancel_message_supersedes_the_earlier_issuance(self):
        issued = feature(sent=NOW - timedelta(minutes=20))
        self.assertEqual(len(self.cycle(issued)), 1)
        cancel = feature(sent=NOW - timedelta(minutes=1), action="CAN", message_type="Cancel")
        self.assertEqual(self.cycle(issued, cancel), [])

    def test_ended_actions_test_status_and_non_operational_products_are_never_spoken(self):
        cases = {
            "VTEC CAN": feature(action="CAN"),
            "VTEC EXP": feature(action="EXP"),
            "VTEC UPG": feature(action="UPG"),
            "messageType Cancel": feature(message_type="Cancel"),
            "status Test": feature(status="Test"),
            "status Exercise": feature(status="Exercise"),
            "test product class": feature(product_class="T"),
        }
        for label, feat in cases.items():
            with self.subTest(case=label):
                self.assertEqual(self.cycle(feat), [])

    def test_an_already_ended_alert_is_omitted(self):
        self.assertEqual(self.cycle(feature(expires=NOW - timedelta(minutes=1))), [])

    def test_spoken_order_is_deterministic_regardless_of_feed_order(self):
        tornado = feature("Tornado Warning", etn="0042")
        blizzard = feature("Blizzard Warning", phen="BZ", etn="0003", what="Heavy snow.")
        watch = feature("Tornado Watch", phen="TO", sig="A", etn="0101", what="Conditions favorable.")
        first = self.cycle(tornado, watch, blizzard)
        second = self.cycle(watch, blizzard, tornado)
        self.assertEqual(first, second)
        self.assertEqual([e["event"] for e in first], ["Blizzard Warning", "Tornado Warning", "Tornado Watch"])

    def test_a_failed_fetch_preserves_the_snapshot_and_still_runs_the_coordinator(self):
        self.cycle(feature())
        before = (self.tmp / "active_watches_warnings.json").read_bytes()
        self.feed(status_code=503)
        update_local_wx_data.update_wx_alerts()
        with patch.object(update_local_wx_data.session, "get", side_effect=requests.ConnectionError("down")):
            update_local_wx_data.update_wx_alerts()
        self.assertEqual((self.tmp / "active_watches_warnings.json").read_bytes(), before)
        self.assertEqual(self.coordinator_runs, 3)

    def test_the_coordinator_runs_every_cycle_and_before_current_temp(self):
        order = []
        with patch.object(update_local_wx_data, "_run_alert_coordinator", side_effect=lambda: order.append("coord")), \
             patch.object(update_local_wx_data, "_trigger_current_temp", side_effect=lambda: order.append("temp")):
            self.cycle(feature())                         # first run: fingerprint baseline only
            self.cycle(feature())                         # unchanged
            self.cycle(feature(), feature("Tornado Watch", sig="A", etn="0101"))
        self.assertEqual(order, ["coord", "coord", "coord", "temp"])


def cap(identifier, *, status="Actual", msg_type="Alert", references=(), expires=None, description="Child taken."):
    return {
        "identifier": identifier, "sent": iso(NOW - timedelta(minutes=5)),
        "expires": iso(expires or NOW + timedelta(hours=4)), "sender": "test", "sender_name": "KBI",
        "status": status, "msg_type": msg_type, "references": list(references),
        "event": "Child Abduction Emergency", "event_code": "CAE", "headline": "AMBER Alert",
        "description": description, "instruction": "Call 911.", "areas": ["020169"],
    }


class AmberHarness(unittest.TestCase):
    CFG = {"enabled": True, "ipaws_base_url": "https://example.invalid", "event_codes": ["CAE"],
           "same_codes": ["020169"], "include_instruction_in_forecast": True}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-amber-sources-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.active = self.tmp / "active_amber_alerts.json"
        self.caps = {}
        self.cfg = dict(self.CFG)
        patches = [
            patch.object(amber_poll, "ACTIVE_FILE", self.active),
            patch.object(amber_poll, "LOCKFILE", str(self.tmp / "poll.lock")),
            patch.object(amber_poll, "load_amber_alert_config", side_effect=lambda: self.cfg),
            patch.object(amber_poll, "fetch_feed_entries", side_effect=self._feed),
            patch.object(amber_poll, "fetch_and_parse_cap", side_effect=self._cap),
            patch.object(amber_poll, "_run_alert_coordinator"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.coordinator = amber_poll._run_alert_coordinator

    def _feed(self, base_url):
        return [{"id": link, "link": link, "event": "CAE", "statefips": "20", "updated": None} for link in self.caps]

    def _cap(self, link):
        value = self.caps[link]
        if isinstance(value, Exception):
            raise value
        return value

    def poll(self):
        amber_poll.main()
        return json.loads(self.active.read_text())


class AmberSnapshotTests(AmberHarness):
    def test_active_alert_snapshot(self):
        self.caps = {"l1": cap("CAP-1")}
        entries = self.poll()
        self.assertEqual([e["identifier"] for e in entries], ["CAP-1"])
        self.assertEqual(self.coordinator.call_count, 1)

    def test_a_failed_individual_cap_fetch_keeps_the_previous_snapshot(self):
        self.caps = {"l1": cap("CAP-1"), "l2": cap("CAP-2")}
        before = self.poll()
        self.caps["l2"] = ipaws.CapFetchError("timeout")
        self.assertEqual(self.poll(), before)                         # not a partial, authoritative set
        self.assertEqual(self.coordinator.call_count, 2)              # expirations still apply

    def test_a_feed_failure_keeps_the_previous_snapshot_and_runs_the_coordinator(self):
        self.caps = {"l1": cap("CAP-1")}
        before = self.poll()
        with patch.object(amber_poll, "fetch_feed_entries", side_effect=requests.ConnectionError("down")):
            self.assertEqual(self.poll(), before)
        self.assertEqual(self.coordinator.call_count, 2)

    def test_disabled_amber_writes_a_confirmed_empty_set(self):
        self.caps = {"l1": cap("CAP-1")}
        self.poll()
        self.cfg = {"enabled": False}
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.coordinator.call_count, 2)

    def test_an_actual_cancel_without_info_removes_the_alert_through_the_poller(self):
        self.caps = {"l1": cap("CAP-1")}
        self.assertEqual(len(self.poll()), 1)
        cancel = dict(cap("CAN-1", msg_type="Cancel", references=["CAP-1"]),
                      event="", event_code="", areas=[], expires="", description="", instruction="")
        self.caps = {"cancel": cancel, "l1": cap("CAP-1")}
        self.assertEqual(self.poll(), [])

    def test_cancel_update_supersession_and_non_actual_status(self):
        self.caps = {
            "orig": cap("CAP-1", description="Original text."),
            "upd": cap("CAP-2", msg_type="Update", references=["CAP-1"], description="Updated text."),
            "gone": cap("CAP-3"),
            "cancel": cap("CAP-4", msg_type="Cancel", references=["CAP-3"]),
            "test": cap("CAP-5", status="Test"),
            "exercise": cap("CAP-6", status="Exercise"),
            "ack": cap("CAP-7", msg_type="Ack"),
            "expired": cap("CAP-8", expires=NOW - timedelta(minutes=1)),
            "test-cancel": cap("CAP-9", status="Test", msg_type="Cancel", references=["CAP-2"]),
        }
        entries = self.poll()
        self.assertEqual([e["identifier"] for e in entries], ["CAP-2"])
        self.assertIn("Updated text.", entries[0]["text"])

    def test_an_uninterpretable_qualifying_message_keeps_the_previous_complete_snapshot(self):
        """Finding 4 (replaces the r0137 test that skipped it): a retrieved but unusable qualifying CAP message
        may be the active alert or the Cancel that retires one -- never commit the rest as authoritative."""
        self.caps = {"l1": cap("CAP-1"), "l2": cap("CAP-2")}
        before = self.poll()
        for label, failure in (("malformed XML", ipaws.CapParseError("unparseable")),
                               ("missing mandatory field", ipaws.CapParseError("lacks mandatory")),
                               ("malformed references", ipaws.CapParseError("malformed references"))):
            with self.subTest(case=label):
                self.caps = {"l1": cap("CAP-1"), "l2": failure, "l3": cap("CAP-3")}
                self.assertEqual(self.poll(), before)
        self.assertEqual(self.coordinator.call_count, 4)          # still runs: known expirations keep applying

    def test_an_irrelevant_parsed_message_does_not_block_the_snapshot(self):
        self.caps = {"l1": cap("CAP-1"), "test": cap("CAP-9", status="Test")}
        self.assertEqual([e["identifier"] for e in self.poll()], ["CAP-1"])


CAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>CAP-2</identifier><sender>kbi@ks</sender><sent>2026-11-13T16:00:00-06:00</sent>
  <status>Actual</status><msgType>Update</msgType><scope>Public</scope>
  <references>kbi@ks,CAP-1,2026-11-13T15:00:00-06:00 kbi@ks,CAP-0,2026-11-13T14:00:00-06:00</references>
  <info><language>en-US</language><event>Child Abduction Emergency</event>
    <eventCode><valueName>SAME</valueName><value>CAE</value></eventCode>
    <expires>2026-11-14T16:00:00-06:00</expires><senderName>KBI</senderName><headline>AMBER Alert</headline>
    <description>Child taken.</description><instruction>Call 911.</instruction>
    <area><areaDesc>Saline</areaDesc><geocode><valueName>SAME</valueName><value>020169</value></geocode></area>
  </info>
</alert>"""


def cap_xml(identifier, *, status="Actual", msg_type="Alert", references=None, info=True, sent=True,
            expires="2026-11-14T16:00:00-06:00", description="Child taken."):
    refs = f"<references>{references}</references>" if references is not None else ""
    sent_el = "<sent>2026-11-13T16:00:00-06:00</sent>" if sent else ""
    info_el = f"""<info><language>en-US</language><event>Child Abduction Emergency</event>
    <eventCode><valueName>SAME</valueName><value>CAE</value></eventCode>
    <expires>{expires}</expires><senderName>KBI</senderName><headline>AMBER Alert</headline>
    <description>{description}</description><instruction>Call 911.</instruction>
    <area><areaDesc>Saline</areaDesc><geocode><valueName>SAME</valueName><value>020169</value></geocode></area>
  </info>""" if info else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>{identifier}</identifier><sender>kbi@ks</sender>{sent_el}
  <status>{status}</status><msgType>{msg_type}</msgType><scope>Public</scope>{refs}
  {info_el}
</alert>""".encode()


def ref(*identifiers):
    return " ".join(f"kbi@ks,{i},2026-11-13T15:00:00-06:00" for i in identifiers)


class CapAuthorityTests(unittest.TestCase):
    """Finding 3: root fields are parsed without <info>; only Actual messages cancel or supersede."""

    def parse(self, xml):
        with patch.object(ipaws, "_http_get", return_value=xml):
            return ipaws.fetch_and_parse_cap("https://example.invalid/cap")

    def current(self, *xmls):
        return [c["identifier"] for c in ipaws.current_alerts([self.parse(x) for x in xmls])]

    def test_an_actual_cancel_without_info_is_parsed_from_its_root_fields(self):
        parsed = self.parse(cap_xml("CAN-1", msg_type="Cancel", references=ref("CAP-1"), info=False))
        self.assertEqual((parsed["identifier"], parsed["status"], parsed["msg_type"], parsed["references"]),
                         ("CAN-1", "Actual", "Cancel", ["CAP-1"]))
        self.assertEqual((parsed["event_code"], parsed["areas"], parsed["expires"]), ("", [], ""))

    def test_an_actual_cancel_retires_the_referenced_alert_in_either_feed_order(self):
        alert = cap_xml("CAP-1")
        cancel = cap_xml("CAN-1", msg_type="Cancel", references=ref("CAP-1"), info=False)
        self.assertEqual(self.current(alert, cancel), [])
        self.assertEqual(self.current(cancel, alert), [])

    def test_an_actual_update_supersedes_the_earlier_issuance(self):
        self.assertEqual(self.current(cap_xml("CAP-1"), cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1"))),
                         ["CAP-2"])

    def test_a_test_cancel_or_exercise_update_never_removes_an_actual_alert(self):
        alert = cap_xml("CAP-1")
        test_cancel = cap_xml("CAN-T", status="Test", msg_type="Cancel", references=ref("CAP-1"), info=False)
        exercise_update = cap_xml("UPD-X", status="Exercise", msg_type="Update", references=ref("CAP-1"))
        self.assertEqual(self.current(alert, test_cancel), ["CAP-1"])
        self.assertEqual(self.current(exercise_update, alert), ["CAP-1"])

    def test_chained_updates_in_any_order_leave_only_the_newest(self):
        orig = cap_xml("CAP-1")
        u1 = cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1"))
        u2 = cap_xml("CAP-3", msg_type="Update", references=ref("CAP-2"))
        for order in ((orig, u1, u2), (u2, orig, u1), (u1, u2, orig)):
            with self.subTest(order=[o[100:140] for o in order]):
                self.assertEqual(self.current(*order), ["CAP-3"])

    def test_messages_that_cannot_be_interpreted_raise_cap_parse_error(self):
        cases = {
            "malformed XML": b"<alert><identifier>x",
            "missing sent": cap_xml("CAP-1", sent=False),
            "unknown status": cap_xml("CAP-1", status="Bogus"),
            "malformed references": cap_xml("CAP-2", msg_type="Update", references="kbi@ks;CAP-1"),
            "actual alert without info": cap_xml("CAP-1", info=False),
            "actual cancel referencing nothing": cap_xml("CAN-1", msg_type="Cancel", info=False),
        }
        for label, xml in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ipaws.CapParseError):
                    self.parse(xml)

    def test_a_non_actual_message_without_info_is_parsed_and_irrelevant(self):
        parsed = self.parse(cap_xml("T-1", status="Test", info=False))
        self.assertEqual(ipaws.current_alerts([parsed]), [])


class CapParsingTests(unittest.TestCase):
    def test_status_msgtype_and_references_are_parsed(self):
        with patch.object(ipaws, "_http_get", return_value=CAP_XML.encode()):
            parsed = ipaws.fetch_and_parse_cap("https://example.invalid/cap")
        self.assertEqual((parsed["status"], parsed["msg_type"]), ("Actual", "Update"))
        self.assertEqual(parsed["references"], ["CAP-1", "CAP-0"])

    def test_a_transport_failure_raises_instead_of_looking_like_a_cancellation(self):
        with patch.object(ipaws, "_http_get", side_effect=requests.Timeout("slow")):
            with self.assertRaises(ipaws.CapFetchError):
                ipaws.fetch_and_parse_cap("https://example.invalid/cap")

    def test_unparseable_xml_raises_instead_of_looking_like_absence(self):
        with patch.object(ipaws, "_http_get", return_value=b"<not-cap"):
            with self.assertRaises(ipaws.CapParseError):
                ipaws.fetch_and_parse_cap("https://example.invalid/cap")


class CoordinatorInvocationTests(unittest.TestCase):
    """Both pollers run the ONE coordinator; nothing runs the retired AMBER-only publisher."""

    def test_pollers_invoke_wx_alert_py_and_never_amber_alert_py(self):
        for module in (update_local_wx_data, amber_poll):
            source = Path(module.__file__).read_text()
            with self.subTest(module=module.__name__):
                self.assertIn('"wx_alert.py"', source)
                self.assertNotIn('"amber_alert.py"', source)
        with patch.object(amber_poll.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            amber_poll._run_alert_coordinator()
        self.assertTrue(run.call_args.args[0][-1].endswith("/wx_alert.py"))


if __name__ == "__main__":
    unittest.main()
