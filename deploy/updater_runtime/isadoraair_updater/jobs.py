"""Root-owned durable job state and append-only bounded log access."""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .security import assert_root_protected, assert_root_protected_parents


TERMINAL_STATES = frozenset({"succeeded", "failed", "manual_intervention_required"})
ACTIVE_STATES = frozenset({"accepted", "running"})
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")
MAX_JOB_RECORDS = 1000
MAX_MAINTENANCE_RECORDS = 100
MAINTENANCE_ACTIONS = frozenset({"RESTART_OPERATOR_SERVICE", "STORE_ALSA_STATE"})
MAINTENANCE_STATES = frozenset({"accepted", "running", "succeeded", "failed"})


class JobError(ValueError):
    pass


def _canonical_job_id(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise JobError("invalid job id") from exc
    if str(parsed) != value:
        raise JobError("job id is not canonical")
    return value


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class JobStore:
    def __init__(self, jobs_root: Path, logs_root: Path, *, acquire_daemon_lock: bool = True):
        self.jobs_root = Path(jobs_root)
        self.logs_root = Path(logs_root)
        assert_root_protected_parents(self.jobs_root)
        assert_root_protected_parents(self.logs_root)
        self.jobs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        assert_root_protected(self.jobs_root)
        assert_root_protected(self.logs_root)
        os.chmod(self.jobs_root, 0o700)
        os.chmod(self.logs_root, 0o700)
        self.maintenance_root = self.jobs_root / "maintenance"
        self.maintenance_root.mkdir(mode=0o700, exist_ok=True)
        assert_root_protected(self.maintenance_root)
        os.chmod(self.maintenance_root, 0o700)
        self._lock_handle = None
        if acquire_daemon_lock:
            lock_path = self.jobs_root / ".daemon.lock"
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            lock_fd = os.open(lock_path, flags, 0o600)
            lock_info = os.fstat(lock_fd)
            if not stat.S_ISREG(lock_info.st_mode) or (os.geteuid() == 0 and lock_info.st_uid != 0):
                os.close(lock_fd)
                raise JobError("daemon lock protection is invalid")
            self._lock_handle = os.fdopen(lock_fd, "r+b")
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(self._lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                self._lock_handle.close()
                raise JobError("another updater daemon owns the job store") from exc

    def close(self):
        if self._lock_handle is not None:
            fcntl.flock(self._lock_handle, fcntl.LOCK_UN)
            self._lock_handle.close()
            self._lock_handle = None

    def _state_path(self, job_id: str) -> Path:
        return self.jobs_root / f"{_canonical_job_id(job_id)}.json"

    def _log_path(self, job_id: str) -> Path:
        return self.logs_root / f"{_canonical_job_id(job_id)}.log"

    def _maintenance_path(self, operation_id: str) -> Path:
        return self.maintenance_root / f"{_canonical_job_id(operation_id)}.json"

    def _atomic_write(self, path: Path, data: dict):
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(raw) > 1024 * 1024:
            raise JobError("job state exceeds 1 MiB")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def load(self, job_id: str) -> dict:
        path = self._state_path(job_id)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise JobError("job does not exist") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or (os.geteuid() == 0 and info.st_uid != 0)):
                raise JobError("job state protection is invalid")
            raw = os.read(fd, 1024 * 1024 + 1)
        finally:
            os.close(fd)
        if len(raw) > 1024 * 1024:
            raise JobError("job state exceeds its size limit")
        try:
            state = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise JobError("job state is corrupt") from exc
        if not isinstance(state, dict) or state.get("job_id") != job_id or state.get("schema_version") != 1:
            raise JobError("job state identity/schema mismatch")
        return state

    def list_states(self) -> list[dict]:
        result = []
        for path in sorted(self.jobs_root.glob("*.json")):
            try:
                result.append(self.load(path.stem))
            except JobError:
                continue
        return result

    def accept(self, job_id: str, target_release: str, fingerprint: str) -> tuple[dict, bool]:
        _canonical_job_id(job_id)
        path = self._state_path(job_id)
        if path.exists():
            current = self.load(job_id)
            if current.get("requested_target_release_id") != target_release or current.get("expected_plan_fingerprint") != fingerprint:
                raise JobError("same job id was reused with different authorization facts")
            return current, False
        if len(list(self.jobs_root.glob("*.json"))) >= MAX_JOB_RECORDS:
            raise JobError("root job-state retention limit reached; operator review is required")
        for state in self.list_states():
            if state.get("state") in ACTIVE_STATES:
                raise JobError("another update job is active")
        now = _now()
        state = {
            "schema_version": 1,
            "job_id": job_id,
            "requested_target_release_id": target_release,
            "expected_plan_fingerprint": fingerprint,
            "state": "accepted",
            "current_step": "accepted",
            "milestones": [],
            "created_at": now,
            "updated_at": now,
            "failure_classification": "",
            "failure_detail": "",
            "trusted_plan": None,
            "checkpoint": None,
        }
        self._atomic_write(path, state)
        self.append_log(job_id, "job accepted")
        return state, True

    def update(self, job_id: str, **changes) -> dict:
        state = self.load(job_id)
        allowed = {"state", "current_step", "failure_classification", "failure_detail", "trusted_plan", "checkpoint"}
        if set(changes) - allowed:
            raise JobError("attempt to write unknown job-state fields")
        state.update(changes)
        state["updated_at"] = _now()
        self._atomic_write(self._state_path(job_id), state)
        return state

    def milestone(self, job_id: str, name: str) -> dict:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
            raise JobError("invalid milestone name")
        state = self.load(job_id)
        if name not in state["milestones"]:
            state["milestones"].append(name)
        state["state"] = "running"
        state["current_step"] = name
        state["updated_at"] = _now()
        self._atomic_write(self._state_path(job_id), state)
        self.append_log(job_id, f"milestone: {name}")
        return state

    def fail(self, job_id: str, classification: str, detail: str, *, manual: bool) -> dict:
        safe_detail = " ".join(str(detail).split())[:4000]
        state = self.update(
            job_id,
            state="manual_intervention_required" if manual else "failed",
            current_step="failed",
            failure_classification=classification[:64],
            failure_detail=safe_detail,
        )
        self.append_log(job_id, f"failure {classification}: {safe_detail}")
        return state

    def succeed(self, job_id: str) -> dict:
        state = self.update(job_id, state="succeeded", current_step="succeeded")
        self.append_log(job_id, "job succeeded")
        return state

    def append_log(self, job_id: str, message: str):
        line = json.dumps({"time": _now(), "message": " ".join(str(message).split())[:4000]}, separators=(",", ":")) + "\n"
        path = self._log_path(job_id)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or (os.geteuid() == 0 and info.st_uid != 0)):
                raise JobError("job log protection is invalid")
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

    def tail_log(self, job_id: str, maximum: int) -> str:
        if not 1 <= maximum <= 65536:
            raise JobError("invalid log-tail bound")
        path = self._log_path(job_id)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            return ""
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or (os.geteuid() == 0 and info.st_uid != 0)):
                raise JobError("job log protection is invalid")
            size = info.st_size
            os.lseek(fd, max(0, size - maximum), os.SEEK_SET)
            raw = os.read(fd, maximum)
        finally:
            os.close(fd)
        return raw.decode("utf-8", "replace")

    def load_maintenance(self, operation_id: str) -> dict:
        path = self._maintenance_path(operation_id)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise JobError("maintenance operation does not exist") from exc
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                    or (os.geteuid() == 0 and info.st_uid != 0)):
                raise JobError("maintenance result protection is invalid")
            raw = os.read(fd, 8193)
        finally:
            os.close(fd)
        if len(raw) > 8192:
            raise JobError("maintenance result exceeds its size limit")
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise JobError("maintenance result is corrupt") from exc
        expected_fields = {
            "schema_version", "operation_id", "action", "service", "state",
            "result_code", "result_detail", "created_at", "updated_at",
        }
        if (not isinstance(record, dict) or set(record) != expected_fields
                or record.get("schema_version") != 1
                or record.get("operation_id") != operation_id
                or record.get("action") not in MAINTENANCE_ACTIONS
                or record.get("state") not in MAINTENANCE_STATES
                or not isinstance(record.get("result_code"), str)
                or not isinstance(record.get("result_detail"), str)
                or len(record["result_code"]) > 64 or len(record["result_detail"]) > 160
                or not isinstance(record.get("created_at"), str)
                or not isinstance(record.get("updated_at"), str)):
            raise JobError("maintenance result identity/schema mismatch")
        service = record.get("service")
        if ((record["action"] == "STORE_ALSA_STATE" and service is not None)
                or (record["action"] == "RESTART_OPERATOR_SERVICE" and not isinstance(service, str))):
            raise JobError("maintenance result action/service mismatch")
        return record

    def create_maintenance(self, operation_id: str, action: str, service: str | None) -> dict:
        _canonical_job_id(operation_id)
        if action not in MAINTENANCE_ACTIONS:
            raise JobError("unknown maintenance action")
        if action == "STORE_ALSA_STATE" and service is not None:
            raise JobError("ALSA maintenance cannot carry a service")
        if action == "RESTART_OPERATOR_SERVICE" and not isinstance(service, str):
            raise JobError("restart maintenance requires an exact service")
        records = sorted(self.maintenance_root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for path in records:
            if len(records) < MAX_MAINTENANCE_RECORDS:
                break
            try:
                current = self.load_maintenance(path.stem)
            except JobError:
                continue
            if current["state"] in {"succeeded", "failed"}:
                path.unlink()
                records.remove(path)
        if len(records) >= MAX_MAINTENANCE_RECORDS:
            raise JobError("maintenance result retention limit reached")
        now = _now()
        record = {
            "schema_version": 1,
            "operation_id": operation_id,
            "action": action,
            "service": service,
            "state": "accepted",
            "result_code": "",
            "result_detail": "",
            "created_at": now,
            "updated_at": now,
        }
        self._atomic_write(self._maintenance_path(operation_id), record)
        return record

    def update_maintenance(self, operation_id: str, *, state: str,
                           result_code: str = "", result_detail: str = "") -> dict:
        if state not in MAINTENANCE_STATES - {"accepted"}:
            raise JobError("invalid maintenance state transition")
        record = self.load_maintenance(operation_id)
        record["state"] = state
        record["result_code"] = re.sub(r"[^A-Z0-9_]", "_", str(result_code).upper())[:64]
        record["result_detail"] = re.sub(r"[^A-Za-z0-9_. -]", "?", str(result_detail))[:160]
        record["updated_at"] = _now()
        self._atomic_write(self._maintenance_path(operation_id), record)
        return record
