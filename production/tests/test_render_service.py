"""2.22C2A -- the isadoraair-render executor and the sole rendition authority.

* the executor core: fixed argv over descriptors only (``-protocol_whitelist
  fd``, ``-fd N fd:``, no path but the pinned tool), every input proven by
  hash, a SEALED output;
* the sandbox every render step gets (the launcher's ``render`` profile): no
  filesystem writes, no reads outside the system trees a renderer needs, no
  sockets, a fixed environment, bounded processes;
* the protocol: no command/executable/filter/path/URL can be expressed; a
  malformed, oversized or substituted request is refused and poisons nothing;
* admission: an active ceiling, a pending ceiling, ``busy`` beyond them, slots
  released on success, failure, timeout and client disconnect;
* the client accepts only a sealed, size- and hash-verified descriptor for the
  exact render it asked for, from the service's own account;
* rendition authority: ordinary code cannot create a ``rendition`` row;
* independence from the validation service, both ways; Monitoring.

Run under production/tests/run_with_validation_service.sh (a delegated scope,
the real validation service and a real render service).
"""
import array
import errno
import fcntl
import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.db import transaction
from django.db.models import QuerySet
from django.db.models.signals import pre_save
from django.test import SimpleTestCase, TestCase, override_settings

from production.errors import ImmutableMediaError
from production.models import ProductionMedia, rendition_authority
from production.services import (
    confinement, intake, reference_audio, render, render_health, render_service, validation,
)
from production.services.render import client, executor, protocol, provenance, rendition
from spoken.tests import support as audio

from .support import IsolatedMediaRootMixin, ffmpeg_available
from .test_validation_service import _connects, _eventually, _private_socket_path, raw_request

REPO = Path(__file__).resolve().parents[2]
SYSTEM_PYTHON = "/usr/bin/python3"
FFMPEG = shutil.which("ffmpeg", path=render_service.TOOL_PATH)
SAFE_FILTER = r"^[A-Za-z0-9_=:.,;|\[\]-]+$"


def _run_leaves():
    try:
        root = confinement.validation_root(confinement.RENDER)
        return [p for p in root.iterdir() if p.name.startswith("run-")] if root.exists() else []
    except confinement.ConfinementUnavailable:
        return []


@contextmanager
def in_process_render_service(testcase, **options):
    """The render service in THIS test process (which holds a delegated
    subtree like the unit's), on a private socket."""
    service = render_service.RenderService(_private_socket_path(testcase), **options)
    failure = []

    def serve():
        try:
            service.serve_forever()
        except BaseException as exc:                    # noqa: BLE001 -- reported to the test
            failure.append(exc)

    quiet = mock.patch.object(render_service, "_log")
    quiet.start()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        _eventually(lambda: failure or _connects(service.socket_path), 60)
        if failure:
            raise failure[0]
        with override_settings(PRODUCTION_RENDER_SOCKET=service.socket_path):
            yield service
    finally:
        service.stop()
        thread.join(60)
        quiet.stop()


def ingest(data, kind=ProductionMedia.KIND_RECORDING):
    return intake.ingest_stream(io.BytesIO(data), kind=kind, validate=True).media


def build_request(source, recipe_key, params=None, **overrides):
    """A well-formed protocol request for ``source`` (what render.client
    sends), plus the read-only descriptors and a closer."""
    recipe, resolved, plan = provenance.prepare(recipe_key, params)
    opened = __import__("production.services.media_io", fromlist=["open_media"]).open_media(source)
    references = {name: reference_audio.resolve(key)
                  for name, key in sorted(provenance.reference_keys(plan, resolved).items())}
    pins = {name: ref.provenance() for name, ref in references.items()}
    body = {
        "v": 1, "op": "render", "request_id": "ab" * 16, "correlation": {"item": None, "lease": None},
        "source": client._media_entry(opened.media), "recipe": {"key": recipe.key, "version": recipe.version},
        "params": dict(params or {}),
        "references": {name: {**client._media_entry(ref.media), "key": ref.key, "reference": ref.reference_id}
                       for name, ref in references.items()},
        "expected_digest": provenance.digest(provenance.document(recipe, resolved, pins)), "timeout": 120.0,
    }
    body.update(overrides)
    fds = [opened.file.fileno(), *(references[name].file.fileno() for name in sorted(references))]

    def close():
        opened.file.close()
        for ref in references.values():
            ref.close()
    return body, fds, close


def send(path, body, fds, *, raw=None):
    """One request; returns (answer, received descriptors)."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(120)
    try:
        sock.connect(path)
        payload = raw if raw is not None else (json.dumps(body) + "\n").encode()
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", list(fds)))] if fds else []
        try:
            sock.sendmsg([payload], ancillary)
        except (BrokenPipeError, ConnectionResetError):
            pass
        return client._receive(sock, time.monotonic() + 120)
    finally:
        sock.close()


class MediaFixtures(IsolatedMediaRootMixin):
    def setUp(self):
        super().setUp()
        self.bed_row = reference_audio.register("bed.3min_ctr", ingest(audio.bed(1.0), ProductionMedia.KIND_BED))
        self.source = ingest(audio.voice(1.0))


# -- the executor core ----------------------------------------------------------------------

@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ExecutorBoundaryTests(MediaFixtures, TestCase):
    def execute(self, body, fds, run_step):
        parsed = protocol.parse_request(body, max_bytes=2 ** 30, max_seconds=render_service.MAX_SOURCE_SECONDS)
        return executor.execute(parsed, fds[0], dict(zip(sorted(parsed["references"]), fds[1:])), run_step=run_step,
                                ffmpeg=FFMPEG, deadline=time.monotonic() + 120,
                                max_seconds=render_service.MAX_SOURCE_SECONDS)

    def confined(self, calls):
        def run_step(argv, pass_fds, timeout):
            calls.append((list(argv), tuple(pass_fds)))
            return confinement.execute(argv, timeout_seconds=timeout, pass_fds=pass_fds,
                                       limits=render_service.render_limits(), subtree=confinement.RENDER,
                                       profile="render", env=dict(render_service.TOOL_ENVIRONMENT))
        return run_step

    def test_every_step_is_fixed_argv_over_descriptors_and_the_output_is_sealed(self):
        import re
        body, fds, close = build_request(self.source, "3min_ctr")
        calls = []
        try:
            rendered = self.execute(body, fds, self.confined(calls))
        finally:
            close()
        try:
            self.assertEqual([argv[argv.index("-loglevel") + 1] for argv, _ in calls],
                             ["error", "error", "info", "error"])           # prepare, mix, analysis, encode
            for argv, pass_fds in calls:
                self.assertEqual(argv[0], FFMPEG)
                self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "fd")
                self.assertFalse([arg for arg in argv[1:] if "/" in arg], argv)   # no path at all
                given = [int(argv[i + 1]) for i, arg in enumerate(argv) if arg == "-fd"]
                self.assertEqual(sorted(given), sorted(pass_fds))                  # exactly the handed fds
                for flag in ("-af", "-filter_complex"):
                    if flag in argv:
                        self.assertRegex(argv[argv.index(flag) + 1], SAFE_FILTER)
            seals = fcntl.fcntl(rendered.fd, fcntl.F_GET_SEALS)
            self.assertEqual(seals & executor.SEALS, executor.SEALS)
            with self.assertRaises(PermissionError):
                os.write(rendered.fd, b"x")
            with self.assertRaises(PermissionError):
                os.ftruncate(rendered.fd, 0)
            self.assertEqual(executor.fd_sha256(rendered.fd), rendered.sha256)
        finally:
            os.close(rendered.fd)

    def test_inputs_must_be_the_claimed_read_only_regular_files(self):
        other = ingest(audio.voice(1.5))
        body, fds, close = build_request(self.source, "lcl_psa")
        never = mock.Mock(side_effect=AssertionError("a step ran"))
        try:
            cases = {}
            with open(layout_path(other), "rb") as substitute:
                cases["substituted bytes"] = ([substitute.fileno()], "descriptor_mismatch")
                self.assert_refused(body, cases, never)
            copy = Path(tempfile.mkdtemp(prefix="render-rw-")) / "same-bytes"
            self.addCleanup(shutil.rmtree, copy.parent, True)
            copy.write_bytes(layout_path(self.source).read_bytes())          # the claimed bytes, writable
            rw = os.open(copy, os.O_RDWR)
            read_end, write_end = os.pipe()
            directory = os.open(str(REPO), os.O_RDONLY)
            try:
                cases = {"a pipe": ([read_end], "descriptor_invalid"), "a directory": ([directory], "descriptor_invalid"),
                         "opened for writing": ([rw], "descriptor_invalid")}
                self.assert_refused(body, cases, never)
            finally:
                for fd in (read_end, write_end, directory, rw):
                    if fd is not None:
                        os.close(fd)
            wrong_size = {**body, "source": {**body["source"], "bytes": body["source"]["bytes"] + 1}}
            self.assert_refused(wrong_size, {"wrong size": ([fds[0]], "descriptor_mismatch")}, never)
        finally:
            close()

    def assert_refused(self, body, cases, run_step):
        for label, (fds, code) in cases.items():
            with self.subTest(case=label), self.assertRaises(render.RenderError) as raised:
                self.execute(body, fds, run_step)
            self.assertEqual(raised.exception.code, code)

    def test_recipe_parameters_references_and_provenance_are_checked_before_anything_runs(self):
        body, fds, close = build_request(self.source, "3min_ctr")
        never = mock.Mock(side_effect=AssertionError("a step ran"))
        ref = body["references"]["bed"]
        try:
            cases = {
                "unknown recipe": ({**body, "recipe": {"key": "no_such", "version": 1}}, "unknown_recipe"),
                "other version": ({**body, "recipe": {"key": "3min_ctr", "version": 2}}, "recipe_version_mismatch"),
                "string parameter": ({**body, "params": {"bed_gain_db": "-10; rm -rf /"}}, "invalid_parameters"),
                "path as reference": ({**body, "params": {"bed": "/etc/passwd"}}, "invalid_parameters"),
                "url as reference": ({**body, "params": {"bed": "http://example.invalid/x.wav"}}, "invalid_parameters"),
                "unknown parameter": ({**body, "params": {"filter": "amovie=/etc/passwd"}}, "invalid_parameters"),
                "missing reference": ({**body, "references": {}}, "reference_mismatch"),
                "other reference key": ({**body, "references": {"bed": {**ref, "key": "bed.other"}}},
                                        "reference_mismatch"),
                "other pin": ({**body, "references": {"bed": {**ref, "sha256": "0" * 64}}}, "provenance_mismatch"),
                "other expectation": ({**body, "expected_digest": "1" * 64}, "provenance_mismatch"),
            }
            for label, (variant, code) in cases.items():
                with self.subTest(case=label), self.assertRaises(render.RenderError) as raised:
                    self.execute(variant, fds, never)
                self.assertEqual(raised.exception.code, code)
            with self.subTest(case="reference bytes not the pinned ones"), \
                    self.assertRaises(render.RenderError) as raised:
                self.execute(body, [fds[0], fds[0]], never)                   # the voice where the bed should be
            self.assertEqual(raised.exception.code, "descriptor_mismatch")
        finally:
            close()

    def test_step_failures_map_to_stable_codes_and_leak_nothing(self):
        body, fds, close = build_request(self.source, "lcl_psa")
        before = set(os.listdir("/proc/self/fd"))
        try:
            for status, code, retryable in (("timeout", "render_timeout", True), ("stopped", "cancelled", True),
                                            ("resource_limit", "render_resource_limit", False),
                                            ("confinement_unavailable", "sandbox_unavailable", True),
                                            ("unavailable", "render_unavailable", True),
                                            ("failed", "render_failed", False)):
                with self.subTest(status=status), self.assertRaises(render.RenderError) as raised:
                    self.execute(body, fds, lambda argv, pass_fds, timeout, s=status: {
                        "status": s, "returncode": 1, "stderr": "/secret/path detail"})
                self.assertEqual((raised.exception.code, raised.exception.retryable), (code, retryable))
        finally:
            close()
        self.assertLessEqual(set(os.listdir("/proc/self/fd")) - before, set())


def _close_quietly(fd):
    try:
        os.close(fd)
    except OSError:
        pass


def _links(prefix):
    """This process's descriptors whose target starts with ``prefix``."""
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{name}").startswith(prefix):
                found.append(int(name))
        except OSError:
            pass
    return found


def confined_step(calls, before=None):
    def run_step(argv, pass_fds, timeout):
        if before is not None:
            before(argv, pass_fds)
        calls.append((list(argv), tuple(pass_fds)))
        return confinement.execute(argv, timeout_seconds=timeout, pass_fds=pass_fds,
                                   limits=render_service.render_limits(), subtree=confinement.RENDER,
                                   profile="render", env=dict(render_service.TOOL_ENVIRONMENT))
    return run_step


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class InputSnapshotTests(MediaFixtures, TestCase):
    """C2A corrective A: every input becomes an immutable, service-owned
    snapshot of EXACTLY the bytes that passed verification. A read-only
    descriptor does not make its inode immutable: a second, writable handle
    can rewrite the same inode, same size, after the check. The renderer
    never reads the caller's inode again once the sealed snapshot exists."""

    def mutable_copy(self, row):
        """A caller-side file holding ``row``'s bytes (payload A), a read-only
        descriptor of it, and ``mutate()``: rewrite THAT inode in place, same
        size, with payload B (A's header, silent samples) through a separate
        writable descriptor."""
        payload = layout_path(row).read_bytes()
        directory = Path(tempfile.mkdtemp(prefix="render-input-"))
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "input"
        path.write_bytes(payload)
        read_only = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(_close_quietly, read_only)
        substitute = payload[:44] + bytes(len(payload) - 44)

        def mutate():
            writable = os.open(path, os.O_WRONLY)
            try:
                os.pwrite(writable, substitute, 0)
            finally:
                os.close(writable)
            assert os.fstat(read_only).st_size == len(payload)
            assert executor.fd_sha256(read_only) == hashlib.sha256(substitute).hexdigest() != row.sha256
        return read_only, mutate, path

    def parsed(self, body):
        return protocol.parse_request(body, max_bytes=2 ** 30, max_seconds=render_service.MAX_SOURCE_SECONDS)

    def execute(self, body, source_fd, reference_fds, *, before=None, release=None):
        return executor.execute(self.parsed(body), source_fd, reference_fds, run_step=confined_step([], before),
                                ffmpeg=FFMPEG, deadline=time.monotonic() + 120,
                                max_seconds=render_service.MAX_SOURCE_SECONDS, release=release)

    def test_a_snapshot_is_sealed_and_holds_exactly_the_verified_bytes(self):
        read_only, mutate, _ = self.mutable_copy(self.source)
        entry = client._media_entry(self.source)
        snapshot = executor.snapshot_input(read_only, entry, "source")
        try:
            self.assertTrue(os.readlink(f"/proc/self/fd/{snapshot}").startswith("/memfd:isadoraair-render-input"))
            self.assertEqual(fcntl.fcntl(snapshot, fcntl.F_GET_SEALS) & executor.SEALS, executor.SEALS)
            attempts = {
                "write": lambda: os.pwrite(snapshot, b"x", 0),
                "shrink": lambda: os.ftruncate(snapshot, 0),
                "grow": lambda: os.ftruncate(snapshot, entry["bytes"] + 1),
                "append": lambda: os.pwrite(snapshot, b"x", entry["bytes"]),
                "reseal": lambda: fcntl.fcntl(snapshot, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE),
                "writable map": lambda: __import__("mmap").mmap(snapshot, entry["bytes"]),
            }
            for label, attempt in attempts.items():
                with self.subTest(attempt=label), self.assertRaises(PermissionError):
                    attempt()
            mutate()                                                    # the caller's inode now holds B
            self.assertEqual((os.fstat(snapshot).st_size, executor.fd_sha256(snapshot)),
                             (entry["bytes"], entry["sha256"]))
            reopened = executor.reopen_read_only(snapshot)
            try:
                self.assertEqual(executor.fd_sha256(reopened), entry["sha256"])
            finally:
                os.close(reopened)
        finally:
            os.close(snapshot)

    def test_steps_only_ever_reopen_sealed_snapshots(self):
        read_only, _, _ = self.mutable_copy(self.source)
        unsealed = executor.new_memfd("probe")
        try:
            for label, fd in (("the caller's descriptor", read_only), ("an unsealed memfd", unsealed)):
                with self.subTest(fd=label), self.assertRaises(render.RenderError):
                    executor.reopen_read_only(fd)
        finally:
            os.close(unsealed)

    def test_a_mutation_between_sealed_snapshot_and_first_step_has_no_effect(self):
        """The exact boundary: the snapshots are sealed, the first step has
        not started (``release`` runs there) -- both the source's and the
        bed's inode are rewritten. The render is the render of A."""
        body, fds, close = build_request(self.source, "3min_ctr")
        self.addCleanup(close)
        baseline = self.execute(body, fds[0], {"bed": fds[1]})
        os.close(baseline.fd)
        source, mutate_source, _ = self.mutable_copy(self.source)
        bed, mutate_bed, _ = self.mutable_copy(self.bed_row.media)
        order = []

        def release():
            order.append("snapshots sealed")
            mutate_source()
            mutate_bed()

        def before(argv, pass_fds):
            order.append("step")
            if order.count("step") == 1:
                for fd in pass_fds[:1]:                                  # the prepare step's input
                    self.assertEqual(executor.fd_sha256(fd), self.source.sha256)
        rendered = self.execute(body, source, {"bed": bed}, before=before, release=release)
        try:
            self.assertEqual(order[:2], ["snapshots sealed", "step"])
            self.assertEqual((rendered.sha256, rendered.size, rendered.measured_input),
                             (baseline.sha256, baseline.size, baseline.measured_input))
        finally:
            os.close(rendered.fd)

    def test_codex_input_substitution_reproduction_through_the_service(self):
        """Codex: verified payload A; the same inode overwritten with
        same-size payload B; the render must still consume A."""
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            body, fds, close = build_request(self.source, "3min_ctr")
            self.addCleanup(close)
            answer, received = send(service.socket_path, body, fds)
            baseline = json.loads(answer)
            for fd in received:
                os.close(fd)
            self.assertEqual(baseline["status"], "ok", baseline)
            source, mutate_source, path = self.mutable_copy(self.source)
            bed, mutate_bed, bed_path = self.mutable_copy(self.bed_row.media)
            real_step, seen = service.run_step, []

            def first_step_mutates(argv, pass_fds, timeout, cancelled=None):
                if not seen:
                    # the service already let go of the caller's descriptors:
                    # only this test's own still name the files
                    seen.append((_links(str(path)), _links(str(bed_path))))
                    mutate_source()
                    mutate_bed()
                return real_step(argv, pass_fds, timeout, cancelled)
            with mock.patch.object(service, "run_step", first_step_mutates):
                answer, received = send(service.socket_path, body, [source, bed])
            answer = json.loads(answer)
            try:
                self.assertEqual(answer["status"], "ok", answer)
                self.assertEqual(seen, [([source], [bed])])
                client._verify_output(received[0], answer)
                self.assertEqual((answer["output"], answer["measured_input"]),
                                 (baseline["output"], baseline["measured_input"]))
            finally:
                for fd in received:
                    os.close(fd)
            self.assertNotEqual(executor.fd_sha256(source), self.source.sha256)    # the inode did change

    def test_snapshot_exhaustion_fails_cleanly_and_releases_everything(self):
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            body, fds, close = build_request(self.source, "3min_ctr")
            self.addCleanup(close)
            never = mock.Mock(side_effect=AssertionError("a step ran"))
            calls = {"n": 0}
            real_write = executor._write_all

            def exhausted(fd, data):
                calls["n"] += 1
                if calls["n"] > 1:                                       # the bed's snapshot: no room
                    raise OSError(errno.ENOMEM, "no room")
                real_write(fd, data)
            with mock.patch.object(service, "run_step", never), mock.patch.object(executor, "_write_all", exhausted):
                answer, received = send(service.socket_path, body, fds)
            self.assertEqual((json.loads(answer)["code"], received), ("render_resource_limit", []))
            with mock.patch.object(service, "run_step", never), mock.patch.object(executor, "MAX_INPUT_BYTES", 1024):
                answer, received = send(service.socket_path, body, fds)
            self.assertEqual((json.loads(answer)["code"], received), ("input_too_large", []))
            # the service closed every descriptor it received and every partial snapshot
            inputs = [str(layout_path(self.source)), str(layout_path(self.bed_row.media))]
            self.assertTrue(_eventually(lambda: [_links(name) for name in inputs] == [[fds[0]], [fds[1]]], 10))
            self.assertEqual(_links("/memfd:isadoraair-render"), [])
            status = render_service.status_snapshot(service.socket_path)
            self.assertEqual((status["active"], status["pending"]), (0, 0))
            answer, received = send(service.socket_path, body, fds)          # the slot came back
            self.assertEqual(json.loads(answer)["status"], "ok")
            for fd in received:
                os.close(fd)

    def test_the_largest_single_input_snapshots_within_bounds(self):
        """A PLATFORM_MAX_BYTES (512 MiB) input -- the largest one media may
        be -- snapshots in one pass, sealed, and is gone once closed."""
        from production import policy
        size = policy.PLATFORM_MAX_BYTES
        directory = Path(tempfile.mkdtemp(prefix="render-large-"))
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / "large"
        with open(path, "wb") as handle:
            handle.truncate(size)
        digest, zeros = hashlib.sha256(), bytes(1024 * 1024)
        for _ in range(size // len(zeros)):
            digest.update(zeros)
        read_only = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
        self.addCleanup(_close_quietly, read_only)
        entry = {"bytes": size, "sha256": digest.hexdigest()}
        snapshot = executor.snapshot_input(read_only, entry, "source")
        try:
            self.assertTrue(executor.is_sealed(snapshot))
            self.assertEqual(os.fstat(snapshot).st_size, size)
        finally:
            os.close(snapshot)
        self.assertEqual(_links("/memfd:isadoraair-render"), [])
        with self.assertRaises(render.RenderError) as raised:            # grown past its claim: refused
            with open(path, "ab") as handle:
                handle.write(b"x")
            executor.snapshot_input(read_only, entry, "source")
        self.assertEqual(raised.exception.code, "descriptor_mismatch")
        self.assertEqual(_links("/memfd:isadoraair-render"), [])


def layout_path(row):
    from production.services import layout
    return layout.resolve_storage_path(row.storage_key)


# -- the sandbox every render step gets -----------------------------------------------------

PROBE = r"""
import json, os, socket, sys
targets = json.loads(sys.argv[1])
found = {"env": dict(os.environ)}
for label, path in targets["read"].items():
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            os.close(os.open(path, os.O_RDONLY))
        found[label] = "allowed"
    except OSError as exc:
        found[label] = type(exc).__name__
for label, path in targets["write"].items():
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        found[label] = "allowed"
    except OSError as exc:
        found[label] = type(exc).__name__
for label, family in (("inet", socket.AF_INET), ("inet6", socket.AF_INET6), ("unix", socket.AF_UNIX),
                      ("netlink", socket.AF_NETLINK)):
    try:
        socket.socket(family, socket.SOCK_STREAM if family != socket.AF_NETLINK else socket.SOCK_RAW).close()
        found["socket_" + label] = "allowed"
    except OSError as exc:
        found["socket_" + label] = type(exc).__name__
print(json.dumps(found))
"""


class SandboxTests(IsolatedMediaRootMixin, SimpleTestCase):
    """What a hostile render step (a compromised ffmpeg) could do -- probed by
    running a probe program through the service's own run_step."""

    def setUp(self):
        super().setUp()
        self.service = render_service.RenderService(_private_socket_path(self), ffmpeg=FFMPEG)

    def probe(self, extra_env=None):
        runtime = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
        targets = {
            "read": {"repo_source": str(REPO / "manage.py"), "home": str(Path.home()),
                     "media_root": str(self.root.parent), "runtime_dir": runtime, "opt": "/opt",
                     "ffmpeg_binary": FFMPEG, "usr_lib": "/usr/lib"},
            "write": {"tmp": f"/tmp/isadoraair-render-probe-{os.getpid()}",
                      "runtime": os.path.join(runtime, f"render-probe-{os.getpid()}"),
                      "repo": str(REPO / f"render-probe-{os.getpid()}"), "dev_shm": f"/dev/shm/probe-{os.getpid()}"},
        }
        with mock.patch.dict(os.environ, extra_env or {}):
            result = self.service.run_step([SYSTEM_PYTHON, "-I", "-S", "-c", PROBE, json.dumps(targets)], (), 30)
        self.assertEqual(result["status"], "ok", result)
        for path in targets["write"].values():
            self.assertFalse(os.path.exists(path), path)
        return json.loads(result["stdout"])

    def test_no_writes_no_station_reads_no_sockets_and_a_fixed_environment(self):
        found = self.probe(extra_env={"GST_PLUGIN_PATH": "/tmp/evil", "LD_PRELOAD": "/tmp/evil.so",
                                      "FFREPORT": "file=/tmp/x", "http_proxy": "http://evil"})
        for label in ("tmp", "runtime", "repo", "dev_shm"):
            self.assertEqual(found[label], "PermissionError", label)
        for label in ("repo_source", "home", "media_root", "runtime_dir", "opt"):
            self.assertEqual(found[label], "PermissionError", label)
        for label in ("ffmpeg_binary", "usr_lib"):
            self.assertEqual(found[label], "allowed", label)
        for family in ("inet", "inet6", "unix", "netlink"):
            self.assertEqual(found["socket_" + family], "PermissionError", family)
        self.assertEqual(found["env"], render_service.TOOL_ENVIRONMENT)

    def test_a_fork_bomb_is_bounded_and_the_leaf_destroyed(self):
        bomb = "import os\nwhile True:\n    try:\n        os.fork()\n    except OSError:\n        pass\n"
        result = self.service.run_step([SYSTEM_PYTHON, "-I", "-S", "-c", bomb], (), 5)
        self.assertIn(result["status"], ("timeout", "resource_limit"))
        self.assertTrue(result["cgroup"]["cleaned"])
        self.assertLessEqual(result["cgroup"].get("pids_peak") or 0, render_service.render_limits().group_tasks)
        self.assertEqual(_run_leaves(), [])

    def test_the_tool_cannot_be_anything_outside_the_system_trees(self):
        script = Path(tempfile.mkdtemp(prefix="render-tool-")) / "tool.sh"
        self.addCleanup(shutil.rmtree, script.parent, True)
        script.write_text("#!/bin/sh\necho ran\n")
        script.chmod(0o755)
        result = self.service.run_step([str(script)], (), 10)
        self.assertNotEqual(result.get("stdout", "").strip(), "ran")         # exec refused by Landlock


# -- the service: protocol, admission, lifecycle ---------------------------------------------

@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ServiceProtocolTests(MediaFixtures, TestCase):
    def test_a_well_formed_request_returns_one_sealed_flac(self):
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            body, fds, close = build_request(self.source, "3min_ctr")
            try:
                answer, received = send(service.socket_path, body, fds)
            finally:
                close()
        answer = json.loads(answer)
        self.assertEqual(answer["status"], "ok", answer)
        self.assertEqual(len(received), 1)
        try:
            client._verify_output(received[0], answer)
            self.assertTrue(answer["toolchain"].endswith(protocol.EXECUTOR_MARKER))
        finally:
            os.close(received[0])
        self.assertEqual(_run_leaves(), [])

    def test_nothing_but_a_render_can_be_expressed(self):
        spy = mock.Mock(side_effect=AssertionError("a step ran"))
        with in_process_render_service(self, ffmpeg=FFMPEG) as service, \
                mock.patch.object(service, "run_step", spy):
            body, fds, close = build_request(self.source, "lcl_psa")
            try:
                attacks = {
                    "run a command": {**body, "op": "RUN_COMMAND"},
                    "exec": {**body, "op": "exec"},
                    "argv field": {**body, "argv": ["/bin/sh", "-c", "id"]},
                    "ffmpeg args": {**body, "ffmpeg_args": ["-i", "/etc/passwd"]},
                    "filtergraph": {**body, "filter_complex": "amovie=/etc/passwd"},
                    "executable": {**body, "executable": "/bin/sh"},
                    "a path": {**body, "source": {**body["source"], "path": "/etc/passwd"}},
                    "shell metacharacters": {**body, "request_id": "$(id)" + "a" * 26},
                    "other version": {**body, "v": 2},
                    "url as recipe": {**body, "recipe": {"key": "http://evil/x", "version": 1}},
                }
                for label, attack in attacks.items():
                    with self.subTest(attack=label):
                        answer, received = send(service.socket_path, attack, fds)
                        self.assertEqual((json.loads(answer)["status"], received), ("error", []))
                for label, raw in {"malformed json": b"{not json\n", "empty": b"\n",
                                   "oversized": b"{" + b" " * (protocol.REQUEST_LIMIT_BYTES + 10) + b"}\n"}.items():
                    with self.subTest(attack=label):
                        answer, received = send(service.socket_path, None, fds, raw=raw)
                        self.assertEqual(json.loads(answer)["code"], "invalid_request")
                with self.subTest(attack="too many descriptors"):
                    answer, _ = send(service.socket_path, body, fds * 4)
                    self.assertEqual(json.loads(answer)["code"], "invalid_request")
                with self.subTest(attack="too few descriptors"):
                    answer, _ = send(service.socket_path, body, [])
                    self.assertEqual(json.loads(answer)["code"], "invalid_request")
            finally:
                close()
        spy.assert_not_called()

    def test_bad_requests_and_failed_renders_poison_nothing(self):
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            body, fds, close = build_request(self.source, "lcl_psa")
            try:
                for _ in range(3):
                    send(service.socket_path, None, fds, raw=b"garbage\n")
                with mock.patch.object(service, "run_step", return_value={"status": "failed", "returncode": 1,
                                                                           "stderr": "boom"}):
                    answer, received = send(service.socket_path, body, fds)
                self.assertEqual((json.loads(answer)["code"], received), ("render_failed", []))
                self.assertNotIn("boom", answer.decode())                      # no tool output to clients
                answer, received = send(service.socket_path, body, fds)
                self.assertEqual(json.loads(answer)["status"], "ok")
                for fd in received:
                    os.close(fd)
            finally:
                close()
            status = render_service.status_snapshot(service.socket_path)
            self.assertEqual((status["active"], status["pending"], status["last_failure"]["code"]),
                             (0, 0, "render_failed"))

    def test_admission_ceilings_busy_and_slot_release(self):
        """max_active 1, max_pending 1: one renders, one waits, the rest are
        busy at once; every slot comes back."""
        long_voice = ingest(audio.voice(60.0))
        slow = render_service.render_limits().__class__(**{**render_service.render_limits().__dict__,
                                                            "cpu_percent": 1})
        with in_process_render_service(self, ffmpeg=FFMPEG, max_active="1", max_pending="1", limits=slow) as service:
            body, fds, close = build_request(long_voice, "lcl_psa")
            try:
                held = []
                for _ in range(2):
                    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    sock.connect(service.socket_path)
                    sock.sendmsg([(json.dumps(body) + "\n").encode()],
                                 [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
                    held.append(sock)
                self.assertTrue(_eventually(lambda: (render_service.status_snapshot(service.socket_path) or {})
                                            .get("active") == 1 and (render_service.status_snapshot(
                                                service.socket_path) or {}).get("pending") == 1, 30),
                                render_service.status_snapshot(service.socket_path))
                for _ in range(3):
                    answer, received = send(service.socket_path, body, fds)
                    self.assertEqual((json.loads(answer)["status"], received), ("busy", []))
                self.assertEqual(render_health.observe(service.socket_path)["at_capacity"], True)
                self.assertLessEqual(len(_run_leaves()), 1)                   # never more than max_active
                for sock in held:
                    sock.close()                                              # the clients go away
                self.assertTrue(_eventually(lambda: render_service.status_snapshot(service.socket_path)["active"]
                                            == 0 and not _run_leaves(), 30))
            finally:
                close()
            body, fds, close = build_request(self.source, "lcl_psa")
            try:
                answer, received = send(service.socket_path, body, fds)       # slots released: it renders
                self.assertEqual(json.loads(answer)["status"], "ok")
                for fd in received:
                    os.close(fd)
            finally:
                close()

    def test_a_render_past_its_deadline_times_out_and_releases_its_slot(self):
        long_voice = ingest(audio.voice(30.0))
        slow = render_service.render_limits().__class__(**{**render_service.render_limits().__dict__,
                                                            "cpu_percent": 1})
        with in_process_render_service(self, ffmpeg=FFMPEG, limits=slow) as service:
            body, fds, close = build_request(long_voice, "lcl_psa", timeout=1.0)
            try:
                answer, received = send(service.socket_path, body, fds)
            finally:
                close()
            self.assertEqual((json.loads(answer)["code"], received), ("render_timeout", []))
            self.assertTrue(_eventually(lambda: not _run_leaves(), 15))
            self.assertEqual(render_service.status_snapshot(service.socket_path)["active"], 0)

    def test_only_the_services_own_account_is_served(self):
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            self.assertEqual(os.stat(service.socket_path).st_mode & 0o777, 0o600)
            self.assertEqual(os.stat(os.path.dirname(service.socket_path)).st_mode & 0o777, 0o700)
            with mock.patch.object(confinement, "peer_uid", return_value=os.geteuid() + 1):
                try:
                    answer, received = send(service.socket_path, {}, [])
                except render.RenderError:
                    answer, received = b"", []                               # reset: closed without a word
            self.assertEqual((answer, received), (b"", []))
        open_dir = tempfile.mkdtemp(prefix="render-open-")
        self.addCleanup(shutil.rmtree, open_dir, True)
        os.chmod(open_dir, 0o755)
        with self.assertRaises(RuntimeError):
            render_service.RenderService(os.path.join(open_dir, "r.sock"), ffmpeg=FFMPEG).bind()

    def test_leftover_runs_are_reaped_before_the_service_listens(self):
        import secrets
        import subprocess
        root = confinement.establish_root(confinement.RENDER)
        leaf = root / f"run-{secrets.token_hex(16)}"
        leaf.mkdir()
        sleeper = subprocess.Popen(["/bin/sleep", "300"])
        (leaf / "cgroup.procs").write_text(str(sleeper.pid))
        self.addCleanup(lambda: (sleeper.poll() is None and sleeper.kill(), sleeper.wait()))
        with in_process_render_service(self, ffmpeg=FFMPEG):
            self.assertTrue(_eventually(lambda: sleeper.poll() is not None, 10))
            self.assertFalse(leaf.exists())


# -- the client -----------------------------------------------------------------------------

@contextmanager
def fake_service(testcase, answer_for):
    """A stand-in render service (same account) answering ``answer_for(body)``
    -> (json-able answer or raw bytes, descriptors to send)."""
    path = _private_socket_path(testcase)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(4)
    stop = threading.Event()

    def serve():
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                data, ancdata, _, _ = conn.recvmsg(protocol.REQUEST_LIMIT_BYTES, socket.CMSG_SPACE(4 * 8))
                for level, kind, payload in ancdata:
                    for i in range(0, len(payload) - len(payload) % 4, 4):
                        os.close(int.from_bytes(payload[i:i + 4], sys.byteorder, signed=True))
                answer, fds = answer_for(json.loads(data))
                line = answer if isinstance(answer, bytes) else (json.dumps(answer) + "\n").encode()
                conn.sendmsg([line], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else [])
                for fd in fds:
                    os.close(fd)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        with override_settings(PRODUCTION_RENDER_SOCKET=path):
            yield
    finally:
        stop.set()
        listener.close()
        thread.join(5)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class ClientTests(MediaFixtures, TestCase):
    def good_answer(self, body, flac=b"fLaC" + b"\0" * 64, seal=True):
        fd = executor.new_memfd("fake")
        os.write(fd, flac)
        if seal:
            fcntl.fcntl(fd, fcntl.F_ADD_SEALS, executor.SEALS)
        recipe, resolved, plan = provenance.prepare(body["recipe"]["key"], body["params"])
        pins = {name: protocol.pin_of(protocol.parse_request(body, max_bytes=2 ** 30,
                                                             max_seconds=render_service.MAX_SOURCE_SECONDS)
                                      ["references"][name]) for name in body["references"]}
        document = provenance.document(recipe, resolved, pins)
        answer = {"v": 1, "status": "ok", "request_id": body["request_id"], "recipe": body["recipe"],
                  "digest": body["expected_digest"], "provenance": document,
                  "toolchain": f"ffmpeg version x; render-engine 1; {protocol.EXECUTOR_MARKER}",
                  "measured_input": {}, "output": {"sha256": executor.fd_sha256(fd), "bytes": len(flac)},
                  "executor": {"service": "isadoraair-render", "protocol": 1}}
        return answer, [fd]

    def assert_client_refuses(self, code, answer_for):
        with fake_service(self, answer_for), self.assertRaises(render.RenderError) as raised:
            client.request_render(self.source, "lcl_psa")
        self.assertEqual(raised.exception.code, code)

    def test_the_client_accepts_only_a_sealed_verified_answer_for_its_own_request(self):
        def tweak(**changes):
            def answer_for(body):
                answer, fds = self.good_answer(body)
                answer.update(changes)
                return answer, fds
            return answer_for

        def unsealed(body):
            return self.good_answer(body, seal=False)

        def two_fds(body):
            answer, fds = self.good_answer(body)
            return answer, fds + [os.dup(fds[0])]

        def wrong_size(body):
            answer, fds = self.good_answer(body)
            answer["output"]["bytes"] += 1
            return answer, fds

        def wrong_hash(body):
            answer, fds = self.good_answer(body)
            answer["output"]["sha256"] = "0" * 64
            return answer, fds

        def no_fd(body):
            answer, fds = self.good_answer(body)
            os.close(fds[0])
            return answer, []
        cases = {
            "unsealed memfd": ("descriptor_invalid", unsealed), "two descriptors": ("descriptor_invalid", two_fds),
            "no descriptor": ("descriptor_invalid", no_fd), "size mismatch": ("descriptor_mismatch", wrong_size),
            "hash mismatch": ("descriptor_mismatch", wrong_hash),
            "another request": ("provenance_mismatch", tweak(request_id="cd" * 16)),
            "another digest": ("provenance_mismatch", tweak(digest="2" * 64)),
            "another recipe version": ("provenance_mismatch", tweak(recipe={"key": "lcl_psa", "version": 9})),
            "no executor identity": ("provenance_mismatch", tweak(toolchain="ffmpeg version x")),
            "an error answer": ("render_failed", lambda body: (protocol.error("render_failed"), [])),
            "busy": ("busy", lambda body: (protocol.error("busy"), [])),
            "garbage": ("service_unavailable", lambda body: (b"not json\n", [])),
        }
        for label, (code, answer_for) in cases.items():
            with self.subTest(case=label):
                self.assert_client_refuses(code, answer_for)

    def test_the_socket_must_belong_to_the_services_own_account(self):
        with fake_service(self, self.good_answer), \
                mock.patch.object(confinement, "peer_uid", return_value=os.geteuid() + 1), \
                self.assertRaises(render.RenderError) as raised:
            client.request_render(self.source, "lcl_psa")
        self.assertEqual(raised.exception.code, "service_unavailable")

    def test_no_service_fails_closed_and_retryably(self):
        with override_settings(PRODUCTION_RENDER_SOCKET="/nonexistent/render.sock"), \
                self.assertRaises(render.RenderError) as raised:
            client.request_render(self.source, "lcl_psa")
        self.assertEqual((raised.exception.code, raised.exception.retryable), ("service_unavailable", True))


# -- rendition authority -----------------------------------------------------------------

@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class RenditionAuthorityTests(MediaFixtures, TestCase):
    def columns(self, **overrides):
        values = dict(kind=ProductionMedia.KIND_RENDITION, storage_key="ab/" + "c" * 32, sha256="d" * 64,
                      byte_size=10, derived_from=self.source, recipe_key="lcl_psa", recipe_version=1,
                      recipe_params_digest="e" * 64, toolchain=f"x; {protocol.EXECUTOR_MARKER}",
                      validation_state="unvalidated")
        values.update(overrides)
        return values

    def authority(self, **overrides):
        values = dict(derived_from_id=self.source.pk, recipe_key="lcl_psa", recipe_version=1,
                      recipe_params_digest="e" * 64, toolchain=f"x; {protocol.EXECUTOR_MARKER}", sha256="d" * 64)
        values.update(overrides)
        return rendition_authority(**values)

    def test_ordinary_orm_code_cannot_create_a_rendition(self):
        before = ProductionMedia.objects.count()
        attempts = {
            "objects.create": lambda: ProductionMedia.objects.create(**self.columns()),
            "save": lambda: ProductionMedia(**self.columns()).save(),
            "bulk_create": lambda: ProductionMedia.objects.bulk_create([ProductionMedia(**self.columns())]),
            "base manager bulk_create": lambda: ProductionMedia._base_manager.bulk_create(
                [ProductionMedia(**self.columns())]),
            "reverse manager": lambda: self.source.derivatives.create(**{
                k: v for k, v in self.columns().items() if k != "derived_from"}),
        }
        for label, attempt in attempts.items():
            with self.subTest(path=label), self.assertRaises(ImmutableMediaError) as raised, transaction.atomic():
                attempt()
            self.assertEqual(raised.exception.code, "rendition_authority")
        self.assertEqual(ProductionMedia.objects.count(), before)

    def test_the_authority_admits_exactly_one_matching_row(self):
        for label, overrides in {"other sha": {"sha256": "f" * 64}, "other digest": {"recipe_params_digest": "1" * 64},
                                 "other parent": {"derived_from": ingest(audio.voice(1.2))},
                                 "other toolchain": {"toolchain": "forged"}, "other version": {"recipe_version": 2}}.items():
            with self.subTest(case=label), self.authority(), self.assertRaises(ImmutableMediaError), \
                    transaction.atomic():
                ProductionMedia.objects.create(**self.columns(**overrides))
        with self.authority(), transaction.atomic():
            ProductionMedia.objects.create(**self.columns())
            with self.assertRaises(ImmutableMediaError), transaction.atomic():         # single use
                ProductionMedia.objects.create(**self.columns(storage_key="ab/" + "9" * 32))
        with self.authority(), self.assertRaises(ImmutableMediaError):
            with self.authority():                                                # no nesting
                pass

    def test_a_pre_save_receiver_cannot_alter_the_authorized_rendition(self):
        def receiver(sender, instance, **kwargs):
            if sender is ProductionMedia:
                instance.recipe_params_digest = "0" * 64
        pre_save.connect(receiver, weak=False)
        try:
            with self.authority(), self.assertRaises(ImmutableMediaError), transaction.atomic():
                ProductionMedia.objects.create(**self.columns())
        finally:
            pre_save.disconnect(receiver)
        self.assertFalse(ProductionMedia.objects.filter(kind=ProductionMedia.KIND_RENDITION).exists())

    def test_only_issued_single_use_outputs_for_their_own_source_become_renditions(self):
        forged = client.ExecutorOutput(fd=executor.new_memfd("forged"), request_id="ab" * 16,
                                       source_id=str(self.source.pk), recipe_key="lcl_psa", recipe_version=1,
                                       digest="e" * 64, provenance_json="{}", toolchain=protocol.EXECUTOR_MARKER,
                                       sha256="d" * 64, size=0, measured_input={})
        self.addCleanup(os.close, forged.fd)
        with self.assertRaises(render.RenderError) as raised:
            rendition.ingest(forged, self.source)
        self.assertEqual(raised.exception.code, "rendition_authority")
        output = client.request_render(self.source, "lcl_psa")
        other = ingest(audio.voice(1.3))
        with self.assertRaises(render.RenderError) as raised:
            rendition.ingest(output, other)                            # rendered from another source
        self.assertEqual(raised.exception.code, "rendition_authority")
        with self.assertRaises(render.RenderError) as raised:
            rendition.ingest(output, self.source)                      # already used
        self.assertEqual(raised.exception.code, "rendition_authority")
        self.assertFalse(ProductionMedia.objects.filter(kind=ProductionMedia.KIND_RENDITION).exists())

    def test_the_real_chain_keeps_descriptor_continuity_and_records_the_executor(self):
        output = client.request_render(self.source, "3min_ctr")
        sealed_sha = executor.fd_sha256(output.fd)
        with self.assertRaises(PermissionError):
            os.pwrite(output.fd, b"tampered", 0)                       # the bytes cannot change in flight
        result = rendition.ingest(output, self.source)
        media = result.media
        self.assertEqual((media.kind, media.validation_state, media.sha256), ("rendition", "valid", sealed_sha))
        self.assertTrue(media.toolchain.endswith(protocol.EXECUTOR_MARKER))
        self.assertEqual(media.recipe_params_digest, result.evidence.provenance_digest)
        self.assertFalse(client.issued(output))                        # consumed: single use


# -- independence from the validation service; Monitoring ------------------------------------

@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class IndependenceTests(MediaFixtures, TestCase):
    def test_validation_runs_while_the_renderer_is_saturated(self):
        long_voice = ingest(audio.voice(60.0))
        slow = render_service.render_limits().__class__(**{**render_service.render_limits().__dict__,
                                                            "cpu_percent": 1})
        with in_process_render_service(self, ffmpeg=FFMPEG, max_active="1", max_pending="0", limits=slow) as service:
            body, fds, close = build_request(long_voice, "lcl_psa")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(service.socket_path)
                sock.sendmsg([(json.dumps(body) + "\n").encode()],
                             [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
                self.assertTrue(_eventually(lambda: (render_service.status_snapshot(service.socket_path) or {})
                                            .get("active") == 1, 30))
                answer, _ = send(service.socket_path, body, fds)
                self.assertEqual(json.loads(answer)["status"], "busy")          # the renderer is full
                started = time.monotonic()
                validated = ingest(audio.voice(2.0))                            # ...the validator is not
                self.assertTrue(validated.is_valid)
                self.assertLess(time.monotonic() - started, 30)
            finally:
                sock.close()
                close()

    def test_a_missing_validator_leaves_the_renderer_healthy(self):
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            with override_settings(PRODUCTION_VALIDATION_SOCKET="/nonexistent/validator.sock"), \
                    self.assertRaises(render.RenderError) as raised:
                render.render(self.source, "lcl_psa")
            self.assertEqual((raised.exception.code, raised.exception.retryable), ("rendition_unvalidated", True))
            self.assertEqual(raised.exception.media.validation_state, "unvalidated")      # an unbindable orphan
            self.assertTrue(render_health.observe(service.socket_path)["ready"])
            result = render.render(self.source, "lcl_psa")                          # validator back: it works
            self.assertTrue(result.media.is_valid)

    def test_a_missing_renderer_leaves_the_validator_healthy(self):
        with override_settings(PRODUCTION_RENDER_SOCKET="/nonexistent/render.sock"):
            with self.assertRaises(render.RenderError) as raised:
                render.render(self.source, "lcl_psa")
            self.assertEqual(raised.exception.code, "service_unavailable")
            self.assertTrue(ingest(audio.voice(1.0)).is_valid)


class RenderHealthTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_observe_reports_readiness_capacity_and_failures_read_only(self):
        with in_process_render_service(self, ffmpeg=FFMPEG) as service:
            seen = render_health.observe(service.socket_path)
            self.assertEqual((seen["ready"], seen["max_active"], seen["max_pending"], seen["active"], seen["pending"]),
                             (True, 1, 2, 0, 0))
            os.unlink(service.status_path())
            self.assertEqual(render_health.observe(service.socket_path)["reason"], render_health.STATUS_MISSING)
        self.assertEqual(render_health.observe("/nonexistent/render.sock")["reason"], render_health.SOCKET_MISSING)
        stale = Path(_private_socket_path(self))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(stale))
        listener.close()                                                       # a dead predecessor's socket
        self.assertEqual(render_health.observe(str(stale))["reason"], render_health.NOT_LISTENING)

    def test_the_monitoring_probe_reports_the_render_unit(self):
        from monitoring.services import probes
        check = mock.Mock(id=987, systemd_unit=probes.RENDER_UNIT)
        with in_process_render_service(self, ffmpeg=FFMPEG):
            status, detail = probes._render_readiness(check, {"NRestarts": "0"},
                                                      {"active_state": "active", "sub_state": "running"})
        self.assertEqual((status, detail["restart_via_dashboard"], detail["active"], detail["max_active"]),
                         ("ok", False, 0, 1))
        status, detail = probes._render_readiness(check, {"NRestarts": "0"},
                                                  {"active_state": "inactive", "sub_state": "dead"})
        self.assertEqual((status, detail["reason"]), ("critical", "service_inactive"))
