"""Test support: a REAL isadoraair-validation service, as a transient unit of
the invoking user's own systemd user manager, with the lifecycle properties of
deploy/isadoraair-validation.service (delegated cpu/memory/pids subtree,
DelegateSubgroup=supervisor, KillMode=control-group, Restart=always). Nothing
here needs or uses privilege, and nothing touches a system unit.

Tests use it where the SERVICE's own environment is what is being tested: a
station runtime lacking a decoder or GI, tools that hang or escape, the
service's own limits, and its death, stop and restart.
"""
from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from django.test import override_settings

REPO = Path(__file__).resolve().parents[2]
UNIT_PROPERTIES = (
    "Delegate=cpu memory pids", "DelegateSubgroup=supervisor", "KillMode=control-group",
    "Restart=always", "RestartSec=1", "UMask=0077",
)


def _manager(method: str, unit: str) -> None:
    subprocess.run(["busctl", "--user", "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager", method, "ss", unit, "replace"],
                   check=True, capture_output=True, timeout=30)


class TransientValidationService:
    def __init__(self, *, env=None, path_prefix=None, limits=None):
        self.runtime = Path(tempfile.mkdtemp(prefix="isadoraair-validation-t.",
                                             dir=os.environ.get("XDG_RUNTIME_DIR") or None))
        self.runtime.chmod(0o700)
        self.socket = str(self.runtime / "validator.sock")
        self.unit = f"isadoraair-validation-t{uuid.uuid4().hex[:12]}.service"
        variables = {name: value for name, value in os.environ.items() if name.startswith("GST_")}
        variables.update(HOME=os.environ.get("HOME", "/"), PRODUCTION_VALIDATION_SOCKET=self.socket,
                         PATH=(f"{path_prefix}:" if path_prefix else "") + os.environ["PATH"])
        variables.update(env or {})
        command = ["systemd-run", "--user", "--quiet", "--collect", f"--unit={self.unit}",
                   f"--property=WorkingDirectory={REPO}"]
        command += [f"--property={item}" for item in UNIT_PROPERTIES]
        command += [f"--setenv={name}={value}" for name, value in variables.items()]
        command += [sys.executable, str(REPO / "manage.py"), "production_validation_service"]
        command += [f"--limit={name}={value}" for name, value in (limits or {}).items()]
        subprocess.run(command, check=True, capture_output=True, timeout=30)
        self.wait_ready()

    # -- lifecycle -------------------------------------------------------------
    def wait_ready(self, seconds=30.0, *, not_pid=None):
        """Until a (new) service process accepts connections -- a socket FILE
        alone may be the dead predecessor's."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            pid = self.main_pid()
            if pid and pid != not_pid and self._accepts():
                return pid
            time.sleep(0.05)
        raise AssertionError(f"{self.unit} did not become ready")

    def _accepts(self):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1)
            probe.connect(self.socket)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def stop(self):
        try:
            _manager("StopUnit", self.unit)
        except subprocess.CalledProcessError:
            pass
        deadline = time.monotonic() + 30
        while self.cgroup() is not None and time.monotonic() < deadline:
            time.sleep(0.05)
        shutil.rmtree(self.runtime, ignore_errors=True)

    def restart(self):
        _manager("RestartUnit", self.unit)

    def kill_supervisor(self, signum=9):
        pid = self.main_pid()
        os.kill(pid, signum)
        return pid

    # -- what the kernel says ----------------------------------------------------
    def cgroup(self):
        uid = os.getuid()
        found = glob.glob(f"/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service/*/{self.unit}")
        return Path(found[0]) if found else None

    def main_pid(self):
        cgroup = self.cgroup()
        try:
            pids = (cgroup / "supervisor" / "cgroup.procs").read_text().split() if cgroup else []
        except OSError:
            return None
        return int(pids[0]) if pids else None

    def leaves(self):
        cgroup = self.cgroup()
        root = cgroup / "iportal-validation" if cgroup else None
        if root is None or not root.is_dir():
            return []
        return sorted(path for path in root.iterdir() if path.name.startswith("run-"))

    @staticmethod
    def tasks(leaf):
        try:
            return [int(pid) for pid in (leaf / "cgroup.procs").read_text().split()]
        except OSError:
            return []

    # -- using it from a test -------------------------------------------------------
    @contextmanager
    def active(self):
        """Point this test process's validation client at this service."""
        with override_settings(PRODUCTION_VALIDATION_SOCKET=self.socket):
            yield self


@contextmanager
def validation_service(**options):
    service = TransientValidationService(**options)
    try:
        with service.active():
            yield service
    finally:
        service.stop()
