"""Test package. Importing it installs the destructive-test guard (isadoraair/test_safety.py)."""
from isadoraair import test_safety as _test_safety

_test_safety.install()

# D3E-D: the Operator Board reads Monitoring's published state file for the
# worker's health. No test may read a station's live file: point the board at
# a disposable, initially absent location (tests that need a state document
# write one and pass it explicitly, or patch this).
import tempfile as _tempfile
from pathlib import Path as _Path

from active_messages import diagnostics as _diagnostics

_diagnostics.MONITORING_STATE_PATH = _Path(_tempfile.mkdtemp(prefix="am-monitoring-state-")) / "absent.json"
