import json

from django.core.management.base import BaseCommand, CommandError

from aircheck.models import AircheckSession
from aircheck.services import recorder, recovery


class Command(BaseCommand):
    """P2 1.13B -- the slower-cadence half of Aircheck recovery.

    Kept deliberately separate from the one-minute
    maintain_aircheck_buffer command/timer: automatic pending-
    finalization retry calls the real recorder._finalize_segment_set,
    which can legitimately run for a long time on a large recording
    (bounded by recorder.FFMPEG_TIMEOUT_SECONDS plus its own duration-
    derived decode-validation ceiling -- up to several hours). A
    systemd Type=oneshot unit's process lifetime IS its timer's busy
    window; running that inside the one-minute buffer unit would
    silently suspend routine /run reconciliation (handoff/legacy
    evacuation) for the same duration. This command's own timer runs
    every 5 minutes instead, and systemd will not start a new instance
    of it while a previous one is still running -- combined with
    aircheck.services.recovery's own per-session finalization_lock,
    this means overlapping invocations never duplicate work even if
    the timer interval is ever shortened.

    All decision logic (grace period, lock ownership, retry, retention
    age/byte policy) lives in aircheck.services.recovery -- this
    command only calls it, reports the result, and records a
    heartbeat."""

    help = (
        "Automatic pending-Aircheck-finalization retry plus bounded age/"
        "byte retention of failed/quarantined recovery material. Runs "
        "from isadoraair-aircheck-recovery.timer every 5 minutes. "
        "--dry-run performs the full inventory/decision pass with zero "
        "mutations. --retry-session <id> manually retries one session "
        "(pending OR explicitly failed) regardless of the normal grace "
        "period/auto-retry restriction, for operator-directed recovery."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true",
            help="Report inventory and what retention would delete; make zero mutations.",
        )
        parser.add_argument(
            "--retry-session", type=int, default=None, metavar="SESSION_ID",
            help=(
                "Manually retry exactly one session's finalization (pending "
                "or explicitly failed), bypassing the automatic grace period "
                "and the routine no-auto-retry-on-explicit-failure policy. "
                "Still fully mediated by the per-session finalization lock."
            ),
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        retry_session_id = options["retry_session"]

        if retry_session_id is not None:
            if dry_run:
                raise CommandError("--retry-session and --dry-run are mutually exclusive")
            outcome = recovery.retry_one_session_by_id(retry_session_id)
            self.stdout.write(f"manual retry of session {retry_session_id}: {outcome}")
            return

        inventory = recovery.inventory_summary()
        self.stdout.write("=== Aircheck recovery inventory ===")
        self.stdout.write(json.dumps(inventory, indent=2, default=str))

        result = recovery.run_recovery_maintenance(dry_run=dry_run)
        self.stdout.write("=== Aircheck recovery maintenance result ===")
        self.stdout.write(json.dumps(result, indent=2, default=str))

        if dry_run:
            deleted = result["retention"]["deleted"]
            if deleted:
                self.stdout.write("Would delete under retention:")
                for recovery_set, reason in deleted:
                    self.stdout.write(
                        f"  session={recovery_set.session_id} kind={recovery_set.kind} "
                        f"bytes={recovery_set.size_bytes} age_s={round(recovery_set.age_seconds)} "
                        f"reason={reason} path={recovery_set.path}"
                    )
            else:
                self.stdout.write("Would delete: nothing")
            return

        recorder.record_recovery_heartbeat(result)
        self.stdout.write("aircheck recovery maintenance: heartbeat recorded")
