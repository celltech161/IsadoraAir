"""Station-aware operator adapter for Runtime Foundation E provisioning."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_bundle import RuntimeBundleError
from isadoraair.runtime_components import RuntimeComponentContractError, load_runtime_components
from isadoraair.runtime_native import NativeRuntimeProvisioner
from isadoraair.runtime_provisioning import RuntimeProvisioner, RuntimeProvisioningError
from isadoraair.runtime_recovery import (
    RuntimeRecoveryError,
    load_recovery_payload,
    piper_selection_digest,
)
from isadoraair.runtime_requirements import (
    ComponentRequirement,
    RuntimeRequirements,
    resolve_current_runtime_requirements,
)
from isadoraair.runtime_surfaces import RuntimeSystemSurfaceManager

# Runtime Foundation E7B (2026-08-29): a fixed, non-station-specific
# reason string recorded on every ComponentRequirement synthesized from
# a recovery payload below -- deliberately distinct from any reason
# resolve_current_runtime_requirements() would ever produce (those are
# always live-station-configuration-derived), so a plan/apply's own
# printed `required by:` line makes the origin unambiguous.
RECOVERY_PAYLOAD_REASON = "Runtime Foundation E7 recovery payload"


def _requirements_for_recovery_native() -> RuntimeRequirements:
    """fdkaac's requiredness for a --recovery-payload native provisioning
    call is never re-derived from live station configuration (Runtime
    Foundation E1) -- the payload's own native_fdkaac component already
    passed E7's fail-closed load, which is itself only possible because
    an operator's recovery-component policy justified including it (see
    docs/RUNTIME_BACKUP_PAYLOAD.md). Re-querying E1 here would silently
    reintroduce exactly the dormant-Kokoro-style blind spot this
    integration exists to avoid."""
    return RuntimeRequirements(
        components={
            "fdkaac": ComponentRequirement(name="fdkaac", required=True, reasons=(RECOVERY_PAYLOAD_REASON,)),
            "kokoro": ComponentRequirement(name="kokoro"),
            "piper": ComponentRequirement(name="piper"),
        }
    )


def _requirements_for_recovery_tts(
    tts_bundle, station_requirements: RuntimeRequirements | None = None
) -> RuntimeRequirements:
    """kokoro/piper requiredness for a --recovery-payload TTS
    provisioning call comes from what the embedded, already-validated
    E3 bundle actually contains -- never from resolve_current_runtime_requirements()
    (Runtime Foundation E1), which is exactly the signal that misses a
    station's dormant-but-still-live Kokoro usage (webrequests/road_conditions'
    hardcoded KOKORO_BINARY callers bypass StationTTSVoice entirely --
    see runtime_recovery.py's module docstring). Piper's model list is
    remains the exception: its model/config identities are owned by the
    restored station database.  The caller must supply E1-resolved
    station requirements when Piper is present; bundle/payload/station
    digests have already been proven equal before this helper runs."""
    components: dict[str, ComponentRequirement] = {
        "fdkaac": ComponentRequirement(name="fdkaac"),
    }
    for name in ("kokoro", "piper"):
        present = tts_bundle.components.get(name)
        if present is None:
            components[name] = ComponentRequirement(name=name)
            continue
        piper_models: tuple[PiperModelRequirement, ...] = ()
        if name == "piper":
            if station_requirements is None:
                raise RuntimeRecoveryError(
                    "Piper recovery requires restored station configuration"
                )
            selected = station_requirements.components.get("piper")
            if selected is None or not selected.required:
                raise RuntimeRecoveryError(
                    "recovery payload contains Piper but the restored station selects no Piper models"
                )
            piper_models = selected.piper_models
        components[name] = ComponentRequirement(
            name=name, required=True, reasons=(RECOVERY_PAYLOAD_REASON,), piper_models=piper_models
        )
    return RuntimeRequirements(components=components)


def _trusted_uid(value: str) -> int:
    try:
        uid = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "trusted preparer UID must be a non-negative integer"
        ) from exc
    if uid < 0:
        raise argparse.ArgumentTypeError(
            "trusted preparer UID must be a non-negative integer"
        )
    return uid


class Command(BaseCommand):
    help = (
        "Plan/apply offline TTS, explicitly prepare/publish native fdkaac, "
        "or plan/apply the E5 system-surface contract (--surfaces)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--bundle",
            type=Path,
            help="Existing E3 directory containing runtime-bundle.json and offline TTS payloads.",
        )
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--plan", action="store_true", help="Inspect and report without publication.")
        mode.add_argument("--apply", action="store_true", help="Perform only the reported offline plan.")
        mode.add_argument(
            "--prepare-fdkaac",
            action="store_true",
            help="Verify local sources and build/validate an unprivileged prepared prefix.",
        )
        mode.add_argument(
            "--publish-fdkaac",
            action="store_true",
            help="Protected publication of an already prepared fdkaac prefix.",
        )
        parser.add_argument(
            "--fdkaac",
            action="store_true",
            help="Make --plan report the native fdkaac component rather than E3 TTS.",
        )
        parser.add_argument(
            "--surfaces",
            action="store_true",
            help=(
                "Make --plan/--apply act on the E5 system-surface contract (installed "
                "TTS launcher, canonical runtime/data directories, tmpfiles config) "
                "rather than E3 TTS or E4 native fdkaac."
            ),
        )
        parser.add_argument(
            "--native-source-dir",
            type=Path,
            help="Explicit local directory containing the two manifest-pinned source archives.",
        )
        parser.add_argument(
            "--prepared-native-root",
            type=Path,
            help="Caller-owned output/input directory for the explicit E4 handoff.",
        )
        parser.add_argument(
            "--trusted-preparer-uid",
            type=_trusted_uid,
            help=(
                "Explicit trusted administrative UID that owns prepared fdkaac "
                "material; required for canonical publication."
            ),
        )
        parser.add_argument(
            "--bootstrap-fdkaac",
            action="store_true",
            help="Explicitly select fdkaac even when current station features do not require it.",
        )
        parser.add_argument(
            "--target-root",
            type=Path,
            default=Path("/"),
            help="Map canonical absolute paths beneath a caller-owned staging root.",
        )
        parser.add_argument(
            "--recovery-payload",
            type=Path,
            help=(
                "Runtime Foundation E7B: an already-extracted, already-validated "
                "recovery payload root (runtime-recovery.json directly inside it). "
                "Supplies --bundle (TTS) or the native fdkaac source directory "
                "automatically -- do not also pass those -- and REPLACES live-"
                "station-derived requirements with exactly what this payload "
                "declares, never Runtime Foundation E1. For disaster-recovery "
                "restore only; not used for an ordinary connected install."
            ),
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable output to stdout.",
        )

    def _write_plan(self, plan, *, json_output):
        if json_output:
            self.stdout.write(plan.to_json())
            return
        self.stdout.write(f"Runtime provisioning plan: {'READY' if plan.ready else 'BLOCKED'}")
        self.stdout.write(f"  bundle: {plan.bundle_id}")
        self.stdout.write(f"  target root: {plan.target_root}")
        if not plan.components:
            self.stdout.write("  no canonical TTS component is selected")
        for component in plan.components:
            self.stdout.write(
                f"  {component.name}: {component.action} (current: {component.current_status})"
            )
            for reason in component.reasons:
                self.stdout.write(f"    required by: {reason}")
            for error in component.errors:
                self.stdout.write(f"    error: {error}")
        for error in plan.errors:
            self.stdout.write(f"  station configuration: {error}")

    def _write_surfaces_plan(self, plan, *, json_output):
        if json_output:
            self.stdout.write(plan.to_json())
            return
        self.stdout.write(f"System-surface plan: {'READY' if plan.ready else 'BLOCKED'}")
        self.stdout.write(f"  target root: {plan.target_root}")
        self.stdout.write(f"  action: {plan.action}")
        self.stdout.write(
            f"  protected publication: {'required' if plan.privilege_required else 'not required'}"
        )
        for name in sorted(plan.current_evidence.surfaces):
            item = plan.current_evidence.surfaces[name]
            self.stdout.write(f"  {name}: {item.state} ({item.path})")
        for error in plan.errors:
            self.stdout.write(f"  error: {error}")

    def handle(self, *args, **options):
        json_output = options["json_output"]
        try:
            manifest = load_runtime_components()
            if options["surfaces"]:
                if any(
                    options[flag]
                    for flag in (
                        "bundle",
                        "fdkaac",
                        "prepare_fdkaac",
                        "publish_fdkaac",
                        "native_source_dir",
                        "prepared_native_root",
                        "trusted_preparer_uid",
                        "bootstrap_fdkaac",
                        "recovery_payload",
                    )
                ):
                    raise CommandError(
                        "--surfaces cannot be combined with E3 TTS or E4 native options"
                    )
                if not (options["plan"] or options["apply"]):
                    raise CommandError("--surfaces requires --plan or --apply")
                surfaces = RuntimeSystemSurfaceManager(
                    product_manifest=manifest, target_root=options["target_root"]
                )
                if options["plan"]:
                    plan = surfaces.plan()
                    self._write_surfaces_plan(plan, json_output=json_output)
                    if not plan.ready:
                        raise CommandError("system-surface plan contains blocking errors")
                    return
                result = surfaces.apply()
                if json_output:
                    self.stdout.write(result.to_json())
                else:
                    outcome = "NO-OP" if result.no_op else "APPLIED"
                    self.stdout.write(f"System-surface provisioning: {outcome}")
                    for name in result.changed_surfaces:
                        self.stdout.write(f"  repaired: {name}")
                    self.stdout.write(
                        f"  all surfaces healthy: {result.evidence.healthy}"
                    )
                return
            native_mode = (
                options["fdkaac"]
                or options["prepare_fdkaac"]
                or options["publish_fdkaac"]
            )
            recovery_payload = None
            if options["recovery_payload"] is not None:
                try:
                    recovery_payload = load_recovery_payload(
                        options["recovery_payload"], product_manifest=manifest
                    )
                except (RuntimeRecoveryError, RuntimeBundleError, RuntimeProvisioningError) as exc:
                    safe = " ".join(str(exc).split())[:512] or "recovery payload failed to load"
                    raise CommandError(f"--recovery-payload: {safe}") from None
                if native_mode:
                    if options["native_source_dir"] is not None:
                        raise CommandError(
                            "--recovery-payload already supplies the native fdkaac source -- "
                            "do not also pass --native-source-dir"
                        )
                    if recovery_payload.native_source is None:
                        raise CommandError("recovery payload has no native_fdkaac component")
                    requirements = _requirements_for_recovery_native()
                else:
                    if options["bundle"] is not None:
                        raise CommandError(
                            "--recovery-payload already supplies the tts bundle -- "
                            "do not also pass --bundle"
                        )
                    if recovery_payload.tts_bundle is None:
                        raise CommandError("recovery payload has no tts component")
                    station_requirements = None
                    if "piper" in recovery_payload.tts_bundle.components:
                        try:
                            station_requirements = resolve_current_runtime_requirements(manifest)
                            current_piper_digest = piper_selection_digest(station_requirements)
                        except Exception as exc:
                            raise CommandError(
                                "recovery payload contains Piper but restored station configuration could not be inspected"
                            ) from exc
                        if recovery_payload.piper_selection_digest != current_piper_digest:
                            raise CommandError(
                                "recovery payload Piper model/config identity does not match the restored station selection"
                            )
                    requirements = _requirements_for_recovery_tts(
                        recovery_payload.tts_bundle, station_requirements
                    )
            else:
                try:
                    requirements = resolve_current_runtime_requirements(manifest)
                except Exception as exc:
                    raise RuntimeProvisioningError(
                        "station configuration could not be inspected"
                    ) from exc
            if native_mode:
                if options["bundle"] is not None:
                    raise CommandError("--bundle is only valid for E3 TTS provisioning")
                if options["apply"]:
                    raise CommandError("use explicit --prepare-fdkaac and --publish-fdkaac phases")
                native = NativeRuntimeProvisioner(
                    requirements=requirements,
                    product_manifest=manifest,
                    target_root=options["target_root"],
                    bootstrap=options["bootstrap_fdkaac"],
                )
                effective_source_dir = (
                    Path(recovery_payload.native_source.source_dir)
                    if recovery_payload is not None
                    else options["native_source_dir"]
                )
                if options["plan"]:
                    plan = native.plan(
                        source_dir=effective_source_dir,
                        prepared_root=options["prepared_native_root"],
                        expected_preparer_uid=options["trusted_preparer_uid"],
                    )
                    if json_output:
                        self.stdout.write(plan.to_json())
                    else:
                        self.stdout.write(
                            f"Native fdkaac plan: {'READY' if plan.ready else 'BLOCKED'}"
                        )
                        self.stdout.write(
                            f"  action: {plan.action} (current: {plan.current_status})"
                        )
                        self.stdout.write(
                            f"  protected publication: {'required' if plan.privilege_required else 'not required'}"
                        )
                        for error in plan.errors:
                            self.stdout.write(f"  error: {error}")
                    if not plan.ready:
                        raise CommandError("native provisioning plan contains blocking errors")
                    return
                if options["prepare_fdkaac"]:
                    if options["trusted_preparer_uid"] is not None:
                        raise CommandError(
                            "--trusted-preparer-uid is only valid for protected publication"
                        )
                    if effective_source_dir is None or options["prepared_native_root"] is None:
                        raise CommandError(
                            "--prepare-fdkaac requires --prepared-native-root, and either "
                            "--native-source-dir or --recovery-payload"
                        )
                    result = native.prepare(
                        source_dir=effective_source_dir,
                        prepared_root=options["prepared_native_root"],
                    )
                    if json_output:
                        self.stdout.write(result.to_json())
                    else:
                        self.stdout.write(
                            "Native fdkaac preparation: " + ("NO-OP" if result.no_op else "READY")
                        )
                        if result.prepared_root:
                            self.stdout.write(f"  prepared root: {result.prepared_root}")
                    return
                if options["native_source_dir"] is not None or options["prepared_native_root"] is None:
                    raise CommandError(
                        "--publish-fdkaac requires only --prepared-native-root"
                    )
                if (
                    options["target_root"].absolute() == Path("/")
                    and options["trusted_preparer_uid"] is None
                ):
                    raise CommandError(
                        "canonical native publication requires --trusted-preparer-uid"
                    )
                result = native.publish(
                    prepared_root=options["prepared_native_root"],
                    expected_preparer_uid=options["trusted_preparer_uid"],
                )
                if json_output:
                    self.stdout.write(result.to_json())
                else:
                    self.stdout.write(
                        "Native fdkaac publication: " + ("NO-OP" if result.no_op else "APPLIED")
                    )
                    self.stdout.write(
                        f"  Foundation E2 fdkaac: {result.evidence.components['fdkaac'].status.upper()}"
                    )
                return
            if options["native_source_dir"] is not None or options["prepared_native_root"] is not None:
                raise CommandError("native paths require --fdkaac or an explicit fdkaac phase")
            if options["bootstrap_fdkaac"]:
                raise CommandError("--bootstrap-fdkaac requires native fdkaac mode")
            if options["trusted_preparer_uid"] is not None:
                raise CommandError("--trusted-preparer-uid requires native fdkaac mode")
            effective_bundle = (
                recovery_payload.tts_bundle.root if recovery_payload is not None else options["bundle"]
            )
            if effective_bundle is None:
                raise CommandError("E3 TTS provisioning requires --bundle or --recovery-payload")
            provisioner = RuntimeProvisioner(
                bundle_root=effective_bundle,
                requirements=requirements,
                product_manifest=manifest,
                target_root=options["target_root"],
            )
            if options["plan"]:
                plan = provisioner.plan()
                self._write_plan(plan, json_output=json_output)
                if not plan.ready:
                    raise CommandError("runtime provisioning plan contains blocking errors")
                return
            result = provisioner.apply()
            if json_output:
                self.stdout.write(result.to_json())
            else:
                outcome = "NO-OP" if result.no_op else "APPLIED"
                self.stdout.write(f"Runtime provisioning: {outcome}")
                for component in result.changed_components:
                    self.stdout.write(f"  published: {component}")
                self.stdout.write(f"  Foundation E2 acceptance: {result.evidence.result.upper()}")
        except CommandError:
            raise
        except (
            RuntimeBundleError,
            RuntimeComponentContractError,
            RuntimeProvisioningError,
            RuntimeRecoveryError,
        ) as exc:
            safe = " ".join(str(exc).split())[:512] or "runtime provisioning failed"
            if json_output:
                self.stdout.write(
                    json.dumps(
                        {"error": safe, "result": "fail", "schema_version": 1},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
            raise CommandError(safe) from None
