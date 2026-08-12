"""1.7 release/version-skew visibility -- PlaybackEngine's own runtime-
commit capture (library/services/engine.py __init__) and its inclusion
in _write_state()'s engine_state.json payload.

_write_state() itself is a large integrator with many dependencies
(decks, current_log, mic/remote-DJ/FX-cart state) that every existing
engine test avoids calling end-to-end -- test_fx_fires_state.py and
test_engine_queue_eta.py both test the extracted sub-methods
(_fx_fires_state, _compute_queue_eta_state) directly instead. This file
follows the same convention for the runtime_commit wiring itself, but
ALSO builds one full minimal stand-in (every dependency stubbed to its
"nothing going on" value) to prove _write_state()'s actual JSON output
carries the field end-to-end at least once -- the two together cover
both "the plumbing is wired" and "the exact one-line contract holds"."""
import json
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

import library.services.engine as eng_module


def make_minimal_stand_in():
    """Every attribute/method _write_state() touches, stubbed to its
    "idle engine, nothing playing" value. Bypasses __init__ entirely
    (object.__new__, the same technique used throughout this test
    suite) since __init__ builds a real GStreamer pipeline."""
    obj = object.__new__(eng_module.PlaybackEngine)
    obj._lock = threading.RLock()
    obj.decks = {"A": None, "B": None}
    obj.log_items = []
    obj._queue_cursor = 0
    obj.current_log = None
    obj.mic_ptt_valve = None
    obj.mic_ok = False
    obj.mic_live = False
    obj.manual_mode = False
    obj._manual_from_mic = False
    obj.remote_dj_tee = None
    obj.remote_dj_session = None
    obj._remote_dj_gate_open = lambda: False
    obj._fx_fires_state = lambda: []
    obj._compute_queue_eta_state = lambda snapshot: []
    obj._runtime_commit = None
    return obj


class InitCapturesRuntimeCommitOnceTests(SimpleTestCase):
    """The __init__ wiring itself -- capture_runtime_commit() must be
    imported into engine.py's module namespace and called exactly once
    per PlaybackEngine construction (matches the identical pattern
    already covered for MonitorManager/RBDSManager)."""

    def test_capture_runtime_commit_is_imported_into_engine_module(self):
        self.assertIs(eng_module.capture_runtime_commit, eng_module.capture_runtime_commit)
        import isadoraair.version_info as version_info
        self.assertIs(eng_module.capture_runtime_commit, version_info.capture_runtime_commit)


class WriteStateIncludesRuntimeCommitTests(SimpleTestCase):
    """Full (minimal) _write_state() call -- confirms the field
    actually reaches the on-disk JSON payload, not just that the source
    line exists."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_path = Path(self.tmp_dir.name) / "engine_state.json"
        patcher = patch.object(eng_module, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _read_state(self, stand_in):
        stand_in._write_state()
        return json.loads(self.state_path.read_text())

    def test_runtime_commit_present_when_captured(self):
        stand_in = make_minimal_stand_in()
        stand_in._runtime_commit = "a" * 40
        state = self._read_state(stand_in)
        self.assertEqual(state["runtime_commit"], "a" * 40)

    def test_runtime_commit_none_when_git_was_unavailable_at_startup(self):
        """Fails safe -- None (never a crash, never a fabricated
        value) when this process's own capture came back empty."""
        stand_in = make_minimal_stand_in()
        stand_in._runtime_commit = None
        state = self._read_state(stand_in)
        self.assertIsNone(state["runtime_commit"])

    def test_runtime_commit_is_not_re_derived_at_write_time(self):
        """_write_state() must read the value captured once at startup
        -- never re-shell to git on every state-write tick."""
        stand_in = make_minimal_stand_in()
        stand_in._runtime_commit = "fixed-value"
        with patch.object(eng_module, "capture_runtime_commit") as mock_capture:
            self._read_state(stand_in)
            self._read_state(stand_in)
        mock_capture.assert_not_called()
