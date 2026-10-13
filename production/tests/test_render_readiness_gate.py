"""r0109 release: the readiness gates that make Update Center's
``systemctl enable --now`` + ``ActiveState`` check exact for the two Phase C
units (it otherwise sees a Type=simple service ``active`` even while it is
crash-looping -- reproduced in the r0109 station rehearsal).

* isadoraair-render: ExecStartPost= ``production_render_ready`` -- exits 0
  only once the accepted C2A readiness handshake (render_health.observe)
  succeeds, non-zero on timeout; read-only.
* isadoraair-spoken-render-worker: ExecStartPost= a bounded survival window.
"""
import configparser
import time
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from production.services import render_service

from .support import IsolatedMediaRootMixin
from .test_render_service import MediaFixtures, _private_socket_path, in_process_render_service

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _service_section(unit):
    parser = configparser.RawConfigParser(strict=False, delimiters=("=",), interpolation=None)
    parser.optionxform = str
    parser.read_string((DEPLOY / unit).read_text())
    return parser["Service"]


class UnitGateTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_the_render_unit_gates_its_start_on_the_readiness_handshake(self):
        service = _service_section("isadoraair-render.service")
        self.assertEqual(service["ExecStartPost"],
                         "@@ISA_ROOT@@/venv/bin/python @@ISA_ROOT@@/manage.py production_render_ready --timeout 60")
        self.assertEqual(service["TimeoutStartSec"], "90")
        # Update Center bounds `enable --now` at 120 s: the start timeout (90 s)
        # and the gate (60 s) both end first.
        self.assertEqual(service["Type"], "simple")

    def test_the_worker_unit_gates_its_start_on_surviving_start_up(self):
        service = _service_section("isadoraair-spoken-render-worker.service")
        self.assertEqual(service["ExecStartPost"], "/usr/bin/sleep 10")
        self.assertEqual(service["TimeoutStartSec"], "60")
        self.assertTrue(Path("/usr/bin/sleep").is_file())


class ReadyCommandTests(MediaFixtures, TestCase):
    def test_ready_when_the_service_answers_its_handshake(self):
        with in_process_render_service(self) as service:
            out = StringIO()
            call_command("production_render_ready", "--timeout", "5", stdout=out)
            self.assertIn("ready (at most 1 rendering, 2 waiting)", out.getvalue())
            call_command("production_render_ready", "--timeout", "5", "--socket", service.socket_path, stdout=StringIO())

    def test_not_ready_fails_after_the_timeout(self):
        path = _private_socket_path(self)                 # nothing listens here
        started = time.monotonic()
        with self.assertRaisesRegex(CommandError, "not ready after 0.6s: socket_missing"):
            call_command("production_render_ready", "--timeout", "0.6", "--socket", path, stdout=StringIO())
        self.assertLess(time.monotonic() - started, 5)

    def test_the_timeout_is_bounded(self):
        for value in ("0", "-1", "601"):
            with self.subTest(value=value), self.assertRaisesRegex(CommandError, "between 0 and 600"):
                call_command("production_render_ready", "--timeout", value, stdout=StringIO())

    def test_it_never_renders_or_writes(self):
        with in_process_render_service(self) as service:
            before = (service.active, service.pending, render_service.status_snapshot(service.socket_path))
            call_command("production_render_ready", "--timeout", "5", stdout=StringIO())
            self.assertEqual((service.active, service.pending), before[:2])
            self.assertIsNone(render_service.status_snapshot(service.socket_path)["last_failure"])
