"""r0137: the single canonical WxAlert/wx_alert.flac through the REAL publish_weather_asset.

* the file is always replaced atomically (a new inode) and nothing else accumulates;
* while a publication is in progress, the engine's shared deck-open lock cannot be taken;
* a failed publication rolls back to bytes with the SAME recorded identity, so the coordinator's state keeps
  authorizing the previous, still-valid announcement.
"""
import os
from datetime import timedelta

from django.utils import timezone

from isadoraair import wx_alert_state
from library.models import Category, CategoryKind, Track
from weather.publication import PublicationError, destination_lock_path, publish_weather_asset

from .test_publication import PublicationTestCase


class WxAlertSingleFilePublicationTests(PublicationTestCase):
    def setUp(self):
        super().setUp()
        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        Category.objects.get_or_create(code="WxAlert", defaults={"name": "WxAlert", "kind": kind})
        self.dest = wx_alert_state.audio_path(self.library_root.resolve())

    def publish(self, content, *, succeed=True, during_analysis=None):
        candidate = self.make_candidate("candidate.flac", content)
        real = self._patch_analyze(succeed=succeed)
        with real as analyze:
            if during_analysis is not None:
                inner = analyze.side_effect

                def observing(*args, **kwargs):
                    during_analysis()
                    return inner(*args, **kwargs)
                analyze.side_effect = observing
            return publish_weather_asset(candidate, "WxAlert", "wx_alert.flac", producer="wx_alert.py",
                                         alert_family="combined")

    def test_repeated_publication_reuses_one_file_and_one_track(self):
        inodes = []
        for i in range(3):
            self.publish(f"fLaC generation {i}".encode())
            inodes.append(os.stat(self.dest).st_ino)
        self.assertEqual(len(set(inodes)), 3)                                  # atomic replace each time
        self.assertEqual(Track.objects.filter(filepath=str(self.dest)).count(), 1)
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()),
                         [".wx_alert.flac.publish.lock", "wx_alert.flac"])

    def test_the_engine_cannot_take_the_deck_open_lock_while_a_publication_is_in_progress(self):
        observed = []

        def probe():
            with wx_alert_state.shared_publication_lock(destination_lock_path(self.dest)) as held:
                observed.append(held)
        self.publish(b"fLaC first")
        self.publish(b"fLaC second", during_analysis=probe)
        self.assertEqual(observed, [False])
        with wx_alert_state.shared_publication_lock(destination_lock_path(self.dest)) as held:
            self.assertTrue(held)

    def test_a_failed_publication_restores_audio_with_the_recorded_identity(self):
        self.publish(b"fLaC accepted generation")
        identity = wx_alert_state.audio_identity(self.dest)
        state = {"schema": 1, "generation": 4, "active": True,
                 "valid_until": wx_alert_state.format_ts(timezone.now() + timedelta(hours=1)),
                 "published_at": wx_alert_state.format_ts(timezone.now()), "audio": identity}
        with self.assertRaises(PublicationError):
            self.publish(b"fLaC rejected candidate, different length", succeed=False)
        self.assertEqual(self.dest.read_bytes(), b"fLaC accepted generation")
        self.assertEqual(wx_alert_state.audio_identity(self.dest), identity)
        decision = wx_alert_state.playback_decision(state, wx_alert_state.audio_identity(self.dest), timezone.now())
        self.assertTrue(decision.allowed, decision.reason)
