"""2.22B lifecycle correction -- the isadoraair-validation service owns every
validation run; the web process is only its client.

* the protocol: only the fixed validator commands, media as a descriptor,
  nothing a client can choose about executables, options, paths, environment
  or limits; a private socket and a same-account peer;
* fail closed without the service, and a clean retry once it is back;
* the SERVICE's limits govern every run;
* lifecycle (real systemd user units with the production unit's properties):
  the requesting worker SIGKILLed, a frozen client, the service SIGKILLed,
  stopped and restarted, and leftover leaves reaped at start-up -- in every
  case no validator task survives and no later request is needed.
"""
import array
import io
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from production.models import ProductionMedia
from production.services import confinement, intake, validation, validator_commands
from production.services.validation_service import ValidationService

from .support import IsolatedMediaRootMixin, fixture
from .validation_service_support import REPO, validation_service

PY = sys.executable
MIB = 1024 * 1024


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return False


def _eventually(condition, seconds, step=0.05):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


def _tone(path: Path, seconds=1.0):
    import math
    import struct
    import wave
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(48000)
        handle.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(i / 10)))
                                    for i in range(int(48000 * seconds))))


def fake_tools(testcase, **scripts):
    """A PATH directory whose tools (e.g. a hanging ``ffprobe``) the SERVICE
    resolves instead of the real ones -- a stand-in for hostile media that
    makes a real validator hang or misbehave."""
    directory = Path(tempfile.mkdtemp(prefix="fake-validators-"))
    testcase.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
    for name, body in scripts.items():
        path = directory / name
        path.write_text(textwrap.dedent(body).lstrip())
        path.chmod(0o755)
    return str(directory)


HANGS_WITH_A_DETACHED_DESCENDANT = """
    #!/bin/bash
    # A validator that never finishes and leaves a descendant in its own session.
    setsid sleep 300 </dev/null >/dev/null 2>&1 &
    exec sleep 300
"""


def raw_request(socket_path, payload: bytes, fds=()):
    """Speak the protocol directly (whatever a hostile local client could send)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(30)
    try:
        sock.connect(socket_path)
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", list(fds)))] if fds else []
        sock.sendmsg([payload], ancillary)
        data = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except ConnectionResetError:          # the service closed on unread (hostile) input
                break
            if not chunk:
                break
            data += chunk
        return json.loads(data) if data else None
    finally:
        sock.close()


WORKER = """
import json, os, sys
sys.path.insert(0, {repo!r})
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "isadoraair.settings")
import django
django.setup()
from production.services import confinement, validator_commands
media = sys.argv[1]
result = confinement.run_confined(validator_commands.probe("ffprobe", "wav", media),
                                  timeout_seconds=float(sys.argv[2]), media=media)
print(json.dumps({{"status": result["status"], "stderr": result.get("stderr", "")[:200]}}), flush=True)
"""


class WorkerMixin:
    """A separate process standing in for one Gunicorn worker mid-request."""

    def start_worker(self, service, media, timeout):
        env = {**os.environ, "PRODUCTION_VALIDATION_SOCKET": service.socket}
        worker = subprocess.Popen([PY, "-c", WORKER.format(repo=str(REPO)), str(media), str(timeout)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(lambda: (worker.poll() is None and worker.kill(), worker.wait()))
        return worker

    def wait_for_run(self, service, tasks=2):
        found = {}

        def populated():
            for leaf in service.leaves():
                pids = service.tasks(leaf)
                if len(pids) >= tasks:
                    found["leaf"], found["pids"] = leaf, pids
                    return True
            return False

        self.assertTrue(_eventually(populated, 30), "the validation run never started")
        return found["leaf"], found["pids"]

    def assert_run_destroyed(self, service, leaf, pids, seconds):
        self.assertTrue(_eventually(lambda: not any(_alive(pid) for pid in pids), seconds),
                        f"validator tasks survived: {[pid for pid in pids if _alive(pid)]}")
        self.assertTrue(_eventually(lambda: not leaf.exists(), seconds), f"{leaf} was not removed")


# -----------------------------------------------------------------------------

class ProtocolTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.wav = Path(cls.tmp.name) / "tone.wav"
        _tone(cls.wav)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)

    def test_a_real_validation_runs_entirely_in_the_service(self):
        seen = []
        real = confinement.run_confined

        def spy(args, **kwargs):
            result = real(args, **kwargs)
            seen.append(result)
            return result

        own_root = Path("/sys/fs/cgroup" + confinement.confined_exec.own_cgroup()).parent
        with mock.patch.object(confinement, "run_confined", side_effect=spy), \
                mock.patch.object(confinement, "execute", side_effect=AssertionError("the web side never executes")):
            outcome = validation._analyze_path(self.wav, require_engine_decode=True)
        self.assertEqual(outcome.status, validation.STATUS_VALID, outcome)
        self.assertGreaterEqual(len(seen), 5)                              # 2 versions, decoder, probe, decode, engine
        for result in seen:
            self.assertTrue(result["confined"] and result["cgroup"]["cleaned"], result)
            self.assertFalse(result["cgroup"]["path"].startswith(str(own_root) + "/"), "ran in the client's subtree")
            self.assertRegex(Path(result["cgroup"]["path"]).name, r"^run-[0-9a-f]{32}$")

    def test_only_the_fixed_validator_commands_are_executed(self):
        recorder = fake_tools(self, ffprobe="""
            #!/bin/bash
            echo "RAN $*"
        """)
        media = os.open(self.wav, os.O_RDONLY)
        self.addCleanup(os.close, media)
        pipe_r, pipe_w = os.pipe()
        self.addCleanup(os.close, pipe_r)
        self.addCleanup(os.close, pipe_w)
        probe = validator_commands.probe("ffprobe", "wav", validator_commands.MEDIA)

        def request(**fields):
            return (json.dumps({"v": 1, "argv": probe, "timeout": 5, **fields}) + "\n").encode()

        hostile = {
            "a shell": (request(argv=["/bin/sh", "-c", "echo RAN"]), [media]),
            "an arbitrary executable": (request(argv=["/usr/bin/env", "echo", "RAN"]), []),
            "an extra option": (request(argv=["ffprobe", "-version", "-report"]), []),
            "an unknown demuxer": (request(argv=validator_commands.probe("ffprobe", "hls", validator_commands.MEDIA)),
                                   [media]),
            "a path instead of a descriptor": (request(argv=validator_commands.probe("ffprobe", "wav", "/etc/passwd")),
                                               []),
            "a media command without a descriptor": (request(), []),
            "a descriptor that is not a regular file": (request(), [pipe_r]),
            "two descriptors": (request(), [media, media]),
            "a descriptor for a non-media command": (request(argv=["ffprobe", "-version"]), [media]),
            "an environment": (request(env={"LD_PRELOAD": "/tmp/x.so"}), [media]),
            "limits": (request(limits={"group_memory_bytes": 1 << 40}), [media]),
            "a bad timeout": (request(timeout=-1), [media]),
            "a NaN timeout": (b'{"v": 1, "argv": %s, "timeout": NaN}\n' % json.dumps(probe).encode(), [media]),
            "an unknown version": (request(v=2), [media]),
            "not JSON": (b"ffprobe -version\n", []),
            "an oversized request": (b"[" + b"1," * 40000 + b"1]\n", []),
        }
        with validation_service(path_prefix=recorder) as service:
            for label, (payload, fds) in hostile.items():
                with self.subTest(label):
                    answer = raw_request(service.socket, payload, fds)
                    self.assertIsNotNone(answer)
                    self.assertEqual(answer["status"], "unavailable", answer)
                    self.assertNotIn("RAN", answer.get("stdout", ""))
                    self.assertEqual(service.leaves(), [])
            # ... and the real thing runs, seeing the media ONLY as /proc/self/fd/<n>
            answer = raw_request(service.socket, request(), [media])
            self.assertEqual(answer["status"], "ok", answer)
            self.assertIn("RAN -v error -hide_banner -protocol_whitelist file -f wav", answer["stdout"])
            self.assertRegex(answer["stdout"], r"/proc/self/fd/\d+\n$")
            self.assertNotIn(str(self.wav), answer["stdout"])

    def test_the_service_caps_the_deadline_whatever_the_client_asks(self):
        hang = fake_tools(self, ffprobe="""
            #!/bin/bash
            exec sleep 300
        """)
        with validation_service(path_prefix=hang) as service:
            started = time.monotonic()
            answer = raw_request(service.socket, (json.dumps(
                {"v": 1, "argv": ["ffprobe", "-version"], "timeout": 1e9}) + "\n").encode())
            elapsed = time.monotonic() - started
        self.assertEqual(answer["status"], "timeout", answer)
        self.assertLess(elapsed, validation.CAPABILITY_TIMEOUT_SECONDS + 5)

    def test_the_socket_is_private_and_the_peer_must_be_this_account(self):
        with validation_service() as service:
            self.assertEqual(os.stat(service.socket).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(service.socket)).st_mode & 0o777, 0o700)
        with tempfile.TemporaryDirectory() as open_dir:
            os.chmod(open_dir, 0o755)
            with self.assertRaises(RuntimeError):
                ValidationService(os.path.join(open_dir, "v.sock")).bind()
        # a server not owned by this account is never trusted by the client
        with validation_service(), mock.patch.object(confinement, "peer_uid", return_value=os.geteuid() + 1):
            result = confinement.run_confined(["ffprobe", "-version"], timeout_seconds=5)
        self.assertEqual(result["status"], "confinement_unavailable")
        self.assertIn("another account", result["stderr"])

    def test_a_client_of_another_account_is_refused_without_running_anything(self):
        """The server half of the same check, in-process."""
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            service = ValidationService(os.path.join(directory, "v.sock"))
            service.bind()
            ran = []
            with mock.patch.object(confinement, "peer_uid", return_value=os.geteuid() + 1), \
                    mock.patch.object(confinement, "execute", side_effect=lambda *a, **k: ran.append(a)):
                thread = threading.Thread(target=lambda: service.handle(service.listener.accept()[0]))
                thread.start()
                answer = raw_request(service.socket_path, b'{"v": 1, "argv": ["ffprobe", "-version"], "timeout": 5}\n')
                thread.join(10)
            service.stop()
        self.assertIsNone(answer)
        self.assertEqual(ran, [])

    def test_the_services_own_limits_govern_every_run(self):
        hungry = fake_tools(self, ffprobe=f"""
            #!{PY}
            import subprocess, sys
            eat = "b = bytearray(b'\\\\x01') * (48 * 1024 * 1024); import time; time.sleep(30)"
            kids = [subprocess.Popen([sys.executable, "-c", eat]) for _ in range(3)]
            sys.exit(max(kid.wait() for kid in kids) and 1)
        """)
        with validation_service(path_prefix=hungry, limits={"group_memory_bytes": 96 * MIB}) as service:
            answer = raw_request(service.socket, b'{"v": 1, "argv": ["ffprobe", "-version"], "timeout": 30}\n')
            self.assertEqual(answer["status"], "resource_limit", answer)
            self.assertGreaterEqual(answer["cgroup"]["oom_kill"], 1)
            self.assertEqual(service.leaves(), [])


class FailClosedAndRetryTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)

    def stored(self):
        return intake.ingest_stream(io.BytesIO(fixture("wav16_mono.wav")), kind="upload", validate=False).media

    def test_without_the_service_validation_fails_closed_and_a_retry_succeeds(self):
        media = self.stored()
        with override_settings(PRODUCTION_VALIDATION_SOCKET="/nonexistent/isadoraair-validation/validator.sock"), \
                mock.patch.object(confinement, "execute", side_effect=AssertionError("never unconfined, never local")):
            outcome = validation.validate_media(media)
        self.assertEqual((outcome.status, outcome.code), ("infrastructure_error", "confinement_unavailable"))
        row = ProductionMedia.objects.get(pk=media.pk)
        self.assertEqual(row.validation_state, "unvalidated")
        validation.clear_capability_cache()
        retried = validation.validate_media(media)                    # the service is back
        self.assertEqual(retried.status, "valid", retried)

    def test_a_stopped_service_fails_closed_and_its_replacement_serves_the_retry(self):
        media = self.stored()
        with validation_service() as service:
            service.stop()
            outcome = validation.validate_media(media)
            self.assertEqual((outcome.status, outcome.code), ("infrastructure_error", "confinement_unavailable"))
            self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_state, "unvalidated")
        with validation_service():
            validation.clear_capability_cache()
            self.assertEqual(validation.validate_media(media).status, "valid")


class LifecycleTests(WorkerMixin, SimpleTestCase):
    """Codex's 2.22B lifecycle blocker and its supervisor-side twin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.media = Path(self.tmp.name) / "upload.wav"
        _tone(self.media, 0.5)
        self.hang = fake_tools(self, ffprobe=HANGS_WITH_A_DETACHED_DESCENDANT)

    def test_a_sigkilled_request_worker_takes_its_validation_with_it(self):
        """Codex's reproduction: the requesting worker is SIGKILLed mid-run and NO
        further request ever arrives. The run must not outlive it."""
        with validation_service(path_prefix=self.hang) as service:
            worker = self.start_worker(service, self.media, timeout=120)
            leaf, pids = self.wait_for_run(service, tasks=2)
            os.kill(worker.pid, signal.SIGKILL)
            worker.wait()
            self.assert_run_destroyed(service, leaf, pids, seconds=5)        # long before its 120 s deadline
            self.assertEqual(service.leaves(), [])

    def test_a_frozen_client_cannot_extend_the_hard_deadline(self):
        """The service's own deadline, independent of the client: the worker is
        stopped (connection open, never reading) and the run still dies on time."""
        with validation_service(path_prefix=self.hang) as service:
            worker = self.start_worker(service, self.media, timeout=3)
            leaf, pids = self.wait_for_run(service, tasks=2)
            started = time.monotonic()
            os.kill(worker.pid, signal.SIGSTOP)
            self.addCleanup(lambda: worker.poll() is None and os.kill(worker.pid, signal.SIGKILL))
            self.assert_run_destroyed(service, leaf, pids, seconds=10)
            self.assertLess(time.monotonic() - started, 10)
            os.kill(worker.pid, signal.SIGKILL)

    def test_a_sigkilled_service_never_leaves_a_run_behind_and_recovers(self):
        with validation_service(path_prefix=self.hang) as service:
            worker = self.start_worker(service, self.media, timeout=120)
            leaf, pids = self.wait_for_run(service, tasks=2)
            old = service.kill_supervisor(signal.SIGKILL)
            self.assert_run_destroyed(service, leaf, pids, seconds=15)       # systemd, then the restarted service
            new = service.wait_ready(not_pid=old)
            self.assertNotEqual(new, old)
            self.assertEqual(service.leaves(), [])
            self.assertEqual(json.loads(worker.communicate(timeout=30)[0])["status"], "confinement_unavailable")
            retry = confinement.run_confined(["ffmpeg", "-version"], timeout_seconds=10)
            self.assertEqual(retry["status"], "ok", retry)

    def test_stopping_the_service_destroys_its_runs_and_then_fails_closed(self):
        with validation_service(path_prefix=self.hang) as service:
            worker = self.start_worker(service, self.media, timeout=120)
            leaf, pids = self.wait_for_run(service, tasks=2)
            service.stop()
            self.assert_run_destroyed(service, leaf, pids, seconds=15)
            self.assertIsNone(service.cgroup())
            self.assertFalse(os.path.exists(service.socket))
            self.assertIn(json.loads(worker.communicate(timeout=30)[0])["status"],
                          ("confinement_unavailable", "stopped"))
            after = confinement.run_confined(["ffmpeg", "-version"], timeout_seconds=10)
            self.assertEqual(after["status"], "confinement_unavailable")

    def test_restarting_the_service_destroys_its_runs_and_serves_again(self):
        with validation_service(path_prefix=self.hang) as service:
            self.start_worker(service, self.media, timeout=120)
            leaf, pids = self.wait_for_run(service, tasks=2)
            old = service.main_pid()
            service.restart()
            self.assert_run_destroyed(service, leaf, pids, seconds=15)
            service.wait_ready(not_pid=old)
            self.assertEqual(service.leaves(), [])
            retry = confinement.run_confined(["ffmpeg", "-version"], timeout_seconds=10)
            self.assertEqual(retry["status"], "ok", retry)

    def test_a_restarted_service_reaps_leftover_leaves_before_accepting_work(self):
        """No PID decides anything: whatever run-* leaf a previous instance left in
        the service's exclusive subtree is destroyed at start-up."""
        with validation_service() as service:
            root = service.cgroup() / "iportal-validation"
            leftover = root / f"run-{os.urandom(16).hex()}"
            leftover.mkdir()
            old = service.kill_supervisor(signal.SIGKILL)
            new = service.wait_ready(not_pid=old)
            self.assertNotEqual(new, old)
            self.assertTrue(_eventually(lambda: not leftover.exists(), 10), "leftover leaf survived the restart")
