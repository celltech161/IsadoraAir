"""Runtime Foundation E7A -- read-only operator interface for validating
one disaster-recovery runtime payload. Never provisions, never mutates
the payload or any canonical Foundation E path."""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_recovery import RESULT_PASS, validate_current_recovery_payload


class Command(BaseCommand):
    help = (
        "Read-only validation of one Runtime Foundation E7 disaster-recovery "
        "payload -- integrity, product-contract identity, and (using the live "
        "station database) Piper selection freshness. Never modifies anything."
    )

    def add_arguments(self, parser):
        parser.add_argument("payload", type=Path, help="Path to a runtime-recovery.json payload root.")
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable evidence to stdout.",
        )

    def handle(self, *args, **options):
        evidence = validate_current_recovery_payload(options["payload"])
        if options["json_output"]:
            self.stdout.write(evidence.to_json())
        else:
            self.stdout.write(f"Recovery payload: {evidence.result.upper()}")
            if evidence.manifest_error:
                self.stdout.write(f"  manifest: {evidence.manifest_error}")
            for name in sorted(evidence.components):
                component = evidence.components[name]
                self.stdout.write(f"  {name}: {component.state}")
                for diagnostic in component.diagnostics:
                    self.stdout.write(f"    {diagnostic}")
            freshness = evidence.piper_freshness
            self.stdout.write(f"  piper station selection: {freshness.state}")
        if evidence.result != RESULT_PASS:
            raise CommandError("recovery payload failed validation")
