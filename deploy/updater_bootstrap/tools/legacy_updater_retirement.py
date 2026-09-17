"""Retirement of the obsolete pre-Phase-D `isadoraair-updater.service` unit
on a station that has already established Phase-D authority.

Context: before the Phase-D bootstrap supervisor (`updater-bootstrapd.
service`) existed, `isadoraair-updater.service` WAS the protected updater
(r0003-r0006 manual bridge era -- see docs/UPDATE_CENTER.md). r0026's D0
final bootstrap bridge installed the supervisor ALONGSIDE the existing
unit, deliberately leaving it in place as a rollback path, and never
defined what should happen to it afterward (docs/UPDATE_CENTER_PHASE_D.md's
own "what remains" list: "... retiring the old updater, r0026/r0027").

This module is that missing definition. The canonical completed-Phase-D
systemd authority contract:

    updater-bootstrapd.service:  enabled, active
    isadoraair-updater.service:  masked,  inactive

`disabled` (merely not auto-starting at boot) is NOT sufficient: a
disabled unit remains trivially startable by `systemctl start`, and both
units declare an overlapping `RuntimeDirectory=isadoraair-updater` --
starting the legacy unit is capable of colliding with the live
supervisor-owned worker socket directory. `masked` (a symlink to
/dev/null) makes `systemctl start isadoraair-updater.service` fail
immediately, closing that hazard structurally rather than by convention.

This module is deliberately dependency-free (no Django, no application
checkout import) -- it mirrors updatecenter/backend_client.py's own
"unprivileged strict client" convention, since it must run standalone as
root via `sudo python3` against a real station, exactly like this
package's sibling protected_runtime_release.py (release-authoring) and
the real worker's own daemon.py (station execution).

Two halves:
  - Pure, unprivileged, fully unit-testable decision logic (dataclasses +
    check_retirement_preflight/is_already_retired) -- everything above
    `main()`.
  - Privileged mechanics (systemctl/file operations) -- everything from
    `gather_snapshot` down, exercised for real only when run as root.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import hashlib
import json
import shutil
import socket
import subprocess
import sys
import uuid
from pathlib import Path


LEGACY_UNIT = "isadoraair-updater.service"
SUPERVISOR_UNIT = "updater-bootstrapd.service"
LEGACY_UNIT_PATH = Path("/etc/systemd/system") / LEGACY_UNIT
LEGACY_UNIT_DROPIN_DIR = Path("/etc/systemd/system") / f"{LEGACY_UNIT}.d"
RUNTIME_STATE_PATH = Path("/var/lib/isadoraair-updater-bootstrap/runtime-state.json")
WORKER_SOCKET_PATH = Path("/run/isadoraair-updater/updater.sock")
ROLLBACK_ROOT = Path("/var/backups/isadoraair")
SYSTEMCTL = "/usr/bin/systemctl"
PROTOCOL_VERSION = 3
MAX_RESPONSE_BYTES = 131072


# --------------------------------------------------------------------------
# Pure decision logic -- no subprocess, no filesystem I/O, fully testable
# without root.
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SystemdUnitState:
    load_state: str
    unit_file_state: str
    active_state: str


@dataclasses.dataclass(frozen=True)
class RuntimeStateFacts:
    active_generation: int
    active_descriptor_sha256: str
    active_slot: str
    activation: object


@dataclasses.dataclass(frozen=True)
class PingFacts:
    ok: bool
    protected_runtime_valid: bool
    update_execution_enabled: bool
    maintenance_busy: bool


@dataclasses.dataclass(frozen=True)
class PreflightSnapshot:
    supervisor: SystemdUnitState
    legacy: SystemdUnitState
    worker_socket_exists: bool
    ping: PingFacts | None
    runtime_state: RuntimeStateFacts | None


class RetirementRefused(RuntimeError):
    """A preflight condition was not satisfied. Never raised for the
    legacy-unit-active case -- see LegacyUnitActiveError."""


class LegacyUnitActiveError(RetirementRefused):
    """The legacy unit is unexpectedly ACTIVE. This is deliberately a
    distinct exception type: the normal retirement procedure must never
    attempt `systemctl stop isadoraair-updater.service` on its own
    initiative (its shared RuntimeDirectory= ownership could remove
    /run/isadoraair-updater out from under the live supervisor-owned
    worker) -- an active legacy unit requires a separate, deliberately
    NOT-implemented-here controlled recovery procedure."""


def is_already_retired(snapshot: PreflightSnapshot) -> bool:
    """True iff the legacy unit is already in the canonical retired
    state (masked + inactive) -- the idempotent no-op case."""
    return snapshot.legacy.unit_file_state == "masked" and snapshot.legacy.active_state == "inactive"


def check_retirement_preflight(snapshot: PreflightSnapshot) -> None:
    """Raises RetirementRefused (LegacyUnitActiveError for the one
    special case) with a specific, actionable reason unless every
    required condition holds. Never mutates anything -- callers decide
    what to do with a clean bill of health."""

    # The one condition checked first and distinctly: an active legacy
    # unit is a STOP condition, not merely one more failed check.
    if snapshot.legacy.active_state not in {"inactive", "failed"}:
        raise LegacyUnitActiveError(
            f"{LEGACY_UNIT} is {snapshot.legacy.active_state!r}, not inactive -- STOP. "
            "Do not run `systemctl stop` on it as part of normal retirement; its shared "
            "RuntimeDirectory=isadoraair-updater ownership could remove the live worker's "
            "socket directory. This requires a separate, deliberately manual recovery decision."
        )

    if snapshot.supervisor.load_state != "loaded":
        raise RetirementRefused(f"{SUPERVISOR_UNIT} is not loaded (load_state={snapshot.supervisor.load_state!r})")
    if snapshot.supervisor.unit_file_state != "enabled":
        raise RetirementRefused(f"{SUPERVISOR_UNIT} is not enabled (unit_file_state={snapshot.supervisor.unit_file_state!r})")
    if snapshot.supervisor.active_state != "active":
        raise RetirementRefused(f"{SUPERVISOR_UNIT} is not active (active_state={snapshot.supervisor.active_state!r})")

    if not snapshot.worker_socket_exists:
        raise RetirementRefused(f"worker socket {WORKER_SOCKET_PATH} does not exist -- worker is not reachable")

    if snapshot.ping is None or not snapshot.ping.ok:
        raise RetirementRefused("supervisor/worker PING did not succeed -- backend is not reachable")
    if not snapshot.ping.protected_runtime_valid:
        raise RetirementRefused("worker reports protected_runtime_valid=false")
    if not snapshot.ping.update_execution_enabled:
        raise RetirementRefused("worker reports update_execution_enabled=false")
    if snapshot.ping.maintenance_busy:
        raise RetirementRefused(
            "worker reports maintenance_busy=true -- an update/handoff may be in flight; "
            "this is this tool's own proxy for 'no active/locked UpdateJob', checked from "
            "the privileged side without any Django/database dependency"
        )

    if snapshot.runtime_state is None:
        raise RetirementRefused("protected-runtime state is unreadable or invalid")
    if snapshot.runtime_state.activation is not None:
        raise RetirementRefused(
            f"protected-runtime activation is in flight ({snapshot.runtime_state.activation!r}), not null"
        )

    if snapshot.legacy.load_state not in {"loaded", "not-found"}:
        raise RetirementRefused(f"{LEGACY_UNIT} load_state is unexpected: {snapshot.legacy.load_state!r}")


# --------------------------------------------------------------------------
# Privileged mechanics -- subprocess/filesystem I/O. Exercised for real
# only under root; individually testable with fakes/monkeypatching.
# --------------------------------------------------------------------------


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [SYSTEMCTL, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, timeout=15, check=False,
    )


def _unit_state(unit: str) -> SystemdUnitState:
    result = _systemctl("show", unit, "--property=LoadState,UnitFileState,ActiveState")
    values = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            values[key] = value
    return SystemdUnitState(
        load_state=values.get("LoadState", "unknown"),
        unit_file_state=values.get("UnitFileState", "unknown"),
        active_state=values.get("ActiveState", "unknown"),
    )


def _ping_backend(socket_path: Path = WORKER_SOCKET_PATH, *, timeout: float = 5.0) -> PingFacts | None:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(timeout)
            connection.connect(str(socket_path))
            request = json.dumps({"protocol_version": PROTOCOL_VERSION, "action": "PING"}).encode("utf-8") + b"\n"
            connection.sendall(request)
            chunks = []
            while True:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk or sum(len(c) for c in chunks) > MAX_RESPONSE_BYTES:
                    break
        raw = b"".join(chunks)
        payload = json.loads(raw.decode("utf-8").strip())
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("ok"):
        return PingFacts(ok=False, protected_runtime_valid=False, update_execution_enabled=False, maintenance_busy=True)
    return PingFacts(
        ok=True,
        protected_runtime_valid=payload.get("protected_runtime_valid") is True,
        update_execution_enabled=payload.get("update_execution_enabled") is True,
        maintenance_busy=payload.get("maintenance_busy") is not False,
    )


def _read_runtime_state(path: Path = RUNTIME_STATE_PATH) -> RuntimeStateFacts | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return RuntimeStateFacts(
            active_generation=data["active_generation"],
            active_descriptor_sha256=data["active_descriptor_sha256"],
            active_slot=data["active_slot"],
            activation=data.get("activation"),
        )
    except KeyError:
        return None


def gather_snapshot() -> PreflightSnapshot:
    """Collects everything check_retirement_preflight() needs from the
    real host -- the only function in this module that touches multiple
    real subsystems at once, deliberately kept this thin so the decision
    logic above never needs a live system to be tested."""
    return PreflightSnapshot(
        supervisor=_unit_state(SUPERVISOR_UNIT),
        legacy=_unit_state(LEGACY_UNIT),
        worker_socket_exists=WORKER_SOCKET_PATH.exists(),
        ping=_ping_backend(),
        runtime_state=_read_runtime_state(),
    )


def create_rollback_directory(*, root: Path = ROLLBACK_ROOT, now: datetime.datetime | None = None) -> Path:
    timestamp = (now or datetime.datetime.now(datetime.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    destination = root / f"legacy-updater-retirement-{timestamp}"
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    return destination


def _sha256_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture_rollback_evidence(rollback_dir: Path, snapshot: PreflightSnapshot) -> dict:
    """Copies the unit file/drop-ins and records identifying state --
    root-only permissions, no secrets (systemd unit files/state JSON
    here carry no credentials)."""
    evidence: dict = {"schema_version": 1, "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    if LEGACY_UNIT_PATH.is_file():
        saved_unit = rollback_dir / LEGACY_UNIT_PATH.name
        shutil.copy2(LEGACY_UNIT_PATH, saved_unit)
        saved_unit.chmod(0o600)
        evidence["legacy_unit_file"] = str(saved_unit)
        evidence["legacy_unit_sha256"] = _sha256_of(saved_unit)
    else:
        evidence["legacy_unit_file"] = None
        evidence["legacy_unit_sha256"] = None

    if LEGACY_UNIT_DROPIN_DIR.is_dir():
        saved_dropins = rollback_dir / LEGACY_UNIT_DROPIN_DIR.name
        shutil.copytree(LEGACY_UNIT_DROPIN_DIR, saved_dropins)
        for dropin in saved_dropins.rglob("*"):
            if dropin.is_file():
                dropin.chmod(0o600)
        evidence["legacy_unit_dropins"] = str(saved_dropins)
    else:
        evidence["legacy_unit_dropins"] = None

    show = _systemctl(
        "show", LEGACY_UNIT, SUPERVISOR_UNIT,
        "--property=LoadState,UnitFileState,ActiveState,MainPID,FragmentPath",
    )
    (rollback_dir / "systemctl-show-before.txt").write_text(show.stdout, encoding="utf-8")
    (rollback_dir / "systemctl-show-before.txt").chmod(0o600)

    evidence["supervisor"] = dataclasses.asdict(snapshot.supervisor)
    evidence["legacy_before"] = dataclasses.asdict(snapshot.legacy)
    evidence["worker_socket_exists"] = snapshot.worker_socket_exists
    evidence["ping"] = dataclasses.asdict(snapshot.ping) if snapshot.ping else None
    evidence["runtime_state"] = dataclasses.asdict(snapshot.runtime_state) if snapshot.runtime_state else None

    try:
        socket_stat = WORKER_SOCKET_PATH.stat()
        evidence["worker_socket_inode"] = socket_stat.st_ino
        evidence["worker_socket_mode"] = oct(socket_stat.st_mode)
        evidence["worker_socket_uid"] = socket_stat.st_uid
        evidence["worker_socket_gid"] = socket_stat.st_gid
    except OSError:
        evidence["worker_socket_inode"] = None

    evidence_path = rollback_dir / "evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
    evidence_path.chmod(0o600)
    rollback_dir.chmod(0o700)
    return evidence


@dataclasses.dataclass(frozen=True)
class RetirementResult:
    already_retired: bool
    rollback_dir: Path | None
    is_enabled_after: str
    is_active_after: str


def retire(*, apply: bool = False) -> RetirementResult:
    """The one idempotent, strictly-preflighted retirement operation.

    apply=False (default) performs preflight and reports what WOULD
    happen -- never mutates anything. apply=True performs the real
    sequence: disable-if-enabled -> backup -> remove installed regular
    unit file -> systemctl mask -> daemon-reload -> verify. Never
    restarts updater-bootstrapd.service -- masking an unrelated inactive
    unit has no reason to."""
    snapshot = gather_snapshot()

    if is_already_retired(snapshot):
        return RetirementResult(
            already_retired=True, rollback_dir=None,
            is_enabled_after=snapshot.legacy.unit_file_state, is_active_after=snapshot.legacy.active_state,
        )

    check_retirement_preflight(snapshot)  # raises on any unmet condition

    if not apply:
        return RetirementResult(
            already_retired=False, rollback_dir=None,
            is_enabled_after=snapshot.legacy.unit_file_state, is_active_after=snapshot.legacy.active_state,
        )

    rollback_dir = create_rollback_directory()
    capture_rollback_evidence(rollback_dir, snapshot)

    if snapshot.legacy.unit_file_state == "enabled":
        disable_result = _systemctl("disable", LEGACY_UNIT)
        if disable_result.returncode != 0:
            raise RetirementRefused(f"systemctl disable {LEGACY_UNIT} failed: {disable_result.stderr.strip()}")

    if LEGACY_UNIT_PATH.exists():
        LEGACY_UNIT_PATH.unlink()

    mask_result = _systemctl("mask", LEGACY_UNIT)
    if mask_result.returncode != 0:
        raise RetirementRefused(f"systemctl mask {LEGACY_UNIT} failed: {mask_result.stderr.strip()}")

    reload_result = _systemctl("daemon-reload")
    if reload_result.returncode != 0:
        raise RetirementRefused(f"systemctl daemon-reload failed: {reload_result.stderr.strip()}")

    after = _unit_state(LEGACY_UNIT)
    if after.unit_file_state != "masked" or after.active_state != "inactive":
        raise RetirementRefused(
            f"post-mask verification failed: unit_file_state={after.unit_file_state!r}, "
            f"active_state={after.active_state!r}"
        )

    (rollback_dir / "systemctl-show-after.txt").write_text(
        _systemctl("show", LEGACY_UNIT, SUPERVISOR_UNIT,
                   "--property=LoadState,UnitFileState,ActiveState,MainPID").stdout,
        encoding="utf-8",
    )
    (rollback_dir / "systemctl-show-after.txt").chmod(0o600)

    return RetirementResult(
        already_retired=False, rollback_dir=rollback_dir,
        is_enabled_after=after.unit_file_state, is_active_after=after.active_state,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Perform the real retirement (default: dry-run/check only).")
    args = parser.parse_args(argv)

    try:
        result = retire(apply=args.apply)
    except RetirementRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1

    if result.already_retired:
        print(f"ALREADY RETIRED: {LEGACY_UNIT} is masked/inactive -- no action taken.")
        return 0
    if not args.apply:
        print(f"PREFLIGHT OK: {LEGACY_UNIT} may be retired (dry-run -- pass --apply to perform it).")
        return 0
    print(f"RETIRED: {LEGACY_UNIT} is now {result.is_enabled_after}/{result.is_active_after}.")
    print(f"Rollback evidence: {result.rollback_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
