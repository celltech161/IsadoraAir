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


CAP_SENT = (NOW - timedelta(minutes=5)).replace(microsecond=0)      # a CAP dateTime has whole seconds


def cap(identifier, *, status="Actual", msg_type="Alert", references=(), expires=None, description="Child taken.",
        sender="test", sent=None):
    """A parsed CAP message as fetch_and_parse_cap returns it. ``references`` are identifiers of messages by the
    same sender sent at the same time (the helper's default), expressed as complete reference identities."""
    sent = sent or CAP_SENT
    return {
        "identifier": identifier, "sent": iso(sent),
        "expires": iso(expires or NOW + timedelta(hours=4)), "sender": sender, "sender_name": "KBI",
        "status": status, "msg_type": msg_type, "references": list(references),
        "reference_identities": [ipaws.message_identity(sender, r, iso(sent)) for r in references],
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
  <info><language>en-US</language><category>Rescue</category><event>Child Abduction Emergency</event>
    <urgency>Immediate</urgency><severity>Severe</severity><certainty>Likely</certainty>
    <eventCode><valueName>SAME</valueName><value>CAE</value></eventCode>
    <expires>2026-11-14T16:00:00-06:00</expires><senderName>KBI</senderName><headline>AMBER Alert</headline>
    <description>Child taken.</description><instruction>Call 911.</instruction>
    <area><areaDesc>Saline</areaDesc><geocode><valueName>SAME</valueName><value>020169</value></geocode></area>
  </info>
</alert>"""


INFO_DEFAULTS = {"category": "Rescue", "event": "Child Abduction Emergency", "urgency": "Immediate",
                 "severity": "Severe", "certainty": "Likely"}


def cap_xml(identifier, *, status="Actual", msg_type="Alert", references=None, info=True, sent=True,
            expires="2026-11-14T16:00:00-06:00", description="Child taken.", scope="Public",
            info_fields=None, raw_info=None, sender="kbi@ks", sent_at="2026-11-13T16:00:00-06:00"):
    """A CAP 1.2 message. info_fields overrides mandatory <info> elements (None drops one, "" empties it);
    raw_info replaces the whole <info> block (e.g. "<info/>")."""
    refs = f"<references>{references}</references>" if references is not None else ""
    sent_el = f"<sent>{sent_at}</sent>" if sent else ""
    scope_el = f"<scope>{scope}</scope>" if scope is not None else ""
    fields = dict(INFO_DEFAULTS, **(info_fields or {}))
    mandatory = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items() if v is not None)
    info_el = raw_info if raw_info is not None else (f"""<info><language>en-US</language>{mandatory}
    <eventCode><valueName>SAME</valueName><value>CAE</value></eventCode>
    <expires>{expires}</expires><senderName>KBI</senderName><headline>AMBER Alert</headline>
    <description>{description}</description><instruction>Call 911.</instruction>
    <area><areaDesc>Saline</areaDesc><geocode><valueName>SAME</valueName><value>020169</value></geocode></area>
  </info>""" if info else "")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>{identifier}</identifier><sender>{sender}</sender>{sent_el}
  <status>{status}</status><msgType>{msg_type}</msgType>{scope_el}{refs}
  {info_el}
</alert>""".encode()


def ref(*identifiers, sender="kbi@ks", sent="2026-11-13T16:00:00-06:00"):
    """<references> text naming each identifier by the complete identity cap_xml gives its default message."""
    return " ".join(f"{sender},{i},{sent}" for i in identifiers)


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


class CapStructureValidationTests(unittest.TestCase):
    """Correction B: CAP 1.2 mandatory root and <info> elements (OASIS CAP-v1.2-os 3.2.1/3.2.2) are validated before
    an Actual Alert/Update can be authoritative; optional elements are not required."""

    def parse(self, xml):
        with patch.object(ipaws, "_http_get", return_value=xml):
            return ipaws.fetch_and_parse_cap("https://example.invalid/cap")

    def assert_rejected(self, xml):
        with self.assertRaises(ipaws.CapParseError):
            self.parse(xml)

    def test_an_actual_update_with_an_empty_info_block_is_rejected(self):
        self.assert_rejected(cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1"), raw_info="<info/>"))

    def test_missing_scope_is_rejected(self):
        self.assert_rejected(cap_xml("CAP-1", scope=None))

    def test_each_missing_mandatory_info_element_is_rejected(self):
        for name in ("category", "event", "urgency", "severity", "certainty"):
            with self.subTest(missing=name):
                self.assert_rejected(cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1"),
                                             info_fields={name: None}))

    def test_empty_mandatory_elements_are_rejected(self):
        for name in ("category", "event", "urgency", "severity", "certainty"):
            with self.subTest(empty=name):
                self.assert_rejected(cap_xml("CAP-1", info_fields={name: ""}))
        for root in ("scope", "status", "msgType"):
            with self.subTest(empty_root=root):
                kwargs = {"scope": ""} if root == "scope" else {"status": ""} if root == "status" else {"msg_type": ""}
                self.assert_rejected(cap_xml("CAP-1", **kwargs))

    def test_invalid_enumeration_values_are_rejected(self):
        cases = {"category": "Weather", "urgency": "Now", "severity": "Bad", "certainty": "Very Likely"}
        for name, value in cases.items():
            with self.subTest(field=name, value=value):
                self.assert_rejected(cap_xml("CAP-1", info_fields={name: value}))
        for kwargs in ({"scope": "Everyone"}, {"status": "actual"}, {"msg_type": "Notify"}):
            with self.subTest(root=kwargs):
                self.assert_rejected(cap_xml("CAP-1", **kwargs))

    def test_malformed_sent_and_expires_datetimes_are_rejected(self):
        self.assert_rejected(cap_xml("CAP-1").replace(b"2026-11-13T16:00:00-06:00</sent>", b"yesterday</sent>"))
        self.assert_rejected(cap_xml("CAP-1", expires="2026-11-14 16:00"))

    def test_correctly_structured_actual_alert_and_update_are_accepted(self):
        self.assertEqual(self.parse(cap_xml("CAP-1"))["msg_type"], "Alert")
        update = self.parse(cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1")))
        self.assertEqual((update["msg_type"], update["references"]), ("Update", ["CAP-1"]))

    def test_optional_elements_are_not_required(self):
        minimal = cap_xml("CAP-1", raw_info="<info><category>Rescue</category><event>Child Abduction Emergency"
                                            "</event><urgency>Immediate</urgency><severity>Severe</severity>"
                                            "<certainty>Likely</certainty></info>")
        parsed = self.parse(minimal)
        self.assertEqual((parsed["headline"], parsed["expires"], parsed["areas"]), ("", "", []))

    def test_a_valid_info_less_actual_cancel_is_accepted(self):
        parsed = self.parse(cap_xml("CAN-1", msg_type="Cancel", references=ref("CAP-1"), info=False))
        self.assertEqual(parsed["references"], ["CAP-1"])

    def test_a_valid_non_actual_message_never_affects_actual_alerts(self):
        alert = self.parse(cap_xml("CAP-1"))
        for status in ("Test", "Exercise", "System", "Draft"):
            with self.subTest(status=status):
                update = self.parse(cap_xml("X-1", status=status, msg_type="Update", references=ref("CAP-1")))
                self.assertEqual([c["identifier"] for c in ipaws.current_alerts([alert, update])], ["CAP-1"])

    def test_a_non_actual_message_with_incomplete_info_is_irrelevant_not_malformed(self):
        parsed = self.parse(cap_xml("T-1", status="Test", raw_info="<info/>"))
        self.assertEqual(ipaws.current_alerts([parsed]), [])


ATOM = "{http://www.w3.org/2005/Atom}"


def atom_feed(*entries):
    """entries: (entry_id, link, event or None, statefips or None)."""
    parts = []
    for entry_id, link, event, statefips in entries:
        cats = ""
        if event is not None:
            cats += f'<category label="event" term="{event}"/>'
        if statefips is not None:
            cats += f'<category label="statefips" term="{statefips}"/>'
        parts.append(f"<entry><id>{entry_id}</id><link href=\"{link}\"/>"
                     f"<updated>2026-11-13T16:00:00Z</updated>{cats}</entry>")
    return ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom">'
            + "".join(parts) + "</feed>").encode()


class AtomToSnapshotTests(unittest.TestCase):
    """Correction A, end to end through the REAL fetch_feed_entries -> prefilters -> fetch_and_parse_cap ->
    current_alerts -> snapshot path (only the HTTP transport, the config bridge and the coordinator are doubles)."""

    BASE = "https://ipaws.invalid"
    CFG = {"enabled": True, "ipaws_base_url": BASE, "event_codes": ["CAE", "BLU", "MEP"],
           "same_codes": ["020169"], "include_instruction_in_forecast": True}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="isadoraair-amber-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.active = self.tmp / "active_amber_alerts.json"
        (self.tmp / "active_watches_warnings.json").write_text("[]")
        self.feed = []
        self.documents = {}
        self.fetched = []
        future = (NOW + timedelta(days=1)).astimezone(timezone(timedelta(hours=-6))).strftime("%Y-%m-%dT%H:%M:%S-06:00")
        self.alert = cap_xml("CAP-1", expires=future)
        patches = [
            patch.object(amber_poll, "ACTIVE_FILE", self.active),
            patch.object(amber_poll, "LOCKFILE", str(self.tmp / "poll.lock")),
            patch.object(amber_poll, "load_amber_alert_config", side_effect=lambda: self.CFG),
            patch.object(amber_poll, "_run_alert_coordinator"),
            patch.object(ipaws, "_http_get", side_effect=self._http_get),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _http_get(self, url):
        if url.endswith("/rest/feed"):
            return atom_feed(*self.feed)
        self.fetched.append(url)
        document = self.documents[url]
        if isinstance(document, Exception):
            raise document
        return document

    def publish(self, entry_id, xml, event="CAE", statefips="20"):
        link = f"{self.BASE}/rest/eas/{entry_id}"
        self.documents[link] = xml
        return (entry_id, link, event, statefips)

    def poll(self):
        self.fetched = []
        amber_poll.main()
        return json.loads(self.active.read_text())

    def hold_alert(self):
        self.feed = [self.publish("e-alert", self.alert)]
        self.assertEqual([e["identifier"] for e in self.poll()], ["CAP-1"])     # 1: recognized

    def unclassified_cancel(self, status="Actual"):
        return self.publish("e-cancel", cap_xml("CAN-1", status=status, msg_type="Cancel",
                                                references=ref("CAP-1"), info=False), event=None, statefips=None)

    def test_an_unclassified_info_less_actual_cancel_retires_the_alert_and_the_announcement(self):
        from isadoraair import wx_alert_state
        self.hold_alert()
        now = datetime.now(timezone.utc)
        alerts, _sources, _ = wx_alert_state.effective_alerts(self.tmp, now)
        audio = self.tmp / "wx_alert.flac"
        audio.write_bytes(b"announcement containing CAP-1")
        state = {"schema": 1, "generation": 1, "active": True, "fingerprint": wx_alert_state.content_fingerprint(alerts),
                 "valid_until": wx_alert_state.format_ts(now + timedelta(hours=1)),
                 "published_at": wx_alert_state.format_ts(now), "audio": wx_alert_state.audio_identity(audio),
                 "sources": {"nws": "ok", "amber": "ok"}}
        self.assertTrue(wx_alert_state.playback_decision(
            state, wx_alert_state.audio_identity(audio), now, self.tmp).allowed)
        self.feed.append(self.unclassified_cancel())                              # 2+3: no event/state, no <info>
        self.assertEqual(self.poll(), [])                                         # 4+5: seen and applied
        self.assertIn(f"{self.BASE}/rest/eas/e-cancel", self.fetched)
        decision = wx_alert_state.playback_decision(state, wx_alert_state.audio_identity(audio), now, self.tmp)
        self.assertFalse(decision.allowed)                                        # 6: no longer authorized
        self.assertIn("sources changed", decision.reason)

    def test_a_test_or_exercise_cancel_never_retires_an_actual_alert(self):
        for status in ("Test", "Exercise"):
            with self.subTest(status=status):
                self.hold_alert()
                self.feed.append(self.unclassified_cancel(status=status))
                self.assertEqual([e["identifier"] for e in self.poll()], ["CAP-1"])   # 7

    def test_feed_order_does_not_change_the_result(self):
        self.hold_alert()
        cancel = self.unclassified_cancel()
        self.feed = [cancel] + self.feed                                          # 10
        self.assertEqual(self.poll(), [])

    def test_a_cancel_arriving_in_the_same_cycle_as_its_alert_is_applied(self):
        self.feed = [self.unclassified_cancel(), self.publish("e-alert", self.alert)]
        self.assertEqual(self.poll(), [])

    def test_unrelated_classified_entries_are_never_fetched(self):
        noise = [self.publish(f"n{i}", b"<never fetched/>", event=ev, statefips=st)
                 for i, (ev, st) in enumerate([("TOR", "20"), ("CAE", "48"), ("SVR", "40"), ("BLU", "06")] * 50)]
        self.feed = noise
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.fetched, [])                                        # 8: 200 irrelevant, 0 fetches
        self.feed = noise + [self.publish("e-alert", self.alert)]
        self.poll()
        self.assertEqual(self.fetched, [f"{self.BASE}/rest/eas/e-alert"])

    def test_unclassified_entries_are_not_fetched_while_no_actual_alert_is_held(self):
        self.feed = [self.publish(f"u{i}", b"<never fetched/>", event=None, statefips=None) for i in range(10)]
        self.assertEqual(self.poll(), [])
        self.assertEqual(self.fetched, [])

    def test_a_failed_retrieval_of_a_cancellation_candidate_keeps_the_previous_snapshot(self):
        self.hold_alert()
        before = self.active.read_bytes()
        link = f"{self.BASE}/rest/eas/e-flaky"
        self.documents[link] = requests.ConnectionError("reset")
        self.feed.append(("e-flaky", link, None, None))
        with self.assertLogs(amber_poll.log, "WARNING") as logged:
            self.poll()
        self.assertEqual(self.active.read_bytes(), before)                        # 9: no partial commit
        self.assertIn("previous snapshot kept", "\n".join(logged.output))

    def test_more_unclassified_candidates_than_the_bound_fail_safe_without_a_fetch_storm(self):
        self.hold_alert()
        before = self.active.read_bytes()
        many = [self.publish(f"u{i}", cap_xml(f"X-{i}", status="Test"), event=None, statefips=None)
                for i in range(amber_poll.MAX_UNCLASSIFIED_CAP_FETCHES + 1)]
        self.feed = [self.publish("e-alert", self.alert)] + many
        with self.assertLogs(amber_poll.log, "WARNING"):
            self.poll()
        self.assertEqual(self.active.read_bytes(), before)
        self.assertEqual(self.fetched, [f"{self.BASE}/rest/eas/e-alert"])         # bounded: nothing beyond it

    def test_a_malformed_update_never_supersedes_the_alert_and_the_snapshot_stays_byte_identical(self):
        self.hold_alert()
        before = self.active.read_bytes()
        self.feed.append(self.publish("e-update", cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1"),
                                                          raw_info="<info/>")))
        with self.assertLogs(amber_poll.log, "WARNING") as logged:
            self.poll()
        self.assertEqual(self.active.read_bytes(), before)                        # not superseded, nothing committed
        output = "\n".join(logged.output)                                       # observable in the journal
        self.assertIn("CAP-2", output)
        self.assertIn("previous snapshot kept", output)
        self.assertEqual(amber_poll._run_alert_coordinator.call_count, 2)         # it sees an unchanged snapshot


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


SENDER_A, SENDER_B = "kbi@ks", "other@ks"
T_1600 = "2026-11-13T16:00:00-06:00"
T_1700 = "2026-11-13T17:00:00-06:00"
T_1600_UTC = "2026-11-13T22:00:00+00:00"          # the same instant as T_1600


class CapReferenceIdentityTests(unittest.TestCase):
    """Final correction: a reference names a message by its complete CAP 1.2 identity (sender, identifier, sent);
    an identifier alone is never enough, and a malformed reference is never skipped."""

    def parse(self, xml):
        with patch.object(ipaws, "_http_get", return_value=xml):
            return ipaws.fetch_and_parse_cap("https://example.invalid/cap")

    def survivors(self, *xmls):
        """[(sender, identifier, sent)] of the alerts still in effect."""
        return [(c["sender"], c["identifier"], c["sent"])
                for c in ipaws.current_alerts([self.parse(x) for x in xmls])]

    def assert_rejected(self, xml):
        with self.assertRaises(ipaws.CapParseError):
            self.parse(xml)

    def cancel(self, references, *, sender="fema@ipaws", identifier="CAN-1"):
        return cap_xml(identifier, msg_type="Cancel", references=references, info=False, sender=sender)

    # -- 1-3: single, update and multiple references -------------------------------------------------------------

    def test_a_valid_single_reference_cancel_retires_exactly_the_referenced_message(self):
        parsed = self.parse(self.cancel(ref("CAP-1")))
        self.assertEqual(parsed["reference_identities"], [(SENDER_A, "CAP-1", T_1600_UTC)])
        self.assertEqual(parsed["references"], ["CAP-1"])
        self.assertEqual(self.survivors(cap_xml("CAP-1"), cap_xml("CAP-2"), self.cancel(ref("CAP-1"))),
                         [(SENDER_A, "CAP-2", T_1600)])

    def test_an_update_supersedes_the_prior_issuance_it_references(self):
        update = cap_xml("CAP-2", msg_type="Update", references=ref("CAP-1"), sent_at=T_1700)
        self.assertEqual(self.survivors(cap_xml("CAP-1"), update), [(SENDER_A, "CAP-2", T_1700)])

    def test_multiple_references_in_one_message_are_each_honored(self):
        update = cap_xml("CAP-4", msg_type="Update", sent_at=T_1700,
                         references=" ".join([ref("CAP-1"), ref("CAP-2", sender=SENDER_B)]))
        parsed = self.parse(update)
        self.assertEqual([r[:2] for r in parsed["reference_identities"]], [(SENDER_A, "CAP-1"), (SENDER_B, "CAP-2")])
        alerts = [cap_xml("CAP-1"), cap_xml("CAP-2", sender=SENDER_B), cap_xml("CAP-2"), cap_xml("CAP-3")]
        self.assertEqual(sorted(self.survivors(*alerts, update)),
                         sorted([(SENDER_A, "CAP-2", T_1600), (SENDER_A, "CAP-3", T_1600), (SENDER_A, "CAP-4", T_1700)]))

    # -- 4-7: sender and sent are part of the identity -------------------------------------------------------------

    def test_a_cancel_retires_only_the_sender_it_references_when_two_senders_share_an_identifier(self):
        a = cap_xml("CAP-1", sender=SENDER_A)
        b = cap_xml("CAP-1", sender=SENDER_B)                                   # same identifier AND same sent
        for order in ((a, b), (b, a)):
            with self.subTest(retired=SENDER_A, order=[o[60:90] for o in order]):
                self.assertEqual(self.survivors(*order, self.cancel(ref("CAP-1", sender=SENDER_A))),
                                 [(SENDER_B, "CAP-1", T_1600)])
        self.assertEqual(self.survivors(a, b, self.cancel(ref("CAP-1", sender=SENDER_B))),
                         [(SENDER_A, "CAP-1", T_1600)])

    def test_an_update_supersedes_only_the_sender_it_references(self):
        update = cap_xml("CAP-9", msg_type="Update", references=ref("CAP-1", sender=SENDER_B), sent_at=T_1700)
        self.assertEqual(sorted(self.survivors(cap_xml("CAP-1", sender=SENDER_A),
                                               cap_xml("CAP-1", sender=SENDER_B), update)),
                         sorted([(SENDER_A, "CAP-1", T_1600), (SENDER_A, "CAP-9", T_1700)]))

    def test_the_cancelling_sender_need_not_be_the_referenced_sender(self):
        for canceller in (SENDER_A, SENDER_B, "fema@ipaws"):
            with self.subTest(canceller=canceller):
                self.assertEqual(
                    self.survivors(cap_xml("CAP-1", sender=SENDER_A),
                                   self.cancel(ref("CAP-1", sender=SENDER_A), sender=canceller)), [])

    def test_same_sender_and_identifier_at_different_times_are_different_messages(self):
        early = cap_xml("CAP-1", sent_at=T_1600)
        late = cap_xml("CAP-1", sent_at=T_1700)
        self.assertEqual(self.survivors(early, late, self.cancel(ref("CAP-1", sent=T_1600))),
                         [(SENDER_A, "CAP-1", T_1700)])
        self.assertEqual(self.survivors(early, late, self.cancel(ref("CAP-1", sent=T_1700))),
                         [(SENDER_A, "CAP-1", T_1600)])
        self.assertEqual(self.survivors(early, late, self.cancel(ref("CAP-1", sent="2026-11-13T16:00:01-06:00"))),
                         [(SENDER_A, "CAP-1", T_1600), (SENDER_A, "CAP-1", T_1700)])     # no such message

    def test_sent_is_compared_as_an_instant_so_equivalent_offsets_match_and_distinct_instants_do_not(self):
        alert = cap_xml("CAP-1", sent_at=T_1600)
        self.assertEqual(self.survivors(alert, self.cancel(ref("CAP-1", sent=T_1600_UTC))), [])
        self.assertEqual(self.survivors(alert, self.cancel(ref("CAP-1", sent="2026-11-13T23:00:00+01:00"))), [])
        self.assertEqual(len(self.survivors(alert, self.cancel(ref("CAP-1", sent="2026-11-13T22:00:00+01:00")))), 1)
        self.assertEqual(len(self.survivors(alert, self.cancel(ref("CAP-1", sent="2026-11-13T16:00:00+00:00")))), 1)

    def test_sender_and_identifier_are_compared_exactly(self):
        alert = cap_xml("CAP-1")
        for sender, identifier in (("KBI@ks", "CAP-1"), ("kbi@ks", "cap-1"), ("kbi@ks", "CAP-1x")):
            with self.subTest(sender=sender, identifier=identifier):
                self.assertEqual(len(self.survivors(alert, self.cancel(f"{sender},{identifier},{T_1600}"))), 1)

    # -- 8-12: malformed references are rejected, never skipped ------------------------------------------------------

    def test_an_invalid_reference_timestamp_is_rejected(self):
        for bad in ("yesterday", "2026-11-13 16:00:00-06:00", "2026-13-45T16:00:00-06:00", "2026-11-13T16:00:00",
                    "2026-11-13T25:00:00-06:00", "2026-11-13T16:00:00-0600", "2026-11-13T16:00:00.5-06:00",
                    "2026-11-13T16:00:00Z", "16:00:00-06:00", "0", "null"):
            for build in (lambda r: self.cancel(r), lambda r: cap_xml("CAP-2", msg_type="Update", references=r)):
                with self.subTest(timestamp=bad):
                    self.assert_rejected(build(f"{SENDER_A},CAP-1,{bad}"))

    def test_a_missing_or_empty_reference_component_is_rejected(self):
        for bad in (f"{SENDER_A},CAP-1", f"{SENDER_A},CAP-1,", f",CAP-1,{T_1600}", f"{SENDER_A},,{T_1600}",
                    ",,", "CAP-1", f"{SENDER_A},CAP-1,{T_1600},extra"):
            with self.subTest(reference=bad):
                self.assert_rejected(self.cancel(bad))

    def test_restricted_characters_in_a_referenced_sender_are_rejected(self):
        for sender in ("kb&amp;i@ks", "kb&lt;i@ks", "a,b"):          # "&", "<", and a comma (a fourth component)
            with self.subTest(sender=sender):
                self.assert_rejected(self.cancel(f"{sender},CAP-1,{T_1600}"))

    def test_restricted_characters_in_a_referenced_identifier_are_rejected(self):
        for identifier in ("CAP&amp;1", "CAP&lt;1", "CAP,1"):
            with self.subTest(identifier=identifier):
                self.assert_rejected(self.cancel(f"{SENDER_A},{identifier},{T_1600}"))

    def test_a_malformed_entry_among_valid_references_rejects_the_whole_message(self):
        good = ref("CAP-1")
        for bad in (f"{good} {SENDER_A};CAP-2;{T_1600}", f"{good} {SENDER_A},CAP-2,not-a-time",
                    f"{SENDER_A},CAP-2 {T_1600} {good}", f"{good} {SENDER_A},CAP-2", f"{good} ,,"):
            for build in (lambda r: self.cancel(r), lambda r: cap_xml("CAP-3", msg_type="Update", references=r)):
                with self.subTest(references=bad):
                    self.assert_rejected(build(bad))

    def test_parse_references_returns_complete_identities_and_raises_value_error_uniformly(self):
        self.assertEqual(ipaws.parse_references(f"{ref('A-1')}  \n {ref('B-2', sender=SENDER_B, sent=T_1700)}"),
                         [(SENDER_A, "A-1", T_1600_UTC), (SENDER_B, "B-2", "2026-11-13T23:00:00+00:00")])
        self.assertEqual(ipaws.parse_references(""), [])
        self.assertEqual(ipaws.parse_references(None), [])
        for bad in ("a,b", f"a,b,{T_1600}x", "a,b,2026-02-30T00:00:00+00:00"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ipaws.parse_references(bad)

    def test_message_identity_is_idempotent_and_never_conflates_distinct_instants(self):
        once = ipaws.message_identity(SENDER_A, "CAP-1", T_1600)
        self.assertEqual(once, ipaws.message_identity(*once))
        self.assertNotEqual(once, ipaws.message_identity(SENDER_A, "CAP-1", T_1700))
        self.assertNotEqual(once, ipaws.message_identity(SENDER_B, "CAP-1", T_1600))

    # -- 14-16: authority rules that must survive ---------------------------------------------------------------------

    def test_an_info_less_actual_cancel_retains_its_authority_with_a_complete_reference(self):
        cancel = self.cancel(ref("CAP-1"))
        self.assertNotIn(b"<info", cancel)
        self.assertEqual(self.survivors(cap_xml("CAP-1"), cancel), [])
        self.assertEqual(self.survivors(cancel, cap_xml("CAP-1")), [])

    def test_a_test_or_exercise_message_with_an_exact_reference_still_cancels_nothing(self):
        alert = cap_xml("CAP-1")
        for status in ("Test", "Exercise", "System", "Draft"):
            for msg_type, info in (("Cancel", False), ("Update", True)):
                with self.subTest(status=status, msg_type=msg_type):
                    other = cap_xml("X-1", status=status, msg_type=msg_type, references=ref("CAP-1"), info=info)
                    self.assertEqual(self.survivors(alert, other), [(SENDER_A, "CAP-1", T_1600)])

    def test_update_chains_with_distinct_senders_and_times_resolve_in_every_feed_order(self):
        import itertools
        original = cap_xml("CAP-1", sender=SENDER_A, sent_at=T_1600)
        first = cap_xml("CAP-2", sender=SENDER_B, msg_type="Update", sent_at=T_1700,
                        references=ref("CAP-1", sender=SENDER_A, sent=T_1600))
        second = cap_xml("CAP-3", sender=SENDER_A, msg_type="Update", sent_at="2026-11-13T18:00:00-06:00",
                         references=ref("CAP-2", sender=SENDER_B, sent=T_1700))
        for order in itertools.permutations((original, first, second)):
            with self.subTest(order=[xml.split(b"<identifier>")[1][:5] for xml in order]):
                self.assertEqual(self.survivors(*order), [(SENDER_A, "CAP-3", "2026-11-13T18:00:00-06:00")])

    # -- fail closed when only an identifier is available ----------------------------------------------------------------

    def test_authority_is_never_inferred_from_an_identifier_alone(self):
        alert = cap("CAP-1")
        legacy_cancel = dict(cap("CAN-1", msg_type="Cancel", references=["CAP-1"]))
        del legacy_cancel["reference_identities"]
        with self.assertRaises(ipaws.CapParseError):
            ipaws.current_alerts([alert, legacy_cancel])

    def test_a_message_that_can_air_without_a_valid_identity_is_not_guessed_at(self):
        for field, value in (("sender", ""), ("sender", None), ("identifier", "A B"), ("sent", "yesterday"),
                             ("sent", None)):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ipaws.CapParseError):
                    ipaws.current_alerts([dict(cap("CAP-1"), **{field: value})])


class ReferenceIdentityEndToEndTests(unittest.TestCase):
    """Atom feed -> CAP retrieval -> reference parsing -> active-set reduction -> authoritative snapshot ->
    combined-alert playback eligibility, with only the HTTP transport, the config bridge and the coordinator doubled."""

    BASE = AtomToSnapshotTests.BASE
    CFG = AtomToSnapshotTests.CFG
    setUp = AtomToSnapshotTests.setUp
    _http_get = AtomToSnapshotTests._http_get
    publish = AtomToSnapshotTests.publish
    poll = AtomToSnapshotTests.poll

    @property
    def future(self):
        return (NOW + timedelta(days=1)).astimezone(timezone(timedelta(hours=-6))).strftime("%Y-%m-%dT%H:%M:%S-06:00")

    def alert_xml(self, identifier, sender, description):
        return cap_xml(identifier, sender=sender, description=description, expires=self.future)

    def eligibility(self):
        """Would a combined announcement built from the CURRENT snapshots be allowed to air now?"""
        from isadoraair import wx_alert_state
        now = datetime.now(timezone.utc)
        alerts, _sources, _excluded = wx_alert_state.effective_alerts(self.tmp, now)
        audio = self.tmp / "wx_alert.flac"
        audio.write_bytes(b"combined announcement")
        state = {"schema": 1, "generation": 1, "active": True, "fingerprint": wx_alert_state.content_fingerprint(alerts),
                 "valid_until": wx_alert_state.format_ts(now + timedelta(hours=1)),
                 "published_at": wx_alert_state.format_ts(now), "audio": wx_alert_state.audio_identity(audio),
                 "sources": {"nws": "ok", "amber": "ok"}}
        decision = wx_alert_state.playback_decision(state, wx_alert_state.audio_identity(audio), now, self.tmp)
        return alerts, decision

    def test_a_cancel_for_one_sender_leaves_another_senders_same_identifier_alert_eligible(self):
        from isadoraair import wx_alert_state
        self.feed = [self.publish("a", self.alert_xml("CAP-1", SENDER_A, "Child taken from Salina.")),
                     self.publish("b", self.alert_xml("CAP-1", SENDER_B, "Child taken from Abilene."))]
        held = self.poll()
        self.assertEqual(sorted((e["identifier"], e["text"]) for e in held),
                         [("CAP-1", "Child taken from Abilene. Call 911."), ("CAP-1", "Child taken from Salina. Call 911.")])
        alerts, _ = self.eligibility()
        self.assertEqual(len(alerts), 2)                                    # the coordinator keeps both issuances

        cancel = self.publish("c", cap_xml("CAN-1", msg_type="Cancel", references=ref("CAP-1", sender=SENDER_A),
                                           info=False, sender="fema@ipaws"), event=None, statefips=None)
        for feed in ([*self.feed, cancel], [cancel, *self.feed]):           # feed order must not matter
            self.feed = feed
            snapshot = self.poll()
            self.assertEqual([e["text"] for e in snapshot], ["Child taken from Abilene. Call 911."])
            alerts, decision = self.eligibility()
            self.assertEqual([a["text"] for a in alerts], ["Child taken from Abilene. Call 911."])
            self.assertTrue(decision.allowed, decision.reason)              # the unrelated alert is not withdrawn

    def test_a_cancel_naming_the_wrong_sent_time_withdraws_nothing(self):
        self.feed = [self.publish("a", self.alert_xml("CAP-1", SENDER_A, "Child taken.")),
                     self.publish("c", cap_xml("CAN-1", msg_type="Cancel", references=ref("CAP-1", sent=T_1700),
                                               info=False), event=None, statefips=None)]
        self.assertEqual([e["identifier"] for e in self.poll()], ["CAP-1"])
        self.assertTrue(self.eligibility()[1].allowed)

    def test_an_exact_identity_cancel_still_retires_the_announcement(self):
        self.feed = [self.publish("a", self.alert_xml("CAP-1", SENDER_A, "Child taken."))]
        self.assertEqual(len(self.poll()), 1)
        self.feed.append(self.publish("c", cap_xml("CAN-1", msg_type="Cancel", references=ref("CAP-1"), info=False),
                                      event=None, statefips=None))
        self.assertEqual(self.poll(), [])

    def test_a_malformed_reference_keeps_the_previous_snapshot_byte_for_byte(self):
        self.feed = [self.publish("a", self.alert_xml("CAP-1", SENDER_A, "Child taken."))]
        self.poll()
        before = self.active.read_bytes()
        runs = amber_poll._run_alert_coordinator.call_count
        malformed = {
            "invalid timestamp": f"{SENDER_A},CAP-1,2026-13-45T16:00:00-06:00",
            "missing timestamp": f"{SENDER_A},CAP-1",
            "bad referenced sender": f"kb&amp;i@ks,CAP-1,{T_1600}",
            "bad entry among good ones": f"{ref('CAP-0')} {SENDER_A},CAP-1,soon",
        }
        for label, references in malformed.items():
            for kind, info in (("Update", True), ("Cancel", False)):
                with self.subTest(case=label, msg_type=kind):
                    xml = cap_xml("CAP-2", msg_type=kind, references=references, info=info, expires=self.future)
                    self.feed = [self.publish("a", self.alert_xml("CAP-1", SENDER_A, "Child taken.")),
                                 self.publish("m", xml, event="CAE" if info else None, statefips="20" if info else None)]
                    with self.assertLogs(amber_poll.log, "WARNING") as logged:
                        self.poll()
                    self.assertEqual(self.active.read_bytes(), before)       # no supersession, no partial commit
                    self.assertIn("previous snapshot kept", "\n".join(logged.output))
                    runs += 1
                    self.assertEqual(amber_poll._run_alert_coordinator.call_count, runs)
        self.assertTrue(self.eligibility()[1].allowed)                      # still the same authoritative alert


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
