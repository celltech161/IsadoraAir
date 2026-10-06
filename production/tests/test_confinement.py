"""2.22B / B6 -- the kernel boundary around ONE validation run (the executor).

``confinement.execute`` runs only inside the isadoraair-validation service
(test_validation_service covers the service, its lifecycle and the client);
these tests drive it directly, from a process holding the same kind of
delegated subtree the service holds. Synthetic helper programs (plain Python
children run through the real launcher) prove the boundary is really established -- not merely requested:
aggregate cgroup limits, per-process rlimits, Landlock and seccomp; that no
task of a validation run survives ``execute`` however it re-parents,
re-sessions or tries to migrate itself; that every failure is a retryable
infrastructure outcome, never a verdict on the media; and that nothing ever
runs outside the boundary.

These tests need a delegated cgroup v2 subtree, exactly as the validation
service has (see production/tests/run_with_validation_service.sh); without one
they FAIL rather than
skip -- the absence of the boundary is precisely what must never go unnoticed.
"""
import os
import resource
import secrets
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from production.services import confined_exec, confinement, validation
from production.services.confinement import Limits, execute

PY = sys.executable
MIB = 1024 * 1024
HOW_TO_RUN = ("no delegated cgroup v2 subtree for this test process -- run the suite through "
              "`production/tests/run_with_validation_service.sh <python> manage.py test ...` (production "
              "gives the validation service its subtree with Delegate=/DelegateSubgroup= in "
              "deploy/isadoraair-validation.service)")


def _script(body: str, *argv) -> list[str]:
    return [PY, "-c", textwrap.dedent(body), *map(str, argv)]


def _proc_status(field: str, pid="self") -> str:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:                                  # a zombie still answers kill(0)
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (FileNotFoundError, IndexError):
        return False


def _wait_dead(pids, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and any(_alive(pid) for pid in pids):
        time.sleep(0.02)
    return [pid for pid in pids if _alive(pid)]


def _pids(stdout: str, tag: str) -> list[int]:
    return [int(line.split()[1]) for line in stdout.splitlines() if line.startswith(tag + " ")]


class ConfinementTestCase(SimpleTestCase):
    def setUp(self):
        super().setUp()
        try:
            self.root = confinement.establish_root()
        except confinement.ConfinementUnavailable as exc:
            self.fail(f"{HOW_TO_RUN}: {exc}")

    def assert_scope_gone(self, result, pids=()):
        """After execute returns: the leaf is gone (rmdir only succeeds on
        an EMPTY cgroup) and every named task is dead."""
        kernel = result["cgroup"]
        self.assertTrue(kernel["cleaned"], result)
        self.assertFalse(Path(kernel["path"]).exists(), kernel)
        self.assertEqual([pid for pid in pids if _alive(pid)], [], "a validation task survived")


class BoundaryIsEstablishedTests(ConfinementTestCase):
    def test_this_test_process_has_a_delegated_validation_subtree(self):
        enabled = set((self.root / "cgroup.subtree_control").read_text().split())
        self.assertTrue({"cpu", "memory", "pids"} <= enabled, enabled)
        self.assertEqual(self.root.name, confinement.VALIDATION_CGROUP_NAME)

    def test_the_tree_runs_in_its_own_leaf_under_every_aggregate_limit(self):
        limits = Limits(group_memory_bytes=300 * MIB, group_tasks=23, cpu_percent=50)
        result = execute(_script("""
            from pathlib import Path
            leaf = "/sys/fs/cgroup" + open("/proc/self/cgroup").read().strip()[3:]
            for name in ("memory.max", "memory.swap.max", "memory.oom.group", "pids.max", "cpu.max"):
                p = Path(leaf, name)
                print(name, p.read_text().strip() if p.exists() else "-")
            print("leaf", leaf)
        """), timeout_seconds=30, limits=limits)
        self.assertEqual(result["status"], "ok", result)
        seen = dict(line.split(" ", 1) for line in result["stdout"].splitlines())
        self.assertEqual(seen["memory.max"], str(300 * MIB))
        self.assertIn(seen["memory.swap.max"], ("0", "-"))
        self.assertEqual((seen["memory.oom.group"], seen["pids.max"]), ("1", "23"))
        self.assertEqual(seen["cpu.max"], "50000 100000")
        self.assertEqual(Path(seen["leaf"]).parent, self.root)
        self.assertEqual(seen["leaf"], result["cgroup"]["path"])
        self.assert_scope_gone(result)

    def test_the_child_runs_under_every_per_process_limit(self):
        limits = Limits(memory_bytes=700 * MIB, cpu_seconds=17, file_size_bytes=3 * MIB, open_files=99)
        result = execute(_script("""
            import resource as r
            print(r.getrlimit(r.RLIMIT_AS), r.getrlimit(r.RLIMIT_CPU), r.getrlimit(r.RLIMIT_FSIZE),
                  r.getrlimit(r.RLIMIT_NOFILE), r.getrlimit(r.RLIMIT_CORE))
        """), timeout_seconds=30, limits=limits)
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["confined"])
        expected = ((700 * MIB, 700 * MIB), (17, 22), (3 * MIB, 3 * MIB), (99, 99), (0, 0))
        self.assertEqual(result["stdout"].strip(), " ".join(str(item) for item in expected))

    def test_the_child_is_sandboxed_by_no_new_privs_seccomp_and_landlock(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "written"
            result = execute(_script("""
                import sys
                status = dict(l.split(":", 1) for l in open("/proc/self/status").read().splitlines())
                print("nnp", status["NoNewPrivs"].strip(), "seccomp", status["Seccomp"].strip())
                open("/dev/null", "w").write("ok")          # the one writable path
                try:
                    open(sys.argv[1], "w").write("x")
                    print("WROTE")
                except PermissionError:
                    print("write-refused")
            """, target), timeout_seconds=30)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["stdout"].split(), ["nnp", "1", "seccomp", "2", "write-refused"])
            self.assertFalse(target.exists())

    def test_settings_configure_the_limits(self):
        with override_settings(PRODUCTION_VALIDATION_LIMITS={"memory_bytes": 512 * MIB, "cpu_seconds": 9,
                                                             "group_tasks": 12}):
            limits = confinement.configured_limits()
        self.assertEqual((limits.memory_bytes, limits.cpu_seconds, limits.group_tasks), (512 * MIB, 9, 12))
        self.assertEqual(confinement.configured_limits(), Limits())
        for bad in ({"memory_bytes": 0}, {"cpu_seconds": -1}, {"open_files": True}, {"group_tasks": 0},
                    {"group_memory_bytes": "1G"}, {"nonsense": 1}):
            with self.subTest(bad=bad), override_settings(PRODUCTION_VALIDATION_LIMITS=bad):
                with self.assertRaises((ValueError, TypeError)):
                    confinement.configured_limits()

    def test_the_parent_web_process_is_untouched(self):
        names = ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE", "RLIMIT_NOFILE", "RLIMIT_CORE", "RLIMIT_NPROC")
        before = ({name: resource.getrlimit(getattr(resource, name)) for name in names},
                  confined_exec.own_cgroup(), _proc_status("NoNewPrivs"), _proc_status("Seccomp"))
        result = execute(_script("print('x')"), timeout_seconds=30, limits=Limits(memory_bytes=300 * MIB))
        self.assertEqual(result["status"], "ok", result)
        after = ({name: resource.getrlimit(getattr(resource, name)) for name in names},
                 confined_exec.own_cgroup(), _proc_status("NoNewPrivs"), _proc_status("Seccomp"))
        self.assertEqual(before, after)
        self.assertEqual((after[2], after[3]), ("0", "0"))
        with tempfile.NamedTemporaryFile() as handle:   # and it can still write files
            handle.write(b"x")


class ResourceExhaustionTests(ConfinementTestCase):
    def test_a_single_process_memory_bomb_hits_the_address_space_limit(self):
        result = execute(_script("""
            blocks = []
            for _ in range(64):
                blocks.append(bytearray(64 * 1024 * 1024))   # 4 GiB in total
            print("allocated everything")
        """), timeout_seconds=60, limits=Limits(memory_bytes=256 * MIB))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertNotIn("allocated everything", result["stdout"])
        self.assert_scope_gone(result)

    def test_aggregate_memory_across_several_children_is_bounded_by_the_cgroup(self):
        """Each child stays far below its own RLIMIT_AS; together they exceed the
        leaf's memory.max -- only the aggregate limit can stop this."""
        started = time.monotonic()
        result = execute(_script("""
            import subprocess, sys, time
            eat = "b = bytearray(b'\\\\x01') * (160 * 1024 * 1024); print('child', flush=True); import time; time.sleep(60)"
            kids = [subprocess.Popen([sys.executable, "-c", eat]) for _ in range(3)]
            for kid in kids:
                print("pid", kid.pid, flush=True)
            for kid in kids:
                kid.wait()
            print("survived")
        """), timeout_seconds=60, limits=Limits(memory_bytes=1024 * MIB, group_memory_bytes=256 * MIB))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertGreaterEqual(result["cgroup"]["oom_kill"], 1, result)
        self.assertNotIn("survived", result["stdout"])
        self.assertLess(time.monotonic() - started, 30)
        self.assert_scope_gone(result, _pids(result["stdout"], "pid"))

    def test_a_cpu_runaway_is_killed_by_the_cpu_limit_long_before_the_wall_timeout(self):
        started = time.monotonic()
        result = execute(_script("while True:\n    pass"), timeout_seconds=60, limits=Limits(cpu_seconds=1))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertLess(time.monotonic() - started, 30)
        self.assert_scope_gone(result)

    def test_aggregate_cpu_time_across_several_children_is_bounded(self):
        """Four spinning children: none reaches its own RLIMIT_CPU before the
        tree's aggregate CPU budget is spent."""
        started = time.monotonic()
        result = execute(_script("""
            import subprocess, sys
            spin = "while True: pass"
            kids = [subprocess.Popen([sys.executable, "-c", spin]) for _ in range(4)]
            for kid in kids:
                print("pid", kid.pid, flush=True)
            for kid in kids:
                kid.wait()
        """), timeout_seconds=60, limits=Limits(cpu_seconds=3))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertGreaterEqual(result["cgroup"]["cpu_usec"], 3_000_000)
        self.assertLess(time.monotonic() - started, 12)      # 4 x 3 s of per-process CPU would be far later
        self.assert_scope_gone(result, _pids(result["stdout"], "pid"))

    def test_a_hung_tool_is_stopped_by_the_wall_timeout(self):
        started = time.monotonic()
        result = execute(_script("import time; time.sleep(600)"), timeout_seconds=1.0)
        self.assertEqual(result["status"], "timeout", result)
        self.assertLess(time.monotonic() - started, 15)
        self.assert_scope_gone(result)

    def test_fork_pressure_is_bounded_by_the_task_limit(self):
        result = execute(_script("""
            import os, sys, time
            made = 0
            try:
                while made < 10000:
                    pid = os.fork()
                    if pid == 0:
                        time.sleep(300)
                        os._exit(0)
                    made += 1
            except OSError:
                pass
            print("forked", made, flush=True)
            sys.exit(3)
        """), timeout_seconds=60, limits=Limits(group_tasks=16))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertLess(int(result["stdout"].split()[1]), 16)
        self.assertGreaterEqual(result["cgroup"]["pids_max_events"], 1)
        self.assert_scope_gone(result)

    def test_thread_pressure_is_bounded_by_the_task_limit(self):
        result = execute(_script("""
            import sys, threading, time
            made = 0
            try:
                while made < 10000:
                    threading.Thread(target=time.sleep, args=(300,), daemon=True).start()
                    made += 1
            except RuntimeError:
                pass
            print("threads", made, flush=True)
            sys.exit(3)
        """), timeout_seconds=60, limits=Limits(group_tasks=16))
        self.assertEqual(result["status"], "resource_limit", result)
        self.assertLess(int(result["stdout"].split()[1]), 16)
        self.assert_scope_gone(result)

    def test_file_output_is_impossible(self):
        """RLIMIT_FSIZE remains as a backstop, but Landlock already refuses every
        file write outside /dev/null."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "grows"
            result = execute(_script("""
                import sys
                with open(sys.argv[1], "wb") as handle:
                    handle.write(b"\\0" * (64 * 1024 * 1024))
            """, target), timeout_seconds=60, limits=Limits(file_size_bytes=1 * MIB))
            self.assertEqual(result["status"], "failed", result)
            self.assertIn("PermissionError", result["stderr"])
            self.assertFalse(target.exists())


class NoTaskEscapesTests(ConfinementTestCase):
    """Every way a descendant can leave the parent's process tree, session or
    process group -- and every way it could try to leave the cgroup -- still
    ends with the task dead when execute returns."""

    def test_an_ordinary_child_is_killed_with_the_tool_on_timeout(self):
        result = execute(_script("""
            import subprocess, sys, time
            kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
            print("pid", kid.pid, flush=True)
            time.sleep(300)
        """), timeout_seconds=2.0)
        self.assertEqual(result["status"], "timeout", result)
        self.assert_scope_gone(result, _pids(result["stdout"], "pid"))

    def test_descendants_are_reaped_after_the_tool_exits_successfully(self):
        result = execute(_script("""
            import subprocess, sys, time
            kid = subprocess.Popen([sys.executable, "-c",
                "import subprocess, sys, time; g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']);"
                " print('pid', g.pid, flush=True); time.sleep(300)"])
            print("pid", kid.pid, flush=True)
            time.sleep(0.5)
        """), timeout_seconds=30.0)
        self.assertEqual(result["status"], "ok", result)
        pids = _pids(result["stdout"], "pid")
        self.assertEqual(len(pids), 2, result)
        self.assert_scope_gone(result, pids)

    def test_a_child_that_calls_setsid_and_outlives_the_tool_is_killed(self):
        """Codex's reproduction: setsid() escaped the old process-group kill."""
        result = execute(_script("""
            import os, sys, time
            if os.fork() == 0:
                os.setsid()
                print("pid", os.getpid(), "sid", os.getsid(0), flush=True)
                time.sleep(300)
                os._exit(0)
            time.sleep(0.5)
        """), timeout_seconds=30.0)
        self.assertEqual(result["status"], "ok", result)
        pids = _pids(result["stdout"], "pid")
        self.assertEqual(len(pids), 1, result)
        self.assert_scope_gone(result, pids)

    def test_a_double_forked_daemon_in_its_own_session_and_group_is_killed(self):
        result = execute(_script("""
            import os, sys, time
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    os.setpgid(0, 0)
                    print("pid", os.getpid(), flush=True)
                    devnull = os.open("/dev/null", os.O_RDWR)
                    for fd in (0, 1, 2):            # a real daemon detaches from the pipes
                        os.dup2(devnull, fd)
                    time.sleep(300)
                os._exit(0)
            time.sleep(0.5)
        """), timeout_seconds=30.0)
        self.assertEqual(result["status"], "ok", result)
        pids = _pids(result["stdout"], "pid")
        self.assertEqual(len(pids), 1, result)
        self.assert_scope_gone(result, pids)

    def test_a_parent_that_exits_before_its_descendant_leaves_nothing_behind(self):
        result = execute(_script("""
            import os, sys, time
            if os.fork() == 0:
                if os.fork() == 0:
                    time.sleep(0.5)                 # orphaned once its parent exits
                    print("pid", os.getpid(), "ppid", os.getppid(), flush=True)
                    time.sleep(300)
                os._exit(0)
            time.sleep(1.5)
        """), timeout_seconds=30.0)
        self.assertEqual(result["status"], "ok", result)
        pids = _pids(result["stdout"], "pid")
        self.assertEqual(len(pids), 1, result)
        self.assert_scope_gone(result, pids)

    def test_a_descendant_cannot_migrate_itself_out_of_the_leaf(self):
        """Writing cgroup.procs (Landlock), clone3(CLONE_INTO_CGROUP) (seccomp),
        creating a cgroup to flee into, or signalling the web process all fail;
        the would-be escapee dies with the run."""
        web = "/sys/fs/cgroup" + confined_exec.own_cgroup()
        result = execute(_script("""
            import ctypes, os, sys, time
            web, parent_pid = sys.argv[1], int(sys.argv[2])
            libc = ctypes.CDLL(None, use_errno=True)
            leaf = "/sys/fs/cgroup" + open("/proc/self/cgroup").read().strip()[3:]
            root = os.path.dirname(leaf)
            unit = os.path.dirname(root)
            if os.fork() == 0:
                os.setsid()
                for target in (web, root, unit, leaf):
                    try:
                        with open(os.path.join(target, "cgroup.procs"), "w") as handle:
                            handle.write("0")
                        print("ESCAPED-procs", target, flush=True)
                    except OSError as exc:
                        print("procs-refused", exc.errno, flush=True)
                try:
                    os.mkdir(os.path.join(unit, "flee"))
                    print("ESCAPED-mkdir", flush=True)
                except OSError as exc:
                    print("mkdir-refused", exc.errno, flush=True)
                class CloneArgs(ctypes.Structure):
                    _fields_ = [(n, ctypes.c_uint64) for n in ("flags", "pidfd", "child_tid", "parent_tid",
                        "exit_signal", "stack", "stack_size", "tls", "set_tid", "set_tid_size", "cgroup")]
                fd = os.open(web, os.O_RDONLY | os.O_DIRECTORY)
                args = CloneArgs(flags=0x200000000, exit_signal=17, cgroup=fd)      # CLONE_INTO_CGROUP
                if libc.syscall(435, ctypes.byref(args), ctypes.c_size_t(ctypes.sizeof(args))) == 0:
                    os._exit(0)
                print("clone3-refused", ctypes.get_errno(), flush=True)
                try:
                    os.kill(parent_pid, 0)
                    print("ESCAPED-signal", flush=True)
                except PermissionError:
                    print("signal-refused", flush=True)
                print("pid", os.getpid(), "cgroup", open("/proc/self/cgroup").read().strip()[3:], flush=True)
                time.sleep(300)
                os._exit(0)
            time.sleep(1.5)
        """, web, os.getpid()), timeout_seconds=30.0)
        self.assertEqual(result["status"], "ok", result)
        out = result["stdout"]
        self.assertNotIn("ESCAPED", out)
        self.assertEqual(out.count("procs-refused"), 4, out)
        for line in ("mkdir-refused", "clone3-refused 38", "signal-refused"):
            self.assertIn(line, out)
        self.assertIn("cgroup " + result["cgroup"]["path"][len("/sys/fs/cgroup"):], out)
        self.assertFalse(Path(web).parent.joinpath("flee").exists())
        self.assert_scope_gone(result, _pids(out, "pid"))

    def test_reaping_destroys_every_run_leaf_without_consulting_any_pid(self):
        """What the validation service does at start-up and when it stops: every
        run-* leaf in its exclusive subtree is killed and removed. Ownership is
        the subtree, not a PID -- nothing is asked about any process' liveness."""
        sleepers, leaves = [], []
        unrelated = self.root / "not-a-run"
        unrelated.mkdir()
        self.addCleanup(unrelated.rmdir)
        try:
            for _ in range(2):
                leaf = self.root / f"run-{secrets.token_hex(16)}"
                leaf.mkdir()
                sleeper = subprocess.Popen(["/bin/sleep", "300"])
                (leaf / "cgroup.procs").write_text(str(sleeper.pid))
                sleepers.append(sleeper)
                leaves.append(leaf)
            with mock.patch.object(confinement.os, "kill", side_effect=AssertionError("no PID is consulted")):
                reaped = confinement.reap_all(self.root)
            self.assertEqual(sorted(r["path"] for r in reaped), sorted(map(str, leaves)))
            self.assertTrue(all(r["cleaned"] for r in reaped))
            for sleeper in sleepers:
                sleeper.wait(timeout=5)
                self.assertEqual(sleeper.returncode, -9)
            self.assertFalse(any(leaf.exists() for leaf in leaves))
            self.assertTrue(unrelated.exists())                    # only run leaves are touched
        finally:
            for sleeper in sleepers:
                if sleeper.poll() is None:
                    sleeper.kill()
                    sleeper.wait()
            for leaf in leaves:
                confinement._destroy(leaf)

    def test_run_leaves_are_named_by_a_random_id_never_a_pid(self):
        result = execute(_script("print('x')"), timeout_seconds=30)
        name = Path(result["cgroup"]["path"]).name
        self.assertRegex(name, r"^run-[0-9a-f]{32}$")
        self.assertNotIn(str(os.getpid()), name)

    def test_a_leaf_that_cannot_be_emptied_is_reported_never_ignored(self):
        with mock.patch.object(confinement, "CLEANUP_TIMEOUT_SECONDS", 0.2), \
                mock.patch.object(confinement, "_populated", return_value=True):
            result = execute(_script("print('x')"), timeout_seconds=30)
        self.addCleanup(confinement._destroy, Path(result["cgroup"]["path"]))
        self.assertEqual(result["status"], "confinement_unavailable", result)
        self.assertFalse(result["cgroup"]["cleaned"])


class FailClosedTests(ConfinementTestCase):
    def _assert_never_ran(self, result):
        self.assertEqual(result["status"], "confinement_unavailable", result)
        self.assertNotIn("RAN", result.get("stdout", ""))

    def test_a_missing_launcher_never_runs_the_tool_unconfined(self):
        with mock.patch.object(confinement, "LAUNCHER", "/nonexistent/confined_exec.py"):
            self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))

    def test_no_delegated_subtree_never_runs_the_tool(self):
        for path in ("/sys/fs/cgroup/user.slice/iportal-validation",           # root-owned: not delegated
                     "/sys/fs/cgroup/iportal-validation",
                     "/tmp/iportal-validation", "relative/iportal-validation",
                     "/sys/fs/cgroup/../tmp/iportal-validation"):
            with self.subTest(path=path), override_settings(PRODUCTION_VALIDATION_CGROUP=path):
                self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))
        self.assertFalse(Path("/sys/fs/cgroup/user.slice/iportal-validation").exists())

    def test_a_missing_controller_never_runs_the_tool(self):
        with mock.patch.object(confinement, "REQUIRED_CONTROLLERS", ("memory", "pids", "no-such-controller")):
            self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))

    def test_cpu_is_a_required_controller(self):
        """The promised one-CPU bound depends on the cpu controller: a subtree
        without it is refused, never silently weakened."""
        self.assertEqual(set(confinement.REQUIRED_CONTROLLERS), {"cpu", "memory", "pids"})
        real_read = confinement._read

        def no_cpu(path):
            text = real_read(path)
            if path.name in ("cgroup.controllers", "cgroup.subtree_control"):
                return " ".join(word for word in text.split() if word != "cpu")
            return text

        with mock.patch.object(confinement, "_read", side_effect=no_cpu):
            result = execute(_script("print('RAN')"), timeout_seconds=30)
        self._assert_never_ran(result)
        self.assertIn("cpu", result["stderr"])

    def test_every_leaf_facility_is_required(self):
        """No swap control, no CPU bandwidth file, no cgroup.kill, no
        memory.oom.group, no pids.max: refused, never weakened."""
        real_exists = Path.exists
        for missing in ("memory.swap.max", "cpu.max", "cgroup.kill", "memory.oom.group", "pids.max", "memory.max"):
            def exists(path, missing=missing):
                if path.name == missing and path.parent.name.startswith("run-"):
                    return False
                return real_exists(path)
            with self.subTest(missing=missing), mock.patch.object(Path, "exists", exists):
                result = execute(_script("print('RAN')"), timeout_seconds=30)
            self._assert_never_ran(result)
            self.assertIn(missing, result["stderr"])
        self.assertEqual([p for p in self.root.iterdir() if p.name.startswith("run-")], [])

    def test_a_limit_that_does_not_read_back_never_runs_the_tool(self):
        real_read = confinement._read
        for name in ("memory.swap.max", "cpu.max", "memory.max", "pids.max"):
            def unlimited(path, name=name):
                return "max" if path.name == name else real_read(path)
            with self.subTest(limit=name), mock.patch.object(confinement, "_read", side_effect=unlimited):
                self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))

    def test_limits_the_launcher_refuses_never_run_the_tool(self):
        real = confinement._launcher_argv

        def zero_memory(leaf, executable, args, limits):
            argv = real(leaf, executable, args, limits)
            argv[argv.index("--memory") + 1] = "0"
            return argv

        with mock.patch.object(confinement, "_launcher_argv", zero_memory):
            self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))

    def test_a_cgroup_the_launcher_cannot_join_never_runs_the_tool(self):
        real = confinement._launcher_argv

        def elsewhere(leaf, executable, args, limits):
            argv = real(leaf, executable, args, limits)
            argv[argv.index("--cgroup") + 1] = str(self.root / "not-a-leaf")
            return argv

        with mock.patch.object(confinement, "_launcher_argv", elsewhere):
            self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))

    def test_landlock_or_seccomp_unavailable_never_runs_the_tool(self):
        """The launcher's own fail-closed path (in process, with a refusing libc)."""
        class Refusing:
            def syscall(self, *args):
                return -1

            def prctl(self, *args):
                return -1

        with self.assertRaises(confined_exec._Refused):
            confined_exec._landlock(Refusing())
        with self.assertRaises(confined_exec._Refused):
            confined_exec._seccomp(Refusing())
        with self.assertRaises(confined_exec._Refused):
            confined_exec._no_new_privs(Refusing())
        for step in ("_landlock", "_seccomp", "_no_new_privs"):
            with self.subTest(step=step), \
                    mock.patch.object(confined_exec, "_join_cgroup"), \
                    mock.patch.object(confined_exec, "_libc", return_value=Refusing()), \
                    mock.patch.object(confined_exec, "_fail", side_effect=SystemExit) as fail, \
                    mock.patch.object(confined_exec.os, "execv") as execv:
                for other in {"_landlock", "_seccomp", "_no_new_privs"} - {step}:
                    mock.patch.object(confined_exec, other).start()
                try:
                    with self.assertRaises(SystemExit):
                        confined_exec.main(["--cgroup", "/sys/fs/cgroup/x", "--memory", "1", "--cpu", "1",
                                            "--fsize", "1", "--nofile", "8", "--", "/bin/true"])
                finally:
                    mock.patch.stopall()
                self.assertEqual(fail.call_args.args[0], confined_exec.EXIT_LIMITS)
                execv.assert_not_called()

    def test_a_missing_tool_is_unavailable_not_a_failure_of_the_media(self):
        result = execute(["/nonexistent/definitely-not-a-tool", "x"], timeout_seconds=5)
        self.assertEqual(result["status"], "unavailable")

    def test_a_clean_retry_works_after_a_confinement_failure(self):
        with override_settings(PRODUCTION_VALIDATION_CGROUP="/sys/fs/cgroup/user.slice/iportal-validation"):
            self._assert_never_ran(execute(_script("print('RAN')"), timeout_seconds=30))
        limited = execute(_script("while True:\n    pass"), timeout_seconds=30, limits=Limits(cpu_seconds=1))
        self.assertEqual(limited["status"], "resource_limit")
        good = execute(_script("print('fine')"), timeout_seconds=30)
        self.assertEqual((good["status"], good["stdout"].strip()), ("ok", "fine"))
        self.assert_scope_gone(good)


class ValidationFailureMappingTests(SimpleTestCase):
    def test_killed_engine_probe_maps_to_a_known_infrastructure_code(self):
        """Pre-existing latent defect: 'engine_probe_killed' was not a known code,
        so a killed GStreamer child crashed validation with AssertionError."""
        outcome = validation._run_failure({"status": "failed", "returncode": -15}, "engine_probe")
        self.assertEqual((outcome.status, outcome.code), (validation.STATUS_INFRASTRUCTURE, "engine_probe_killed"))
        for status, code in (("resource_limit", "validation_resource_limit"),
                             ("confinement_unavailable", "confinement_unavailable")):
            outcome = validation._run_failure({"status": status, "returncode": None}, "decode")
            self.assertEqual(outcome.code, code)
