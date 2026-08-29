"""Runtime Foundation E5 system-surface plan/apply/validate/lock/rollback tests."""

from __future__ import annotations

import multiprocessing
import os
import stat
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from isadoraair.runtime_components import load_runtime_components
from isadoraair.runtime_provisioning import ProvisioningLayout, RuntimeProvisioningError, runtime_provision_lock
from isadoraair.runtime_surfaces import (
    STATE_ABSENT,
    STATE_HEALTHY,
    STATE_SYMLINK,
    STATE_UNSAFE_PERMISSIONS,
    STATE_WRONG_CONTENT,
    STATE_WRONG_OWNER,
    STATE_WRONG_TYPE,
    RuntimeSystemSurfaceManager,
    SystemSurfaceSeams,
    _run_tmpfiles,
    validate_system_surfaces,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _lock_worker(target, product, entered, release=None):
    layout = ProvisioningLayout.from_manifest(product, target_root=target)
    with runtime_provision_lock(layout):
        entered.set()
        if release is not None and not release.wait(10):
            raise RuntimeError("lock test release timed out")


class SurfaceFixture(SimpleTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e5-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = self.root / "target"
        self.target.mkdir()
        self.product = deepcopy(load_runtime_components())

    def manager(self, *, target=None, seams=None):
        return RuntimeSystemSurfaceManager(
            target_root=target or self.target,
            product_manifest=self.product,
            project_root=PROJECT_ROOT,
            seams=seams or SystemSurfaceSeams(),
        )

    def paths(self, target=None):
        layout = ProvisioningLayout.from_manifest(self.product, target_root=target or self.target)
        return layout


class PlanTests(SurfaceFixture):
    def test_fresh_target_root_plans_install_for_every_surface(self):
        plan = self.manager().plan()
        self.assertTrue(plan.ready)
        self.assertEqual(plan.action, "install")
        self.assertEqual(
            plan.surfaces_needing_repair,
            ("launcher", "runtime_root", "tmpfiles_config", "tts_asset_root"),
        )
        for item in plan.current_evidence.surfaces.values():
            self.assertEqual(item.state, STATE_ABSENT)

    def test_plan_is_read_only(self):
        before = sorted(str(p) for p in self.target.rglob("*"))
        self.manager().plan()
        after = sorted(str(p) for p in self.target.rglob("*"))
        self.assertEqual(before, after)

    def test_healthy_target_plans_no_op(self):
        manager = self.manager()
        manager.apply()
        plan = self.manager().plan()
        self.assertEqual(plan.action, "no_op")
        self.assertEqual(plan.surfaces_needing_repair, ())
        self.assertTrue(plan.current_evidence.healthy)


class ApplyIdempotenceTests(SurfaceFixture):
    def test_first_apply_installs_and_second_is_exact_no_op(self):
        manager = self.manager()
        before = sorted(str(p) for p in self.target.rglob("*"))
        first = manager.apply()
        self.assertFalse(first.no_op)
        self.assertTrue(first.evidence.healthy)
        self.assertEqual(
            set(first.changed_surfaces), {"launcher", "tmpfiles_config", "tts_asset_root", "runtime_root"} & set(first.changed_surfaces)
        )
        after_first = sorted(str(p) for p in self.target.rglob("*"))
        self.assertNotEqual(before, after_first)

        second = self.manager().apply()
        self.assertTrue(second.no_op)
        self.assertEqual(second.changed_surfaces, ())
        after_second = sorted(str(p) for p in self.target.rglob("*"))
        self.assertEqual(after_first, after_second)

    def test_repeated_apply_does_not_recreate_tmpfiles_config(self):
        manager = self.manager()
        manager.apply()
        config = manager._tmpfiles_destination()
        mtime_before = config.stat().st_mtime_ns
        self.manager().apply()
        self.assertEqual(config.stat().st_mtime_ns, mtime_before)

    def test_unrelated_files_below_persistent_directories_survive(self):
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        marker = layout.runtime_root / "kokoro" / "generations" / "e3-existing" / "marker.txt"
        marker.parent.mkdir(parents=True)
        marker.write_text("do not touch", encoding="utf-8")

        result = self.manager().apply()
        self.assertTrue(result.no_op)
        self.assertEqual(marker.read_text(encoding="utf-8"), "do not touch")

    def test_default_launcher_content_embeds_canonical_application_root_not_mapped_target(self):
        """The core regression: target_root governs WHERE the launcher is
        written, never what its own persistent content references. A
        launcher written beneath a disposable/offline target root must
        still, by default, reference the canonical /opt/isadoraair --
        not the installer host's own mount point -- so it keeps working
        once that target filesystem actually becomes / after boot."""
        manager = self.manager()
        result = manager.apply()
        layout = self.paths()
        text = layout.tts_cli.read_text(encoding="utf-8")

        canonical_root = self.product["canonical_paths"]["application_root"]
        self.assertIn(canonical_root, text)
        self.assertNotIn(str(layout.application_root), text)
        self.assertNotIn(str(self.target), text)
        self.assertNotIn("@@ISADORAAIR_APPLICATION_ROOT@@", text)
        self.assertNotIn("/home/jreed", text)
        # No provider runtime path is embedded -- provider selection stays
        # owned by isadoraair.tts, entered only via `-m`. The template's own
        # explanatory docstring mentions Kokoro/Piper by name (to say why
        # they're absent); what must never appear is an actual provider
        # runtime path.
        self.assertNotIn("isadoraair-runtime/kokoro", text)
        self.assertNotIn("isadoraair-runtime/piper", text)
        self.assertNotIn("provider_cli", text)
        self.assertIn("-m", text)
        self.assertIn("isadoraair.tts", text)
        # File placement itself IS correctly mapped beneath the target.
        self.assertTrue(str(layout.tts_cli).startswith(str(self.target)))
        self.assertIn("launcher", result.changed_surfaces)

    def test_canonical_root_default_also_embeds_canonical_application_root(self):
        canonical_manager = RuntimeSystemSurfaceManager(
            target_root="/", product_manifest=self.product, project_root=PROJECT_ROOT
        )
        content = canonical_manager._rendered_launcher().decode()
        canonical_root = self.product["canonical_paths"]["application_root"]
        self.assertIn(canonical_root, content)

    def test_changing_target_root_alone_never_changes_embedded_runtime_path(self):
        mapped = self.manager()._rendered_launcher()
        canonical = RuntimeSystemSurfaceManager(
            target_root="/", product_manifest=self.product, project_root=PROJECT_ROOT
        )._rendered_launcher()
        self.assertEqual(mapped, canonical)

    def test_explicit_opt_in_seam_embeds_mapped_application_root(self):
        """Never inferred merely from target_root != '/' -- only when
        explicitly requested, for a disposable execution-testing seam."""
        manager = RuntimeSystemSurfaceManager(
            target_root=self.target,
            product_manifest=self.product,
            project_root=PROJECT_ROOT,
            embed_mapped_application_root=True,
        )
        layout = self.paths()
        content = manager._rendered_launcher().decode()
        self.assertIn(str(layout.application_root), content)
        canonical_marker_line = f'APPLICATION_ROOT_MARKER = "{self.product["canonical_paths"]["application_root"]}"'
        self.assertNotIn(canonical_marker_line, content)

    def test_validation_flags_mount_prefix_content_as_wrong_content(self):
        """A launcher that incorrectly contains the installer mount
        prefix (as if built with the old, buggy behavior) must not
        validate as healthy under the product-default manager."""
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        buggy_content = layout.tts_cli.read_text(encoding="utf-8").replace(
            self.product["canonical_paths"]["application_root"], str(layout.application_root)
        )
        layout.tts_cli.write_text(buggy_content, encoding="utf-8")
        layout.tts_cli.chmod(0o755)

        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["launcher"].state, STATE_WRONG_CONTENT)

    def test_default_offline_target_launcher_with_canonical_content_validates_healthy(self):
        """A launcher installed beneath a non-canonical target_root, but
        correctly containing the canonical embedded application root
        (the product default), must validate healthy -- the same
        rendering contract governs both apply and validate."""
        self.assertNotEqual(self.target, Path("/"))
        self.manager().apply()

        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["launcher"].state, STATE_HEALTHY)
        self.assertTrue(evidence.healthy)

    def test_directory_and_file_modes_and_ownership(self):
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        launcher_mode = stat.S_IMODE(layout.tts_cli.stat().st_mode)
        self.assertEqual(launcher_mode, 0o755)
        runtime_root_mode = stat.S_IMODE(layout.runtime_root.stat().st_mode)
        self.assertEqual(runtime_root_mode, 0o755)
        self.assertEqual(layout.runtime_root.stat().st_uid, os.geteuid())


class ValidationStateTests(SurfaceFixture):
    def test_wrong_type_where_directory_expected(self):
        layout = self.paths()
        layout.runtime_root.parent.mkdir(parents=True, exist_ok=True)
        layout.runtime_root.write_text("not a directory", encoding="utf-8")
        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["runtime_root"].state, STATE_WRONG_TYPE)

    def test_symlinked_directory_is_flagged(self):
        layout = self.paths()
        layout.runtime_root.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside-dir"
        outside.mkdir()
        os.symlink(outside, layout.runtime_root)
        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["runtime_root"].state, STATE_SYMLINK)

    def test_symlinked_launcher_is_flagged(self):
        layout = self.paths()
        layout.tts_cli.parent.mkdir(parents=True, exist_ok=True)
        outside = self.root / "outside-file"
        outside.write_bytes(b"anything")
        os.symlink(outside, layout.tts_cli)
        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["launcher"].state, STATE_SYMLINK)

    def test_group_or_world_writable_launcher_is_unsafe(self):
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        layout.tts_cli.chmod(0o755 | 0o022)
        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["launcher"].state, STATE_UNSAFE_PERMISSIONS)

    def test_wrong_owner_launcher_is_flagged(self):
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        with patch("isadoraair.runtime_surfaces.os.geteuid", return_value=os.geteuid() + 1):
            evidence = validate_system_surfaces(
                target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
            )
        self.assertEqual(evidence.surfaces["launcher"].state, STATE_WRONG_OWNER)

    def test_stale_launcher_content_is_wrong_content(self):
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        layout.tts_cli.write_text("#!/bin/sh\necho stale\n", encoding="utf-8")
        layout.tts_cli.chmod(0o755)
        evidence = validate_system_surfaces(
            target_root=self.target, product_manifest=self.product, project_root=PROJECT_ROOT
        )
        self.assertEqual(evidence.surfaces["launcher"].state, STATE_WRONG_CONTENT)

    def test_repair_after_tamper_restores_healthy_state(self):
        manager = self.manager()
        manager.apply()
        layout = self.paths()
        layout.tts_cli.write_text("#!/bin/sh\necho tampered\n", encoding="utf-8")
        layout.tts_cli.chmod(0o755)
        layout.runtime_root.chmod(0o777)

        result = self.manager().apply()
        self.assertFalse(result.no_op)
        self.assertIn("launcher", result.changed_surfaces)
        self.assertIn("runtime_root", result.changed_surfaces)
        self.assertTrue(result.evidence.healthy)


class SecurityConfinementTests(SurfaceFixture):
    def test_target_root_symlinked_parent_cannot_escape(self):
        outside = self.root / "outside"
        outside.mkdir()
        layout = self.paths()
        layout.runtime_root.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, layout.runtime_root.parent / "escape-marker")
        os.rename(layout.runtime_root.parent, self.root / "renamed-opt")
        os.symlink(self.root / "renamed-opt", layout.runtime_root.parent)
        with self.assertRaisesRegex(RuntimeProvisioningError, "symlink"):
            self.manager().apply()

    def test_canonical_apply_requires_root_privilege(self):
        manager = RuntimeSystemSurfaceManager(
            target_root="/", product_manifest=self.product, project_root=PROJECT_ROOT
        )
        with patch("isadoraair.runtime_surfaces.os.geteuid", return_value=12345):
            with self.assertRaisesRegex(RuntimeProvisioningError, "root privileges"):
                manager.apply()

    def test_noncanonical_target_root_must_be_caller_owned(self):
        manager = self.manager()
        with patch("isadoraair.runtime_surfaces.os.geteuid", return_value=os.geteuid() + 1):
            with self.assertRaisesRegex(RuntimeProvisioningError, "owned by the caller"):
                manager.apply()

    def test_apply_never_touches_real_host_canonical_paths(self):
        real_launcher = Path("/usr/local/bin/isadoraair-tts")
        real_runtime_root = Path("/opt/isadoraair-runtime")
        self.assertFalse(real_launcher.exists())
        self.assertFalse(real_runtime_root.exists())
        self.manager().apply()
        self.assertFalse(real_launcher.exists())
        self.assertFalse(real_runtime_root.exists())


class TmpfilesExecutionTests(SurfaceFixture):
    def test_tmpfiles_invocation_is_argv_list_absolute_and_isolated(self):
        observed = {}

        def capture(command, *, cwd, timeout, label):
            observed.update(command=command, cwd=cwd, timeout=timeout, label=label)

        config_file = self.target / "etc" / "tmpfiles.d" / "isadoraair-runtime.conf"
        with patch("isadoraair.runtime_surfaces._run_bounded", side_effect=capture):
            _run_tmpfiles(config_file, target_root=self.target)
        self.assertIsInstance(observed["command"], list)
        self.assertTrue(observed["command"][0].startswith("/"))
        self.assertIn("--create", observed["command"])
        self.assertIn(f"--root={self.target}", observed["command"])
        self.assertEqual(observed["label"], "systemd-tmpfiles")

    def test_real_tmpfiles_creates_mapped_directories_only(self):
        manager = self.manager()
        result = manager.apply()
        self.assertTrue(result.evidence.healthy)
        layout = self.paths()
        self.assertTrue(layout.runtime_root.is_dir())
        self.assertTrue(layout.tts_root.is_dir())
        self.assertFalse(Path("/opt/isadoraair-runtime").exists())
        self.assertFalse(Path("/var/lib/isadoraair/tts").exists())

    def test_tmpfiles_config_deterministic_content_for_canonical_and_mapped(self):
        manager_mapped = self.manager()
        mapped_content = manager_mapped._rendered_tmpfiles_config()
        self.assertIn(f" {os.geteuid()} ".encode(), mapped_content)
        self.assertNotIn(b"@@ISADORAAIR_SURFACE_UID@@", mapped_content)

        canonical = RuntimeSystemSurfaceManager(
            target_root="/", product_manifest=self.product, project_root=PROJECT_ROOT
        )
        canonical_content = canonical._rendered_tmpfiles_config()
        self.assertIn(b" 0 0 ", canonical_content)


class RollbackTests(SurfaceFixture):
    def setUp(self):
        super().setUp()
        self.manager().apply()
        self.layout = self.paths()
        self.original_launcher = self.layout.tts_cli.read_bytes()

    def _tamper_launcher_and_config_to_need_repair(self):
        self.layout.tts_cli.write_bytes(b"#!/bin/sh\necho old\n")
        self.layout.tts_cli.chmod(0o755)

    def test_launcher_publish_failure_preserves_prior_launcher(self):
        self._tamper_launcher_and_config_to_need_repair()

        def checkpoint(name):
            if name == "after_launcher_publish":
                raise RuntimeProvisioningError("injected failure")

        seams = SystemSurfaceSeams(checkpoint=checkpoint)
        with self.assertRaisesRegex(RuntimeProvisioningError, "injected failure"):
            self.manager(seams=seams).apply()
        self.assertEqual(self.layout.tts_cli.read_bytes(), b"#!/bin/sh\necho old\n")

    def test_tmpfiles_execution_failure_preserves_prior_state(self):
        self.layout.runtime_root.chmod(0o777)

        def failing_run_tmpfiles(config_file, target_root):
            raise RuntimeProvisioningError("tmpfiles execution failed")

        seams = SystemSurfaceSeams(run_tmpfiles=failing_run_tmpfiles)
        with self.assertRaisesRegex(RuntimeProvisioningError, "tmpfiles execution failed"):
            self.manager(seams=seams).apply()
        # Directory establishment is intentionally non-destructive/non-
        # transactional: the previously-repaired file surfaces were
        # already healthy so nothing needed publishing before the
        # injected tmpfiles failure, and the still-unhealthy directory
        # is left as-is rather than force-corrected outside the
        # authoritative tmpfiles mechanism.
        self.assertEqual(stat.S_IMODE(self.layout.runtime_root.stat().st_mode), 0o777)

    def test_final_validation_failure_rolls_back_launcher(self):
        """Corrupt the launcher AFTER tmpfiles execution but before the
        real final-validation check runs, proving that a genuine final-
        validation failure (not just an injected exception) triggers
        the same rollback as any other boundary."""
        self._tamper_launcher_and_config_to_need_repair()
        # Also put the directory surface in need of repair so the
        # tmpfiles-execution step (and its checkpoint) actually runs.
        self.layout.runtime_root.chmod(0o777)

        def checkpoint(name):
            if name == "after_tmpfiles_execution":
                self.layout.tts_cli.write_bytes(b"#!/bin/sh\necho corrupted-late\n")
                self.layout.tts_cli.chmod(0o755)

        seams = SystemSurfaceSeams(checkpoint=checkpoint)
        with self.assertRaisesRegex(RuntimeProvisioningError, "failed final validation"):
            self.manager(seams=seams).apply()
        self.assertEqual(self.layout.tts_cli.read_bytes(), b"#!/bin/sh\necho old\n")

    def test_rollback_failure_preserves_original_as_cause(self):
        self._tamper_launcher_and_config_to_need_repair()

        def checkpoint(name):
            if name == "after_launcher_publish":
                raise RuntimeProvisioningError("original failure")

        seams = SystemSurfaceSeams(checkpoint=checkpoint)
        manager = self.manager(seams=seams)
        with patch.object(
            RuntimeSystemSurfaceManager, "_restore_file", side_effect=RuntimeProvisioningError("restore failed")
        ):
            with self.assertRaisesRegex(RuntimeProvisioningError, "rollback failed") as caught:
                manager.apply()
        self.assertIn("original failure", str(caught.exception.__cause__))

    def test_persistent_directories_are_never_deleted_on_failure(self):
        marker = self.layout.runtime_root / "kokoro" / "generations" / "e3-existing"
        marker.mkdir(parents=True)
        (marker / "keep.txt").write_text("keep", encoding="utf-8")
        self._tamper_launcher_and_config_to_need_repair()

        def checkpoint(name):
            if name == "after_launcher_publish":
                raise RuntimeProvisioningError("injected failure")

        seams = SystemSurfaceSeams(checkpoint=checkpoint)
        with self.assertRaises(RuntimeProvisioningError):
            self.manager(seams=seams).apply()
        self.assertTrue((marker / "keep.txt").exists())
        self.assertEqual((marker / "keep.txt").read_text(encoding="utf-8"), "keep")


class LockingTests(SurfaceFixture):
    def test_surfaces_use_real_common_cross_process_provision_lock(self):
        context = multiprocessing.get_context("fork")
        first_entered = context.Event()
        second_entered = context.Event()
        release = context.Event()
        first = context.Process(
            target=_lock_worker, args=(self.target, self.product, first_entered, release)
        )
        second = context.Process(
            target=_lock_worker, args=(self.target, self.product, second_entered)
        )
        first.start()
        self.addCleanup(lambda: first.is_alive() and first.terminate())
        self.assertTrue(first_entered.wait(5))
        second.start()
        self.addCleanup(lambda: second.is_alive() and second.terminate())
        self.assertFalse(second_entered.wait(0.2))
        release.set()
        self.assertTrue(second_entered.wait(5))
        first.join(5)
        second.join(5)
        self.assertEqual((first.exitcode, second.exitcode), (0, 0))

    def test_lock_path_matches_e3_e4_shared_lock(self):
        layout = self.paths()
        expected = layout.runtime_root / ".provision.lock"
        with runtime_provision_lock(layout):
            self.assertTrue(expected.exists())


class ManagementCommandTests(TestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="isadoraair-e5-cmd-")
        self.addCleanup(temporary.cleanup)
        self.target = Path(temporary.name)

    def test_plan_is_explicit_and_json_safe(self):
        import io
        import json as json_module

        stdout = io.StringIO()
        call_command(
            "provision_runtime_components",
            "--surfaces",
            "--plan",
            "--target-root",
            str(self.target),
            "--json",
            stdout=stdout,
        )
        payload = json_module.loads(stdout.getvalue())
        self.assertEqual(payload["action"], "install")
        self.assertFalse(list(self.target.rglob("*")))

    def test_mode_requires_plan_or_apply(self):
        with self.assertRaises(CommandError):
            call_command(
                "provision_runtime_components", "--surfaces", "--target-root", str(self.target)
            )

    def test_surfaces_cannot_combine_with_native_or_bundle_options(self):
        with self.assertRaises(CommandError):
            call_command(
                "provision_runtime_components",
                "--surfaces",
                "--fdkaac",
                "--plan",
                "--target-root",
                str(self.target),
            )
        with self.assertRaises(CommandError):
            call_command(
                "provision_runtime_components",
                "--surfaces",
                "--plan",
                "--bundle",
                str(self.target),
            )

    def test_apply_installs_and_is_idempotent(self):
        call_command(
            "provision_runtime_components",
            "--surfaces",
            "--apply",
            "--target-root",
            str(self.target),
        )
        product = load_runtime_components()
        layout = ProvisioningLayout.from_manifest(product, target_root=self.target)
        self.assertTrue(layout.tts_cli.is_file())
        self.assertTrue(layout.runtime_root.is_dir())
        self.assertTrue(layout.tts_root.is_dir())

        import io

        stdout = io.StringIO()
        call_command(
            "provision_runtime_components",
            "--surfaces",
            "--plan",
            "--target-root",
            str(self.target),
            "--json",
            stdout=stdout,
        )
        import json as json_module

        payload = json_module.loads(stdout.getvalue())
        self.assertEqual(payload["action"], "no_op")
