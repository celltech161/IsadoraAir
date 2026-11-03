"""QA helper (not collected by default test discovery): time a reconciliation scan of a large synthetic Library
in the guarded sandbox and print the measurements.

    run_with_validation_service.sh <python> manage.py test library.tests.measure_reconciliation_performance

20,000 Tracks and about 26,000 small media files: 18,000 healthy (2,000 of them in another category's
folder), 1,000 missing, 5,000 unregistered, 500 metadata-duplicate pairs and 50 byte-identical copies.
Files are tiny (the scan reads metadata only, except equal-size files for duplicate hashing).
"""
import json
import resource
import time
import tracemalloc

from django.db import connection
from django.test import TransactionTestCase

from library.models import Track
from library.services import library_reconciliation as reconciliation
from library.tests.test_library_reconciliation_b import _ReconFixture


class MeasureReconciliation(_ReconFixture, TransactionTestCase):
    """Committed like production: a TestCase's one transaction would hold 20,000 path locks at once."""

    TRACKS, MISSING, UNREGISTERED, DRIFT, DUP_PAIRS, COPIES = 20000, 1000, 5000, 2000, 500, 50

    def test_measure(self):
        started = time.perf_counter()
        objs = []
        for i in range(self.TRACKS):
            folder = "POP" if i < self.DRIFT else "ROCK"
            path = self.root / folder / f"t{i:05d}.flac"
            if i >= self.MISSING:
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(i.to_bytes(4, "big") * (64 + i % 997))     # mostly distinct sizes
            title = f"Song {i // 2:05d}" if i < 2 * self.DUP_PAIRS else f"Song {i:05d}"
            objs.append(Track(filepath=str(path), filename=path.name, title=title, artist=self.artist,
                              category=self.rock, format="flac", duration_seconds=180.0 + (i % 2) * 0.5))
        for start in range(0, len(objs), 1000):
            Track.objects.bulk_create(objs[start:start + 1000])
        for i in range(self.UNREGISTERED):
            path = self.root / "INBOX" / f"u{i:05d}.flac"
            path.parent.mkdir(exist_ok=True)
            path.write_bytes((i + 100000).to_bytes(4, "big") * (2000 + i))
        for i in range(self.COPIES):                                        # byte-identical copies
            source = self.root / "ROCK" / f"t{self.DRIFT + i:05d}.flac"
            (self.root / "COPIES").mkdir(exist_ok=True)
            (self.root / "COPIES" / f"copy{i:03d}.flac").write_bytes(source.read_bytes())
        build = time.perf_counter() - started

        tracemalloc.start()
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        queries = {"count": 0, "seconds": 0.0}

        def count(execute, sql, params, many, context):
            t0 = time.perf_counter()
            try:
                return execute(sql, params, many, context)
            finally:
                queries["count"] += 1
                queries["seconds"] += time.perf_counter() - t0

        with connection.execute_wrapper(count):
            started = time.perf_counter()
            report = reconciliation.run_scan()
            scan = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        started = time.perf_counter()
        response = self.client.get("/api/tracks/reconciliation/findings/", {"type": "unregistered", "page": 50})
        first_page = time.perf_counter() - started
        started = time.perf_counter()
        self.client.get("/api/tracks/reconciliation/findings/", {"q": "t00999"})
        search = time.perf_counter() - started
        self.assertEqual(response.status_code, 200)
        size = (reconciliation.report_dir() / f"scan-{report['scan_id']}.json").stat().st_size
        result = {"tracks": self.TRACKS, "files": report["counts"]["files_total"], "build_seconds": round(build, 1),
                  "scan_seconds": round(scan, 2), "scan_timings": report["timings_seconds"],
                  "scan_db_statements": queries["count"], "scan_db_seconds": round(queries["seconds"], 2),
                  "python_peak_traced_mb": round(peak / 1e6, 1),
                  "process_max_rss_mb_before_after": [round(rss_before / 1024, 1), round(rss_after / 1024, 1)],
                  "report_mb": round(size / 1e6, 2), "findings": len(report["findings"]),
                  "hashed_mb": round(report["budgets"]["hashed_bytes"] / 1e6, 2),
                  "findings_api_page_seconds": round(first_page, 3), "findings_api_search_seconds": round(search, 3),
                  "counts": report["counts"]}
        print("\n=====BEGIN-MEASURE=====\n" + json.dumps(result, indent=2) + "\n=====END-MEASURE=====")
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["counts"]["missing"], self.MISSING)
        self.assertEqual(report["counts"]["unregistered"], self.UNREGISTERED + self.COPIES)
