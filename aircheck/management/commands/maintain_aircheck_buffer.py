from django.core.management.base import BaseCommand, CommandError

from aircheck.services.recorder import AIRCHECK_IDLE_BUFFER_MAX_BYTES, maintain_idle_buffer


class Command(BaseCommand):
    """Kept thin on purpose -- all decision logic (lock, active-session
    check, size threshold, telnet reopen, failure handling) lives in
    aircheck.services.recorder.maintain_idle_buffer. This command just
    calls it and reports the result. Intended to run from
    isadoraair-aircheck-buffer.timer every minute; safe to run
    concurrently with a real Start/Stop or with itself (non-blocking
    lock acquisition means an overlapping/contended run just reports
    "lock_busy" and exits)."""

    help = (
        "Idle-buffer maintenance for the always-on Aircheck output.file "
        "working buffer at AIRCHECK_CURRENT_PATH -- rolls it over via the "
        "existing aircheck.reopen telnet call when it's grown past the "
        "safety limit and no Aircheck session is active. Never touches "
        "an active session's recording."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--max-bytes",
            type=int,
            default=None,
            help=(
                "Override the safety threshold in bytes, for deterministic "
                "testing/manual acceptance only. Must be a positive integer. "
                f"Production default is the hard-coded {AIRCHECK_IDLE_BUFFER_MAX_BYTES} "
                "(64 MiB) -- omit this flag in normal operation."
            ),
        )

    def handle(self, *args, **options):
        max_bytes = options["max_bytes"]
        if max_bytes is not None and max_bytes <= 0:
            raise CommandError("--max-bytes must be a positive integer")

        kwargs = {} if max_bytes is None else {"max_bytes": max_bytes}
        result = maintain_idle_buffer(**kwargs)
        self.stdout.write(f"aircheck idle buffer maintenance: {result}")
