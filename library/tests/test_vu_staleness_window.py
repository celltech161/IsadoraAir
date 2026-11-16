"""Console VU meters: the staleness window (2026-11-16 field report).

Both meters judge a level sample stale by comparing the VIEWER's clock with the engine's timestamp. A KOGR operator PC
running ~0.84 s ahead of the NTP-synced station (Windows time sync, round trip ~0.02 s) saw a permanently blank
Program Level meter under the old 0.75 s window while /api/engine/levels/ answered 200 with fresh data. The window is
now one shared constant of 1.0 s.
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TEMPLATE = Path(__file__).resolve().parents[1] / "templates/library/dashboard.html"


def _function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}\n", start)]


class VuStalenessWindowTests(SimpleTestCase):
    def setUp(self):
        self.source = TEMPLATE.read_text()

    def test_one_shared_one_second_window(self):
        self.assertEqual(re.findall(r"const VU_STALE_SECONDS = ([0-9.]+);", self.source), ["1.0"])
        self.assertLess(self.source.index("const VU_STALE_SECONDS"), self.source.index("async function pollLevels()"))

    def test_program_level_uses_the_window(self):
        body = _function(self.source, "async function pollLevels()")
        self.assertIn("const stale = !data.ts || (now - data.ts) > VU_STALE_SECONDS;", body)
        self.assertIn("if (stale) { vuRenderSilence(); renderRemoteDjVu(null); return; }", body)

    def test_remote_mic_meter_uses_the_same_window(self):
        body = _function(self.source, "function renderRemoteDjVu(remoteDj)")
        self.assertIn("if (!remoteDj.ts || (now - remoteDj.ts) > VU_STALE_SECONDS) {", body)

    def test_no_other_hard_coded_level_staleness_remains(self):
        self.assertEqual(re.findall(r"\(now - [A-Za-z.]*ts\) > [0-9.]+", self.source), [])
