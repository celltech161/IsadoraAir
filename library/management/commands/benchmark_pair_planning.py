"""1.1 spec (2026-08-11): performance benchmark for the matched-pair
landing-mode search (find_matched_pair) and full-pool exact-fit
extraction (_extract_fit_candidates/_pick_best_fit), at 250x250,
1000x1000, and 3000x3000 synthetic pool sizes.

Deliberately prints wall-clock timings for human/report review only --
no assertions, no brittle CI timing thresholds baked in. Pool sizes are
in-memory FitCandidate lists (no DB round trip), isolating the
algorithm's own complexity characteristics from unrelated ORM/query
overhead, which is what O(M log N + N log N) claims are actually about."""
import random
import time

from django.core.management.base import BaseCommand

from library.services.log_builder import FitCandidate, find_matched_pair


def _make_pool(size, seed):
    rng = random.Random(seed)
    return [
        FitCandidate(
            track_id=i,
            identity_keys=frozenset({f"artist:{i}"}),  # every track a distinct "artist" -- no incidental conflicts
            duration_seconds=rng.uniform(120, 360),  # 2-6 minutes, a plausible real-world spread
            effective_weight=rng.uniform(1.0, 12.0),
        )
        for i in range(size)
    ]


class Command(BaseCommand):
    help = "Benchmark find_matched_pair / full-pool candidate extraction at 250/1000/3000-size pools."

    def add_arguments(self, parser):
        parser.add_argument(
            "--sizes", type=str, default="250,1000,3000",
            help="Comma-separated pool sizes to benchmark (default: 250,1000,3000).",
        )
        parser.add_argument(
            "--repeats", type=int, default=5,
            help="How many timed find_matched_pair calls to average per pool size (default: 5).",
        )

    def handle(self, *args, **options):
        sizes = [int(s.strip()) for s in options["sizes"].split(",") if s.strip()]
        repeats = options["repeats"]

        self.stdout.write("1.1 matched-pair search benchmark -- wall-clock only, no CI assertions.\n")
        self.stdout.write(f"{'pool size':>10} | {'build (ms)':>12} | {'search avg (ms)':>16} | {'search max (ms)':>16}")
        self.stdout.write("-" * 62)

        for size in sizes:
            build_start = time.perf_counter()
            pool_a = _make_pool(size, seed=1)
            pool_b = _make_pool(size, seed=2)
            build_ms = (time.perf_counter() - build_start) * 1000

            durations_ms = []
            for i in range(repeats):
                rng = random.Random(100 + i)
                remaining_seconds = rng.uniform(360, 720)  # within the landing zone
                start = time.perf_counter()
                find_matched_pair(pool_a, pool_b, remaining_seconds, rng=rng)
                durations_ms.append((time.perf_counter() - start) * 1000)

            avg_ms = sum(durations_ms) / len(durations_ms)
            max_ms = max(durations_ms)
            self.stdout.write(f"{size:>10} | {build_ms:>12.2f} | {avg_ms:>16.2f} | {max_ms:>16.2f}")

        self.stdout.write(
            "\nExpected shape: search time should grow much slower than pool-size-squared "
            "(a Cartesian-product O(M*N) implementation would roughly 12x from 1000->3000; "
            "this is O(M log N + N log N), so the actual ratio should be far smaller). "
            "No fixed pass/fail threshold is asserted here -- see the final report's "
            "complexity discussion for interpretation."
        )
