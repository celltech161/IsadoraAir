"""/library/ list responses are applied newest-request-only (Codex P1, independent review of bdc8b9a).

Two filter changes in quick succession issue two /api/tracks/ requests. Without a guard, a delayed answer for the
EARLIER filter can resolve last and overwrite the Track and unique-Artist totals (and rows) of the CURRENT filter.
loadTracks() now aborts the previous request and ignores any answer whose generation is not the newest. The rendered
browser behaviour is exercised outside the sandbox (evidence: browser/race.py); this module pins the template.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TEMPLATE = Path(__file__).resolve().parents[1] / "templates" / "library" / "library.html"


class LoadTracksResponseOrderTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        source = TEMPLATE.read_text(encoding="utf-8")
        start = source.index("async function loadTracks() {")
        cls.body = source[start:source.index("\n}\n", start)]
        cls.source = source

    def test_every_call_takes_a_new_generation_and_aborts_the_previous_request(self):
        self.assertIn("let tracksRequestGeneration = 0;", self.source)
        self.assertIn("const generation = ++tracksRequestGeneration;", self.body)
        self.assertIn("if (tracksAbort) tracksAbort.abort();", self.body)
        self.assertIn("fetch('/api/tracks/?' + buildQuery(), { signal: controller.signal })", self.body)

    def test_no_page_state_is_written_before_the_generation_is_confirmed_current(self):
        guard = self.body.index("if (generation !== tracksRequestGeneration) return;\n  tracksAbort = null;")
        for write in ("totalPages = data.pages;", "lastTotal = data.total;", "filterSelection.total = data.total",
                      "stats.textContent = libraryCountsLabel(", "body.innerHTML ="):
            with self.subTest(write=write):
                self.assertGreater(self.body.index(write), guard)

    def test_superseded_or_aborted_answers_are_dropped_silently(self):
        self.assertIn("if (generation !== tracksRequestGeneration || !resp.ok) return;", self.body)
        self.assertIn("if (controller.signal.aborted || generation !== tracksRequestGeneration) return;", self.body)

    def test_only_one_list_fetch_site_exists(self):
        self.assertEqual(len(re.findall(r"fetch\('/api/tracks/\?'", self.source)), 1)
