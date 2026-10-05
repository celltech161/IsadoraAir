"""2.22B / B6 -- OS-enforced resource confinement of media validation.

Synthetic helper programs (plain Python children) prove the KERNEL limits are
really applied -- not merely requested -- and that every failure is a
retryable infrastructure outcome, never a verdict on the media and never a
risk to the calling (web) process.
"""
import os
import resource
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from production.services import confinement, validation
from production.services.confinement import Limits, run_confined

PY = sys.executable
MIB = 1024 * 1024


def _script(body: str) -> list[str]:
    return [PY, "-c", textwrap.dedent(body)]


class LimitsAreAppliedTests(SimpleTestCase):
    def test_the_child_really_runs_under_every_configured_limit(self):
        limits = Limits(memory_bytes=700 * MIB, cpu_seconds=17, file_size_bytes=3 * MIB, open_files=99)
        result = run_confined(_script("""
            import resource
            r = resource
            print(r.getrlimit(r.RLIMIT_AS), r.getrlimit(r.RLIMIT_CPU), r.getrlimit(r.RLIMIT_FSIZE),
                  r.getrlimit(r.RLIMIT_NOFILE), r.getrlimit(r.RLIMIT_CORE))
        """), timeout_seconds=30, limits=limits)
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["confined"])
        expected = ((700 * MIB, 700 * MIB), (17, 22), (3 * MIB, 3 * MIB), (99, 99), (0, 0))
        self.assertEqual(result["stdout"].strip(), " ".join(str(item) for item in expected))

    def test_settings_configure_the_limits(self):
        with override_settings(PRODUCTION_VALIDATION_LIMITS={"memory_bytes": 512 * MIB, "cpu_seconds": 9}):
            limits = confinement.configured_limits()
        self.assertEqual((limits.memory_bytes, limits.cpu_seconds), (512 * MIB, 9))
        self.assertEqual(confinement.configured_limits(), Limits())
        for bad in ({"memory_bytes": 0}, {"cpu_seconds": -1}, {"open_files": True}, {"nonsense": 1}):
            with self.subTest(bad=bad), override_settings(PRODUCTION_VALIDATION_LIMITS=bad):
                with self.assertRaises((ValueError, TypeError)):
                    confinement.configured_limits()

    def test_the_parent_web_process_limits_are_untouched(self):
        before = {name: resource.getrlimit(getattr(resource, name))
                  for name in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE")}
        run_confined(_script("print('x')"), timeout_seconds=30, limits=Limits(memory_bytes=300 * MIB))
        after = {name: resource.getrlimit(getattr(resource, name)) for name in before}
        self.assertEqual(before, after)


class ResourceExhaustionTests(SimpleTestCase):
    def test_memory_pressure_is_stopped_by_the_address_space_limit(self):
        result = run_confined(_script("""
            blocks = []
            for _ in range(64):
                blocks.append(bytearray(64 * 1024 * 1024))   # 4 GiB in total
            print("allocated everything")
        """), timeout_seconds=60, limits=Limits(memory_bytes=256 * MIB))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertNotIn("allocated everything", result["stdout"])

    def test_cpu_runaway_is_killed_by_the_cpu_limit_long_before_the_wall_timeout(self):
        started = time.monotonic()
        result = run_confined(_script("while True:\n    pass"), timeout_seconds=60,
                              limits=Limits(cpu_seconds=1))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertLess(time.monotonic() - started, 30)

    def test_a_hung_tool_is_stopped_by_the_wall_timeout(self):
        started = time.monotonic()
        result = run_confined(_script("import time; time.sleep(600)"), timeout_seconds=1.0)
        self.assertEqual(result["status"], "timeout", result)
        self.assertLess(time.monotonic() - started, 15)

    def test_oversized_file_output_is_stopped_by_the_file_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "grows"
            result = run_confined(_script(f"""
                with open({str(target)!r}, "wb") as handle:
                    for _ in range(64):
                        handle.write(b"\\0" * (1024 * 1024))
                        handle.flush()
            """), timeout_seconds=60, limits=Limits(file_size_bytes=1 * MIB))
            self.assertEqual(result["status"], "resource_limit", result)
            self.assertLessEqual(target.stat().st_size, 1 * MIB)


class ProcessTreeTests(SimpleTestCase):
    def _alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # A zombie still answers kill(0); check its state.
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
        except FileNotFoundError:
            return False

    def _wait_dead(self, pids, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and any(self._alive(pid) for pid in pids):
            time.sleep(0.05)
        return [pid for pid in pids if self._alive(pid)]

    def _tree(self, pidfile, *, leader_exits):
        return _script(f"""
            import os, subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c",
                "import subprocess, sys, time; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']);"
                " open({str(pidfile)!r} + '.g', 'w').write(str(g.pid)); time.sleep(300)"])
            open({str(pidfile)!r}, "w").write(str(child.pid))
            deadline = time.time() + 10
            while not os.path.exists({str(pidfile)!r} + ".g") and time.time() < deadline:
                time.sleep(0.05)
            if {leader_exits!r}:
                sys.exit(0)
            time.sleep(300)
        """)

    def test_timeout_kills_the_entire_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pids"
            result = run_confined(self._tree(pidfile, leader_exits=False), timeout_seconds=3.0)
            self.assertEqual(result["status"], "timeout", result)
            pids = [int(pidfile.read_text()), int(Path(str(pidfile) + ".g").read_text())]
            self.assertEqual(self._wait_dead(pids), [])

    def test_descendants_that_outlive_their_parent_are_reaped_after_a_normal_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            pidfile = Path(tmp) / "pids"
            result = run_confined(self._tree(pidfile, leader_exits=True), timeout_seconds=30.0)
            self.assertEqual(result["status"], "ok", result)
            pids = [int(pidfile.read_text()), int(Path(str(pidfile) + ".g").read_text())]
            self.assertEqual(self._wait_dead(pids), [])


class FailClosedTests(SimpleTestCase):
    def test_a_missing_launcher_never_runs_the_tool_unconfined(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "ran"
            with mock.patch.object(confinement, "LAUNCHER", str(Path(tmp) / "missing.py")):
                result = run_confined(_script(f"open({str(sentinel)!r}, 'w').write('x')"), timeout_seconds=30)
            self.assertEqual(result["status"], "confinement_unavailable", result)
            self.assertFalse(sentinel.exists())

    def test_limits_that_cannot_be_applied_never_run_the_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            sentinel = Path(tmp) / "ran"
            # The launcher itself refuses a non-positive limit (exit 125 + marker).
            with mock.patch.object(confinement, "_launcher_argv", lambda executable, args, limits: [
                PY, "-I", "-S", confinement.LAUNCHER, "--memory", "0", "--cpu", "1", "--fsize", "1",
                "--nofile", "8", "--", executable, *args,
            ]):
                result = run_confined(_script(f"open({str(sentinel)!r}, 'w').write('x')"), timeout_seconds=30)
            self.assertEqual(result["status"], "confinement_unavailable", result)
            self.assertFalse(sentinel.exists())

    def test_a_missing_tool_is_unavailable_not_a_failure_of_the_media(self):
        result = run_confined(["/nonexistent/definitely-not-a-tool", "x"], timeout_seconds=5)
        self.assertEqual(result["status"], "unavailable")

    def test_a_clean_retry_works_after_a_confinement_failure(self):
        bad = run_confined(_script("while True:\n    pass"), timeout_seconds=30, limits=Limits(cpu_seconds=1))
        self.assertEqual(bad["status"], "resource_limit")
        good = run_confined(_script("print('fine')"), timeout_seconds=30)
        self.assertEqual((good["status"], good["stdout"].strip()), ("ok", "fine"))


class ValidationUsesConfinementTests(SimpleTestCase):
    """The real Phase-A validator, end to end, through the confined runner."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.wav = Path(cls.tmp.name) / "tone.wav"
        import math
        import struct
        import wave
        with wave.open(str(cls.wav), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(48000)
            handle.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 10))) for i in range(48000)))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)

    def test_every_validator_tool_runs_confined(self):
        seen = []
        real = confinement.run_confined

        def spy(args, **kwargs):
            result = real(args, **kwargs)
            seen.append((Path(args[0]).name, result.get("confined")))
            return result

        with mock.patch.object(confinement, "run_confined", side_effect=spy):
            outcome = validation._analyze_path(self.wav, require_engine_decode=True)
        self.assertTrue(seen)
        self.assertTrue(all(confined for _name, confined in seen), seen)
        # ffprobe, ffmpeg and the GStreamer child (via the interpreter) all ran.
        names = {name for name, _ in seen}
        self.assertTrue({"ffprobe", "ffmpeg"} <= names, names)
        self.assertIn(outcome.status, (validation.STATUS_VALID, validation.STATUS_INFRASTRUCTURE))

    def test_a_resource_limit_is_retryable_infrastructure_never_invalid(self):
        with override_settings(PRODUCTION_VALIDATION_LIMITS={"memory_bytes": 48 * MIB}):
            starved = validation._analyze_path(self.wav, require_engine_decode=False)
        self.assertEqual(starved.status, validation.STATUS_INFRASTRUCTURE, starved)
        self.assertIn(starved.code, validation.INFRASTRUCTURE_CODES)
        # ... and a retry with the normal limits validates the same bytes.
        validation.clear_capability_cache()
        retried = validation._analyze_path(self.wav, require_engine_decode=False)
        self.assertEqual(retried.status, validation.STATUS_VALID, retried)

    def test_unavailable_confinement_fails_closed_as_retryable_infrastructure(self):
        with mock.patch.object(confinement, "LAUNCHER", "/nonexistent/confined_exec.py"):
            outcome = validation._analyze_path(self.wav, require_engine_decode=False)
        self.assertEqual(outcome.status, validation.STATUS_INFRASTRUCTURE)
        self.assertIn(outcome.code, ("confinement_unavailable", "probe_unavailable"))

    def test_killed_engine_probe_maps_to_a_known_infrastructure_code(self):
        """Pre-existing latent defect: 'engine_probe_killed' was not a known code,
        so a killed GStreamer child crashed validation with AssertionError."""
        outcome = validation._run_failure({"status": "failed", "returncode": -15}, "engine_probe")
        self.assertEqual((outcome.status, outcome.code), (validation.STATUS_INFRASTRUCTURE, "engine_probe_killed"))
        for status, code in (("resource_limit", "validation_resource_limit"),
                             ("confinement_unavailable", "confinement_unavailable")):
            outcome = validation._run_failure({"status": status, "returncode": None}, "decode")
            self.assertEqual(outcome.code, code)
