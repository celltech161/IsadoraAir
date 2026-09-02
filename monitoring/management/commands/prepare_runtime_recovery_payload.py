"""Runtime Foundation E7A/E7B -- operator-facing plan/apply preparation
of one disaster-recovery runtime payload from operator-supplied local
material, and (E7B) explicit selection of the persistent "current"
payload a future backup run will trust.

Deliberately narrow: this command builds/selects the artifact only. It
never provisions Foundation E3/E4/E5 canonical runtime state, never
touches /opt/isadoraair-runtime or /var/lib/isadoraair/tts, never
publishes anything to /usr/local, never restarts a service, and never
fetches anything over the network -- every input is a caller-supplied
local path. See docs/RUNTIME_BACKUP_PAYLOAD.md.

--activate never builds or mutates a payload -- it only atomically
repoints --base-root's `current` pointer at an already-built, already-
validated payload directory. Nothing in this command creates
--base-root itself or changes its ownership; that remains a deliberate,
documented, out-of-session operator/installer step (see
docs/RUNTIME_BACKUP_PAYLOAD.md's "Persistent payload location").

r0031: --phase-d switches --plan/--apply from the original operator-
supplied-local-material flow to a second, entirely derived flow --
capturing THIS host's own installed Phase-D protected-updater state
(never an operator-typed protected path) and attaching it to the
current Foundation-E payload as a new schema-2 payload. --activate is
already fully schema-agnostic and needs no --phase-d awareness of its
own; the same flag activates either kind of payload identically.
Real (non---staging-root) use of --phase-d --apply and of --activate
against the real, root-owned persistent recovery root both require
root (sudo) -- see isadoraair.runtime_recovery.
build_and_attach_installed_phase_d_payload's own docstring for why
this is an inherent property of the security model, not a gap."""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_recovery import (
    RuntimeRecoveryBuilder,
    RuntimeRecoveryError,
    activate_recovery_payload,
    build_and_attach_installed_phase_d_payload,
    plan_installed_phase_d_publication,
)


class Command(BaseCommand):
    help = (
        "Plan/apply preparation of one Runtime Foundation E7 disaster-recovery "
        "payload -- either from operator-supplied local paths (an embedded E3 TTS "
        "bundle and/or E4 native fdkaac source material), or (--phase-d) derived "
        "entirely from this host's own installed Phase-D protected-updater state, "
        "attached to the current payload as a new schema-2 payload -- or explicit "
        "activation of an already-built payload as the persistent 'current' one. "
        "Never provisions canonical runtime state, never touches the network."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--plan", action="store_true", help="Inspect and report without writing anything.")
        mode.add_argument("--apply", action="store_true", help="Build the payload at --output (or, with --phase-d, beneath --base-root/payloads/).")
        mode.add_argument(
            "--activate",
            action="store_true",
            help="Validate --base-root/payloads/--payload-id and atomically select it as --base-root/current.",
        )
        parser.add_argument(
            "--phase-d",
            action="store_true",
            help="(--plan/--apply) Capture this host's installed Phase-D protected-updater "
            "state and attach it to the current Foundation-E payload as a new schema-2 "
            "payload, instead of the operator-supplied --tts-bundle/--native-source-dir "
            "flow. Every capture input is derived from installed configuration -- never "
            "a manually-typed protected path. Requires --base-root; rejects --output/"
            "--tts-bundle/--native-source-dir (all inapplicable). Real use requires root.",
        )
        parser.add_argument(
            "--tts-bundle",
            type=Path,
            help="(--plan/--apply, not --phase-d) Path to an existing, valid Runtime Foundation E3 runtime-bundle.json directory.",
        )
        parser.add_argument(
            "--native-source-dir",
            type=Path,
            help="(--plan/--apply, not --phase-d) Path to a directory containing the manifest-declared fdkaac/libfdk-aac source archives.",
        )
        parser.add_argument(
            "--output",
            type=Path,
            help="(--plan/--apply, not --phase-d) New, caller-owned destination for the built payload. Must not already exist.",
        )
        parser.add_argument(
            "--base-root",
            type=Path,
            help="(--activate; --phase-d) A persistent recovery-payload base root (see docs/RUNTIME_BACKUP_PAYLOAD.md).",
        )
        parser.add_argument(
            "--payload-id",
            help="(--apply) Stable identity for the built payload; defaults to a UTC-timestamp-derived id "
            "('phase-d-...' with --phase-d). (--activate) The payload directory basename under "
            "--base-root/payloads/ to select -- must exactly equal that payload's own manifest "
            "payload_id, or activation is refused.",
        )
        parser.add_argument(
            "--trusted-owner-uid",
            type=int,
            default=0,
            help="(--activate; --phase-d) Expected administrative owner UID for the persistent payload tree (default: 0).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable output to stdout.",
        )

    def handle(self, *args, **options):
        try:
            if options["activate"]:
                self._handle_activate(options)
                return
            if options["phase_d"]:
                self._handle_phase_d(options)
                return
            if options["output"] is None:
                raise CommandError("--plan/--apply require --output")
            self._handle_plan_or_apply(options)
        except CommandError:
            raise
        except RuntimeRecoveryError as exc:
            safe = " ".join(str(exc).split())[:512] or "recovery payload command failed"
            raise CommandError(safe) from None

    def _handle_plan_or_apply(self, options):
        builder = RuntimeRecoveryBuilder()
        kwargs = {
            "tts_bundle": options["tts_bundle"],
            "native_source_dir": options["native_source_dir"],
            "output": options["output"],
        }
        if options["plan"]:
            plan = builder.plan(**kwargs)
            if options["json_output"]:
                self.stdout.write(plan.to_json())
            else:
                self.stdout.write(f"Recovery payload plan: {'READY' if plan.ready else 'BLOCKED'}")
                self.stdout.write(f"  output: {plan.output}")
                self.stdout.write(f"  includes tts: {plan.includes_tts}")
                self.stdout.write(f"  includes native fdkaac: {plan.includes_native}")
                for error in plan.errors:
                    self.stdout.write(f"  error: {error}")
            if not plan.ready:
                raise CommandError("recovery payload plan contains blocking errors")
            return
        result = builder.apply(payload_id=options["payload_id"], **kwargs)
        if options["json_output"]:
            self.stdout.write(result.to_json())
        else:
            self.stdout.write(f"Recovery payload prepared: {result.payload_id}")
            self.stdout.write(f"  output: {result.output}")
            self.stdout.write(f"  validation result: {result.evidence.result.upper()}")

    def _handle_phase_d(self, options):
        if options["tts_bundle"] is not None or options["native_source_dir"] is not None:
            raise CommandError("--phase-d derives its own inputs from installed configuration -- "
                                "--tts-bundle/--native-source-dir do not apply")
        if options["output"] is not None:
            raise CommandError("--phase-d derives its own destination beneath --base-root/payloads/ -- "
                                "do not pass --output")
        if options["base_root"] is None:
            raise CommandError("--phase-d requires --base-root")
        if options["trusted_owner_uid"] < 0:
            raise CommandError("--trusted-owner-uid must be non-negative")

        if options["plan"]:
            plan = plan_installed_phase_d_publication(
                base_root=options["base_root"],
                new_payload_id=options["payload_id"],
                expected_owner_uid=options["trusted_owner_uid"],
            )
            if options["json_output"]:
                self.stdout.write(plan.to_json())
            else:
                self.stdout.write(f"Phase-D recovery payload plan: {'READY' if plan.ready else 'BLOCKED'}")
                self.stdout.write(f"  base root: {plan.base_root}")
                self.stdout.write(
                    f"  current Foundation-E payload: {plan.source_payload_id} (schema {plan.source_schema_version})"
                )
                if plan.source_requires_refresh_derivation:
                    self.stdout.write(
                        "  current payload is already schema 2 -- will re-derive a fresh "
                        "Foundation-E base from its own embedded material before attaching"
                    )
                self.stdout.write(
                    f"  active: slot {plan.active_slot} generation {plan.active_generation} "
                    f"descriptor {plan.active_descriptor_sha256}"
                )
                if plan.previous_slot is not None:
                    self.stdout.write(
                        f"  previous: slot {plan.previous_slot} generation {plan.previous_generation} "
                        f"descriptor {plan.previous_descriptor_sha256}"
                    )
                else:
                    self.stdout.write("  previous: (none)")
                self.stdout.write(f"  activation in progress: {plan.activation_in_progress}")
                self.stdout.write(
                    f"  trust threshold: {plan.trust_threshold}, "
                    f"signers: {', '.join(plan.public_signer_ids) or '(none)'}"
                )
                self.stdout.write("  private key material included: False")
                self.stdout.write(f"  intended new payload id: {plan.new_payload_id}")
                self.stdout.write(f"  intended destination: {plan.output}")
                for error in plan.errors:
                    self.stdout.write(f"  error: {error}")
            if not plan.ready:
                raise CommandError("Phase-D recovery payload plan contains blocking errors")
            return

        evidence = build_and_attach_installed_phase_d_payload(
            base_root=options["base_root"],
            new_payload_id=options["payload_id"],
            expected_owner_uid=options["trusted_owner_uid"],
        )
        if options["json_output"]:
            self.stdout.write(evidence.to_json())
        else:
            self.stdout.write(f"Phase-D recovery payload built: {evidence.payload_id}")
            self.stdout.write(f"  validation result: {evidence.result.upper()}")
            self.stdout.write(f"  components: {', '.join(sorted(evidence.components))}")
            self.stdout.write(
                f"  NOT activated -- run --activate --base-root {options['base_root']} "
                f"--payload-id {evidence.payload_id} as a deliberate, separate step"
            )

    def _handle_activate(self, options):
        if options["base_root"] is None or options["payload_id"] is None:
            raise CommandError("--activate requires --base-root and --payload-id")
        if options["trusted_owner_uid"] < 0:
            raise CommandError("--trusted-owner-uid must be non-negative")
        target = activate_recovery_payload(
            options["base_root"],
            options["payload_id"],
            expected_owner_uid=options["trusted_owner_uid"],
        )
        if options["json_output"]:
            self.stdout.write(json.dumps({"activated": str(target)}, sort_keys=True, separators=(",", ":")))
        else:
            self.stdout.write(f"Activated recovery payload: {target}")
            self.stdout.write(f"  {options['base_root']}/current now points here")
