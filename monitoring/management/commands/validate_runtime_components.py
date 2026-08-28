"""Read-only operator interface for Runtime Foundation E evidence."""

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_validation import STATUS_PASS, validate_current_runtime


class Command(BaseCommand):
    help = "Validate required canonical runtime components without provisioning or mutation."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only versioned machine-readable evidence to stdout.",
        )

    def handle(self, *args, **options):
        evidence = validate_current_runtime()
        if options["json_output"]:
            self.stdout.write(evidence.to_json())
        else:
            self.stdout.write(f"Runtime components: {evidence.result.upper()}")
            for name in sorted(evidence.components):
                component = evidence.components[name]
                required = "required" if component.required else "optional"
                self.stdout.write(f"  {name}: {component.status} ({required})")
                for reason in component.reasons:
                    self.stdout.write(f"    required by: {reason}")
                for diagnostic in component.diagnostics:
                    self.stdout.write(f"    {diagnostic}")
            for error in evidence.contract_errors:
                self.stdout.write(f"  product contract: {error}")
            for error in evidence.requirement_errors:
                self.stdout.write(f"  station configuration: {error}")
        if evidence.result != STATUS_PASS:
            raise CommandError("one or more required runtime components failed validation")
