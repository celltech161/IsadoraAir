"""monitoring/services/shoutcast.py tests -- the shared Shoutcast v2
/statistics fetcher used by both MonitorManager's listener poll and
probe_encoder_group. No live server required -- urllib is mocked."""
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from monitoring.services.shoutcast import fetch_shoutcast_stats

REAL_SAMPLE_XML = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes" ?><SHOUTCASTSERVER><STREAMSTATS><TOTALSTREAMS>2</TOTALSTREAMS><ACTIVESTREAMS>1</ACTIVESTREAMS><CURRENTLISTENERS>9</CURRENTLISTENERS><STREAM id="1"><CURRENTLISTENERS>8</CURRENTLISTENERS><STREAMSTATUS>0</STREAMSTATUS><STREAMPATH>/1/live</STREAMPATH></STREAM><STREAM id="2"><CURRENTLISTENERS>1</CURRENTLISTENERS><STREAMSTATUS>1</STREAMSTATUS><STREAMPATH>/2/live</STREAMPATH></STREAM></STREAMSTATS></SHOUTCASTSERVER>"""


def _fake_urlopen(body):
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body
    return cm


class FetchShoutcastStatsTests(SimpleTestCase):
    def test_parses_real_captured_wire_format(self):
        """This exact XML shape (STREAM nested inside STREAMSTATS, not
        a root child) is what actually proved the station was down
        during the 2026-08-05 outage -- STREAM id=1's STREAMSTATUS=0
        while the systemd/audio_silence checks both still said 'ok'."""
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", return_value=_fake_urlopen(REAL_SAMPLE_XML)):
            stats = fetch_shoutcast_stats("192.168.1.112", 8000)
        self.assertEqual(stats["1"], {"up": False, "listeners": 8})
        self.assertEqual(stats["2"], {"up": True, "listeners": 1})

    def test_unreachable_server_returns_empty_dict(self):
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", side_effect=OSError("connection refused")):
            stats = fetch_shoutcast_stats("192.168.1.112", 8000)
        self.assertEqual(stats, {})

    def test_malformed_xml_returns_empty_dict(self):
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", return_value=_fake_urlopen(b"not xml at all")):
            stats = fetch_shoutcast_stats("192.168.1.112", 8000)
        self.assertEqual(stats, {})

    def test_stream_with_no_id_attribute_skipped(self):
        xml = b'<SHOUTCASTSERVER><STREAMSTATS><STREAM><STREAMSTATUS>1</STREAMSTATUS></STREAM></STREAMSTATS></SHOUTCASTSERVER>'
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", return_value=_fake_urlopen(xml)):
            stats = fetch_shoutcast_stats("192.168.1.112", 8000)
        self.assertEqual(stats, {})

    def test_missing_listeners_element_defaults_to_zero(self):
        xml = b'<SHOUTCASTSERVER><STREAMSTATS><STREAM id="1"><STREAMSTATUS>1</STREAMSTATUS></STREAM></STREAMSTATS></SHOUTCASTSERVER>'
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", return_value=_fake_urlopen(xml)):
            stats = fetch_shoutcast_stats("192.168.1.112", 8000)
        self.assertEqual(stats["1"], {"up": True, "listeners": 0})
