"""Encoder model tests -- primarily shoutcast_sid, the single source of
truth for the mount->SID normalization that used to be duplicated
inline in both encoder_manager.py's _output_block and monitor.py's
listener poll (2026-08-05 hardening)."""
from django.test import TestCase

from encoders.models import Encoder


def make_encoder(**overrides):
    defaults = dict(
        name="test-encoder", enabled=True, protocol="shoutcast2",
        host="192.168.1.112", port=8000, mount="/4", password="secret",
        format="mp3", bitrate_kbps=192,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


class ShoutcastSidTests(TestCase):
    def test_shoutcast2_strips_leading_slash(self):
        self.assertEqual(make_encoder(protocol="shoutcast2", mount="/4").shoutcast_sid, "4")

    def test_shoutcast2_no_leading_slash(self):
        self.assertEqual(make_encoder(protocol="shoutcast2", mount="4").shoutcast_sid, "4")

    def test_shoutcast2_blank_mount_defaults_to_1(self):
        self.assertEqual(make_encoder(protocol="shoutcast2", mount="").shoutcast_sid, "1")

    def test_shoutcast1_always_1_even_with_a_mount_set(self):
        self.assertEqual(make_encoder(protocol="shoutcast1", mount="/9").shoutcast_sid, "1")

    def test_shoutcast1_blank_mount(self):
        self.assertEqual(make_encoder(protocol="shoutcast1", mount="").shoutcast_sid, "1")

    def test_icecast_returns_none(self):
        self.assertIsNone(make_encoder(protocol="icecast", mount="/stream").shoutcast_sid)
