import json

from django.core.management.base import BaseCommand

from weather.diagnostics import get_weather_diagnostics


class Command(BaseCommand):
    """Read-only Weather diagnostics/readiness report (P1 1.15 / 2.4 Pass
    B). Uses exactly the same weather.diagnostics.get_weather_diagnostics()
    authority the Admin "Weather data storage" subpage does -- this is
    the one shared source of truth, not a second implementation.

    Never touches the network, never invokes TTS/ffmpeg, never writes
    Weather data or Track/config state, never restarts anything. Safe
    to run at any time, as often as wanted.

    Exit code: 0 if no fact is in the `needs_attention` state, 1
    otherwise. `optional_disabled`, `degraded`, and `not_applicable`
    facts never affect the exit code -- an intentionally-off feature
    (or an event-driven artifact with nothing currently active) is not
    a command failure.
    """

    help = "Read-only Weather configuration/data/artifact diagnostics report."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json", action="store_true", dest="as_json",
            help="Print the full snapshot as machine-readable JSON instead of the human summary.",
        )

    def handle(self, *args, **options):
        snapshot = get_weather_diagnostics()

        if options["as_json"]:
            self.stdout.write(json.dumps(snapshot.to_dict(), indent=2))
        else:
            self._print_human(snapshot)

        if snapshot.needs_attention:
            raise SystemExit(1)

    def _print_human(self, snapshot):
        self.stdout.write(f"Weather diagnostics -- {snapshot.generated_at}\n")
        order = {"needs_attention": 0, "degraded": 1, "ready": 2, "not_applicable": 3, "optional_disabled": 4}
        for fact in sorted(snapshot.facts, key=lambda f: (order.get(f.state, 9), f.key)):
            marker = {
                "ready": "OK",
                "optional_disabled": "--",
                "degraded": "!!",
                "needs_attention": "XX",
                "not_applicable": "n/a",
            }.get(fact.state, "??")
            line = f"[{marker:>3}] {fact.key}: {fact.summary}"
            if fact.age_seconds is not None:
                line += f" (age {fact.age_seconds:.0f}s)"
            self.stdout.write(line + "\n")
            if fact.detail:
                self.stdout.write(f"          {fact.detail}\n")

        attention = snapshot.needs_attention
        if attention:
            self.stdout.write(f"\n{len(attention)} item(s) need attention.\n")
        else:
            self.stdout.write("\nNo items need attention.\n")
