"""Runtime Foundation E7B / Phase-D -- the one integration point
disaster-recovery restore orchestration (deploy/restore/75-protected-
updater.sh) calls to actually restore one recovery payload's
protected_updater component.

Two distinct pieces of work, both offline and never touching a live
protected-runtime directory as a source of truth (the recovery payload
is the only source of truth consulted here):

  1. Always: offline, non-privileged restore into an empty --fake-root
     -- full Phase-D trust/signature/descriptor/runtime-state
     verification (isadoraair.phase_d_recovery.restore_phase_d_component),
     proving this exact payload reconstructs a genuine, internally
     consistent protected-updater generation. Never starts the real
     worker (it can't -- it deliberately never assumes root-owned
     ancestry); see that function's own docstring.

  2. Only with --publish-root: materialize that already-restored
     fake-root's tree onto a real/staging filesystem root, so a
     disaster-recovery receipt that records this component afterward
     means what it means for every other runtime-recovery component
     (kokoro/piper/native_fdkaac): genuinely present at the restore
     target, not merely proven reconstructable in a throwaway
     directory. Refuses -- never silently overwrites -- any file that
     already exists at its destination.

Neither step starts, enables, or reloads anything. Activating the
restored generation (real root ownership beyond what --publish-root's
own effective UID naturally produces, starting the supervisor, DISARMED
readiness proof) is a deliberate, separate, privileged step outside
this command's scope -- see docs/RUNTIME_BACKUP_PAYLOAD.md's Phase-D
section for why that boundary exists and stays in place here."""

from __future__ import annotations

import json as json_module
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from isadoraair.phase_d_recovery import PhaseDRecoveryError, publish_phase_d_component
from isadoraair.runtime_recovery import RuntimeRecoveryError, restore_protected_updater_component


class Command(BaseCommand):
    help = (
        "Offline restore of one recovery payload's protected_updater component "
        "into an empty fake root, optionally publishing it onto a real/staging "
        "restore target. Never starts, enables, or reloads anything."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--recovery-payload",
            required=True,
            type=Path,
            help="An already-extracted, already-validated recovery payload root "
            "(runtime-recovery.json directly inside it) -- e.g. lib.sh's "
            "restore_locate_recovery_payload destination.",
        )
        parser.add_argument(
            "--fake-root",
            required=True,
            type=Path,
            help="Destination for the offline, non-privileged restore proof. Must not already exist.",
        )
        parser.add_argument(
            "--publish-root",
            type=Path,
            help="If given, also materialize the restored fake root's tree onto this "
            "real/staging filesystem root (e.g. / for a real target, or the "
            "restore's --staging-root). Refuses rather than overwriting any file "
            "that already exists there.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="json_output",
            help="Write only deterministic machine-readable output to stdout.",
        )

    def handle(self, *args, **options):
        payload = options["recovery_payload"]
        fake_root = options["fake_root"]
        publish_root = options["publish_root"]

        try:
            evidence = restore_protected_updater_component(payload, fake_root=fake_root)
        except RuntimeRecoveryError as exc:
            raise CommandError(str(exc)) from None

        if evidence is None:
            raise CommandError(
                "this recovery payload declares no protected_updater component -- nothing to "
                "restore (check components.protected_updater.state via manage.py "
                "validate_runtime_recovery_payload before calling this command)"
            )

        if publish_root is not None:
            try:
                publish_phase_d_component(fake_root=fake_root, target_root=publish_root)
            except PhaseDRecoveryError as exc:
                raise CommandError(f"protected-updater publish to {publish_root} failed: {exc}") from None
            evidence = {**evidence, "published_to": str(publish_root)}

        if options["json_output"]:
            self.stdout.write(json_module.dumps(evidence, sort_keys=True, separators=(",", ":")))
        else:
            self.stdout.write(f"protected_updater restore: {evidence['result'].upper()}")
            self.stdout.write(f"  active generation: {evidence['active_generation']} (slot {evidence['active_slot']})")
            if evidence.get("previous_generation") is not None:
                self.stdout.write(f"  previous generation: {evidence['previous_generation']}")
            self.stdout.write(f"  fake root: {evidence['restore_root']}")
            if publish_root is not None:
                self.stdout.write(f"  published to: {evidence['published_to']}")
            self.stdout.write(
                "  worker_started: False, readiness: not-run -- activation is a separate, "
                "privileged step outside this command's scope"
            )
