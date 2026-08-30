"""D2-J: the readiness facts a candidate worker must report, and the
supervisor's own classification of what it observes. Mere PID existence
is never treated as readiness -- ReadinessState.ALIVE_NOT_READY is a
distinct, common, expected state a real candidate spends real time in
during a normal start.

D2 uses only a synthetic fixture worker to exercise this (see
test_phase_d2_readiness.py) -- the real worker's own handoff into this
protocol is explicitly D3's job (D2-S)."""
from __future__ import annotations

import dataclasses
import enum
import re

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SLOT_VALUES = frozenset({"A", "B"})
MAX_WIRE_PROTOCOLS = 8


class ReadinessState(enum.Enum):
    STARTED = "started"
    EXITED = "exited"
    ALIVE_NOT_READY = "alive_not_ready"
    MALFORMED = "malformed"
    WRONG_IDENTITY = "wrong_identity"
    READY = "ready"


class ReadinessError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class ReadinessFacts:
    slot: str
    generation: int
    descriptor_sha256: str
    bootstrap_protocol_version: int
    supported_wire_protocols: tuple[int, ...]
    config_parsed: bool
    privilege_drop_self_check_passed: bool
    job_store_ready: bool
    worker_socket_bound: bool

    def fully_ready(self) -> bool:
        return (
            self.config_parsed and self.privilege_drop_self_check_passed
            and self.job_store_ready and self.worker_socket_bound
        )


def parse_readiness_facts_dict(data) -> ReadinessFacts:
    if not isinstance(data, dict):
        raise ReadinessError("readiness facts must be a JSON object")
    known = {
        "slot", "generation", "descriptor_sha256", "bootstrap_protocol_version",
        "supported_wire_protocols", "config_parsed", "privilege_drop_self_check_passed",
        "job_store_ready", "worker_socket_bound",
    }
    if set(data) != known:
        raise ReadinessError(f"readiness facts must have exactly {sorted(known)!r}")
    slot = data["slot"]
    if slot not in SLOT_VALUES:
        raise ReadinessError("slot must be exactly 'A' or 'B'")
    generation = data["generation"]
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ReadinessError("generation must be a positive integer")
    descriptor_sha256 = data["descriptor_sha256"]
    if not isinstance(descriptor_sha256, str) or not SHA256_RE.match(descriptor_sha256):
        raise ReadinessError("descriptor_sha256 must be exactly 64 lowercase hex characters")
    bootstrap_protocol_version = data["bootstrap_protocol_version"]
    if not isinstance(bootstrap_protocol_version, int) or isinstance(bootstrap_protocol_version, bool) or bootstrap_protocol_version < 1:
        raise ReadinessError("bootstrap_protocol_version must be a positive integer")
    wire = data["supported_wire_protocols"]
    if not isinstance(wire, list) or not wire or len(wire) > MAX_WIRE_PROTOCOLS:
        raise ReadinessError("supported_wire_protocols must be a non-empty, bounded list")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in wire):
        raise ReadinessError("supported_wire_protocols must contain only positive integers")
    if len(set(wire)) != len(wire) or list(wire) != sorted(wire):
        raise ReadinessError("supported_wire_protocols must be unique and canonically sorted")
    bool_fields = ("config_parsed", "privilege_drop_self_check_passed", "job_store_ready", "worker_socket_bound")
    for field in bool_fields:
        if not isinstance(data[field], bool):
            raise ReadinessError(f"{field} must be a boolean")
    return ReadinessFacts(
        slot=slot, generation=generation, descriptor_sha256=descriptor_sha256,
        bootstrap_protocol_version=bootstrap_protocol_version, supported_wire_protocols=tuple(wire),
        config_parsed=data["config_parsed"], privilege_drop_self_check_passed=data["privilege_drop_self_check_passed"],
        job_store_ready=data["job_store_ready"], worker_socket_bound=data["worker_socket_bound"],
    )


def classify_readiness(
    *,
    process_exited: bool,
    raw_facts: dict | None,
    expected_slot: str,
    expected_generation: int,
    expected_descriptor_sha256: str,
) -> tuple[ReadinessState, ReadinessFacts | None]:
    """The one classification function -- pure, no I/O, no process
    handle of its own (the caller has already polled the child and
    passed the plain boolean `process_exited`, and already fetched
    `raw_facts` -- or None if nothing has been reported yet -- via
    GET_ACTIVATION_STATUS or an equivalent readiness query). Keeping
    this pure makes every one of D2-J's required distinctions directly
    testable without a real subprocess."""
    if process_exited:
        return ReadinessState.EXITED, None
    if raw_facts is None:
        return ReadinessState.ALIVE_NOT_READY, None
    try:
        facts = parse_readiness_facts_dict(raw_facts)
    except ReadinessError:
        return ReadinessState.MALFORMED, None
    if (facts.slot, facts.generation, facts.descriptor_sha256) != (
        expected_slot, expected_generation, expected_descriptor_sha256,
    ):
        return ReadinessState.WRONG_IDENTITY, facts
    if not facts.fully_ready():
        return ReadinessState.ALIVE_NOT_READY, facts
    return ReadinessState.READY, facts
