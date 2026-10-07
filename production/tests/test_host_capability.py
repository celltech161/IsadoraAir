"""r0107 release: the read-only host preflight the protected updater runs, via
production.0001_initial's migration preflight, before any mutation.

Each facility is checked against synthetic /proc and cgroup trees (both the
pass and every fail shape), the sandbox self-test against deliberately broken
launchers, the ProductionMedia root against real temporary directories, and
the preflight command's registry/output contract the executor enforces.
"""
import json
import os
import re
import shutil
import tempfile
import textwrap
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase, override_settings

from production.services import host_capability as host

from .support import IsolatedMediaRootMixin

# The executor's own acceptance rules for a preflight check result.
PREFLIGHT_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")


def _tree(testcase):
    root = Path(tempfile.mkdtemp(prefix="host-capability-"))
    testcase.addCleanup(shutil.rmtree, root, True)
    return root


def fake_host(testcase, *, fstype="cgroup2", root_controllers="cpuset cpu io memory pids",
              slice_controllers="cpu memory pids", slice_files=tuple(host.REQUIRED_INTERFACE_FILES),
              own="/system.slice/isadoraair-updater.service"):
    base = _tree(testcase)
    proc, cgroup = base / "proc", base / "cgroup"
    (proc / "self").mkdir(parents=True)
    cgroup.mkdir()
    (proc / "self" / "mountinfo").write_text(
        f"25 1 0:22 / / rw - ext4 /dev/sda1 rw\n"
        f"37 28 0:31 / {cgroup} rw,nosuid - {fstype} {fstype} rw\n" if fstype else "25 1 0:22 / / rw - ext4 x rw\n")
    (proc / "self" / "cgroup").write_text(f"0::{own}\n")
    (cgroup / "cgroup.controllers").write_text(root_controllers + "\n")
    slice_dir = cgroup / "system.slice"
    unit = cgroup / own.lstrip("/")
    unit.mkdir(parents=True)
    for directory in (slice_dir, unit):
        (directory / "cgroup.controllers").write_text(slice_controllers + "\n")
        for name in slice_files:
            (directory / name).write_text("max\n")
    return {"proc": proc, "cgroup_mount": cgroup}


class FacilityTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_this_host_provides_everything(self):
        evidence = host.check_validation_host()
        self.assertTrue(evidence["ok"], json.dumps(evidence, indent=1))
        self.assertEqual(set(evidence["facilities"]),
                         {"cgroup_v2_unified", "controllers", "leaf_interface_files",
                          "systemd_delegate_subgroup", "sandbox"})

    def test_a_complete_synthetic_host_passes(self):
        paths = fake_host(self)
        self.assertTrue(host.cgroup_v2_unified(**paths)["ok"])
        self.assertTrue(host.controllers_available(cgroup_mount=paths["cgroup_mount"])["ok"])
        self.assertTrue(host.leaf_interfaces(**paths)["ok"])

    def test_no_unified_cgroup2_hierarchy_fails(self):
        for fstype in ("tmpfs", "cgroup", ""):           # hybrid/legacy layouts, nothing mounted
            with self.subTest(fstype=fstype):
                self.assertFalse(host.cgroup_v2_unified(**fake_host(self, fstype=fstype))["ok"])

    def test_each_missing_controller_fails(self):
        for missing in host.REQUIRED_CONTROLLERS:
            with self.subTest(missing=missing):
                paths = fake_host(self, root_controllers=" ".join(
                    name for name in ("cpu", "memory", "pids", "io") if name != missing))
                result = host.controllers_available(cgroup_mount=paths["cgroup_mount"])
                self.assertFalse(result["ok"])
                self.assertIn(missing, result["detail"])

    def test_each_missing_leaf_interface_file_fails(self):
        for missing in host.REQUIRED_INTERFACE_FILES:
            with self.subTest(missing=missing):
                paths = fake_host(self, slice_files=[name for name in host.REQUIRED_INTERFACE_FILES
                                                     if name != missing])
                result = host.leaf_interfaces(**paths)
                self.assertFalse(result["ok"])
                self.assertIn(missing, result["detail"])

    def test_a_file_without_its_controller_enabled_does_not_count(self):
        paths = fake_host(self, slice_controllers="memory pids")        # cpu.max present, cpu not enabled
        result = host.leaf_interfaces(**paths)
        self.assertFalse(result["ok"])
        self.assertIn("cpu.max", result["detail"])

    def _systemctl(self, output, code=0):
        directory = _tree(self)
        script = directory / "systemctl"
        script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{output}'\nexit {code}\n")
        script.chmod(0o755)
        return (str(script),)

    def test_systemd_must_support_delegate_subgroup(self):
        self.assertTrue(host.systemd_version(systemctl_candidates=self._systemctl("systemd 254 (254.1)"))["ok"])
        self.assertTrue(host.systemd_version(systemctl_candidates=self._systemctl("systemd 259 (259.5)"))["ok"])
        for output, code in (("systemd 253 (253.4)", 0), ("systemd 249", 0), ("not systemd", 0),
                             ("systemd 259", 1)):
            with self.subTest(output=output, code=code):
                self.assertFalse(host.systemd_version(systemctl_candidates=self._systemctl(output, code))["ok"])
        self.assertFalse(host.systemd_version(systemctl_candidates=("/nonexistent/systemctl",))["ok"])

    def _launcher(self, *, landlock="real", seccomp="real", refuse=False):
        real = (Path(host.__file__).with_name("confined_exec.py")).read_text()
        body = real + textwrap.dedent(f"""
            _real_landlock, _real_seccomp = _landlock, _seccomp
            def _landlock(libc):
                if {refuse!r}:
                    raise _Refused("Landlock is not available")
                return _real_landlock(libc) if {landlock!r} == "real" else 6
            def _seccomp(libc):
                return _real_seccomp(libc) if {seccomp!r} == "real" else None
        """)
        path = _tree(self) / "confined_exec.py"
        path.write_text(body)
        return path

    def test_the_sandbox_self_test_proves_the_sandbox_bites(self):
        self.assertTrue(host.sandbox_selftest()["ok"])
        self.assertTrue(host.sandbox_selftest(launcher=self._launcher())["ok"])
        cases = {
            "no Landlock in force": (self._launcher(landlock="noop"), "write"),
            "no seccomp in force": (self._launcher(seccomp="noop"), "clone3"),
            "Landlock unavailable": (self._launcher(refuse=True), "Landlock is not available"),
            "no launcher at all": (Path("/nonexistent/confined_exec.py"), "failed"),
        }
        for label, (launcher, expected) in cases.items():
            with self.subTest(label):
                result = host.sandbox_selftest(launcher=launcher)
                self.assertFalse(result["ok"], result)
                self.assertIn(expected, result["detail"])

    def test_any_failing_facility_fails_the_check(self):
        broken = {"ok": False, "detail": "x"}
        for name in ("cgroup_v2_unified", "controllers_available", "leaf_interfaces", "systemd_version",
                     "sandbox_selftest"):
            with self.subTest(name), mock.patch.object(host, name, return_value=broken):
                self.assertFalse(host.check_validation_host()["ok"])


class MediaRootTests(IsolatedMediaRootMixin, SimpleTestCase):
    def test_an_absent_root_under_a_writable_parent_is_establishable_and_nothing_is_created(self):
        result = host.check_media_root()
        self.assertTrue(result["ok"], result)
        self.assertFalse(self.root.exists())

    def test_an_existing_dedicated_root_is_usable(self):
        self.root.mkdir(mode=0o750)
        for name in ("media", "incoming", "work", "locks"):
            (self.root / name).mkdir(mode=0o750)
        self.assertTrue(host.check_media_root()["ok"])

    def test_judged_as_the_services_will_see_it_not_through_the_updaters_read_only_view(self):
        """The updater's ProtectSystem=strict namespace makes /srv read-only to
        it (os.access says no); the services' account can still create the root."""
        with mock.patch.object(host.os, "access", return_value=False):
            self.assertTrue(host.check_media_root()["ok"])
            self.root.mkdir(mode=0o750)
            self.assertTrue(host.check_media_root()["ok"])

    def test_refusals(self):
        self.root.parent.chmod(0o555)
        self.addCleanup(self.root.parent.chmod, 0o755)
        self.assertFalse(host.check_media_root()["ok"])                     # cannot be created
        self.root.parent.chmod(0o755)
        self.root.write_text("not a directory")
        self.assertFalse(host.check_media_root()["ok"])
        self.root.unlink()
        elsewhere = self.root.parent / "elsewhere"
        elsewhere.mkdir()
        self.root.symlink_to(elsewhere)
        self.assertFalse(host.check_media_root()["ok"])
        self.root.unlink()
        for unsafe in ("/srv/isadoraair", "/etc/production-media", "relative/production-media", ""):
            with self.subTest(unsafe=unsafe):
                self.assertFalse(host.check_media_root(raw=unsafe)["ok"])


class PreflightRegistryTests(IsolatedMediaRootMixin, TestCase):
    """The executor-facing contract (executor._run_migration_preflights)."""

    def run_command(self, *pending):
        from updatecenter.management.commands import updatecenter_migration_preflight as command
        out = StringIO()
        with mock.patch.object(command, "_applied", return_value=[]):     # the test DB has them applied
            call_command("updatecenter_migration_preflight", *[f"--pending={ref}" for ref in pending], stdout=out)
        return json.loads(out.getvalue())

    def assert_executor_accepts(self, payload, pending):
        self.assertEqual(set(payload), {"schema_version", "status", "checks"})
        self.assertLessEqual(len(json.dumps(payload).encode()), 65536)
        for item in payload["checks"]:
            self.assertEqual(set(item), {"id", "migration", "status", "evidence"})
            self.assertRegex(item["id"], PREFLIGHT_ID_RE)
            self.assertIn(item["migration"], pending)
            self.assertIn(item["status"], ("passed", "failed"))
            self.assertIsInstance(item["evidence"], dict)

    def test_production_0001_carries_both_host_checks_and_this_host_passes(self):
        pending = ["production.0001_initial", "library.0089_voicetrack_media"]
        payload = self.run_command(*pending)
        self.assert_executor_accepts(payload, pending)
        self.assertEqual([item["id"] for item in payload["checks"]],
                         ["production.0001.validation_host_capability", "production.0001.media_root_establishable"])
        self.assertEqual(payload["status"], "ok", payload)

    def test_an_unsupported_host_blocks_before_any_mutation(self):
        pending = ["production.0001_initial", "library.0089_voicetrack_media"]
        with mock.patch.object(host, "sandbox_selftest", return_value={"ok": False, "detail": "no Landlock"}):
            payload = self.run_command(*pending)
        self.assert_executor_accepts(payload, pending)
        self.assertEqual(payload["status"], "failed")
        failed = [item for item in payload["checks"] if item["status"] == "failed"]
        self.assertEqual([item["id"] for item in failed], ["production.0001.validation_host_capability"])
        self.assertFalse(failed[0]["evidence"]["facilities"]["sandbox"]["ok"])

    def test_an_unestablishable_media_root_blocks(self):
        with override_settings(PRODUCTION_MEDIA_ROOT="/srv/isadoraair"):
            payload = self.run_command("production.0001_initial")
        self.assertEqual(payload["status"], "failed")
        self.assertEqual([item["id"] for item in payload["checks"] if item["status"] == "failed"],
                         ["production.0001.media_root_establishable"])

    def test_no_host_check_once_production_media_exists(self):
        payload = self.run_command("library.0089_voicetrack_media")
        self.assertEqual(payload["checks"], [])
