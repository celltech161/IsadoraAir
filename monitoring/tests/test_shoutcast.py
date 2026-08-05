"""monitoring/services/shoutcast.py tests -- the shared Shoutcast v2
/statistics fetcher used by both MonitorManager's listener poll and
probe_encoder_group. No live server required -- urllib is mocked."""
import socket
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from monitoring.services.shoutcast import STATS_TIMEOUT_SECONDS, fetch_shoutcast_stats

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

    def test_default_timeout_is_actually_passed_to_urlopen(self):
        """Item 5 of the 2026-08-05 pre-deploy review: this is what
        actually bounds both the connect and every individual blocking
        read on the underlying socket (urllib/http.client apply a single
        socket.settimeout(timeout) that stays in effect for the
        connection's lifetime) -- a monitoring cycle must never be able
        to hang indefinitely on an unresponsive Shoutcast server. Confirm
        the module's own STATS_TIMEOUT_SECONDS constant is what actually
        reaches urlopen(), not just documented as intent."""
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", return_value=_fake_urlopen(REAL_SAMPLE_XML)) as mock_urlopen:
            fetch_shoutcast_stats("192.168.1.112", 8000)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs.get("timeout"), STATS_TIMEOUT_SECONDS)

    def test_explicit_timeout_override_is_passed_through(self):
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", return_value=_fake_urlopen(REAL_SAMPLE_XML)) as mock_urlopen:
            fetch_shoutcast_stats("192.168.1.112", 8000, timeout=1.5)
        _, kwargs = mock_urlopen.call_args
        self.assertEqual(kwargs.get("timeout"), 1.5)

    def test_socket_timeout_returns_empty_dict_not_raised(self):
        """A real connect/read timeout must degrade exactly like any
        other transport failure (empty dict -> every configured SID
        reads as "down"/absent to a caller) -- never propagate and never
        hang the caller waiting on something the try/except doesn't
        actually catch."""
        with patch("monitoring.services.shoutcast.urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            stats = fetch_shoutcast_stats("192.168.1.112", 8000)
        self.assertEqual(stats, {})
