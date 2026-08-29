"""Runtime Foundation E7A -- operator-facing plan/apply preparation of one
disaster-recovery runtime payload from operator-supplied local material.

Deliberately narrow: this command builds the artifact only. It never
provisions Foundation E3/E4/E5 canonical runtime state, never touches
/opt/isadoraair-runtime or /var/lib/isadoraair/tts, never publishes
anything to /usr/local, never restarts a service, and never fetches
anything over the network -- every input is a caller-supplied local
path. See docs/RUNTIME_BACKUP_PAYLOAD.md.
"""

from __future__ import annotations

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.runtime_recovery import RuntimeRecoveryBuilder, RuntimeRecoveryError


class Command(BaseCommand):
    help = (
        "Plan/apply preparation of one Runtime Foundation E7 disaster-recovery "
        "payload (an embedded E3 TTS bundle and/or E4 native fdkaac source "
        "material) from operator-supplied local paths. Never provisions "
        "canonical runtime state, never touches the network."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--plan", action="store_true", help="Inspect and report without writing anything.")
        mode.add_argument("--apply", action="store_true", help="Build the payload at --output.")
        parser.add_argument(
            "--tts-bundle",
            type=Path,
            help="Path to an existing, valid Runtime Foundation E3 runtime-bundle.json directory.",
        )
        parser.add_argument(
            "--native-source-dir",
            type=Path,
            help="Path to a directory containing the manifest-declared fdkaac/libfdk-aac source archives.",
        )
        parser.add_argument(
            "--output",
            type=Path,
            required=True,
            help="New, caller-owned destination for the built payload. Must not already exist.",
        )
        parser.add_argument(
            "--payload-id",
            help="Stable identity for this payload. Defaults to a UTC-timestamp-derived id.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable output to stdout.",
        )

    def handle(self, *args, **options):
        builder = RuntimeRecoveryBuilder()
        kwargs = {
            "tts_bundle": options["tts_bundle"],
            "native_source_dir": options["native_source_dir"],
            "output": options["output"],
        }
        try:
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
        except CommandError:
            raise
        except RuntimeRecoveryError as exc:
            safe = " ".join(str(exc).split())[:512] or "recovery payload preparation failed"
            raise CommandError(safe) from None
