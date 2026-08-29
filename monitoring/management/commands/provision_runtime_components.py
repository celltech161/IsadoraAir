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
from isadoraair.runtime_requirements import resolve_current_runtime_requirements
from isadoraair.runtime_surfaces import RuntimeSystemSurfaceManager


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
            try:
                requirements = resolve_current_runtime_requirements(manifest)
            except Exception as exc:
                raise RuntimeProvisioningError(
                    "station configuration could not be inspected"
                ) from exc
            native_mode = (
                options["fdkaac"]
                or options["prepare_fdkaac"]
                or options["publish_fdkaac"]
            )
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
                if options["plan"]:
                    plan = native.plan(
                        source_dir=options["native_source_dir"],
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
                    if options["native_source_dir"] is None or options["prepared_native_root"] is None:
                        raise CommandError(
                            "--prepare-fdkaac requires --native-source-dir and --prepared-native-root"
                        )
                    result = native.prepare(
                        source_dir=options["native_source_dir"],
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
            if options["bundle"] is None:
                raise CommandError("E3 TTS provisioning requires --bundle")
            provisioner = RuntimeProvisioner(
                bundle_root=options["bundle"],
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
        except (RuntimeBundleError, RuntimeComponentContractError, RuntimeProvisioningError) as exc:
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
