"""Station-aware operator adapter for offline Runtime Foundation E3."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_bundle import RuntimeBundleError
from isadoraair.runtime_components import RuntimeComponentContractError, load_runtime_components
from isadoraair.runtime_provisioning import RuntimeProvisioner, RuntimeProvisioningError
from isadoraair.runtime_requirements import resolve_current_runtime_requirements


class Command(BaseCommand):
    help = "Plan or apply deterministic TTS provisioning from a trusted offline bundle."

    def add_arguments(self, parser):
        parser.add_argument(
            "--bundle",
            required=True,
            type=Path,
            help="Existing directory containing runtime-bundle.json and all offline payload files.",
        )
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--plan", action="store_true", help="Inspect and report without publication.")
        mode.add_argument("--apply", action="store_true", help="Perform only the reported offline plan.")
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

    def handle(self, *args, **options):
        json_output = options["json_output"]
        try:
            manifest = load_runtime_components()
            try:
                requirements = resolve_current_runtime_requirements(manifest)
            except Exception as exc:
                raise RuntimeProvisioningError(
                    "station configuration could not be inspected"
                ) from exc
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
