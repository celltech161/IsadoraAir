"""Roadmap 2.23 Phase 2b -- the Start Next endpoint, its two authorization
layers (GroupAccess reachability + playout.start_next capability with the
real schedule policy), CSRF, the command it queues, the PlayoutTransportConfig
Admin/audit, the Eject occurrence binding, and the 2.23 migrations.

enqueue_engine_command is mocked: these tests prove what reaches the engine
queue, never the real /run/isadoraair IPC.
"""

import datetime as dt
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from authz.models import Capability, GroupRole, Role, RoleCapability, ScheduleAccessConfig, TalentAssignment
from isadoraair.engine_commands import EngineCommandQueueFull
from library.models import GroupAccess, PlayoutTransportConfig

User = get_user_model()
URL = "/api/engine/start-next/"


LIVE_A = {"slot": "A", "log_item_id": 7, "generation": 3}


def _body(**overrides):
    body = {
        "client_request_id": "c1-click-0001",
        "engine_instance": "engine-1",
        "expected": {"live_decks": [LIVE_A]},
    }
    body.update(overrides)
    return body


@override_settings(SECURE_SSL_REDIRECT=False)
class StartNextEndpointTests(TestCase):
    def setUp(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        self.remote_host = User.objects.create_user("sn_remote_host", password="pw")
        self.remote_host.groups.add(Group.objects.get(name="remote_dj"))
        self.contributor = User.objects.create_user("sn_contributor", password="pw")
        contributor_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor.groups.add(contributor_group)
        self.operator = User.objects.create_user("sn_operator", password="pw")
        operators = Group.objects.create(name="sn_operators")
        GroupRole.objects.create(group=operators, role=Role.objects.get(name="Station Administrator"))
        GroupAccess.objects.create(group=operators, allowed_exact="/", allowed_prefixes="/api/engine/")
        self.operator.groups.add(operators)
        self.plain = User.objects.create_user("sn_plain", password="pw")
        self.staff = User.objects.create_user("sn_staff", password="pw", is_staff=True)
        patcher = patch("library.views.enqueue_engine_command")
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, user=None, body=None, client=None, **extra):
        client = client or self.client
        if user is not None:
            client.force_login(user)
        return client.post(URL, data=body if body is not None else _body(),
                           content_type="application/json", **extra)

    def queued(self):
        self.enqueue.assert_called_once()
        return self.enqueue.call_args.args[0]

    # ---- who may ----------------------------------------------------------

    def test_anonymous_is_refused_and_nothing_is_queued(self):
        response = self.post()
        self.assertIn(response.status_code, (302, 401, 403))
        self.enqueue.assert_not_called()

    def test_contributor_and_roleless_users_are_refused(self):
        for user in (self.contributor, self.plain):
            with self.subTest(user=user.username):
                self.assertEqual(self.post(user).status_code, 403)
        self.enqueue.assert_not_called()

    def test_remote_host_is_allowed_through_both_layers(self):
        response = self.post(self.remote_host)
        self.assertEqual(response.status_code, 202, response.content)
        self.assertEqual(self.queued()["command"], "start_next")

    def test_station_administrator_and_staff_are_allowed(self):
        for user in (self.operator, self.staff):
            with self.subTest(user=user.username):
                self.enqueue.reset_mock()
                self.assertEqual(self.post(user).status_code, 202)
                self.enqueue.assert_called_once()

    def test_remote_host_reachability_comes_only_from_the_exact_groupaccess_path(self):
        access = GroupAccess.objects.get(group__name="remote_dj")
        self.assertIn(URL, access.allowed_exact.splitlines())
        access.allowed_exact = "\n".join(
            line for line in access.allowed_exact.splitlines() if line.strip() != URL)
        access.save()
        response = self.post(self.remote_host)
        self.assertEqual(response.status_code, 403)
        self.enqueue.assert_not_called()

    def test_remote_host_capability_layer_is_independent_of_reachability(self):
        RoleCapability.objects.filter(role__name="Remote Host",
                                      capability__slug="playout.start_next").delete()
        self.assertEqual(self.post(self.remote_host).status_code, 403)
        self.enqueue.assert_not_called()

    def test_start_next_does_not_grant_deck_transport_seek_or_studio_mic(self):
        self.client.force_login(self.remote_host)
        for url, body in (
            ("/api/engine/deck/A/", {"action": "eject"}),
            ("/api/engine/deck/A/", {"action": "pause"}),
            ("/api/engine/seek/", {"position": 10.0}),
            ("/api/engine/mic-ptt/", {"active": True}),
        ):
            with self.subTest(url=url, body=body):
                response = self.client.post(url, data=body, content_type="application/json")
                self.assertEqual(response.status_code, 403)
        self.enqueue.assert_not_called()

    # ---- schedule policy -------------------------------------------------

    def _enforce(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = True
        cfg.pre_schedule_allowance_minutes = 0
        cfg.post_schedule_allowance_minutes = 0
        cfg.save()

    def test_enforced_schedule_refuses_a_remote_host_outside_any_assignment(self):
        self._enforce()
        self.assertEqual(self.post(self.remote_host).status_code, 403)
        self.enqueue.assert_not_called()

    def test_enforced_schedule_allows_a_remote_host_inside_an_assignment(self):
        self._enforce()
        for dow in range(7):
            TalentAssignment.objects.create(user=self.remote_host, day_of_week=dow,
                                            start_time=dt.time(0, 0), end_time=dt.time(23, 59, 59))
        self.assertEqual(self.post(self.remote_host).status_code, 202)

    def test_the_capability_row_is_schedule_restricted(self):
        self.assertTrue(Capability.objects.get(slug="playout.start_next").requires_schedule)

    # ---- CSRF ------------------------------------------------------------

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.remote_host)
        response = client.post(URL, data=_body(), content_type="application/json", secure=True)
        self.assertEqual(response.status_code, 403)
        self.enqueue.assert_not_called()
        client.get("/remote-dj/", secure=True)
        token = client.cookies["csrftoken"].value
        # HTTPS CSRF also requires a same-origin Referer, as browsers send.
        response = client.post(URL, data=_body(), content_type="application/json",
                               secure=True, HTTP_X_CSRFTOKEN=token,
                               HTTP_REFERER="https://testserver/remote-dj/")
        self.assertEqual(response.status_code, 202)

    # ---- request/command contract ---------------------------------------

    def test_the_command_carries_identity_freshness_target_and_saved_settings(self):
        config = PlayoutTransportConfig.load()
        config.start_next_fade_ms = 3500
        config.start_next_releases_manual = True
        config.save()
        before = time.time()
        live_b = {"slot": "B", "log_item_id": 9, "generation": 4}
        response = self.post(self.remote_host, _body(expected={"live_decks": [live_b, LIVE_A]}))
        self.assertEqual(response.status_code, 202)
        reply = response.json()
        command = self.queued()
        self.assertEqual(reply["request_id"], command["request_id"])
        self.assertRegex(command["request_id"], r"^[0-9a-f]{32}$")
        self.assertIs(reply["executed"], False)
        self.assertEqual(command["client_request_id"], "c1-click-0001")
        self.assertEqual(command["engine_instance"], "engine-1")
        self.assertEqual(command["expected"], {"live_decks": [LIVE_A, live_b]})   # sorted by slot
        self.assertEqual((command["fade_ms"], command["release_manual"]), (3500, True))
        self.assertEqual(command["user_id"], self.remote_host.pk)
        self.assertLessEqual(before, command["issued_at"])
        self.assertLessEqual(command["issued_at"], time.time())

    def test_each_press_gets_its_own_request_id(self):
        self.post(self.remote_host, _body(client_request_id="press-0000000a"))
        self.post(self.remote_host, _body(client_request_id="press-0000000b"))
        ids = {call.args[0]["request_id"] for call in self.enqueue.call_args_list}
        self.assertEqual(len(ids), 2)

    def test_a_retried_press_is_the_same_engine_request(self):
        # Two POSTs of one press (e.g. a retry after a lost response) queue the
        # SAME request id, which the engine executes at most once.
        first = self.post(self.remote_host).json()
        second = self.post(self.remote_host).json()
        self.assertEqual(first["request_id"], second["request_id"])
        ids = [call.args[0]["request_id"] for call in self.enqueue.call_args_list]
        self.assertEqual(ids, [first["request_id"]] * 2)

    def test_the_same_client_id_from_another_user_is_another_request(self):
        mine = self.post(self.remote_host).json()["request_id"]
        theirs = self.post(self.operator).json()["request_id"]
        self.assertNotEqual(mine, theirs)

    def test_settings_default_when_the_singleton_is_missing(self):
        PlayoutTransportConfig.objects.all().delete()
        self.post(self.operator)
        command = self.queued()
        self.assertEqual((command["fade_ms"], command["release_manual"]), (2000, False))

    def test_every_binding_is_mandatory_and_nothing_is_queued_without_it(self):
        for field in ("client_request_id", "engine_instance", "expected"):
            with self.subTest(missing=field):
                body = _body()
                del body[field]
                self.assertEqual(self.post(self.operator, body).status_code, 400)
        self.assertEqual(self.post(self.operator, {}).status_code, 400)
        self.enqueue.assert_not_called()

    def test_null_empty_and_malformed_bindings_are_refused_before_queueing(self):
        for body in (
            _body(client_request_id=None), _body(client_request_id=""), _body(client_request_id="short"),
            _body(client_request_id="has space here"), _body(client_request_id="x" * 65),
            _body(client_request_id=5), _body(engine_instance=None), _body(engine_instance=""),
            _body(engine_instance=" padded"), _body(engine_instance=5), _body(engine_instance="e" * 65),
            _body(expected=None), _body(expected={}), _body(expected=[]),
            _body(expected={"live_log_item_ids": [7]}),
            _body(expected={"live_decks": [{"slot": "A", "log_item_id": 7}]}),
            _body(expected={"live_decks": [{"slot": "C", "log_item_id": 7, "generation": 1}]}),
            _body(expected={"live_decks": [{**LIVE_A, "log_item_id": 0}]}),
            _body(expected={"live_decks": [{**LIVE_A, "generation": True}]}),
            _body(expected={"live_decks": [LIVE_A, {**LIVE_A, "log_item_id": 8}]}),
            _body(expected={"live_decks": [LIVE_A] * 3}),
            _body(expected={"live_decks": [LIVE_A], "extra": 1}),
            _body(expected={"live_decks": "A"}), ["not", "object"],
        ):
            with self.subTest(body=body):
                self.assertEqual(self.post(self.operator, body).status_code, 400)
        response = self.client.post(URL, data="{", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.enqueue.assert_not_called()

    def test_an_explicit_empty_live_set_is_a_legitimate_stopped_console(self):
        self.assertEqual(self.post(self.operator, _body(expected={"live_decks": []})).status_code, 202)
        self.assertEqual(self.queued()["expected"], {"live_decks": []})

    def test_get_is_not_allowed(self):
        self.client.force_login(self.operator)
        self.assertEqual(self.client.get(URL).status_code, 405)

    def test_a_full_engine_queue_is_a_truthful_503(self):
        self.enqueue.side_effect = EngineCommandQueueFull("full")
        response = self.post(self.operator)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("request_id", response.json())


@override_settings(SECURE_SSL_REDIRECT=False)
class EjectOccurrenceBindingEndpointTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("sn_eject_staff", password="pw", is_staff=True)
        self.client.force_login(self.staff)
        patcher = patch("library.views.enqueue_engine_command")
        self.enqueue = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body):
        return self.client.post("/api/engine/deck/A/", data=body, content_type="application/json")

    def test_expected_occurrence_is_forwarded_for_eject(self):
        self.assertEqual(self.post({"action": "eject", "expected_log_item_id": 41}).status_code, 200)
        self.assertEqual(self.enqueue.call_args.args[0],
                         {"command": "deck_eject", "slot": "A", "expected_log_item_id": 41})

    def test_eject_without_expectation_keeps_the_r0116_payload(self):
        self.post({"action": "eject"})
        self.assertEqual(self.enqueue.call_args.args[0], {"command": "deck_eject", "slot": "A"})

    def test_malformed_expectation_is_refused(self):
        for value in (0, -1, "41", True, 4.5):
            with self.subTest(value=value):
                self.assertEqual(
                    self.post({"action": "eject", "expected_log_item_id": value}).status_code, 400)
        self.enqueue.assert_not_called()

    def test_pause_ignores_the_field(self):
        self.post({"action": "pause", "expected_log_item_id": 41})
        self.assertEqual(self.enqueue.call_args.args[0], {"command": "deck_pause", "slot": "A"})


@override_settings(SECURE_SSL_REDIRECT=False)
class PlayoutTransportConfigTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser("sn_admin", "a@example.invalid", "pw")
        self.client.force_login(self.superuser)

    def test_defaults_match_d1_and_d8(self):
        config = PlayoutTransportConfig.load()
        self.assertEqual((config.pk, config.start_next_fade_ms, config.start_next_releases_manual),
                         (1, 2000, False))

    def test_bounds_are_validated(self):
        config = PlayoutTransportConfig.load()
        for value in (249, 10001):
            config.start_next_fade_ms = value
            with self.assertRaises(ValidationError):
                config.full_clean()
        for value in (250, 10000):
            config.start_next_fade_ms = value
            config.full_clean()

    def test_singleton_save_always_targets_pk_1(self):
        PlayoutTransportConfig(start_next_fade_ms=3000).save()
        self.assertEqual(list(PlayoutTransportConfig.objects.values_list("pk", "start_next_fade_ms")),
                         [(1, 3000)])

    def test_admin_changelist_redirects_to_the_singleton_and_cannot_delete(self):
        response = self.client.get("/admin/library/playouttransportconfig/")
        self.assertRedirects(response, "/admin/library/playouttransportconfig/1/change/",
                             fetch_redirect_response=False)
        page = self.client.get("/admin/library/playouttransportconfig/1/change/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "next Start Next press")
        self.assertNotContains(page, "deletelink")

    @patch("library.admin.emit_config_change_event")
    def test_admin_save_is_audited_with_its_apply_modes(self, emit):
        PlayoutTransportConfig.load()
        response = self.client.post("/admin/library/playouttransportconfig/1/change/", {
            "start_next_fade_ms": "3000", "start_next_releases_manual": "on",
        })
        self.assertEqual(response.status_code, 302)
        kwargs = emit.call_args.kwargs
        self.assertEqual(kwargs["object_type"], "library.PlayoutTransportConfig")
        self.assertEqual(set(kwargs["changed_fields"]),
                         {"start_next_fade_ms", "start_next_releases_manual"})
        self.assertEqual(kwargs["apply_modes"], {
            "start_next_fade_ms": "next_start_next_command",
            "start_next_releases_manual": "next_start_next_command",
        })
        self.assertFalse(kwargs["restart_required"])

    def test_admin_rejects_out_of_bounds_values(self):
        PlayoutTransportConfig.load()
        response = self.client.post("/admin/library/playouttransportconfig/1/change/",
                                    {"start_next_fade_ms": "100"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(PlayoutTransportConfig.load().start_next_fade_ms, 2000)


class StartNextSeedDataTests(TestCase):
    """authz.0009 / library.0092 as applied by the test database's migrate."""

    def test_capability_is_granted_only_to_station_administrator_and_remote_host(self):
        holders = set(RoleCapability.objects.filter(capability__slug="playout.start_next")
                      .values_list("role__name", flat=True))
        self.assertEqual(holders, {"Station Administrator", "Remote Host"})

    def test_remote_host_still_lacks_deck_transport_and_studio_mic(self):
        remote_host = set(RoleCapability.objects.filter(role__name="Remote Host")
                          .values_list("capability__slug", flat=True))
        self.assertTrue({"playout.control", "studio.mic_ptt"}.isdisjoint(remote_host))

    def test_remote_dj_reachability_adds_only_the_start_next_path(self):
        access = GroupAccess.objects.get(group__name="remote_dj")
        lines = [line.strip() for line in access.allowed_exact.splitlines() if line.strip()]
        self.assertEqual(lines.count(URL), 1)
        for forbidden in ("/api/engine/deck/", "/api/engine/seek/", "/api/engine/mic-ptt/"):
            self.assertFalse(any(line.startswith(forbidden) for line in lines))
            self.assertFalse(any(forbidden.startswith(prefix.strip())
                                 for prefix in access.allowed_prefixes.splitlines()
                                 if prefix.strip() and prefix.strip() != "/"))


class StartNextMigrationReversibilityTests(TransactionTestCase):
    """Both data migrations are idempotent and reverse cleanly."""

    def test_authz_0009_and_library_0092_round_trip(self):
        from django.core.management import call_command
        # Earlier TransactionTestCases flush migration-seeded rows; establish
        # exactly the preconditions the two data migrations act on.
        for name in ("Station Administrator", "Remote Host"):
            Role.objects.get_or_create(name=name)
        dj, _ = Group.objects.get_or_create(name="remote_dj")
        GroupAccess.objects.get_or_create(group=dj, defaults={"allowed_exact": "/remote-dj/"})
        try:
            call_command("migrate", "authz", "0008", verbosity=0)
            self.assertFalse(Capability.objects.filter(slug="playout.start_next").exists())
            call_command("migrate", "library", "0091", verbosity=0)
            access = GroupAccess.objects.get(group__name="remote_dj")
            self.assertNotIn(URL, access.allowed_exact.splitlines())
            call_command("migrate", verbosity=0)
            call_command("migrate", verbosity=0)   # idempotent re-run
            self.assertEqual(
                set(RoleCapability.objects.filter(capability__slug="playout.start_next")
                    .values_list("role__name", flat=True)),
                {"Station Administrator", "Remote Host"})
            access = GroupAccess.objects.get(group__name="remote_dj")
            self.assertEqual(access.allowed_exact.splitlines().count(URL), 1)
        finally:
            call_command("migrate", verbosity=0)
