"""Runtime Foundation E7A/E7B -- read-only operator interface for
validating one disaster-recovery runtime payload. Never provisions,
never mutates the payload or any canonical Foundation E path.

This is the one integration point backup/restore orchestration (E7B)
calls to answer "is the selected recovery payload safe to trust" --
callers that need a machine-readable answer should pass --json and read
its structured fields rather than parsing this command's human text.

Exit codes (with --current; a direct `payload` path only ever uses 0/1,
matching every other Foundation E validator command):
  0  valid, and satisfies any --require policy given.
  1  a genuine failure: found but invalid/tampered/stale/wrong-digest,
     or found but does not satisfy an explicit --require policy. A
     caller (e.g. a nightly backup) should always treat this as fatal.
  2  NOT CONFIGURED: --base-root/current has never been set up on this
     host at all (see RecoveryPayloadNotConfiguredError) -- distinct
     from every other failure specifically so a caller can choose to
     treat "not yet adopted" differently from "broken", e.g. a nightly
     backup that only WARNS when no operator policy requires this
     payload to exist yet, but always fails on a broken one."""

from __future__ import annotations

import json as json_module
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_recovery import (
    RECOVERY_POLICY_COMPONENT_NAMES,
    RESULT_PASS,
    RecoveryPayloadNotConfiguredError,
    RuntimeRecoveryError,
    evaluate_recovery_policy,
    parse_recovery_policy_components,
    resolve_current_recovery_payload_root,
    validate_current_recovery_payload,
)


class Command(BaseCommand):
    help = (
        "Read-only validation of one Runtime Foundation E7 disaster-recovery "
        "payload -- integrity, product-contract identity, (using the live "
        "station database) Piper selection freshness, and (with --require) "
        "operator-declared recovery-component policy. Never modifies anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "payload", type=Path, nargs="?", help="Path to a runtime-recovery.json payload root."
        )
        parser.add_argument(
            "--require-components",
            help=(
                "Strict comma-separated recovery policy. Empty means no policy; "
                "empty entries, whitespace, duplicates, and unknown names fail."
            ),
        )
        parser.add_argument(
            "--trusted-owner-uid",
            type=int,
            default=0,
            help="Expected administrative owner UID for --current's persistent payload tree (default: 0).",
        )
        parser.add_argument(
            "--base-root",
            type=Path,
            help="A persistent recovery-payload base root (see docs/RUNTIME_BACKUP_PAYLOAD.md) -- "
            "used with --current instead of a direct payload path.",
        )
        parser.add_argument(
            "--current",
            action="store_true",
            help="Resolve --base-root's current payload pointer instead of taking `payload` directly.",
        )
        parser.add_argument(
            "--require",
            action="append",
            dest="required_components",
            choices=sorted(RECOVERY_POLICY_COMPONENT_NAMES),
            help="A component this payload must positively contain (repeatable): "
            "kokoro, piper, and/or native_fdkaac. Never inferred from station "
            "configuration -- an explicit, operator-declared policy only.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable output to stdout.",
        )

    def handle(self, *args, **options):
        if bool(options["payload"]) == bool(options["current"] or options["base_root"]):
            raise CommandError("supply exactly one of: a direct payload path, or --base-root with --current")
        if options["current"] and not options["base_root"]:
            raise CommandError("--current requires --base-root")
        if options["base_root"] and not options["current"]:
            raise CommandError("--base-root requires --current (there is no other resolution mode)")

        try:
            csv_required = parse_recovery_policy_components(options["require_components"])
        except RuntimeRecoveryError as exc:
            raise CommandError(str(exc)) from None
        repeated = options["required_components"] or []
        if len(repeated) != len(set(repeated)):
            raise CommandError("duplicate --require component names are not allowed")
        overlap = csv_required & set(repeated)
        if overlap:
            raise CommandError(
                f"recovery component policy repeats name(s) across options: {', '.join(sorted(overlap))}"
            )
        if options["trusted_owner_uid"] < 0:
            raise CommandError("--trusted-owner-uid must be non-negative")
        required = csv_required | frozenset(repeated)

        try:
            if options["current"]:
                payload_root = resolve_current_recovery_payload_root(
                    options["base_root"], expected_owner_uid=options["trusted_owner_uid"]
                )
            else:
                payload_root = options["payload"]
        except RecoveryPayloadNotConfiguredError as exc:
            safe = " ".join(str(exc).split())[:512]
            if options["json_output"]:
                self.stdout.write(
                    json_module.dumps({"pointer_configured": False, "resolution_error": safe, "resolved_path": None})
                )
            else:
                self.stdout.write(f"Recovery payload: NOT CONFIGURED -- {safe}")
            raise SystemExit(2) from None
        except RuntimeRecoveryError as exc:
            safe = " ".join(str(exc).split())[:512]
            if options["json_output"]:
                self.stdout.write(
                    json_module.dumps({"pointer_configured": True, "resolution_error": safe, "resolved_path": None})
                )
            raise CommandError(safe) from None

        evidence = validate_current_recovery_payload(payload_root)
        policy_requested = options["require_components"] is not None or bool(repeated)
        policy = evaluate_recovery_policy(evidence, required) if policy_requested else None

        if options["json_output"]:
            payload = evidence.to_dict()
            payload["pointer_configured"] = True
            payload["resolved_path"] = str(payload_root)
            payload["policy"] = policy.to_dict() if policy is not None else None
            self.stdout.write(json_module.dumps(payload, sort_keys=True, separators=(",", ":")))
        else:
            self.stdout.write(f"Recovery payload ({payload_root}): {evidence.result.upper()}")
            if evidence.manifest_error:
                self.stdout.write(f"  manifest: {evidence.manifest_error}")
            for name in sorted(evidence.components):
                component = evidence.components[name]
                self.stdout.write(f"  {name}: {component.state}")
                for diagnostic in component.diagnostics:
                    self.stdout.write(f"    {diagnostic}")
            if evidence.tts_components:
                self.stdout.write(f"  tts components: {', '.join(evidence.tts_components)}")
            freshness = evidence.piper_freshness
            self.stdout.write(f"  piper station selection: {freshness.state}")
            if policy is not None:
                self.stdout.write(
                    f"  recovery policy ({', '.join(sorted(required))}): "
                    f"{'SATISFIED' if policy.satisfied else 'NOT SATISFIED -- missing: ' + ', '.join(sorted(policy.missing))}"
                )

        if evidence.result != RESULT_PASS:
            raise CommandError("recovery payload failed validation")
        if policy is not None and not policy.satisfied:
            raise CommandError(
                f"recovery payload does not satisfy the required-component policy -- missing: "
                f"{', '.join(sorted(policy.missing))}"
            )
