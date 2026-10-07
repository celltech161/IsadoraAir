"""r0107 release finding: the validator commands must be spelled identically by
every process of one installation, however each was launched.

The isadoraair-validation service accepts a command only if its argv EXACTLY
equals the one it rebuilds from its own constants. On a station the client
(Gunicorn, started from its script's shebang -- the virtualenv's real path) and
the service (ExecStart=@@ISA_ROOT@@/venv/bin/python, through the /opt/isadoraair
symlink) see different sys.executable spellings of the same interpreter. With
sys.executable baked into the GStreamer engine command, every engine probe was
refused ("not a validator command") and every upload stayed unvalidated.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from production.services import validation, validator_commands

from .support import IsolatedMediaRootMixin
from .test_validation_service import _tone
from .validation_service_support import REPO, validation_service

_SHOW = r"""
import json, os, sys
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
import django
django.setup()
from production.services import validation, validation_service, validator_commands
tools = validation_service.configured_tools()
print(json.dumps({
    "executable": sys.executable,
    "interpreter": list(validation.GSTREAMER_PROBE_INTERPRETER),
    "script": validation.GSTREAMER_PROBE_SCRIPT,
    "engine_command": validator_commands.engine_decode(
        tools.interpreter, tools.probe_script, validator_commands.MEDIA, tools.probe_child_timeout),
}))
"""


class EquivalentSpellingsMixin:
    def setUp(self):
        super().setUp()
        links = Path(tempfile.mkdtemp(prefix="validation-topology-"))
        self.addCleanup(lambda: [path.unlink() for path in links.iterdir()] and links.rmdir())
        # like /opt/isadoraair -> the real checkout, and a venv reached through it
        (links / "venv").symlink_to(os.path.realpath(sys.prefix))
        (links / "checkout").symlink_to(os.path.realpath(REPO))
        names = sorted(name for name in os.listdir(links / "venv" / "bin") if name.startswith("python"))
        other = next(name for name in names if os.path.join(os.path.realpath(sys.prefix), "bin", name)
                     != sys.executable and name != os.path.basename(sys.executable))
        self.other_python = str(links / "venv" / "bin" / other)
        self.linked_checkout = links / "checkout"
        self.assertNotEqual(self.other_python, sys.executable)


class SpellingTests(EquivalentSpellingsMixin, IsolatedMediaRootMixin, SimpleTestCase):
    def _show(self, python, checkout):
        result = subprocess.run([python, "-c", _SHOW], cwd=checkout, capture_output=True, text=True, timeout=120,
                                env={**os.environ, "PYTHONPATH": str(checkout)})
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        import json
        return json.loads(result.stdout.strip().splitlines()[-1])

    def test_differently_launched_processes_build_identical_validator_commands(self):
        client = self._show(sys.executable, REPO)
        service = self._show(self.other_python, self.linked_checkout)
        self.assertNotEqual(client["executable"], service["executable"])    # the hazard is real here
        for key in ("interpreter", "script", "engine_command"):
            self.assertEqual(client[key], service[key], key)

    def test_the_canonical_interpreter_runs_this_environment(self):
        interpreter = validation.GSTREAMER_PROBE_INTERPRETER[0]
        self.assertEqual(interpreter, os.path.realpath(interpreter) if sys.prefix == sys.base_prefix
                         else os.path.join(os.path.realpath(sys.prefix), "bin", "python"))
        result = subprocess.run([interpreter, "-c", "import sys; print(sys.prefix)"], capture_output=True,
                                text=True, timeout=60)
        self.assertEqual(os.path.realpath(result.stdout.strip()), os.path.realpath(sys.prefix))
        self.assertEqual(validation.GSTREAMER_PROBE_SCRIPT, os.path.realpath(validation.GSTREAMER_PROBE_SCRIPT))


class ServiceLaunchedDifferentlyTests(EquivalentSpellingsMixin, IsolatedMediaRootMixin, SimpleTestCase):
    def test_a_service_launched_through_another_spelling_validates_including_the_engine(self):
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "tone.wav"
            _tone(wav, 1.0)
            with validation_service(python=self.other_python):
                outcome = validation._analyze_path(wav, require_engine_decode=True)
        self.assertEqual(outcome.status, validation.STATUS_VALID, outcome)
