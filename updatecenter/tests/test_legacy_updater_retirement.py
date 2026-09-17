"""Retirement of the obsolete pre-Phase-D isadoraair-updater.service unit.

Proves the canonical completed-Phase-D contract (supervisor enabled/
active, legacy MASKED/inactive -- not merely disabled) via the pure
decision logic in deploy/updater_bootstrap/tools/legacy_updater_
retirement.py, without touching any real system state. The privileged
mechanics (systemctl/file operations) are exercised with a mocked
subprocess boundary -- this suite never actually masks/unmasks a real
unit."""
from __future__ import annotations

import dataclasses
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from deploy.updater_bootstrap.tools import legacy_updater_retirement as retirement
from deploy.updater_bootstrap.tools.legacy_updater_retirement import (
    LegacyUnitActiveError,
    PingFacts,
    PreflightSnapshot,
    RetirementRefused,
    RuntimeStateFacts,
    SystemdUnitState,
    check_retirement_preflight,
    is_already_retired,
)


def _healthy_snapshot(**overrides) -> PreflightSnapshot:
    base = dict(
        supervisor=SystemdUnitState(load_state="loaded", unit_file_state="enabled", active_state="active"),
        legacy=SystemdUnitState(load_state="loaded", unit_file_state="disabled", active_state="inactive"),
        worker_socket_exists=True,
        ping=PingFacts(ok=True, protected_runtime_valid=True, update_execution_enabled=True, maintenance_busy=False),
        runtime_state=RuntimeStateFacts(active_generation=5, active_descriptor_sha256="b" * 64, active_slot="A", activation=None),
    )
    base.update(overrides)
    return PreflightSnapshot(**base)


class CanonicalContractTests(SimpleTestCase):
    """Completed Phase-D authority expects legacy `masked`, not merely
    `disabled` -- the core contract change this task makes."""

    def test_disabled_inactive_is_not_yet_retired(self):
        snapshot = _healthy_snapshot()
        self.assertFalse(is_already_retired(snapshot))

    def test_masked_inactive_is_the_retired_state(self):
        snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="masked", active_state="inactive"),
        )
        self.assertTrue(is_already_retired(snapshot))

    def test_masked_but_somehow_active_is_not_considered_retired(self):
        # Should be structurally impossible (a masked unit refuses to start),
        # but the predicate must not claim victory on active_state alone.
        snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="masked", active_state="active"),
        )
        self.assertFalse(is_already_retired(snapshot))


class PreflightRefusalTests(SimpleTestCase):
    """Every one of the strict preflight conditions must independently
    block retirement -- a healthy snapshot with exactly one condition
    flipped must refuse."""

    def test_healthy_snapshot_passes(self):
        check_retirement_preflight(_healthy_snapshot())  # must not raise

    def test_active_legacy_unit_is_a_distinct_stop_condition(self):
        snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="enabled", active_state="active"),
        )
        with self.assertRaises(LegacyUnitActiveError):
            check_retirement_preflight(snapshot)

    def test_failed_legacy_unit_is_treated_as_inactive_for_preflight(self):
        # A crash-looped-then-given-up unit reports 'failed', not 'inactive' --
        # still safe to retire (never running), must not be conflated with 'active'.
        snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="disabled", active_state="failed"),
        )
        check_retirement_preflight(snapshot)  # must not raise

    def test_supervisor_not_loaded_refused(self):
        snapshot = _healthy_snapshot(
            supervisor=SystemdUnitState(load_state="not-found", unit_file_state="disabled", active_state="inactive"),
        )
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_supervisor_not_enabled_refused(self):
        snapshot = _healthy_snapshot(
            supervisor=SystemdUnitState(load_state="loaded", unit_file_state="disabled", active_state="active"),
        )
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_supervisor_not_active_refused(self):
        snapshot = _healthy_snapshot(
            supervisor=SystemdUnitState(load_state="loaded", unit_file_state="enabled", active_state="inactive"),
        )
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_missing_worker_socket_refused(self):
        snapshot = _healthy_snapshot(worker_socket_exists=False)
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_ping_not_ok_refused(self):
        snapshot = _healthy_snapshot(ping=PingFacts(ok=False, protected_runtime_valid=False, update_execution_enabled=False, maintenance_busy=True))
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_ping_none_refused(self):
        snapshot = _healthy_snapshot(ping=None)
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_protected_runtime_invalid_refused(self):
        snapshot = _healthy_snapshot(ping=PingFacts(ok=True, protected_runtime_valid=False, update_execution_enabled=True, maintenance_busy=False))
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_update_execution_disabled_refused(self):
        snapshot = _healthy_snapshot(ping=PingFacts(ok=True, protected_runtime_valid=True, update_execution_enabled=False, maintenance_busy=False))
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_maintenance_busy_refused(self):
        """maintenance_busy is this tool's own root-side proxy for 'no
        active/locked UpdateJob', checked without any Django dependency."""
        snapshot = _healthy_snapshot(ping=PingFacts(ok=True, protected_runtime_valid=True, update_execution_enabled=True, maintenance_busy=True))
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_unreadable_runtime_state_refused(self):
        snapshot = _healthy_snapshot(runtime_state=None)
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)

    def test_activation_in_flight_refused(self):
        snapshot = _healthy_snapshot(
            runtime_state=RuntimeStateFacts(active_generation=5, active_descriptor_sha256="b" * 64, active_slot="A", activation={"job_id": "x"}),
        )
        with self.assertRaises(RetirementRefused):
            check_retirement_preflight(snapshot)


class RetireOrchestrationTests(SimpleTestCase):
    """The end-to-end retire() sequence, with the systemctl/filesystem
    boundary mocked -- proves ordering and idempotence without ever
    touching a real unit."""

    def _patch_gather(self, snapshot):
        return patch.object(retirement, "gather_snapshot", return_value=snapshot)

    def test_dry_run_never_mutates(self):
        with self._patch_gather(_healthy_snapshot()):
            with patch.object(retirement, "_systemctl") as mock_systemctl, \
                 patch.object(retirement, "create_rollback_directory") as mock_rollback:
                result = retirement.retire(apply=False)
        mock_systemctl.assert_not_called()
        mock_rollback.assert_not_called()
        self.assertFalse(result.already_retired)
        self.assertIsNone(result.rollback_dir)

    def test_already_retired_is_a_clean_noop(self):
        masked_snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="masked", active_state="inactive"),
        )
        with self._patch_gather(masked_snapshot):
            with patch.object(retirement, "_systemctl") as mock_systemctl:
                result = retirement.retire(apply=True)
        mock_systemctl.assert_not_called()
        self.assertTrue(result.already_retired)

    def test_active_legacy_refuses_before_any_mutation(self):
        active_snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="enabled", active_state="active"),
        )
        with self._patch_gather(active_snapshot):
            with patch.object(retirement, "_systemctl") as mock_systemctl:
                with self.assertRaises(LegacyUnitActiveError):
                    retirement.retire(apply=True)
        mock_systemctl.assert_not_called()

    def test_apply_performs_disable_mask_reload_in_order_and_verifies(self):
        ok = MagicMock(returncode=0, stdout="LoadState=loaded\nUnitFileState=masked\nActiveState=inactive\n", stderr="")
        enabled_snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="enabled", active_state="inactive"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            rollback_dir = Path(tmp) / "rollback"
            rollback_dir.mkdir()
            with self._patch_gather(enabled_snapshot):
                with patch.object(retirement, "_systemctl", return_value=ok) as mock_systemctl, \
                     patch.object(retirement, "create_rollback_directory", return_value=rollback_dir), \
                     patch.object(retirement, "capture_rollback_evidence"), \
                     patch.object(retirement, "LEGACY_UNIT_PATH", MagicMock(exists=MagicMock(return_value=False))):
                    result = retirement.retire(apply=True)
            calls = [call.args for call in mock_systemctl.call_args_list]
            self.assertIn(("disable", retirement.LEGACY_UNIT), calls)
            self.assertIn(("mask", retirement.LEGACY_UNIT), calls)
            self.assertIn(("daemon-reload",), calls)
            # disable must precede mask must precede daemon-reload
            self.assertLess(calls.index(("disable", retirement.LEGACY_UNIT)), calls.index(("mask", retirement.LEGACY_UNIT)))
            self.assertLess(calls.index(("mask", retirement.LEGACY_UNIT)), calls.index(("daemon-reload",)))
            self.assertEqual(result.is_enabled_after, "masked")
            self.assertEqual(result.is_active_after, "inactive")

    def test_apply_skips_disable_when_already_disabled(self):
        ok = MagicMock(returncode=0, stdout="LoadState=loaded\nUnitFileState=masked\nActiveState=inactive\n", stderr="")
        disabled_snapshot = _healthy_snapshot(
            legacy=SystemdUnitState(load_state="loaded", unit_file_state="disabled", active_state="inactive"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            rollback_dir = Path(tmp) / "rollback"
            rollback_dir.mkdir()
            with self._patch_gather(disabled_snapshot):
                with patch.object(retirement, "_systemctl", return_value=ok) as mock_systemctl, \
                     patch.object(retirement, "create_rollback_directory", return_value=rollback_dir), \
                     patch.object(retirement, "capture_rollback_evidence"), \
                     patch.object(retirement, "LEGACY_UNIT_PATH", MagicMock(exists=MagicMock(return_value=False))):
                    retirement.retire(apply=True)
            calls = [call.args for call in mock_systemctl.call_args_list]
            self.assertNotIn(("disable", retirement.LEGACY_UNIT), calls)
            self.assertIn(("mask", retirement.LEGACY_UNIT), calls)

    def test_mask_failure_raises_and_does_not_silently_succeed(self):
        fail = MagicMock(returncode=1, stdout="", stderr="mask refused")
        with tempfile.TemporaryDirectory() as tmp:
            rollback_dir = Path(tmp) / "rollback"
            rollback_dir.mkdir()
            with self._patch_gather(_healthy_snapshot()):
                with patch.object(retirement, "_systemctl", return_value=fail), \
                     patch.object(retirement, "create_rollback_directory", return_value=rollback_dir), \
                     patch.object(retirement, "capture_rollback_evidence"), \
                     patch.object(retirement, "LEGACY_UNIT_PATH", MagicMock(exists=MagicMock(return_value=False))):
                    with self.assertRaises(RetirementRefused):
                        retirement.retire(apply=True)

    def test_post_mask_verification_mismatch_raises(self):
        # disable/mask/reload all report success, but the final re-query
        # somehow still shows the old state -- must not report success.
        def fake_systemctl(*args):
            if args and args[0] == "show":
                return MagicMock(returncode=0, stdout="LoadState=loaded\nUnitFileState=enabled\nActiveState=inactive\n", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            rollback_dir = Path(tmp) / "rollback"
            rollback_dir.mkdir()
            with self._patch_gather(_healthy_snapshot()):
                with patch.object(retirement, "_systemctl", side_effect=fake_systemctl), \
                     patch.object(retirement, "create_rollback_directory", return_value=rollback_dir), \
                     patch.object(retirement, "capture_rollback_evidence"), \
                     patch.object(retirement, "LEGACY_UNIT_PATH", MagicMock(exists=MagicMock(return_value=False))):
                    with self.assertRaises(RetirementRefused):
                        retirement.retire(apply=True)


class NeverTouchesRuntimeDirectoryTests(SimpleTestCase):
    """Structural proof: nothing in this module's mechanics references
    /run/isadoraair-updater as a mutation target -- only as a read-only
    existence/ping check."""

    def test_module_source_never_writes_to_run_isadoraair_updater(self):
        import inspect
        source = inspect.getsource(retirement)
        # The socket path constant itself is read-only-referenced (exists()/
        # connect()); prove no rm/unlink/rmtree/mkdir call anywhere mentions it.
        for dangerous in ("unlink", "rmtree", "rmdir", "mkdir"):
            for line in source.splitlines():
                if dangerous in line and "WORKER_SOCKET_PATH" in line:
                    self.fail(f"module source appears to mutate the worker runtime directory: {line!r}")

    def test_retire_never_restarts_supervisor(self):
        """Only a read-only `systemctl show` may ever mention the supervisor
        unit (for before/after evidence) -- no mutating verb (restart/stop/
        start/enable/disable/mask) may ever target it."""
        ok = MagicMock(returncode=0, stdout="LoadState=loaded\nUnitFileState=masked\nActiveState=inactive\n", stderr="")
        with tempfile.TemporaryDirectory() as tmp:
            rollback_dir = Path(tmp) / "rollback"
            rollback_dir.mkdir()
            with patch.object(retirement, "gather_snapshot", return_value=_healthy_snapshot()):
                with patch.object(retirement, "_systemctl", return_value=ok) as mock_systemctl, \
                     patch.object(retirement, "create_rollback_directory", return_value=rollback_dir), \
                     patch.object(retirement, "capture_rollback_evidence"), \
                     patch.object(retirement, "LEGACY_UNIT_PATH", MagicMock(exists=MagicMock(return_value=False))):
                    retirement.retire(apply=True)
            calls = [call.args for call in mock_systemctl.call_args_list]
            mutating_verbs = {"restart", "stop", "start", "enable", "disable", "mask", "unmask"}
            for call_args in calls:
                if retirement.SUPERVISOR_UNIT in call_args:
                    self.assertNotIn(
                        call_args[0], mutating_verbs,
                        f"unexpected mutating call against the supervisor: {call_args!r}",
                    )


class RecoveryConvergenceTests(SimpleTestCase):
    """A restored Phase-D system must converge to masked-legacy-unit
    state -- proven structurally: isadoraair/phase_d_recovery.py's
    capture/validate/restore/publish functions must never reference
    the legacy unit name/path at all, so a restore can neither reinstall
    it as startable nor disturb whatever retirement state a station was
    already in."""

    def test_phase_d_recovery_module_never_references_the_legacy_unit(self):
        import isadoraair.phase_d_recovery as phase_d_recovery

        source = Path(phase_d_recovery.__file__).read_text(encoding="utf-8")
        self.assertNotIn("isadoraair-updater.service", source)
        self.assertNotIn(retirement.LEGACY_UNIT, source)

    def test_phase_d_recovery_never_imports_the_retirement_tool(self):
        """Recovery/restore and legacy-unit retirement are deliberately
        two separate concerns (Phase-D data restore vs. host/operator
        systemd maintenance) -- restore must not orchestrate masking as
        a side effect, successful or failed."""
        import isadoraair.phase_d_recovery as phase_d_recovery

        source = Path(phase_d_recovery.__file__).read_text(encoding="utf-8")
        self.assertNotIn("legacy_updater_retirement", source)


class SignedPolicyExclusionTests(SimpleTestCase):
    """Legacy authority retirement is root/operator/bootstrap maintenance,
    never a signed managed-unit-policy concern -- the generation-5 policy
    must never grow to include isadoraair-updater.service merely to let
    Update Center manipulate it (Phase-D's protected worker managed-unit
    authority is a completely separate concept from host-level updater-
    authority retirement)."""

    def test_generation_five_policy_never_names_the_legacy_unit(self):
        import json
        policy_path = Path("deploy/updater_runtime/protected-policy.json")
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        unit_names = {entry["unit"] for entry in policy["managed_units"]}
        self.assertNotIn(retirement.LEGACY_UNIT, unit_names)
        self.assertNotIn(retirement.SUPERVISOR_UNIT, unit_names)
