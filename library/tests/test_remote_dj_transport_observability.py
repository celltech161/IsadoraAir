import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GObject, Gst

from django.test import SimpleTestCase

import library.services.engine as eng_module
from library.services.remote_dj_connection import RemoteDJConnectionAttempt
from library.services.remote_dj_stats import (
    empty_media_stats_snapshot,
    empty_transport_snapshot,
    parse_webrtc_stats,
    sanitize_browser_stats_payload,
)


ATTEMPT_A = "A2a_attempt_generation_A"
ATTEMPT_B = "A2a_attempt_generation_B"


def _stats_report(**overrides):
    report = {
        "in-audio": {
            "id": "in-audio", "type": "inbound-rtp", "kind": "audio",
            "transport-id": "audio-transport", "packets-received": 81,
            "packets-lost": 3, "packets-discarded": 2, "packets-repaired": 1,
            "jitter": 0.0125, "bytes-received": 65432,
        },
        "out-audio": {
            "id": "out-audio", "type": "outbound-rtp", "kind": "audio",
            "transport-id": "audio-transport", "packets-sent": 92,
            "bytes-sent": 76543,
        },
        "remote-in": {
            "id": "remote-in", "type": "remote-inbound-rtp",
            "local-id": "out-audio", "fraction-lost": 0.03125,
            "round-trip-time": 0.087,
        },
        "audio-transport": {
            "id": "audio-transport", "type": "transport",
            "selected-candidate-pair-id": "selected-pair",
            "dtls-state": "connecting", "dtls-role": "server",
        },
        "selected-pair": {
            "id": "selected-pair", "type": "candidate-pair",
            "local-candidate-id": "local", "remote-candidate-id": "remote",
            "current-round-trip-time": 0.087,
        },
        "local": {
            "id": "local", "type": "local-candidate", "candidate-type": "srflx",
            "protocol": "udp", "address": "203.0.113.10", "port": 50000,
            "candidate": "raw-local-secret", "username-fragment": "secret-local",
        },
        "remote": {
            "id": "remote", "type": "remote-candidate", "candidate-type": "prflx",
            "protocol": "udp", "address": "198.51.100.20", "port": 60000,
            "candidate": "raw-remote-secret", "username-fragment": "secret-remote",
        },
    }
    report.update(overrides)
    return report


class RemoteDJGstStatsParserTests(SimpleTestCase):
    def test_supported_gst_structure_is_walked(self):
        Gst.init(None)
        report = Gst.Structure.new_empty("application/x-webrtc-stats")
        inbound = Gst.Structure.new_empty("inbound")
        inbound.set_value("id", "in")
        inbound.set_value("type", "inbound-rtp")
        inbound.set_value("kind", "audio")
        inbound.set_value("packets-received", 7)
        report.set_value("in", inbound)

        parsed = parse_webrtc_stats(report)

        self.assertEqual(parsed["remote_mic"]["packets_received"], 7)

    def test_selected_pair_is_resolved_through_audio_transport(self):
        parsed = parse_webrtc_stats(_stats_report())
        self.assertEqual(parsed["transport"]["selected_pair"], {
            "exists": True,
            "local_candidate_type": "srflx",
            "remote_candidate_type": "prflx",
            "protocol": "udp",
        })

    def test_multiple_unassociated_transports_are_not_picked_arbitrarily(self):
        report = _stats_report()
        report["in-audio"].pop("transport-id")
        report["out-audio"].pop("transport-id")
        report["other-transport"] = {
            "id": "other-transport", "type": "transport",
            "selected-candidate-pair-id": "selected-pair",
        }
        parsed = parse_webrtc_stats(report)
        self.assertFalse(parsed["transport"]["selected_pair"]["exists"])

    def test_candidate_private_fields_never_reach_exposed_snapshot(self):
        rendered = json.dumps(parse_webrtc_stats(_stats_report()))
        for secret in (
            "203.0.113.10", "198.51.100.20", "50000", "60000",
            "raw-local-secret", "raw-remote-secret", "secret-local", "secret-remote",
        ):
            self.assertNotIn(secret, rendered)

    def test_dtls_state_and_role_are_parsed_when_supported(self):
        parsed = parse_webrtc_stats(_stats_report())
        self.assertEqual(parsed["transport"]["dtls_state"], "connecting")
        self.assertEqual(parsed["transport"]["dtls_role"], "server")

    def test_inbound_remote_mic_counters_and_jitter_are_extracted(self):
        stats = parse_webrtc_stats(_stats_report())["remote_mic"]
        self.assertEqual(stats, {
            "packets_received": 81, "packets_lost": 3,
            "packets_discarded": 2, "packets_repaired": 1,
            "jitter_ms": 12.5, "bytes_received": 65432,
        })

    def test_outbound_monitor_and_remote_feedback_are_extracted(self):
        stats = parse_webrtc_stats(_stats_report())["monitor_return"]
        self.assertEqual(stats, {
            "packets_sent": 92, "bytes_sent": 76543,
            "remote_fraction_lost": 0.03125, "rtt_ms": 87.0,
        })

    def test_missing_and_version_dependent_fields_are_null(self):
        parsed = parse_webrtc_stats({"only": {
            "id": "only", "type": "inbound-rtp", "kind": "audio",
        }})
        self.assertTrue(all(value is None for value in parsed["remote_mic"].values()))
        self.assertTrue(all(value is None for value in parsed["monitor_return"].values()))
        self.assertIsNone(parsed["transport"]["dtls_state"])
        self.assertFalse(parsed["transport"]["selected_pair"]["exists"])

    def test_real_webrtcbin_exposes_supported_observation_surfaces(self):
        Gst.init(None)
        webrtc = Gst.ElementFactory.make("webrtcbin", "stats-smoke")
        self.assertIsNotNone(webrtc)
        self.assertIsNotNone(webrtc.find_property("ice-connection-state"))
        self.assertIsNotNone(webrtc.find_property("ice-gathering-state"))
        self.assertNotEqual(GObject.signal_lookup("get-stats", webrtc.__gtype__), 0)


class RemoteDJBrowserStatsSanitizerTests(SimpleTestCase):
    def _payload(self):
        return {
            "ice_state": "connected",
            "rtt_ms": 91.25,
            "selected_pair": {
                "exists": True, "local_candidate_type": "prflx",
                "remote_candidate_type": "srflx", "protocol": "UDP",
                "address": "192.0.2.1", "port": 4444, "candidate": "secret",
            },
            "inbound": {
                "packets_received": 101, "packets_lost": 4, "jitter_ms": 8.5,
                "concealed_samples": 22, "concealment_events": 2,
                "total_audio_energy": 1.125, "track_identifier": "device-secret",
            },
            "raw_report": {"anything": "must disappear"},
        }

    def test_browser_payload_is_allow_listed_and_normalized(self):
        sanitized = sanitize_browser_stats_payload(self._payload())
        self.assertEqual(set(sanitized), {
            "ice_state", "rtt_ms", "selected_pair", "inbound",
        })
        self.assertEqual(sanitized["selected_pair"]["protocol"], "udp")
        self.assertEqual(sanitized["inbound"]["jitter_ms"], 8.5)
        rendered = json.dumps(sanitized)
        for forbidden in ("192.0.2.1", "4444", "secret", "device-secret", "raw_report"):
            self.assertNotIn(forbidden, rendered)

    def test_invalid_or_unbounded_values_become_null_not_failures(self):
        payload = self._payload()
        payload["ice_state"] = "invented"
        payload["rtt_ms"] = float("inf")
        payload["inbound"]["packets_received"] = 1 << 60
        payload["selected_pair"]["local_candidate_type"] = "raw-candidate"
        sanitized = sanitize_browser_stats_payload(payload)
        self.assertIsNone(sanitized["ice_state"])
        self.assertIsNone(sanitized["rtt_ms"])
        self.assertIsNone(sanitized["inbound"]["packets_received"])
        self.assertIsNone(sanitized["selected_pair"]["local_candidate_type"])

    def test_non_object_payload_is_rejected(self):
        for payload in (None, [], "stats", 1):
            self.assertIsNone(sanitize_browser_stats_payload(payload))


class RemoteDJTransportGenerationTests(SimpleTestCase):
    def setUp(self):
        self.engine = object.__new__(eng_module.PlaybackEngine)
        self.current = self._session(ATTEMPT_B)
        self.engine.remote_dj_session = self.current
        self.engine._remote_dj_server = MagicMock()

    @staticmethod
    def _session(attempt_id):
        session = eng_module.RemoteDJSession()
        session.connection_attempt = RemoteDJConnectionAttempt(
            attempt_id, 1_700_000_000_000
        )
        session.webrtc = MagicMock()
        return session

    def test_server_ice_state_maps_to_state_and_truthful_milestone(self):
        self.current.webrtc.props.ice_connection_state = SimpleNamespace(
            value_nick="checking"
        )
        context = (self.current, ATTEMPT_B, self.current.webrtc)
        with patch.object(self.engine, "_remote_dj_request_stats"):
            self.engine._remote_dj_on_ice_connection_state(
                self.current.webrtc, None, context
            )
        attempt = self.current.connection_attempt
        self.assertEqual(attempt.transport["server"]["ice_state"], "checking")
        self.assertIn("server_ice_checking", attempt.milestones["server"])
        self.assertEqual(
            attempt.transport["server"]["ice_transitions"][0]["state"],
            "checking",
        )

    def test_server_ice_gathering_state_is_independent(self):
        self.current.webrtc.props.ice_gathering_state = SimpleNamespace(
            value_nick="complete"
        )
        context = (self.current, ATTEMPT_B, self.current.webrtc)
        with patch.object(self.engine, "_remote_dj_request_stats"):
            self.engine._remote_dj_on_ice_gathering_state(
                self.current.webrtc, None, context
            )
        self.assertEqual(
            self.current.connection_attempt.transport["server"]["ice_gathering_state"],
            "complete",
        )
        self.assertEqual(
            self.current.connection_attempt.transport["server"]
            ["ice_gathering_transitions"][0]["state"],
            "complete",
        )

    def test_stale_browser_stats_from_a_cannot_mutate_b(self):
        attempt = self.current.connection_attempt
        before_transport = json.loads(json.dumps(attempt.transport))
        before_media = json.loads(json.dumps(attempt.media_stats))
        before_milestones = json.loads(json.dumps(attempt.milestones))
        self.engine._remote_dj_record_browser_stats(
            ATTEMPT_A, sanitize_browser_stats_payload({"ice_state": "connected"}), 100
        )
        self.assertEqual(attempt.transport, before_transport)
        self.assertEqual(attempt.media_stats, before_media)
        self.assertEqual(attempt.milestones, before_milestones)

    def test_current_browser_stats_add_fixed_state_and_real_milestones(self):
        payload = sanitize_browser_stats_payload({
            "ice_state": "connected",
            "selected_pair": {"exists": True, "local_candidate_type": "host",
                              "remote_candidate_type": "prflx", "protocol": "udp"},
            "inbound": {"packets_received": 2, "total_audio_energy": 0.5},
        })
        self.engine._remote_dj_record_browser_stats(ATTEMPT_B, payload, 222.2)
        attempt = self.current.connection_attempt
        self.assertEqual(attempt.transport["browser"]["ice_state"], "connected")
        self.assertIn("selected_candidate_pair", attempt.milestones["browser"])
        self.assertIn("first_monitor_rtp", attempt.milestones["browser"])
        self.assertIn("first_monitor_audio_energy", attempt.milestones["browser"])

    def test_browser_stats_without_valid_elapsed_never_create_server_milestones(self):
        payload = sanitize_browser_stats_payload({
            "selected_pair": {"exists": True},
            "inbound": {"packets_received": 2, "total_audio_energy": 0.5},
        })
        self.engine._remote_dj_record_browser_stats(ATTEMPT_B, payload, None)
        milestones = self.current.connection_attempt.milestones
        self.assertEqual(milestones["server"], {"token_issued": 0.0})
        self.assertEqual(milestones["browser"], {})

    def test_stale_server_stats_promise_from_a_is_ignored_before_wait(self):
        stale = self._session(ATTEMPT_A)
        promise = MagicMock()
        self.engine._remote_dj_on_stats_ready(
            promise, (stale, ATTEMPT_A, stale.webrtc, "stale")
        )
        promise.wait.assert_not_called()
        self.assertEqual(
            self.current.connection_attempt.transport, empty_transport_snapshot()
        )

    def test_current_server_stats_update_snapshot_and_observed_milestones(self):
        promise = MagicMock()
        promise.get_reply.return_value = _stats_report()
        self.engine._remote_dj_on_stats_ready(
            promise,
            (self.current, ATTEMPT_B, self.current.webrtc, "test"),
        )
        attempt = self.current.connection_attempt
        self.assertEqual(attempt.transport["server"]["dtls_state"], "connecting")
        self.assertEqual(
            attempt.transport["server"]["dtls_state_observations"][0]["state"],
            "connecting",
        )
        self.assertIn("dtls_connecting", attempt.milestones["server"])
        # P1 1.5 Pass B1 correction: this fixture's transport is only
        # "connecting" (never proven connected/usable) -- the truthful
        # name for what a get-stats snapshot alone can prove is
        # "candidate_pair_stats_present", NOT "selected_candidate_pair"
        # (which stays reserved for the browser's own, separately
        # verified, selected/nominated-pair report). See
        # test_server_candidate_pair_milestone_is_never_misreported_as_selected
        # below for the explicit regression against the old name.
        self.assertIn("candidate_pair_stats_present", attempt.milestones["server"])
        self.assertEqual(attempt.media_stats["remote_mic"]["packets_received"], 81)

    def test_server_candidate_pair_milestone_is_never_misreported_as_selected(self):
        """P1 1.5 Pass B1 regression -- production proved this SERVER-side
        get-stats snapshot can report a selected-candidate-pair-id before
        the remote answer/browser candidates even exist. The old
        "selected_candidate_pair" name is a false claim of a usable,
        nominated pair; it must never appear in the SERVER timing domain
        again (the browser domain is untouched -- see
        test_current_browser_stats_add_fixed_state_and_real_milestones)."""
        promise = MagicMock()
        promise.get_reply.return_value = _stats_report()
        self.engine._remote_dj_on_stats_ready(
            promise,
            (self.current, ATTEMPT_B, self.current.webrtc, "test"),
        )
        attempt = self.current.connection_attempt
        self.assertNotIn("selected_candidate_pair", attempt.milestones["server"])
        # The final sanitized topology itself is unaffected by the rename.
        self.assertTrue(attempt.transport["server"]["selected_pair"]["exists"])


class RemoteDJTransportStateContractTests(SimpleTestCase):
    def test_state_is_json_serializable_fixed_and_bounded(self):
        attempt = RemoteDJConnectionAttempt(ATTEMPT_A, 1_700_000_000_000)
        snapshot = attempt.snapshot()
        json.dumps(snapshot)
        self.assertEqual(snapshot["transport"], empty_transport_snapshot())
        self.assertEqual(snapshot["media_stats"], empty_media_stats_snapshot())
        self.assertLess(len(json.dumps(snapshot)), 3000)

    def test_transport_transition_history_has_a_hard_cap(self):
        attempt = RemoteDJConnectionAttempt(ATTEMPT_A, 1_700_000_000_000)
        for index in range(30):
            attempt.record_server_ice_state(
                "checking" if index % 2 else "connected"
            )
        self.assertEqual(
            len(attempt.transport["server"]["ice_transitions"]), 12
        )

    def test_late_server_ice_milestone_cannot_regress_pass_a1_stage(self):
        attempt = RemoteDJConnectionAttempt(ATTEMPT_A, 1_700_000_000_000)
        attempt.record("peer_connected")
        attempt.record("server_ice_checking")
        self.assertEqual(attempt.stage, "peer_connected")
        self.assertEqual(attempt.status, "connected")

    def test_pass_a1_failure_contract_is_unchanged(self):
        attempt = RemoteDJConnectionAttempt(ATTEMPT_A, 1_700_000_000_000)
        attempt.fail("media_routing", "unchanged failure")
        attempt.record("server_ice_completed")
        self.assertEqual(attempt.stage, "failed")
        self.assertEqual(attempt.status, "failed")

    def test_browser_template_uses_bounded_generation_safe_stats_burst(self):
        template = (
            Path(__file__).parents[1] / "templates/library/dashboard.html"
        ).read_text(encoding="utf-8")
        self.assertIn("pc.getStats()", template)
        self.assertIn("rdjPc !== pc", template)
        self.assertIn("rdjAttemptId !== attemptId", template)
        self.assertIn("[0, 250, 750, 1500, 2500]", template)
        self.assertNotIn("setInterval(() => rdjCollectStats", template)
        self.assertNotIn("JSON.stringify(report)", template)
