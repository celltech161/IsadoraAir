"""2.22B lifecycle correction -- the isadoraair-validation service owns every
validation run; the web process is only its client.

* the protocol: only the fixed validator commands, media as a descriptor,
  nothing a client can choose about executables, options, paths, environment
  or limits; a private socket and a same-account peer;
* fail closed without the service, and a clean retry once it is back;
* the SERVICE's limits govern every run;
* admission: a connection flood far beyond the bound never grows threads,
  connections, media descriptors or runs past it; slow clients cannot hold
  admission; excess is a retryable ``busy``, and the service then serves;
* start-up cleanup is a readiness gate: if it cannot be completed and
  verified, the socket is never bound;
* CPU accounting that cannot be read ends the run, retryably;
* lifecycle (real systemd user units with the production unit's properties):
  the requesting worker SIGKILLed, a frozen client, the service SIGKILLed,
  stopped and restarted, and leftover leaves reaped at start-up -- in every
  case no validator task survives and no later request is needed.
"""
import array
import errno
import io
import secrets
import shutil
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

from contextlib import contextmanager

from django.test import SimpleTestCase, TestCase, override_settings

from production.models import ProductionMedia
from production.services import confinement, intake, validation, validator_commands
from production.services import validation_service as service_module
from production.services.validation_service import NotReady, ValidationService

from .support import IsolatedMediaRootMixin, fixture
from .validation_service_support import REPO, TransientValidationService, validation_service

PY = sys.executable
MIB = 1024 * 1024


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):                 # gone meanwhile (ENOENT or ESRCH)
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


def hang_while(testcase, flag: Path):
    """``ffprobe`` that hangs (leaving a detached descendant) while ``flag``
    exists, and is the real ffprobe otherwise."""
    real = shutil.which("ffprobe")
    testcase.assertTrue(real, "ffprobe is required")
    return fake_tools(testcase, ffprobe=f"""
        #!/bin/bash
        if [ -e '{flag}' ]; then
            setsid sleep 300 </dev/null >/dev/null 2>&1 &
            exec sleep 300
        fi
        exec '{real}' "$@"
    """)


def _connects(path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1)
        probe.connect(path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _private_socket_path(testcase) -> str:
    directory = tempfile.mkdtemp(prefix="isadoraair-validation-ip.", dir=os.environ.get("XDG_RUNTIME_DIR") or None)
    os.chmod(directory, 0o700)
    testcase.addCleanup(shutil.rmtree, directory, True)
    return os.path.join(directory, "v.sock")


@contextmanager
def in_process_service(testcase, **options):
    """The validation service in THIS test process (which holds a delegated
    subtree like the unit's) -- where a test must reach into the executor to
    inject a fault, or watch start-up itself."""
    service = ValidationService(_private_socket_path(testcase), **options)
    failure = []

    def serve():
        try:
            service.serve_forever()
        except BaseException as exc:                    # noqa: BLE001 -- reported to the test
            failure.append(exc)

    quiet = mock.patch.object(service_module, "_log")
    quiet.start()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        _eventually(lambda: failure or _connects(service.socket_path), 30)
        if failure:
            raise failure[0]
        with override_settings(PRODUCTION_VALIDATION_SOCKET=service.socket_path):
            yield service
    finally:
        service.stop()
        thread.join(30)
        quiet.stop()


def stale_run(testcase):
    """A leftover run leaf with a live task in it, as a dead instance leaves one."""
    leaf = confinement.establish_root() / f"run-{secrets.token_hex(16)}"
    leaf.mkdir()
    sleeper = subprocess.Popen(["/bin/sleep", "300"])
    (leaf / "cgroup.procs").write_text(str(sleeper.pid))

    def cleanup():
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait()
        confinement._destroy(leaf)

    testcase.addCleanup(cleanup)
    return leaf, sleeper


def raw_request(socket_path, payload: bytes, fds=()):
    """Speak the protocol directly (whatever a hostile local client could send)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(30)
    try:
        sock.connect(socket_path)
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", list(fds)))] if fds else []
        try:
            sock.sendmsg([payload], ancillary)
        except (BrokenPipeError, ConnectionResetError):
            pass                                  # answered ``busy`` and closed before we sent: read it
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
            "a timeout too large for a float": (
                b'{"v": 1, "argv": %s, "timeout": 1%s}\n' % (json.dumps(probe).encode(), b"0" * 400), [media]),
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


    def test_a_service_at_capacity_answers_busy_retryably_and_serves_the_retry(self):
        media = self.stored()
        flag = Path(tempfile.mkdtemp(prefix="hang-flag-")) / "hang"
        self.addCleanup(shutil.rmtree, flag.parent, True)
        flag.touch()
        with validation_service(path_prefix=hang_while(self, flag), args=["--max-active=1", "--max-pending=0"]) \
                as service:
            for _ in range(20):         # (the readiness probe's own connection may still hold the slot)
                holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.addCleanup(holder.close)
                holder.connect(service.socket)
                holder.sendall(b'{"v": 1, "argv": ["ffprobe", "-version"], "timeout": 60}\n')
                if _eventually(lambda: service.leaves(), 2):
                    break
                holder.close()
            self.assertTrue(service.leaves(), "the occupying run never started")
            outcome = validation.validate_media(media)
            self.assertEqual((outcome.status, outcome.code), ("infrastructure_error", "validation_busy"))
            self.assertEqual(ProductionMedia.objects.get(pk=media.pk).validation_state, "unvalidated")
            holder.close()                                          # its run is cancelled; capacity returns
            flag.unlink()
            self.assertTrue(_eventually(lambda: not service.leaves(), 15))
            validation.clear_capability_cache()
            self.assertEqual(validation.validate_media(media).status, "valid")


class AdmissionTests(SimpleTestCase):
    """Codex 2.22B blocker 1: admission itself is bounded. Only an admitted
    connection gets a handler thread; everything beyond max_active +
    max_pending is answered ``busy`` from the accept loop and closed."""

    FLOOD = 128
    LIMIT = service_module.MAX_ACTIVE_RUNS + service_module.MAX_PENDING_REQUESTS

    def setUp(self):
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = Path(tmp.name) / "tone.wav"
        _tone(self.wav, 0.5)
        self.flag = Path(tmp.name) / "hang"
        self.media_fd = os.open(self.wav, os.O_RDONLY)
        self.addCleanup(os.close, self.media_fd)

    def test_the_defaults_are_small_and_explicit(self):
        self.assertEqual((service_module.MAX_ACTIVE_RUNS, service_module.MAX_PENDING_REQUESTS), (2, 4))
        self.assertLessEqual(service_module.REQUEST_READ_TIMEOUT_SECONDS, 5)
        service = ValidationService("/nonexistent/v.sock")
        self.assertEqual((service.max_active, service.max_pending), (2, 4))
        with self.assertRaises(ValueError):
            ValidationService("/nonexistent/v.sock", max_active=0)

    # -- clients ------------------------------------------------------------------
    @staticmethod
    def _connect(path, sock, wait):
        """``wait``: a blocking connect waits for room in the listen backlog (and
        reaches the service); otherwise a full backlog refuses it with EAGAIN."""
        if not wait:
            sock.settimeout(60)
        sock.connect(path)
        sock.settimeout(60)

    @classmethod
    def _complete(cls, path, payload, fds, results, index, wait):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            cls._connect(path, sock, wait)
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", list(fds)))] if fds else []
            try:
                sock.sendmsg([payload], ancillary)
            except OSError:
                pass                                # a busy answer may have closed it already
            data = b""
            while not data.endswith(b"\n"):
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                data += chunk
            results[index] = json.loads(data)["status"] if data.endswith(b"\n") else "no-answer"
        except OSError as exc:
            results[index] = f"connect-error-{exc.errno}"
        finally:
            sock.close()

    @classmethod
    def _slow(cls, path, dribble, stop, results, index, wait=True):
        """Connects and never completes a request: silent, or one byte at a time."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        def closed():
            try:
                data = sock.recv(65536, socket.MSG_DONTWAIT)
            except OSError:
                data = b""
            return "busy" if b'"busy"' in data else "dropped"

        try:
            cls._connect(path, sock, wait)
            started = time.monotonic()
            while not stop.is_set():
                if dribble:
                    try:
                        sock.send(b"{" if time.monotonic() - started < 0.1 else b" ")
                    except OSError:
                        results[index] = (closed(), time.monotonic() - started)
                        return
                readable, _, _ = __import__("select").select([sock], [], [], 0.25)
                if readable:
                    results[index] = (closed(), time.monotonic() - started)
                    return
            results[index] = ("still-open", time.monotonic() - started)
        except OSError as exc:
            results[index] = (f"connect-error-{exc.errno}", 0)
        finally:
            sock.close()

    def _sample(self, service, stop, peaks):
        while not stop.is_set():
            pid = service.main_pid()
            try:
                threads = len(os.listdir(f"/proc/{pid}/task"))
                sockets = media = 0
                for fd in os.listdir(f"/proc/{pid}/fd"):
                    try:
                        target = os.readlink(f"/proc/{pid}/fd/{fd}")
                    except OSError:
                        continue
                    sockets += target.startswith("socket:")
                    media += target == str(self.wav)
            except (OSError, TypeError):
                time.sleep(0.005)
                continue
            leaves = len(service.leaves())
            for name, value in (("threads", threads), ("sockets", sockets), ("media_fds", media),
                                ("leaves", leaves)):
                peaks[name] = max(peaks.get(name, 0), value)
            peaks["samples"] = peaks.get("samples", 0) + 1
            time.sleep(0.005)

    def test_a_flood_far_beyond_the_bound_stays_bounded_and_the_service_then_serves(self):
        self.flag.touch()
        with validation_service(path_prefix=hang_while(self, self.flag)) as service:
            pid = service.main_pid()
            self.assertTrue(_eventually(lambda: len(os.listdir(f"/proc/{pid}/task")) == 1, 10))
            baseline_sockets = sum(os.readlink(f"/proc/{pid}/fd/{fd}").startswith("socket:")
                                   for fd in os.listdir(f"/proc/{pid}/fd"))
            probe = (json.dumps({"v": 1, "argv": validator_commands.probe("ffprobe", "wav", validator_commands.MEDIA),
                                 "timeout": 6}) + "\n").encode()
            version = b'{"v": 1, "argv": ["ffprobe", "-version"], "timeout": 6}\n'
            results, slow_results, peaks = {}, {}, {}
            stop_slow, stop_sampling = threading.Event(), threading.Event()
            sampler = threading.Thread(target=self._sample, args=(service, stop_sampling, peaks), daemon=True)
            sampler.start()
            clients, complete_clients = [], []
            for index in range(self.FLOOD):
                kind, wait = index % 8, index % 16 < 8      # half wait for backlog room, half do not
                if kind < 3:      # 48 media requests, each passing a descriptor
                    target, args = self._complete, (service.socket, probe, [self.media_fd], results, index, wait)
                elif kind < 6:    # 48 plain requests
                    target, args = self._complete, (service.socket, version, (), results, index, wait)
                else:             # 32 slow clients: 16 silent, 16 dribbling a byte at a time
                    target, args = self._slow, (service.socket, kind == 7, stop_slow, slow_results, index, wait)
                clients.append(threading.Thread(target=target, args=args, daemon=True))
                if target == self._complete:
                    complete_clients.append(clients[-1])
            for client in clients:
                client.start()
            for client in complete_clients:
                client.join(60)
            self.assertTrue(_eventually(lambda: len(slow_results) == 32, 15), slow_results)
            stop_slow.set()
            for client in clients:
                client.join(10)
            stop_sampling.set()
            sampler.join(10)

            # "busy" from the service, or EAGAIN from a full kernel backlog (also
            # reported to the real client as busy) -- both bounded refusals
            refused = ("busy", f"connect-error-{errno.EAGAIN}")
            complete = list(results.values())
            slow = [outcome for outcome, _ in slow_results.values()]
            admitted = [o for o in complete if o not in refused] + [o for o in slow if o not in refused]
            report = (f"flood={self.FLOOD} limit={self.LIMIT} peaks={peaks} baseline_sockets={baseline_sockets} "
                      f"complete={ {s: complete.count(s) for s in set(complete)} } "
                      f"slow={ {s: slow.count(s) for s in set(slow)} }")
            if os.environ.get("VALIDATION_FLOOD_REPORT"):
                sys.stderr.write(report + "\n")
            self.assertEqual(len(complete), 96, report)
            self.assertTrue(set(complete) <= {*refused, "timeout"}, report)   # every client got an answer
            self.assertNotIn("still-open", slow, report)                       # no slow client kept a slot
            self.assertLessEqual(len(admitted), self.LIMIT, report)
            self.assertGreaterEqual(complete.count("busy") + slow.count("busy"), self.FLOOD // 4, report)
            self.assertGreater(peaks.get("samples", 0), 50, report)
            # one main thread, one per admitted connection, two output readers per run
            self.assertLessEqual(peaks["threads"], 1 + self.LIMIT + 2 * service_module.MAX_ACTIVE_RUNS, report)
            # admitted connections, plus the one being refused on the accept loop
            self.assertLessEqual(peaks["sockets"] - baseline_sockets, self.LIMIT + 1, report)
            self.assertLessEqual(peaks["media_fds"], self.LIMIT, report)
            self.assertLessEqual(peaks["leaves"], service_module.MAX_ACTIVE_RUNS, report)

            # load gone: a legitimate validation succeeds -- same service process
            self.flag.unlink()
            self.assertTrue(_eventually(lambda: not service.leaves(), 15))
            outcome = validation._analyze_path(self.wav, require_engine_decode=True)
            self.assertEqual(outcome.status, validation.STATUS_VALID, outcome)
            self.assertEqual(service.main_pid(), pid, "the service restarted")
            self.assertEqual(len(os.listdir(f"/proc/{pid}/task")), 1)

    def test_a_full_accept_queue_is_busy_for_the_client_not_unavailable(self):
        path = _private_socket_path(self)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(0)                                          # never accepts: one queued connection fills it
        queued = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(queued.close)
        queued.connect(path)
        with override_settings(PRODUCTION_VALIDATION_SOCKET=path):
            result = confinement.run_confined(["ffprobe", "-version"], timeout_seconds=5)
        self.assertEqual(result["status"], "busy", result)
        self.assertEqual(validation._run_failure(result, "probe").code, "validation_busy")

    def test_slow_clients_cannot_hold_admission(self):
        with validation_service() as service:
            stop, results = threading.Event(), {}
            slow = [threading.Thread(target=self._slow, args=(service.socket, index % 2 == 1, stop, results, index),
                                     daemon=True) for index in range(self.LIMIT)]
            self.addCleanup(lambda: (stop.set(), [thread.join(10) for thread in slow]))
            for thread in slow:
                thread.start()
            time.sleep(0.5)
            self.assertEqual(raw_request(service.socket, b'{"v": 1, "argv": ["ffprobe", "-version"], '
                                                         b'"timeout": 5}\n')["status"], "busy")
            # the service drops them -- while they are still dribbling -- and serves again
            limit = service_module.REQUEST_READ_TIMEOUT_SECONDS
            self.assertTrue(_eventually(lambda: len(results) == self.LIMIT, limit + 5), results)
            self.assertEqual({outcome for outcome, _ in results.values()}, {"dropped"}, results)
            self.assertTrue(all(limit - 1 <= held <= limit + 3 for _, held in results.values()), results)
            answer = raw_request(service.socket, b'{"v": 1, "argv": ["ffprobe", "-version"], "timeout": 5}\n')
            self.assertEqual(answer["status"], "ok", answer)


class StartupReadinessTests(SimpleTestCase):
    """Codex 2.22B blocker 3: start-up cleanup is a readiness gate. If leftover
    runs cannot be destroyed -- or the subtree cannot even be inspected -- the
    socket is never bound; once cleanup can complete, the service starts."""

    def attempt_start(self):
        """Start a service; it must give up (NotReady) without ever binding. A
        service that listens instead is stopped and reported, never left
        serving the test forever."""
        path = _private_socket_path(self)
        service = ValidationService(path)
        outcome = []

        def start():
            try:
                service.serve_forever()
                outcome.append(None)
            except BaseException as exc:                    # noqa: BLE001 -- reported below
                outcome.append(exc)

        with mock.patch.object(service_module, "_log"):
            thread = threading.Thread(target=start, daemon=True)
            thread.start()
            thread.join(15)
            listened = service.listener is not None or _connects(path)
            if thread.is_alive():
                service.stop()
                thread.join(15)
        self.assertFalse(listened, "the service listened although its start-up cleanup failed")
        self.assertFalse(os.path.lexists(path), "the socket was bound")
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], NotReady)
        return str(outcome[0])

    def assert_restored_start_succeeds(self, leaf=None, sleeper=None):
        with in_process_service(self) as service:
            if leaf is not None:
                self.assertFalse(leaf.exists(), "the leftover run survived a successful start")
                self.assertEqual(sleeper.wait(timeout=5), -signal.SIGKILL)
            self.assertEqual(confinement.run_confined(["ffprobe", "-version"], timeout_seconds=10)["status"], "ok")
        self.assertFalse(os.path.lexists(service.socket_path))

    def test_a_scan_failure_never_listens(self):
        leaf, sleeper = stale_run(self)
        with mock.patch.object(confinement, "_children", side_effect=OSError(errno.EIO, "Input/output error")):
            self.assertIn("cannot scan", self.attempt_start())
        self.assertIsNone(sleeper.poll(), "a failed scan is not 'nothing to clean'")
        self.assert_restored_start_succeeds(leaf, sleeper)

    def test_no_usable_subtree_never_listens(self):
        with override_settings(PRODUCTION_VALIDATION_CGROUP="/sys/fs/cgroup/iportal-validation"):
            self.assertIn("start-up cleanup incomplete", self.attempt_start())
        self.assert_restored_start_succeeds()

    def test_a_kill_failure_never_listens(self):
        leaf, sleeper = stale_run(self)
        real_write = confinement._write

        def write(path, value):
            if path.name == "cgroup.kill":
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_write(path, value)

        with mock.patch.object(confinement, "_write", side_effect=write):
            self.assertIn("could not destroy 1 leftover run", self.attempt_start())
        self.assertIsNone(sleeper.poll())
        self.assert_restored_start_succeeds(leaf, sleeper)

    def test_a_leaf_that_stays_populated_after_the_kill_never_listens(self):
        leaf, sleeper = stale_run(self)
        real_populated = confinement._populated
        with mock.patch.object(confinement, "CLEANUP_TIMEOUT_SECONDS", 0.2), \
                mock.patch.object(confinement, "_populated",
                                  side_effect=lambda path: path == leaf or real_populated(path)):
            self.assertIn("could not destroy", self.attempt_start())
        self.assertTrue(leaf.exists())
        self.assert_restored_start_succeeds(leaf, sleeper)

    def test_a_leaf_removal_failure_never_listens(self):
        leaf, sleeper = stale_run(self)
        with mock.patch.object(confinement, "_remove", side_effect=OSError(errno.EBUSY, "Device or resource busy")):
            self.assertIn("could not destroy", self.attempt_start())
        self.assertTrue(leaf.exists())
        self.assert_restored_start_succeeds(leaf, sleeper)

    def test_any_cleaned_false_result_never_listens(self):
        leaf, sleeper = stale_run(self)
        with mock.patch.object(confinement, "_destroy", side_effect=lambda path: {"path": str(path), "cleaned": False}):
            self.assertIn("could not destroy", self.attempt_start())
        self.assertIsNone(sleeper.poll())
        self.assert_restored_start_succeeds(leaf, sleeper)

    def test_a_subtree_still_populated_after_reaping_never_listens(self):
        leaf, sleeper = stale_run(self)
        root = confinement.establish_root()
        real_populated = confinement._populated
        with mock.patch.object(confinement, "_populated", side_effect=lambda path: path == root or real_populated(path)):
            self.assertIn("still holds live tasks", self.attempt_start())
        self.assert_restored_start_succeeds()

    def test_a_real_service_whose_cleanup_cannot_complete_never_listens_then_recovers(self):
        """The real unit, no mocks: while ``flag`` exists, every start of the
        service finds a leftover run leaf it cannot remove (it holds a child
        cgroup). Start-up cleanup fails, the process exits, systemd restarts it
        -- and no instance ever listens. Without the obstacle the next start
        succeeds and validation works."""
        flag = Path(tempfile.mkdtemp(prefix="unremovable-")) / "present"
        self.addCleanup(shutil.rmtree, flag.parent, True)
        flag.touch()
        leaf = f"run-{secrets.token_hex(16)}"
        pre_start = textwrap.dedent(f"""
            if [ -e '{flag}' ]; then
                own=/sys/fs/cgroup$(sed -n 's/^0:://p' /proc/self/cgroup)
                mkdir -p "$(dirname "$own")/iportal-validation/{leaf}/obstacle"
            fi
        """)
        service = TransientValidationService(pre_start=pre_start)
        self.addCleanup(service.stop)
        attempts, accepted, saw_leaf = set(), False, False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pid = service.main_pid()
            if pid:
                attempts.add(pid)
            saw_leaf = saw_leaf or any(path.name == leaf for path in service.leaves())
            accepted = accepted or service.accepts()
            time.sleep(0.05)
        self.assertTrue(saw_leaf, "the obstacle was never in place")
        self.assertFalse(accepted, "the service listened although its start-up cleanup failed")
        self.assertGreaterEqual(len(attempts), 2, "systemd did not keep retrying the start")
        self.assertFalse(os.path.lexists(service.socket), "the socket was bound")
        flag.unlink()
        service.wait_ready()
        with service.active():
            self.assertEqual(confinement.run_confined(["ffprobe", "-version"], timeout_seconds=10)["status"], "ok")
        self.assertEqual(service.leaves(), [])


class CpuAccountingServiceTests(SimpleTestCase):
    """Codex 2.22B blocker 2: CPU accounting that cannot be read, parsed or
    trusted while a validation runs ends that run -- tree destroyed, a
    retryable ``confinement_unavailable`` -- and the service serves the retry."""

    def setUp(self):
        validation.clear_capability_cache()
        self.addCleanup(validation.clear_capability_cache)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.wav = Path(tmp.name) / "tone.wav"
        _tone(self.wav, 0.5)
        self.hang = fake_tools(self, ffprobe=HANGS_WITH_A_DETACHED_DESCENDANT)

    def test_unusable_cpu_accounting_ends_the_run_retryably(self):
        real_read = confinement._read_cpu_stat
        faults = {
            "missing": FileNotFoundError(errno.ENOENT, "No such file or directory"),
            "unreadable": PermissionError(errno.EACCES, "Permission denied"),
            "I/O error": OSError(errno.EIO, "Input/output error"),
            "empty": b"",
            "short read": b"usage_usec 12",
            "no usage_usec": b"user_usec 5\nsystem_usec 5\n",
            "not a number": b"usage_usec twelve\n",
            "negative": b"usage_usec -5\n",
            "repeated": b"usage_usec 1\nusage_usec 2\n",
            "went backwards": "backwards",
        }
        with in_process_service(self) as service:
            for label, fault in faults.items():
                with self.subTest(label):
                    seen = {}

                    def read(leaf, fault=fault, seen=seen):
                        if "pids" not in seen:
                            pids = [int(p) for p in (leaf / "cgroup.procs").read_text().split()]
                            if len(pids) < 2:                    # until the tool and its descendant run
                                return real_read(leaf)
                            seen.update(pids=pids, leaf=leaf, at=time.monotonic())
                        if fault == "backwards":
                            seen["calls"] = seen.get("calls", 0) + 1
                            return b"usage_usec 5000\n" if seen["calls"] == 1 else b"usage_usec 10\n"
                        if isinstance(fault, BaseException):
                            raise fault
                        return fault

                    with mock.patch.dict(os.environ, PATH=f"{self.hang}:{os.environ['PATH']}"), \
                            mock.patch.object(confinement, "_read_cpu_stat", side_effect=read):
                        started = time.monotonic()
                        result = confinement.run_confined(
                            validator_commands.probe("ffprobe", "wav", str(self.wav)), timeout_seconds=20,
                            media=str(self.wav))
                    self.assertEqual(result["status"], "confinement_unavailable", result)
                    self.assertIn("CPU accounting", result["stderr"])
                    self.assertLess(time.monotonic() - started, 10, "the run was not ended at once")
                    self.assertTrue(result["cgroup"]["cleaned"], result)
                    self.assertFalse(seen["leaf"].exists())
                    self.assertEqual([pid for pid in seen["pids"] if _alive(pid)], [], "a validator task survived")
                    self.assertEqual(validation._run_failure(result, "probe").code, "confinement_unavailable")
            # the same service process serves a normal validation afterwards
            outcome = validation._analyze_path(self.wav, require_engine_decode=True)
            self.assertEqual(outcome.status, validation.STATUS_VALID, outcome)


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
