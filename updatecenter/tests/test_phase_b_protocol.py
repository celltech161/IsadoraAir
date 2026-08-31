import json
import uuid

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # also installs runtime on sys.path
from isadoraair_updater.protocol import MAX_REQUEST_BYTES, ProtocolError, decode_request, encode_response
from isadoraair_updater import PROTOCOL_VERSION, RUNTIME_VERSION


def _request(**changes):
    data = {"protocol_version": PROTOCOL_VERSION, "action": "PING"}
    data.update(changes)
    return json.dumps(data).encode()


class StrictProtocolTests(SimpleTestCase):
    def test_runtime_v5_keeps_wire_protocol_v3(self):
        self.assertEqual(PROTOCOL_VERSION, 3)
        self.assertEqual(RUNTIME_VERSION, 5)

    def test_ping_is_exact(self):
        self.assertEqual(decode_request(_request()).action, "PING")

    def test_unknown_action_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(_request(action="RUN_COMMAND"))

    def test_unknown_field_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(_request(path="/etc/passwd"))

    def test_malformed_json_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b"{")

    def test_oversize_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b"x" * (MAX_REQUEST_BYTES + 1))

    def test_start_accepts_only_identifiers(self):
        job_id = str(uuid.uuid4())
        decoded = decode_request(_request(
            action="START_UPDATE", job_id=job_id,
            requested_target_release_id="r0003", expected_plan_fingerprint="a" * 64,
        ))
        self.assertEqual(decoded.job_id, job_id)

    def test_noncanonical_uuid_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(_request(
                action="START_UPDATE", job_id=str(uuid.uuid4()).upper(),
                requested_target_release_id="r0003", expected_plan_fingerprint="a" * 64,
            ))

    def test_release_and_fingerprint_shapes_rejected(self):
        for release, digest in (("main", "a" * 64), ("r0003", "not-a-hash")):
            with self.assertRaises(ProtocolError):
                decode_request(_request(
                    action="START_UPDATE", job_id=str(uuid.uuid4()),
                    requested_target_release_id=release, expected_plan_fingerprint=digest,
                ))

    def test_log_tail_bound_enforced(self):
        with self.assertRaises(ProtocolError):
            decode_request(_request(action="GET_JOB_LOG", job_id=str(uuid.uuid4()), max_bytes=65537))

    def test_response_is_bounded(self):
        with self.assertRaises(ProtocolError):
            encode_response({"ok": True, "data": "x" * 131072})
