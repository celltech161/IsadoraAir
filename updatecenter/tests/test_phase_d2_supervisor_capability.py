"""D2 corrective review, Correction 1: the Phase-D supervisor unit must
retain the SAME AmbientCapabilities the current r0025 worker unit has.

The worker is no longer its own systemd service under Phase D -- it
becomes a root CHILD PROCESS the supervisor launches (launch.py:
launch_worker() -> process.py: launch_tracked(), a plain fork+exec via
subprocess.Popen(), never an execve() that REPLACES the supervisor
itself). Linux ambient capabilities are preserved across BOTH of the
two exec boundaries this involves, as long as neither program is
privileged (setuid/file-capability) -- see capabilities(7)'s own
"ambient capability set is preserved across execve(2) of a program
that is not privileged" rule -- so a capability set on the SUPERVISOR's
own systemd unit is exactly what makes CAP_SETUID/CAP_SETGID reach the
worker CHILD process's own later `runuser --user ISA_USER` call
(isadoraair_updater.process.CommandRunner.run_as_user(), unchanged and
still runuser-based -- see this file's own import-based proof below).
Neither /usr/bin/python3 (the supervisor's own ExecStart target) nor
the worker's own entrypoint script is a setuid/file-capability binary,
so nothing along that chain clears the ambient set before it reaches
runuser.

This exact requirement was proven in production once already (r0006,
docs/UPDATE_CENTER.md's own "r0006 protected-updater hardening" and
"Manual systemd capability acceptance" sections) for the single-hop
case (systemd unit -> runuser directly). This test file exists so a
FUTURE edit to deploy/updater-bootstrapd.service cannot silently drop
the line and only be caught by an expensive, manual, root-required
acceptance run."""
import ast
from pathlib import Path

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT, PROJECT_ROOT, RUNTIME_ROOT  # noqa: F401

SUPERVISOR_UNIT_PATH = PROJECT_ROOT / "deploy" / "updater-bootstrapd.service"
WORKER_UNIT_PATH = PROJECT_ROOT / "deploy" / "isadoraair-updater.service"


class SupervisorUnitRetainsCapabilityTests(SimpleTestCase):
    def setUp(self):
        self.supervisor_lines = set(SUPERVISOR_UNIT_PATH.read_text(encoding="utf-8").splitlines())

    def test_ambient_capabilities_present(self):
        self.assertIn("AmbientCapabilities=CAP_SETUID CAP_SETGID", self.supervisor_lines)

    def test_no_new_privileges_present(self):
        # NoNewPrivileges alone (without the ambient capability line) is
        # exactly the r0006-documented failure mode -- both must be
        # present together, matching the worker unit's own precedent.
        self.assertIn("NoNewPrivileges=true", self.supervisor_lines)

    def test_runs_as_root_not_isa_user(self):
        self.assertIn("User=root", self.supervisor_lines)

    def test_execstart_supplies_required_worker_config(self):
        exec_start = [
            line for line in self.supervisor_lines if line.startswith("ExecStart=")
        ]
        self.assertEqual(len(exec_start), 1)
        self.assertIn(
            "--worker-config /etc/isadoraair/station.json",
            exec_start[0],
        )

    def test_runtime_directories_cover_both_supervisor_and_worker_sockets(self):
        self.assertIn(
            "RuntimeDirectory=isadoraair-updater-bootstrap isadoraair-updater",
            self.supervisor_lines,
        )
        self.assertIn("RuntimeDirectoryMode=0750", self.supervisor_lines)

    def test_no_capability_bounding_set_introduced(self):
        # The task's own explicit instruction: do not casually introduce
        # a CapabilityBoundingSet= restriction alongside this fix.
        self.assertFalse(any(line.startswith("CapabilityBoundingSet") for line in self.supervisor_lines))

    def test_no_broader_ambient_capability_than_the_proven_pair(self):
        ambient_lines = [line for line in self.supervisor_lines if line.startswith("AmbientCapabilities=")]
        self.assertEqual(len(ambient_lines), 1)
        granted = set(ambient_lines[0].split("=", 1)[1].split())
        self.assertEqual(granted, {"CAP_SETUID", "CAP_SETGID"})

    def test_matches_the_proven_worker_unit_capability_line_exactly(self):
        # The worker unit's own r0006-hardened line is the proven-in-
        # production reference -- the supervisor's line must be
        # byte-identical to it, not an independently-drifted copy.
        worker_lines = set(WORKER_UNIT_PATH.read_text(encoding="utf-8").splitlines())
        worker_ambient = next(line for line in worker_lines if line.startswith("AmbientCapabilities="))
        supervisor_ambient = next(line for line in self.supervisor_lines if line.startswith("AmbientCapabilities="))
        self.assertEqual(worker_ambient, supervisor_ambient)

    def test_worker_process_launch_uses_plain_non_shell_exec_that_cannot_strip_ambient_capabilities(self):
        # Confirms the actual code path between "supervisor process" and
        # "worker child process" is a plain subprocess.Popen with no
        # shell and no setuid-adjacent flag -- the structural
        # precondition the capability-inheritance reasoning above
        # depends on. If launch.py/process.py ever started using a
        # setuid wrapper, os.system, or a shell to launch the worker,
        # THIS assertion (not just the unit-file line) would need
        # re-review.
        process_source = (BOOTSTRAP_ROOT / "isadoraair_updater_bootstrap" / "process.py").read_text(encoding="utf-8")
        launch_source = (BOOTSTRAP_ROOT / "isadoraair_updater_bootstrap" / "launch.py").read_text(encoding="utf-8")
        for source, label in ((process_source, "process.py"), (launch_source, "launch.py")):
            with self.subTest(file=label):
                self.assertNotIn("shell=True", source)
                self.assertNotIn("os.system(", source)
                tree = ast.parse(source)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call):
                        for keyword in node.keywords:
                            self.assertFalse(
                                keyword.arg == "shell" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                            )

    def test_worker_still_actually_uses_runuser_for_privilege_drop(self):
        # If the worker ever stops needing runuser entirely, the
        # ambient-capability requirement this whole test file exists
        # to protect would itself become obsolete -- proven here
        # directly rather than assumed, so that a genuine future
        # removal of runuser usage is a deliberate, reviewed test
        # change, not a silent drift this suite stops noticing.
        process_source = (RUNTIME_ROOT / "isadoraair_updater" / "process.py").read_text(encoding="utf-8")
        self.assertIn("runuser", process_source)
        self.assertIn("def run_as_user", process_source)

    def test_no_kill_mode_weakening_the_cgroup_wide_stop_restart_safety_net(self):
        # D2 corrective review, Correction 4: systemd's DEFAULT KillMode
        # (control-group, when the directive is simply absent) sends
        # SIGTERM/SIGKILL to the supervisor's WHOLE cgroup on `systemctl
        # stop`/`restart` -- including the worker child process
        # launch.py spawns (nothing in this codebase moves it to a
        # different cgroup). This is an independent safety net against
        # an orphaned worker surviving a supervisor stop/restart, on
        # top of (not instead of) any explicit termination the
        # supervisor's own code performs. KillMode=process would
        # narrow this to only the main PID -- refused here.
        kill_mode_lines = [line for line in self.supervisor_lines if line.startswith("KillMode=")]
        self.assertEqual(kill_mode_lines, [], "KillMode= must stay absent (systemd's safe control-group default)")

    def test_supervisor_config_never_names_an_executable(self):
        # Corroborates Correction 1's own acceptance target boundary:
        # the supervisor's fixed capability grant exists to support a
        # FIXED, compiled runuser invocation inside the worker -- never
        # a configurable command the supervisor itself might invoke
        # with elevated capabilities. See config.py's own module
        # docstring for the same rule stated at the config-schema level.
        config_source = (BOOTSTRAP_ROOT / "isadoraair_updater_bootstrap" / "config.py").read_text(encoding="utf-8")
        for forbidden in ("\"command\"", "\"commands\"", "\"hook\"", "\"hooks\"", "\"exec\""):
            self.assertNotIn(forbidden, config_source)
