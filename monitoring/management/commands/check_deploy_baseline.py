"""Read-only deployment-baseline preflight -- IsadoraAir 1.2 Phase 3,
consolidated onto Runtime Foundation E evidence by Runtime Foundation E6.

One coherent read-only answer to: is this host structurally capable of
satisfying the station's runtime contract, and can a restored/clean host
establish the required system surfaces without relying on historical
one-off checks? Never modifies the host.

This command is a thin presentation layer over
:func:`isadoraair.deploy_baseline.evaluate_deployment_baseline` -- it
contains no independent capability-detection logic of its own for
anything Runtime Foundation E now owns (fdkaac, Kokoro, Piper, the E5
system surfaces, the pre-existing TTS scratch surface, package
prerequisites). See docs/RUNTIME_DEPLOY_BASELINE.md for the full
consolidation writeup, including exactly which pre-E6 checks were
retained as-is, which were replaced by Foundation E evidence, and why.

Two tiers, reported together by default:
  STRUCTURAL -- host/system checks, package prerequisite presence, the
  E5 system surfaces, the scratch surface, manifest validity. Evaluable
  with no station database at all.
  STATION -- which optional runtimes the station's own configuration
  actually requires, and whether they pass Foundation E's own E2
  component validation. Reports UNRESOLVED (never a guessed PASS) when
  no usable database exists -- use --structural-only to skip this tier
  entirely for a pre-database installer/bootstrap context.

Exit code 0 iff the requested tier(s) all resolve to PASS; nonzero for
FAIL or UNRESOLVED alike (fail-closed, matching Foundation E1/E2's own
existing design for an unresolved station) -- unchanged from this
command's pre-E6 contract, which
deploy/restore/95-validate.sh and updatecenter's release-validation
convention both already depend on.
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.deploy_baseline import (
    LEGACY_DEGRADED,
    LEGACY_MISSING,
    LEGACY_OPTIONAL,
    LEGACY_PASS,
    RESULT_PASS,
    evaluate_deployment_baseline,
)


class Command(BaseCommand):
    help = (
        "Read-only deployment-baseline preflight: legacy host/system checks "
        "(Python, PostgreSQL tools, GStreamer + required elements, "
        "Liquidsoap, ALSA utils + snd-aloop layout, canonical directories) "
        "consolidated with Runtime Foundation E evidence (package "
        "prerequisites, E5 system surfaces, the TTS scratch surface, and "
        "E1/E2 fdkaac/Kokoro/Piper component evidence). Never modifies the "
        "host. Exit code 0 iff everything resolves to PASS; nonzero for "
        "FAIL or UNRESOLVED alike."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--structural-only",
            action="store_true",
            help=(
                "Report only the structural tier (no station database "
                "required) -- for a pre-database installer/bootstrap "
                "context. Skips Foundation E1/E2 station-requirement "
                "evidence entirely rather than reporting it UNRESOLVED."
            ),
        )
        parser.add_argument(
            "--isa-user",
            default=None,
            help=(
                "The service account identity restore/install tooling "
                "resolved (deploy/restore/90-system-config.sh's own "
                "ISA_USER) -- required to evaluate the pre-existing TTS "
                "scratch surface (/run/isadoraair/tts) meaningfully. "
                "Omitted: reported as service-identity-unresolved, never "
                "guessed."
            ),
        )
        parser.add_argument(
            "--target-root",
            type=Path,
            default=Path("/"),
            help=(
                "Map canonical filesystem evidence beneath an offline target root. "
                "A noncanonical target automatically uses structural-only validation; "
                "persistent content is still checked against boot-root canonical paths."
            ),
        )
        parser.add_argument(
            "--isa-uid",
            type=int,
            default=None,
            help="Trusted expected numeric service UID (must be paired with --isa-gid).",
        )
        parser.add_argument(
            "--isa-gid",
            type=int,
            default=None,
            help="Trusted expected numeric service GID (must be paired with --isa-uid).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable output to stdout.",
        )

    def handle(self, *args, **options):
        if (options["isa_uid"] is None) != (options["isa_gid"] is None):
            raise CommandError("--isa-uid and --isa-gid must be supplied together")
        if options["isa_uid"] is not None and (
            options["isa_uid"] < 0 or options["isa_gid"] < 0
        ):
            raise CommandError("--isa-uid and --isa-gid must be non-negative")
        evidence = evaluate_deployment_baseline(
            structural_only=options["structural_only"],
            target_root=options["target_root"],
            isa_user=options["isa_user"],
            expected_uid=options["isa_uid"],
            expected_gid=options["isa_gid"],
        )

        if options["json_output"]:
            self.stdout.write(json.dumps(evidence.to_dict(), sort_keys=True, separators=(",", ":")))
        else:
            self._write_human(evidence)

        if evidence.result != RESULT_PASS:
            self.stderr.write("check_deploy_baseline: FAIL")
            raise SystemExit(1)

    # ---- human-readable presentation --------------------------------

    def _marker(self, state):
        return {
            LEGACY_PASS: self.style.SUCCESS("PASS    "),
            LEGACY_DEGRADED: self.style.WARNING("DEGRADED"),
            LEGACY_MISSING: self.style.ERROR("MISSING "),
            LEGACY_OPTIONAL: "OPTIONAL",
            "pass": self.style.SUCCESS("PASS    "),
            "fail": self.style.ERROR("FAIL    "),
            "unresolved": self.style.WARNING("UNRESOLVED"),
            "optional_absent": "OPTIONAL",
            "not_applicable": "N/A     ",
        }.get(state, state)

    def _write_human(self, evidence):
        structural = evidence.structural
        self.stdout.write(self.style.MIGRATE_HEADING("-- Structural baseline --"))
        if structural.manifest_error:
            self.stdout.write(self._marker("fail") + f"  runtime component contract: {structural.manifest_error}")
            return
        for check in structural.legacy_checks:
            suffix = f": {check.detail}" if check.detail else ""
            self.stdout.write(f"{self._marker(check.state)}  {check.label}{suffix}")
        if structural.system_surfaces_error:
            self.stdout.write(self._marker("fail") + f"  E5 system surfaces: {structural.system_surfaces_error}")
        elif structural.system_surfaces is not None:
            for name in sorted(structural.system_surfaces.surfaces):
                item = structural.system_surfaces.surfaces[name]
                state = "pass" if item.state == "healthy" else "fail"
                self.stdout.write(f"{self._marker(state)}  E5 surface: {name} ({item.state})")
        scratch = structural.scratch_surface
        scratch_state = "pass" if scratch.healthy else ("unresolved" if scratch.state == "unresolved_identity" else "fail")
        self.stdout.write(f"{self._marker(scratch_state)}  TTS scratch surface {scratch.path} ({scratch.state})")
        for diagnostic in scratch.diagnostics:
            self.stdout.write(f"    {diagnostic}")
        for pkg in structural.package_prerequisites:
            missing_suffix = f" missing: {', '.join(pkg.missing)}" if pkg.missing else ""
            if pkg.kind == "build":
                self.stdout.write(
                    f"INFO     package group {pkg.group} "
                    f"({pkg.component}/build-only, non-gating): {pkg.status}{missing_suffix}"
                )
                continue
            self.stdout.write(
                f"{self._marker(pkg.status)}  package group {pkg.group} "
                f"({pkg.component}/{pkg.kind}): {pkg.status}{missing_suffix}"
            )
            for diagnostic in pkg.diagnostics:
                self.stdout.write(f"    {diagnostic}")

        self.stdout.write("")
        if evidence.station is None:
            self.stdout.write(
                "-- Live/station baseline: skipped "
                "(--structural-only or offline --target-root) --"
            )
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("-- Live/station baseline --"))
            for check in evidence.live_checks:
                suffix = f": {check.detail}" if check.detail else ""
                self.stdout.write(f"{self._marker(check.state)}  {check.label}{suffix}")
            station = evidence.station
            if station.requirement_errors:
                for error in station.requirement_errors:
                    self.stdout.write(self._marker("unresolved") + f"  station configuration: {error}")
            for name in sorted(station.components):
                component = station.components[name]
                self.stdout.write(
                    f"{self._marker(component.status)}  {name} (required={component.required}): {component.status}"
                )
                for diag in component.diagnostics:
                    self.stdout.write(f"    {diag}")
            for pkg in evidence.station_package_prerequisites:
                missing_suffix = f" missing: {', '.join(pkg.missing)}" if pkg.missing else ""
                if pkg.kind == "build":
                    self.stdout.write(
                        f"INFO     package group {pkg.group} "
                        f"({pkg.component}/build-only, non-gating, required={pkg.required}): "
                        f"{pkg.status}{missing_suffix}"
                    )
                    continue
                self.stdout.write(
                    f"{self._marker(pkg.status)}  package group {pkg.group} "
                    f"({pkg.component}/{pkg.kind}, required={pkg.required}): "
                    f"{pkg.status}{missing_suffix}"
                )
                for diagnostic in pkg.diagnostics:
                    self.stdout.write(f"    {diagnostic}")

        self.stdout.write("")
        if evidence.result == RESULT_PASS:
            self.stdout.write(self.style.SUCCESS("PASS: deployment baseline satisfied."))
        elif evidence.result == "unresolved":
            self.stdout.write(self.style.WARNING("UNRESOLVED: some evidence could not be resolved -- see above."))
        else:
            self.stdout.write(self.style.ERROR("FAIL: deployment baseline is not satisfied -- see above."))
