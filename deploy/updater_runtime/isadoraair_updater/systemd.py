"""Closed-allowlist systemd file reconciliation and service health checks."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

from .config import StationConfig
from .process import CommandRunner
from .release import KNOWN_MANAGED_UNITS, RESTART_ORDER, TrustedPlan, UnitActivationPolicy, resolve_unit_policy
from .security import assert_root_protected, assert_root_protected_parents


SYSTEMCTL = "/usr/bin/systemctl"
ALSACTL = "/usr/sbin/alsactl"
TOKEN_RE = re.compile(r"@@[A-Z_]+@@")
TOKENS = {
    "@@ISA_USER@@": "isa_user",
    "@@ISA_ROOT@@": "isa_root",
    "@@ISA_HOME@@": "isa_home",
    "@@SYNDICATED_ROOT@@": "syndicated_root",
    "@@WEATHER_ROOT@@": "weather_root",
    "@@OGREMOTE_ROOT@@": "ogremote_root",
}


class SystemdError(RuntimeError):
    pass


class SystemdManager:
    def __init__(self, config: StationConfig, runner: CommandRunner, *, enforce_root_ownership: bool = True,
                 signed_policy=None):
        self.config = config
        self.runner = runner
        self.enforce_root_ownership = enforce_root_ownership
        # D3-C: None (every existing caller, unchanged) means
        # resolve_unit_policy() falls straight through to
        # MANAGED_UNIT_POLICIES alone -- exactly today's behavior,
        # byte for byte (see test_phase_d3_signed_policy.py's own
        # parity test). Only a caller that has actually loaded and
        # independently verified a signed protected-policy document
        # for the CURRENTLY active generation (D4's own future job --
        # see this module's own D3-C scope note in docs/
        # UPDATE_CENTER_PHASE_D.md) would ever pass something else.
        self.signed_policy = signed_policy

    def _systemctl(self, args: list[str], timeout: float = 60):
        result = self.runner.run([SYSTEMCTL, *args], timeout=timeout)
        if not result.ok:
            raise SystemdError(f"systemctl {args[0]} failed")
        return result

    def _render(self, content: bytes) -> bytes:
        if len(content) > 1024 * 1024:
            raise SystemdError("unit template exceeds 1 MiB")
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SystemdError("unit template is not UTF-8") from exc
        if "\x00" in text:
            raise SystemdError("unit template contains NUL")
        for token, key in TOKENS.items():
            text = text.replace(token, self.config.render_values[key])
        unknown = TOKEN_RE.findall(text)
        if unknown:
            raise SystemdError(f"unit template contains unknown render token(s): {sorted(set(unknown))!r}")
        return text.encode("utf-8")

    def _install_one(self, source_root: Path, unit: str) -> bool:
        if unit not in KNOWN_MANAGED_UNITS:
            raise SystemdError(f"unit {unit!r} is outside the installed updater allowlist")
        source = source_root / "deploy" / unit
        if source.parent != source_root / "deploy" or not source.is_file() or source.is_symlink():
            raise SystemdError(f"trusted staged unit source is invalid: {unit}")
        rendered = self._render(source.read_bytes())
        root = self.config.systemd_unit_root
        assert_root_protected_parents(root)
        root.mkdir(parents=True, exist_ok=True, mode=0o755)
        assert_root_protected(root)
        destination = root / unit
        if destination.parent != root:
            raise SystemdError("unit destination escaped configured systemd root")
        if destination.exists() or destination.is_symlink():
            info = destination.lstat()
            if not stat.S_ISREG(info.st_mode) or destination.is_symlink():
                raise SystemdError("existing unit destination is not a regular file")
            if self.enforce_root_ownership and (info.st_uid != 0 or info.st_mode & 0o022):
                raise SystemdError("existing unit destination is not root-owned/non-writable")
            if destination.read_bytes() == rendered:
                return False
        temporary = root / f".{unit}.isadoraair-updater.{os.getpid()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            os.write(fd, rendered)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        return True

    def reconcile(self, source_root: Path, plan: TrustedPlan) -> dict:
        """Install every changed/newly-required unit from the trusted
        staged target, daemon-reload at most once, then activate each
        newly-required unit exactly per resolve_unit_policy()'s own
        closed answer (D3-C: a signed protected-policy document when
        self.signed_policy is set, MANAGED_UNIT_POLICIES otherwise --
        never per manifest text, never inferred from the unit's own
        .service/.timer suffix). ENABLE_NOW units (the five core
        services, and each companion .timer) are `enable --now`d and verified
        active/healthy, exactly the only behavior a required unit had
        before this policy existed. INSTALL_ONLY units (a companion
        oneshot .service meant only to be triggered by its own paired
        ENABLE_NOW .timer) are installed and daemon-reloaded like any
        other required unit, but this method never enables or starts
        them -- only confirms systemd successfully parsed the just-
        installed unit file (see verify_unit_loaded()).

        Both units of a pair are always installed in the SAME first
        pass, before any activation happens at all -- so by the time
        an ENABLE_NOW .timer is actually enabled, its paired
        INSTALL_ONLY .service is already on disk and already covered
        by the one daemon-reload above, regardless of which name
        happens to come first in plan.systemd_units_new_required."""
        changed = []
        for unit in (*plan.systemd_units_changed, *plan.systemd_units_new_required):
            if self._install_one(source_root, unit):
                changed.append(unit)
        if changed:
            self._systemctl(["daemon-reload"])
        enabled = []
        installed_only = []
        for unit in plan.systemd_units_new_required:
            policy = resolve_unit_policy(unit, signed_policy=self.signed_policy)
            if policy is None:
                # _install_one() above already refused any unit outside
                # KNOWN_MANAGED_UNITS (== MANAGED_UNIT_POLICIES.keys()) --
                # unreachable in practice. Kept as a hard fail-closed
                # guard against a future refactor silently decoupling
                # the install allowlist from the activation policy map.
                raise SystemdError(f"unit {unit!r} has no managed-unit activation policy")
            if policy is UnitActivationPolicy.ENABLE_NOW:
                self._systemctl(["enable", "--now", unit], timeout=120)
                self.verify_unit(unit)
                enabled.append(unit)
            else:
                self.verify_unit_loaded(unit)
                installed_only.append(unit)
        return {
            "changed": changed,
            "enabled": enabled,
            "installed_only": installed_only,
            "optional_report_only": list(plan.systemd_units_new_optional),
            "daemon_reload": bool(changed),
        }

    def restart_declared(self, services: tuple[str, ...]) -> list[str]:
        if tuple(name for name in RESTART_ORDER if name in set(services)) != tuple(services):
            raise SystemdError("restart list is not the closed deterministic manifest order")
        restarted = []
        for service in services:
            unit = f"{service}.service"
            if unit not in KNOWN_MANAGED_UNITS:
                raise SystemdError(f"service {service!r} is outside the restart allowlist")
            self._systemctl(["restart", unit], timeout=180)
            self.verify_unit(unit)
            restarted.append(service)
        return restarted

    def restart_operator_service(self, unit: str):
        """Restart one exact root-configured unit; DB/request text is never authority."""
        if unit not in self.config.operator_restart_units:
            raise SystemdError("service is outside the root-owned operator restart allowlist")
        self._systemctl(["restart", unit], timeout=180)
        self._verify_operator_unit(unit)

    def store_alsa_state(self):
        """Persist all live ALSA controls with a fixed executable and argv."""
        result = self.runner.run([ALSACTL, "store"], timeout=30)
        if not result.ok:
            raise SystemdError("alsactl store failed")

    def _verify_operator_unit(self, unit: str):
        if unit not in self.config.operator_restart_units:
            raise SystemdError("cannot verify a unit outside the operator allowlist")
        result = self._systemctl([
            "show", unit, "--property=Type", "--property=ActiveState",
            "--property=SubState", "--property=Result",
        ])
        self._validate_unit_status(unit, result)

    def verify_unit(self, unit: str):
        if unit not in KNOWN_MANAGED_UNITS:
            raise SystemdError("cannot verify an unknown unit")
        result = self._systemctl([
            "show", unit, "--property=Type", "--property=ActiveState",
            "--property=SubState", "--property=Result",
        ])
        self._validate_unit_status(unit, result)

    def verify_unit_loaded(self, unit: str):
        """The INSTALL_ONLY counterpart to verify_unit() above -- for a
        unit this updater deliberately never enables or starts (a
        oneshot .service meant only to be triggered by its own paired
        .timer), this is the narrowest available confirmation that the
        just-installed template is actually usable: systemd parsed it
        successfully after the daemon-reload already run by
        reconcile(). `systemctl show --property=LoadState` is a pure,
        fixed-argv, read-only introspection query -- the same class of
        command verify_unit() already uses -- and never starts, stops,
        or otherwise executes the unit."""
        if unit not in KNOWN_MANAGED_UNITS:
            raise SystemdError("cannot verify an unknown unit")
        result = self._systemctl(["show", unit, "--property=LoadState"])
        values = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        load_state = values.get("LoadState", "")
        if load_state != "loaded":
            raise SystemdError(f"unit {unit} did not load successfully: LoadState={load_state!r}")

    @staticmethod
    def _validate_unit_status(unit: str, result):
        values = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        unit_type = values.get("Type", "")
        active = values.get("ActiveState", "")
        outcome = values.get("Result", "")
        if unit_type == "oneshot":
            if not (outcome in {"", "success"} and active in {"active", "inactive"}):
                raise SystemdError(f"oneshot unit {unit} did not complete successfully")
        elif active != "active":
            raise SystemdError(f"unit {unit} is not active")
