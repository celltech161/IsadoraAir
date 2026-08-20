from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from aircheck.services import recorder
from aircheck.services.recorder import maintain_idle_buffer, record_buffer_heartbeat

# AIRCHECK_CURRENT_PATH/AIRCHECK_IDLE_BUFFER_MAX_BYTES are read as
# recorder.X at call time below, not bound to a local name at import
# time -- see aircheck/views.py's own comment on this same "patch
# where it's used, not where it's defined" gotcha.


class Command(BaseCommand):
    """Kept thin on purpose -- all decision logic (lock, active-session
    check, size threshold, telnet reopen, failure handling) lives in
    aircheck.services.recorder.maintain_idle_buffer, and this command
    does not change or duplicate any of it. This command just calls
    that function, records a heartbeat of the result for the
    /monitoring/ Aircheck card to read (record_buffer_heartbeat, pure
    reporting, never influences the rollover decision), and prints the
    human-readable result. Intended to run from
    isadoraair-aircheck-buffer.timer every minute; safe to run
    concurrently with a real Start/Stop or with itself (non-blocking
    lock acquisition means an overlapping/contended run just reports
    "lock_busy" and exits)."""

    help = (
        "Idle-buffer maintenance for the always-on Aircheck output.file "
        "working buffer at AIRCHECK_CURRENT_PATH -- rolls it over via the "
        "existing aircheck.reopen telnet call when it's grown past the "
        "safety limit and no Aircheck session is active. Never touches "
        "an active session's recording. Records a heartbeat of the "
        "result to AIRCHECK_BUFFER_STATE_PATH for the /monitoring/ card."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-bytes",
            type=int,
            default=None,
            help=(
                "Override the safety threshold in bytes, for deterministic "
                "testing/manual acceptance only. Must be a positive integer. "
                f"Production default is the hard-coded {recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES} "
                "(64 MiB) -- omit this flag in normal operation."
            ),
        )

    def handle(self, *args, **options):
        max_bytes = options["max_bytes"]
        if max_bytes is not None and max_bytes <= 0:
            raise CommandError("--max-bytes must be a positive integer")

        effective_max_bytes = max_bytes if max_bytes is not None else recorder.AIRCHECK_IDLE_BUFFER_MAX_BYTES
        kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
        result = maintain_idle_buffer(**kwargs)

        # Observed AFTER the maintenance action so a "rolled" cycle's
        # heartbeat reflects the fresh (small) file, not its pre-roll
        # size. Best-effort -- a stat failure here must not affect the
        # already-decided result or crash the command.
        try:
            size_bytes = Path(recorder.AIRCHECK_CURRENT_PATH).stat().st_size
        except OSError:
            size_bytes = None
        record_buffer_heartbeat(result, size_bytes, effective_max_bytes)

        self.stdout.write(f"aircheck idle buffer maintenance: {result}")
