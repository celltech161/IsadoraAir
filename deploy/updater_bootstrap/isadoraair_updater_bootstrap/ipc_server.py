"""Update Center Phase D, D3-A: the real Unix-socket server for this
supervisor's own private activation protocol (protocol.py).

Wires protocol.py's decode_request/encode_response + SO_PEERCRED
authorization to supervisor.py's pure state-machine functions and
verification.py's independent candidate proof -- structurally modeled
on isadoraair_updater/daemon.py's own accept-loop shape (thread-per-
connection, bounded single-message-per-connection, PEERCRED check
before dispatch), but this socket's trust domain is completely
different (root-only, never Django/HTTP-reachable -- see protocol.py's
own top docstring).

The worker's REQUEST_ACTIVATION is never authorization by itself: this
server ALWAYS independently re-verifies the candidate bundle
(verification.verify_candidate_bundle) against root-owned trust/
attestation material before ever advancing the transaction past
CANDIDATE_VERIFIED, exactly as D3-A/D3-I require -- it never trusts a
worker-supplied "verified" claim, and it never accepts a path/command/
argv/service/unit/shell/environment field, because protocol.py's own
Request schema has no such fields to accept."""
from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import socket
import threading

from .activation import ActivationPhase, runtime_activation_accepted
from .attestation import build_attestation_statement
from .config import BootstrapConfig
from .protocol import (
    MAX_REQUEST_BYTES, ProtocolError, authorized_peer_uid, decode_request, encode_response,
)
from .security import assert_root_protected_parents
from .slots import Slot, SlotLayout
from .state import IndeterminateStateWriteError, RuntimeState, StateError, read_runtime_state, write_runtime_state_atomically
from .supervisor import SupervisorError, begin_transaction, mark_candidate_verified, request_activation
from .trust import SignatureAssertion, TrustPolicy
from .verification import verify_candidate_bundle

BOOTSTRAP_PROTOCOL_VERSION_CURRENT = 1
WIRE_PROTOCOL_VERSION_CURRENT = 3


class IPCServerError(RuntimeError):
    pass


def _attestations_dir(slots_root: Path, candidate_slot: str) -> Path:
    return Path(slots_root) / ".staging" / f"attestations-{candidate_slot}"


def _descriptor_path(slots_root: Path, candidate_slot: str) -> Path:
    return Path(slots_root) / ".staging" / f"descriptor-{candidate_slot}.json"


def _load_signature_assertions(directory: Path) -> list[SignatureAssertion]:
    """Reads every attestation file the worker staged (see
    runtime_handoff.stage_attestations's own fixed-path convention)
    and independently parses it -- this server never trusts the
    worker's OWN interpretation of a signature, only the raw bytes it
    copied verbatim from root-trusted Git. Schema:
    {"schema_version": 1, "signer_id": "...", "signature_base64": "..."}
    A malformed/unreadable attestation file is skipped (not raised) --
    it simply contributes nothing toward the threshold, exactly like
    an absent or corrupt signature legitimately would; evaluate_
    threshold's own unsatisfied-threshold outcome is what actually
    refuses the candidate, not a parse exception here."""
    import base64
    import json

    assertions: list[SignatureAssertion] = []
    if not directory.is_dir():
        return assertions
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(data, dict) or data.get("schema_version") != 1
                    or not isinstance(data.get("signer_id"), str)
                    or not isinstance(data.get("signature_base64"), str)):
                continue
            signature = base64.b64decode(data["signature_base64"], validate=True)
        except Exception:
            continue
        assertions.append(SignatureAssertion(signer_id=data["signer_id"], signature=signature))
    return assertions


class IPCServer:
    def __init__(
        self, config: BootstrapConfig, trust_policy: TrustPolicy, *,
        layout: SlotLayout | None = None,
        bootstrap_protocol_version: int = BOOTSTRAP_PROTOCOL_VERSION_CURRENT,
        wire_protocol_version: int = WIRE_PROTOCOL_VERSION_CURRENT,
        authorized_uids: set[int] | None = None,
    ):
        self.config = config
        self.trust_policy = trust_policy
        self.layout = layout or SlotLayout(config.slots_root)
        self.bootstrap_protocol_version = bootstrap_protocol_version
        self.wire_protocol_version = wire_protocol_version
        # {0} in production (root only, matching is_authorized_root_
        # peer's own single-UID rule) -- overridable ONLY so this
        # class can be exercised end-to-end under an unprivileged test
        # process, exactly like isadoraair_updater.daemon.
        # UpdaterDaemon's own authorized_uids parameter already does.
        self.authorized_uids = authorized_uids if authorized_uids is not None else {0}
        self.state: RuntimeState = read_runtime_state(config.runtime_state_path)
        self._lock = threading.Lock()
        # D3-A: correlates the WORKER's own idempotency key (wire
        # Request.transaction_id, by convention the durable job_id --
        # see supervisor_client.py's own docstring) to this
        # supervisor's internally-generated ActivationTransaction.
        # transaction_id (supervisor.begin_transaction() always mints
        # its own; it is never caller-supplied). Purely an in-memory
        # convenience for detecting a retried REQUEST_ACTIVATION
        # within the SAME process lifetime -- a supervisor restart
        # loses it, which is safe: a retried request after a restart
        # simply finds `self.state.activation is not None` (loaded
        # fresh from the durable state file) and is refused as "a
        # transaction is already in flight," exactly like any other
        # unrelated conflicting request would be; the worker's own
        # GET_ACTIVATION_STATUS/recovery path (D3-H) is what actually
        # resolves that case, not this dict.
        self._client_transactions: dict[str, str] = {}
        self._socket: socket.socket | None = None
        self._stop = threading.Event()

    def _persist(self, new_state: RuntimeState) -> None:
        write_runtime_state_atomically(self.config.runtime_state_path, new_state)
        self.state = new_state

    def _status_payload(self) -> dict:
        activation = self.state.activation
        return {
            "ok": True,
            "active_slot": self.state.active_slot.value,
            "active_generation": self.state.active_generation,
            "active_descriptor_sha256": self.state.active_descriptor_sha256,
            "activation_in_flight": activation is not None,
            "phase": activation.phase.value if activation is not None else None,
            "runtime_activation_accepted": (
                activation is not None and runtime_activation_accepted(activation.phase)
            ),
        }

    def _handle_request_activation(self, request) -> dict:
        with self._lock:
            internal_id = self._client_transactions.get(request.transaction_id)
            if (internal_id is not None and self.state.activation is not None
                    and self.state.activation.transaction_id == internal_id):
                # D3-A: idempotent replay of an in-flight request from
                # the SAME worker process (e.g. a transport retry) --
                # never begins a second transaction, never re-verifies;
                # just reports current status.
                return self._status_payload()
            if self.state.activation is not None:
                return {"ok": False, "error": "SupervisorError", "detail": "a transaction is already in flight"}
            try:
                candidate_slot = Slot(request.candidate_slot)
            except ValueError:
                return {"ok": False, "error": "ProtocolError", "detail": "invalid candidate_slot"}

            try:
                staged_state = begin_transaction(
                    self.state, candidate_slot=candidate_slot,
                    candidate_generation=request.candidate_generation,
                    candidate_descriptor_sha256=request.candidate_descriptor_sha256,
                )
            except SupervisorError as exc:
                return {"ok": False, "error": "SupervisorError", "detail": str(exc)}
            self._persist(staged_state)
            self._client_transactions[request.transaction_id] = staged_state.activation.transaction_id

            # D3-A/D3-I: INDEPENDENT re-verification. Never trusts the
            # worker's own claim -- reads the descriptor/attestation
            # bytes the worker staged (fixed, convention-based paths;
            # no path ever arrives over the wire) and re-derives every
            # cryptographic/inventory fact itself.
            descriptor_path = _descriptor_path(self.config.slots_root, candidate_slot.value)
            try:
                descriptor_bytes = descriptor_path.read_bytes()
            except OSError:
                return self._fail_in_flight("candidate descriptor is not staged where expected")
            assertions = _load_signature_assertions(
                _attestations_dir(self.config.slots_root, candidate_slot.value),
            )
            # previous_generation is always self.state.active_generation
            # here (never None): begin_transaction() already refused
            # candidate_slot is state.active_slot above, and this
            # supervisor's own RuntimeState always carries a real
            # active_generation once D0 bootstrap has run once --
            # generation is one monotonic counter across the whole
            # system, never per-slot.
            #
            # candidate_minimum_bootstrap_protocol_version is a
            # manifest-level fact (protected_runtime.minimum_
            # bootstrap_protocol_version) protocol.py's own Request has
            # deliberately no field for -- see verification.py's own
            # docstring for why (it is not a descriptor fact). Pinned
            # to 1 (the only bootstrap protocol version that has ever
            # existed) until a future protocol bump requires threading
            # a real value through a NEW, still-bounded Request field;
            # a value greater than what this supervisor understands
            # would still be safely refused once that exists.
            result = verify_candidate_bundle(
                release_id=request.release_id, previous_release_id=request.previous_release_id,
                previous_generation=self.state.active_generation,
                descriptor_bytes=descriptor_bytes, bundle_root=self.layout.slot_path(candidate_slot),
                trust_policy=self.trust_policy, assertions=assertions,
                current_bootstrap_protocol_version=self.bootstrap_protocol_version,
                current_wire_protocol_version=self.wire_protocol_version,
                candidate_minimum_bootstrap_protocol_version=1,
            )
            if not result.ok:
                return self._fail_in_flight("; ".join(result.reasons))
            # The worker's wire claims (candidate_generation,
            # candidate_descriptor_sha256 -- already recorded into
            # this transaction by begin_transaction() above) must
            # match what THIS supervisor independently just derived
            # from the real staged bytes -- never merely "verification
            # succeeded" in isolation. A mismatch here means the wire
            # request described a DIFFERENT candidate than the one
            # actually staged; fail closed rather than committing
            # whichever value happened to be typed on the wire.
            if (result.descriptor is None or result.descriptor_sha256 is None
                    or result.descriptor.generation != request.candidate_generation
                    or result.descriptor_sha256 != request.candidate_descriptor_sha256):
                return self._fail_in_flight(
                    "independently-verified candidate identity does not match the activation request"
                )

            verified_state = mark_candidate_verified(self.state)
            self._persist(verified_state)
            # self._persist() above already advanced self.state to
            # verified_state -- request_activation() below is
            # deliberately called against self.state (not a stale
            # local), so it always operates on whatever this
            # supervisor's own durable record currently says, exactly
            # like every other call site in this class.
            requested_state = request_activation(self.state)
            self._persist(requested_state)
            return self._status_payload()

    def _fail_in_flight(self, detail: str) -> dict:
        from .supervisor import fail_transaction
        try:
            failed_state = fail_transaction(self.state)
            self._persist(failed_state)
        except SupervisorError:
            pass
        return {"ok": False, "error": "CandidateRejected", "detail": detail[:500]}

    def _dispatch(self, request) -> dict:
        if request.action == "PING":
            return {
                "ok": True, "bootstrap_protocol_version": self.bootstrap_protocol_version,
                "active_slot": self.state.active_slot.value, "active_generation": self.state.active_generation,
            }
        if request.action == "GET_RUNTIME_STATE":
            return self._status_payload()
        if request.action == "REQUEST_ACTIVATION":
            return self._handle_request_activation(request)
        if request.action == "GET_ACTIVATION_STATUS":
            with self._lock:
                internal_id = self._client_transactions.get(request.transaction_id)
                if (self.state.activation is None
                        or (internal_id is not None and self.state.activation.transaction_id != internal_id)):
                    return {"ok": True, "activation_in_flight": False, "phase": None,
                            "runtime_activation_accepted": False,
                            "active_slot": self.state.active_slot.value,
                            "active_generation": self.state.active_generation,
                            "active_descriptor_sha256": self.state.active_descriptor_sha256}
                return self._status_payload()
        raise ProtocolError("unknown action")

    def handle_connection(self, connection: socket.socket) -> None:
        try:
            # Read the request BEFORE the authorization check, even
            # for a peer that will end up refused -- draining
            # whatever the peer already sent avoids leaving unread
            # bytes in this socket's own receive buffer at close time,
            # which Linux can otherwise answer with an abrupt RST
            # instead of a clean FIN, breaking the CLIENT's own read
            # loop with ConnectionResetError instead of delivering the
            # (still fully legitimate) rejection response. Matches
            # isadoraair_updater/daemon.py's own proven order exactly
            # (_read_request() before its own PEERCRED check).
            data = bytearray()
            while len(data) <= MAX_REQUEST_BYTES:
                chunk = connection.recv(min(4096, MAX_REQUEST_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if b"\n" in chunk:
                    break
            peer_uid = authorized_peer_uid(connection)
            if peer_uid not in self.authorized_uids:
                raise ProtocolError("peer is not authorized")
            request = decode_request(bytes(data).rstrip(b"\n"))
            response = self._dispatch(request)
        except ProtocolError as exc:
            response = {"ok": False, "error": "ProtocolError", "detail": str(exc)[:500]}
        except (StateError, IndeterminateStateWriteError) as exc:
            response = {"ok": False, "error": "DurabilityError", "detail": str(exc)[:500]}
        except Exception:
            response = {"ok": False, "error": "InternalError", "detail": "activation request failed after an indeterminate boundary"}
        try:
            connection.sendall(encode_response(response))
        except OSError:
            return

    def _prepare_socket(self) -> socket.socket:
        path = self.config.activation_socket
        parent = path.parent
        assert_root_protected_parents(path)
        if path.exists() or path.is_symlink():
            import stat
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise IPCServerError("refusing to replace a non-root/non-socket IPC path")
            path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(16)
        server.settimeout(1.0)
        return server

    def serve_forever(self) -> None:
        self._socket = self._prepare_socket()
        while not self._stop.is_set():
            try:
                connection, _address = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                # stop() may close self._socket while accept() is
                # blocked/about to be entered -- harmless once a stop
                # has actually been requested; anything else re-raises
                # rather than silently swallowing a real bind/accept
                # failure.
                if self._stop.is_set():
                    break
                raise
            with connection:
                connection.settimeout(10)
                self.handle_connection(connection)

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
