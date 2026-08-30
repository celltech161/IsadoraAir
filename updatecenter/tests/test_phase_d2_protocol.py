"""D2-H: the private supervisor<->worker control protocol."""
import os
import socket

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.protocol import (
    MAX_REQUEST_BYTES, ProtocolError, Request, authorized_peer_uid, decode_request, decode_response,
    encode_request, encode_response, is_authorized_root_peer,
)

VALID_UUID = "12345678-1234-4123-8123-123456789abc"


class RequestEncodeDecodeTests(SimpleTestCase):
    def test_ping_round_trips(self):
        request = Request(action="PING")
        decoded = decode_request(encode_request(request))
        self.assertEqual(decoded, request)

    def test_get_runtime_state_round_trips(self):
        request = Request(action="GET_RUNTIME_STATE")
        self.assertEqual(decode_request(encode_request(request)), request)

    def test_request_activation_round_trips(self):
        request = Request(
            action="REQUEST_ACTIVATION", transaction_id=VALID_UUID, candidate_slot="B",
            candidate_generation=5, candidate_descriptor_sha256="a" * 64,
            release_id="r0027", previous_release_id="r0026",
        )
        self.assertEqual(decode_request(encode_request(request)), request)

    def test_request_activation_with_null_predecessor_round_trips(self):
        request = Request(
            action="REQUEST_ACTIVATION", transaction_id=VALID_UUID, candidate_slot="A",
            candidate_generation=1, candidate_descriptor_sha256="a" * 64,
            release_id="r0001", previous_release_id=None,
        )
        self.assertEqual(decode_request(encode_request(request)), request)

    def test_get_activation_status_round_trips(self):
        request = Request(action="GET_ACTIVATION_STATUS", transaction_id=VALID_UUID)
        self.assertEqual(decode_request(encode_request(request)), request)

    def test_unknown_action_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b'{"action": "DELETE_EVERYTHING"}')

    def test_extra_field_for_ping_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b'{"action": "PING", "transaction_id": "' + VALID_UUID.encode() + b'"}')

    def test_missing_required_field_for_request_activation_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b'{"action": "REQUEST_ACTIVATION", "transaction_id": "' + VALID_UUID.encode() + b'"}')

    def test_no_path_field_can_ever_be_smuggled_through(self):
        for forbidden in (b'"path"', b'"command"', b'"argv"', b'"environment"', b'"service"', b'"unit"', b'"shell"'):
            raw = b'{"action": "REQUEST_ACTIVATION", "transaction_id": "' + VALID_UUID.encode() \
                + b'", "candidate_slot": "B", "candidate_generation": 5, ' \
                + b'"candidate_descriptor_sha256": "' + (b"a" * 64) + b'", "release_id": "r0027", ' \
                + b'"previous_release_id": null, ' + forbidden + b': "x"}'
            with self.subTest(field=forbidden):
                with self.assertRaises(ProtocolError):
                    decode_request(raw)

    def test_oversized_request_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b"x" * (MAX_REQUEST_BYTES + 1))

    def test_empty_request_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b"")

    def test_non_json_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b"not json")

    def test_json_array_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b"[1, 2, 3]")

    def test_bad_slot_value_rejected(self):
        raw = (
            b'{"action": "REQUEST_ACTIVATION", "transaction_id": "' + VALID_UUID.encode()
            + b'", "candidate_slot": "C", "candidate_generation": 5, '
            + b'"candidate_descriptor_sha256": "' + (b"a" * 64) + b'", "release_id": "r0027", '
            + b'"previous_release_id": null}'
        )
        with self.assertRaises(ProtocolError):
            decode_request(raw)

    def test_bad_uuid_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_request(b'{"action": "GET_ACTIVATION_STATUS", "transaction_id": "not-a-uuid"}')

    def test_bad_release_id_rejected(self):
        raw = (
            b'{"action": "REQUEST_ACTIVATION", "transaction_id": "' + VALID_UUID.encode()
            + b'", "candidate_slot": "B", "candidate_generation": 5, '
            + b'"candidate_descriptor_sha256": "' + (b"a" * 64) + b'", "release_id": "not-a-release", '
            + b'"previous_release_id": null}'
        )
        with self.assertRaises(ProtocolError):
            decode_request(raw)


class ResponseEncodeDecodeTests(SimpleTestCase):
    def test_ok_response_round_trips(self):
        payload = {"ok": True, "active_slot": "A"}
        self.assertEqual(decode_response(encode_response(payload)), payload)

    def test_missing_ok_key_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_response({"active_slot": "A"})

    def test_non_bool_ok_rejected(self):
        with self.assertRaises(ProtocolError):
            encode_response({"ok": "yes"})

    def test_oversized_response_rejected_at_decode(self):
        with self.assertRaises(ProtocolError):
            decode_response(b"x" * 200000)


class PeerCredentialTests(SimpleTestCase):
    def test_real_socketpair_reports_own_uid(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.assertEqual(authorized_peer_uid(left), os.getuid())
        finally:
            left.close()
            right.close()

    def test_root_authorization_check_is_exact_uid_zero(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # This test process is not root -- proves the check
            # actually discriminates rather than always returning True.
            self.assertFalse(is_authorized_root_peer(left))
        finally:
            left.close()
            right.close()

    def test_non_unix_socket_returns_none_not_a_guess(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            self.assertIsNone(authorized_peer_uid(sock))
