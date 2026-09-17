"""Smoke the standalone systemd-style script import path in isolation."""

import subprocess
import sys
import unittest
from pathlib import Path


WEATHER_ROOT = Path(__file__).resolve().parent.parent


class StandaloneEngineCommandImportTests(unittest.TestCase):
    def test_alert_scripts_can_import_shared_writer_from_weather_working_directory(self):
        code = r'''
import runpy
import sys
import types
from pathlib import Path

delivery = types.ModuleType("delivery")
delivery.deliver = lambda *args, **kwargs: None
voices = types.ModuleType("voices")
class VoiceResolutionError(Exception):
    pass
voices.VoiceResolutionError = VoiceResolutionError
voices.resolve_voice = lambda *args, **kwargs: None
voices.synthesize = lambda *args, **kwargs: None
wxconfig = types.ModuleType("wxconfig")
wxconfig.load_weather_config = lambda: {}
wxconfig.resolve_weather_data_dir = lambda: Path("/tmp")
sys.modules.update(delivery=delivery, voices=voices, wxconfig=wxconfig)

for script in ("wx_alert.py", "amber_alert.py"):
    namespace = runpy.run_path(script, run_name="standalone_import_smoke")
    assert namespace["enqueue_engine_command"].__module__ == "isadoraair.engine_commands"
'''
        result = subprocess.run(
            [sys.executable, "-I", "-c", code],
            cwd=WEATHER_ROOT,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
