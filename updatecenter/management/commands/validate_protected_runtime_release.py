import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from updatecenter.protected_release_validator import (
    ProtectedReleaseValidationError,
    validate_protected_release,
)


class Command(BaseCommand):
    help = "Read-only Phase-D protected-runtime release authoring validation."

    def add_arguments(self, parser):
        parser.add_argument("--manifest", required=True)
        parser.add_argument("--trust-policy", required=True)
        parser.add_argument("--signer-directory", required=True)
        parser.add_argument("--previous-generation", required=True, type=int)
        parser.add_argument("--previous-policy")
        parser.add_argument("--previous-commit")
        parser.add_argument("--target-commit")
        parser.add_argument(
            "--identity-tip",
            help="commit whose ancestry stands in for production's fetched canonical tip "
                 "when resolving the release's immutable identity (default: HEAD)",
        )

    def handle(self, *args, **options):
        checkout = Path(__file__).resolve().parents[3]
        try:
            evidence = validate_protected_release(
                checkout_root=checkout,
                manifest_path=Path(options["manifest"]),
                trust_policy_path=Path(options["trust_policy"]),
                signer_directory=Path(options["signer_directory"]),
                previous_generation=options["previous_generation"],
                previous_policy_path=Path(options["previous_policy"]) if options["previous_policy"] else None,
                previous_commit=options["previous_commit"], target_commit=options["target_commit"],
                identity_tip=options["identity_tip"],
            )
        except ProtectedReleaseValidationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(evidence, sort_keys=True, separators=(",", ":")))
        self.stdout.write(self.style.SUCCESS("PASS: protected-runtime release is authoring-complete"))
