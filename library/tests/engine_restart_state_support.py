"""Shared fixtures for the roadmap 1.20 (revised) restart-safeguard tests.

``engine_state()`` builds an engine_state.json payload with exactly the fields
``PlaybackEngine._write_state`` publishes that the safeguard reads; every test
overrides only what it is about. ``EngineStateFile`` writes it to a private temp
file and points the safeguard at it -- the real /run/isadoraair path is never
read or written.
"""
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

from library.services import engine_restart_safety as safety


def engine_state(**overrides):
    state = {
        "transport": "PLAYING",
        "timestamp": time.time(),
        "engine_session_id": "0b0f6a2c-5e6a-4f0e-9c58-3f6f2f3a9d11",
        "state_revision": {"instance": "0b0f6a2c-5e6a-4f0e-9c58-3f6f2f3a9d11", "seq": 41},
        "mic_configured": True,
        "mic_ok": True,
        "mic_live": False,
        "manual_mode": False,
        "remote_dj_configured": True,
        "remote_dj_connected": False,
        "remote_dj_live": False,
        "remote_dj_reconnecting": False,
        "remote_dj_slot": "available",
        "remote_dj_connection": None,
        # Secret-bearing / identity blocks the safeguard must never copy out.
        "remote_dj_owner": {"user_id": 7, "attempt_id": "ATTEMPT-SECRET-0123456789",
                            "gate_generation": 3},
        "remote_dj_admin_result": {"request_id": "REQ-SECRET", "action": "kick", "outcome": "ok"},
    }
    state.update(overrides)
    return state


class EngineStateFile:
    """Context manager / mixin helper: a temp engine_state.json under test control."""

    def __init__(self, testcase):
        self._dir = tempfile.TemporaryDirectory(prefix="isadoraair-restart-safety-test-")
        testcase.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name) / "engine_state.json"
        patcher = patch.object(safety, "ENGINE_STATE_PATH", self.path)
        patcher.start()
        testcase.addCleanup(patcher.stop)

    def write(self, state=None, **overrides):
        payload = engine_state(**overrides) if state is None else state
        self.path.write_text(json.dumps(payload), encoding="utf-8")

    def write_raw(self, text):
        self.path.write_text(text, encoding="utf-8")

    def remove(self):
        self.path.unlink(missing_ok=True)
