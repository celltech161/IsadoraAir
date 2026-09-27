"""Roadmap 2.5C -- capability enforcement for the real operational
endpoints named in the workorder: Remote DJ token issuance, engine
queue mutation, seek, deck controls, manual/automation mode, Remote DJ
mic gate, studio mic PTT, and FX fire. Also covers remote_dj_page
(page reachability, schedule_policy="ignore") and the corrected Remote
Host capability mapping (playout.queue_manage vs. playout.control --
see authz.migrations.0006).

`enqueue_engine_command` is mocked throughout -- these tests exist to
prove AUTHORIZATION outcomes, not real engine IPC (which would touch
/run/isadoraair/engine_cmd.d on the real filesystem)."""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.urls import reverse

from authz.models import ScheduleAccessConfig

User = get_user_model()


@override_settings(SECURE_SSL_REDIRECT=False)
class EngineEndpointCapabilityTests(TestCase):
    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()

        self.remote_host = User.objects.create_user("engine_2_5c_remote_host", password="pw")
        dj_group, _ = Group.objects.get_or_create(name="remote_dj")
        self.remote_host.groups.add(dj_group)

        self.contributor = User.objects.create_user("engine_2_5c_contributor", password="pw")
        contrib_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contrib_group)

        self.plain = User.objects.create_user("engine_2_5c_plain", password="pw")

        self.staff = User.objects.create_user("engine_2_5c_staff", password="pw", is_staff=True)
        self.su = User.objects.create_superuser("engine_2_5c_su", "su@example.invalid", "pw")

    # ---- queue management (playout.queue_manage): Remote Host YES ----

    def test_set_next_unauthenticated_is_rejected(self):
        resp = self.client.post(reverse("library:api-engine-set-next"), data={"item_id": 1}, content_type="application/json")
        self.assertNotEqual(resp.status_code, 200)

    def test_set_next_denied_for_contributor(self):
        self.client.force_login(self.contributor)
        resp = self.client.post(reverse("library:api-engine-set-next"), data={"item_id": 1}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_set_next_allowed_for_remote_host_under_compatibility(self):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-engine-set-next"), data={"item_id": 1}, content_type="application/json")
        # Not authorization-denied -- may still 400 (no active log in this
        # bare test DB), which is a completely different, expected failure.
        self.assertNotEqual(resp.status_code, 403)

    def test_insert_track_allowed_for_remote_host_under_compatibility(self):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-engine-insert-track"), data={"track_id": 1}, content_type="application/json")
        self.assertNotEqual(resp.status_code, 403)

    def test_insert_track_denied_for_plain_user(self):
        self.client.force_login(self.plain)
        resp = self.client.post(reverse("library:api-engine-insert-track"), data={"track_id": 1}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_playlist_play_now_denied_for_reachable_but_uncapable_user(self):
        """Found during the mandated repository-wide re-sweep: same
        reachability-only shape as the 2.5A defects. A user whose
        GroupAccess reaches this exact regex path (reachability) but
        whose Group isn't bound to any Role (no playout.queue_manage)
        -- proves the capability layer denies it independent of
        GroupAccess, not just "no group reaches this URL at all"."""
        from library.models import GroupAccess, Playlist
        playlist = Playlist.objects.create(name="Sweep Test Playlist")
        reach_only_group = Group.objects.create(name="Play Now Reach Only Group")
        GroupAccess.objects.create(
            group=reach_only_group, allowed_regex=r"^/api/playlists/\d+/play-now/$",
        )
        user = User.objects.create_user("engine_2_5c_play_now_reach_only", password="pw")
        user.groups.add(reach_only_group)

        self.client.force_login(user)
        resp = self.client.post(reverse("library:api-playlist-play-now", args=[playlist.pk]))
        self.assertEqual(resp.status_code, 403)

    def test_playlist_play_now_allowed_for_remote_host_under_compatibility(self):
        from library.models import Playlist
        playlist = Playlist.objects.create(name="Sweep Test Playlist 2")
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-playlist-play-now", args=[playlist.pk]))
        # Not authorization-denied -- 400 (empty playlist in this bare
        # test DB) is the expected, unrelated outcome.
        self.assertNotEqual(resp.status_code, 403)

    # ---- transport control (playout.control): Remote Host NO ----
    # (remote_dj_page's own docstring: "hides ... waveform click-seek"
    # and "deck eject/pause" -- see authz.migrations.0006's docstring
    # for why this is playout.control, distinct from queue_manage)

    @patch("library.views.enqueue_engine_command")
    def test_seek_denied_for_remote_host(self, mock_enqueue):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-engine-seek"), data={"position": 10.0}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    @patch("library.views.enqueue_engine_command")
    def test_seek_allowed_for_staff(self, mock_enqueue):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("library:api-engine-seek"), data={"position": 10.0}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()

    @patch("library.views.enqueue_engine_command")
    def test_deck_command_denied_for_remote_host(self, mock_enqueue):
        self.client.force_login(self.remote_host)
        resp = self.client.post(
            reverse("library:api-engine-deck-command", args=["a"]),
            data={"action": "pause"}, content_type="application/json",
        )
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    @patch("library.views.enqueue_engine_command")
    def test_deck_command_allowed_for_superuser(self, mock_enqueue):
        self.client.force_login(self.su)
        resp = self.client.post(
            reverse("library:api-engine-deck-command", args=["a"]),
            data={"action": "pause"}, content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()

    # ---- studio.mic_ptt: Remote Host NO, operator-only ----

    @patch("library.views.enqueue_engine_command")
    def test_mic_ptt_denied_for_remote_host(self, mock_enqueue):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-engine-mic-ptt"), data={"active": True}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    @patch("library.views.enqueue_engine_command")
    def test_mic_ptt_allowed_for_staff(self, mock_enqueue):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("library:api-engine-mic-ptt"), data={"active": True}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()

    # ---- remote_dj.mic_gate: Remote Host YES (shared control) ----

    @patch("library.views.enqueue_engine_command")
    def test_remote_dj_gate_allowed_for_remote_host(self, mock_enqueue):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-engine-remote-dj-gate"), data={"active": True}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()

    @patch("library.views.enqueue_engine_command")
    def test_remote_dj_gate_denied_for_contributor(self, mock_enqueue):
        self.client.force_login(self.contributor)
        resp = self.client.post(reverse("library:api-engine-remote-dj-gate"), data={"active": True}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    # ---- playout.manual_mode: Remote Host YES ----

    @patch("library.views.enqueue_engine_command")
    def test_manual_mode_allowed_for_remote_host_under_compatibility(self, mock_enqueue):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-engine-manual-mode"), data={"active": True}, content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        mock_enqueue.assert_called_once()

    @patch("library.views.enqueue_engine_command")
    def test_manual_mode_denied_for_plain_user(self, mock_enqueue):
        self.client.force_login(self.plain)
        resp = self.client.post(reverse("library:api-engine-manual-mode"), data={"active": True}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        mock_enqueue.assert_not_called()

    # ---- fx.fire: Remote Host YES ----

    def test_fx_fire_denied_for_contributor(self):
        self.client.force_login(self.contributor)
        resp = self.client.post(reverse("library:api-fx-fire"), data={"cart_id": 1}, content_type="application/json")
        self.assertEqual(resp.status_code, 403)

    def test_fx_fire_allowed_for_remote_host_under_compatibility(self):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-fx-fire"), data={"cart_id": 999999}, content_type="application/json")
        # Not authorization-denied -- 404 (cart doesn't exist in this
        # bare test DB) is the expected, unrelated outcome.
        self.assertNotEqual(resp.status_code, 403)

    # ---- remote_dj.connect scheduled enforcement, at a real endpoint ----

    def test_remote_dj_token_denied_without_capability(self):
        self.client.force_login(self.plain)
        resp = self.client.post(reverse("library:api-remote-dj-token"))
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_token_allowed_for_remote_host_under_compatibility(self):
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-remote-dj-token"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("token", resp.json())

    def test_remote_dj_token_denied_with_enforcement_on_and_no_assignment(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-remote-dj-token"))
        self.assertEqual(resp.status_code, 403)

    def test_remote_dj_token_allowed_with_enforcement_on_and_active_assignment(self):
        import datetime as dt
        from authz.models import TalentAssignment
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.pre_schedule_allowance_minutes = 0
        cfg.post_schedule_allowance_minutes = 0
        cfg.save()
        for dow in range(7):
            TalentAssignment.objects.create(
                user=self.remote_host, day_of_week=dow,
                start_time=dt.time(0, 0), end_time=dt.time(23, 59, 59),
            )
        self.client.force_login(self.remote_host)
        resp = self.client.post(reverse("library:api-remote-dj-token"))
        self.assertEqual(resp.status_code, 200)

    # ---- remote_dj_page: reachability, not the operation itself ----

    def test_remote_dj_page_renders_for_capable_user_even_with_enforcement_on_and_no_assignment(self):
        """schedule_policy='ignore' -- a Remote Host can always see their
        own console/status; the schedule gate lives at token mint, not
        page render (see remote_dj_page's own docstring)."""
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.save()
        self.client.force_login(self.remote_host)
        resp = self.client.get(reverse("library:remote-dj"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"not authorized", resp.content.lower())

    def test_remote_dj_page_shows_unauthorized_fallback_for_a_user_without_the_capability(self):
        """A user whose GroupAccess reaches /remote-dj/ (page
        reachability) but whose Group isn't bound to any Role (no
        remote_dj.connect capability) -- proves the central 2.5
        property directly: reachability != authority, even at page
        render, not just at a mutating endpoint."""
        from library.models import GroupAccess
        reach_only_group = Group.objects.create(name="Reach Only No Role Group")
        GroupAccess.objects.create(group=reach_only_group, allowed_prefixes="/remote-dj/")
        user = User.objects.create_user("engine_2_5c_reach_only", password="pw")
        user.groups.add(reach_only_group)

        self.client.force_login(user)
        resp = self.client.get(reverse("library:remote-dj"))
        self.assertEqual(resp.status_code, 200)
        self.assertTemplateUsed(resp, "library/remote_dj_unauthorized.html")
