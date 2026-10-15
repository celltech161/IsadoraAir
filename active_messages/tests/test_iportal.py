"""iPortal D3B: the producer's Active Messages workspace through the SHARED
recorder (adapter ``active-message``; subject = one exact message UUID) and
the producer's Messages page -- authorization per exact message, Save ≠
Publish, optimistic revision under the row lock, producer recall/discard,
and nothing airs (the native-airing gate stays closed)."""
import json
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import Group
from django.test import Client, TestCase, override_settings

from active_messages import services
from active_messages.iportal import ADAPTER_KEY, NATIVE_AIRING_CLOSED, ActiveMessageAdapter
from active_messages.models import ActiveBroadcastMessage, ActiveBroadcastMessageEvent
from library.models import Category, CategoryKind, GroupAccess
from produced_content.models import ProducedContentTypeAssignment
from production.models import ProductionMedia
from production.recorder import registry
from production.tests.support import fixture

from .support import Domain, assign, content_type, media, user_with_role

M = ActiveBroadcastMessage
PAGE = "/iportal/messages/"
RECORDER = "/iportal/messages/record/"
API = "/iportal/messages/api/recorder/"


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceCase(Domain, TestCase):
    def setUp(self):
        super().setUp()
        # The station grants its producer groups the iPortal (GroupAccess is the
        # site-wide allowlist; staff bypass it).
        for group in Group.objects.filter(name__startswith="test-"):
            GroupAccess.objects.update_or_create(group=group, defaults={"allowed_prefixes": "/iportal/"})
        self.pa = content_type("town_notice", "public_address")                  # self-publish + recall (defaults)
        self.reviewed = content_type("board_notice", "public_address", producer_self_publish=False)
        self.urgent = content_type("school_urgent", "urgent", recallable=False)
        self.disabled = content_type("old_notice", "public_address", enabled=False)
        kind, _ = CategoryKind.objects.get_or_create(code="spot", defaults={"name": "Spot"})
        self.regular = content_type("news_item", "regular", output_mode="accumulating",
                                    target_category=Category.objects.create(code="AMNEWS", name="news", kind=kind))
        self.test_type = content_type("test_audio", "test")
        assign(self.producer, self.pa, self.reviewed, self.urgent, self.disabled, self.regular, self.test_type)
        assign(self.producer2, self.pa)
        self.unassigned = user_with_role("producer3", "Test Producer")
        self.clients = {}

    CSRF = "d3bTestCsrfSecret0123456789abcde"                 # a well-formed 32-character secret

    def client_for(self, user):
        """A logged-in client that enforces CSRF (cookie + matching header)."""
        if user.pk not in self.clients:
            client = Client(enforce_csrf_checks=True)
            client.force_login(user)
            client.cookies[settings.CSRF_COOKIE_NAME] = self.CSRF
            self.clients[user.pk] = client
        return self.clients[user.pk]

    def post(self, user, url, data=None, **extra):
        return self.client_for(user).post(url, data or {}, HTTP_X_CSRFTOKEN=self.CSRF, **extra)

    def post_json(self, user, url, data):
        return self.client_for(user).post(url, json.dumps(data), content_type="application/json",
                                          HTTP_X_CSRFTOKEN=self.CSRF)

    def take(self, user, message, *, mode="record", extra=""):
        return self.client_for(user).generic("POST", f"{API}take/?message={message.pk}&mode={mode}{extra}",
                                             fixture("wav16_mono.wav"), content_type="audio/wav",
                                             HTTP_X_CSRFTOKEN=self.CSRF)

    def context(self, user, message):
        return self.client_for(user).get(f"{API}context/?message={message.pk}")

    def draft(self, user, content_type_, title=""):
        return services.prepare_message(actor=user, content_type=content_type_, title=title)

    def save(self, user, message, media_id, revision):
        return self.post_json(user, f"{API}commit/", {"subject": {"message": str(message.pk)}, "media_id": media_id,
                                                      "revision": revision})

    def recorded(self, user, message):
        response = self.take(user, message)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["media"]["media_id"]

    def saved_draft(self, user, content_type_):
        message = self.draft(user, content_type_)
        revision = self.context(user, message).json()["context"]["revision"]
        self.assertEqual(self.save(user, message, self.recorded(user, message), revision).status_code, 200)
        return M.objects.get(pk=message.pk)


class RecorderIntegrationTests(WorkspaceCase):
    def test_the_adapter_is_registered_and_listed_only_for_producers_of_active_types(self):
        self.assertIsInstance(registry.get(ADAPTER_KEY), ActiveMessageAdapter)
        for user, shown in ((self.producer, True), (self.controller, True), (self.unassigned, False),
                            (self.host, False)):
            with self.subTest(user=user.username):
                page = self.client_for(user).get("/iportal/")
                (self.assertContains if shown else self.assertNotContains)(page, "Urgent &amp; Public Address")
                if shown:
                    self.assertContains(page, PAGE)                               # the tool's entry URL

    def test_the_subject_is_one_exact_message_uuid(self):
        message = self.draft(self.producer, self.pa, "Water main")
        page = self.client_for(self.producer).get(f"{RECORDER}?message={message.pk}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "town_notice")
        context = self.context(self.producer, message).json()["context"]
        self.assertEqual(context["subject"], {"message": str(message.pk)})
        for bad in ("", "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(bad=bad):
                self.assertEqual(self.client_for(self.producer).get(f"{API}context/?message={bad}").status_code, 404)
        # Nothing resolves "the producer's current message" or a kind.
        self.assertEqual(self.client_for(self.producer).get(f"{API}context/?kind=public_address").status_code, 404)

    def test_the_context_explains_the_draft_and_the_dormant_gate(self):
        message = self.draft(self.producer, self.reviewed, "Board meeting")
        context = self.context(self.producer, message).json()["context"]
        display = dict(context["display"])
        self.assertEqual((display["Content type"], display["Class"], display["Producer"], display["Title"],
                          display["State"]), ("board_notice (board_notice)", "Public address", "producer",
                                              "Board meeting", "Prepared"))
        self.assertIn("operator approval", display["Producer self-publish"])
        self.assertEqual(display["Producer recall"], "yes")
        self.assertEqual(display["Maximum length"], "5 min")
        self.assertIn("station default", display["Replay interval"])
        self.assertEqual(display["Native airing"], NATIVE_AIRING_CLOSED)
        self.assertIn("does not publish", context["purpose"])
        self.assertIn(NATIVE_AIRING_CLOSED, context["purpose"])
        self.assertEqual((context["terms"]["badge"], context["terms"]["save"]), ("DRAFT AUDIO", "Save audio to this draft"))
        self.assertIn("awaits operator approval", context["terms"]["saved"])
        self.assertEqual(context["terms"]["editor_hint"],
                         "browser only; saving updates this draft but does not publish it")
        self.assertEqual(context["terms"]["restored"],
                         "Draft restored. Saving updates this draft; publishing is a separate step.")
        page = self.client_for(self.producer).get(f"{RECORDER}?message={message.pk}")
        self.assertContains(page, "saving updates this draft but does not publish it")
        for misleading in ("on air until you save", "Save &amp; put on air", "Edit the on-air take"):
            self.assertNotContains(page, misleading)
        self.assertEqual(context["return_url"], PAGE)
        self.assertNotIn("remove", context["allowed_operations"])
        self.assertTrue({"record", "import", "save"} <= set(context["allowed_operations"]))

    def test_a_prepared_draft_records_and_saves_but_saving_never_publishes(self):
        message = self.draft(self.producer, self.pa)
        context = self.context(self.producer, message).json()["context"]
        take = self.recorded(self.producer, message)
        self.assertEqual(ProductionMedia.objects.get(pk=take).owner_id, self.producer.pk)
        self.assertIsNone(M.objects.get(pk=message.pk).message_media_id)          # a take never binds by itself
        saved = self.save(self.producer, message, take, context["revision"])
        self.assertEqual(saved.status_code, 200, saved.content)
        stored = M.objects.get(pk=message.pk)
        self.assertEqual((stored.state, str(stored.message_media_id)), ("prepared", take))   # Save ≠ Publish
        self.assertNotEqual(saved.json()["context"]["revision"], context["revision"])
        self.assertEqual(saved.json()["context"]["current"]["media_id"], take)
        # Edit derives from the saved take; import works the same way.
        edited = self.take(self.producer, message, mode="edit", extra=f"&derived_from={take}&operations=[]")
        self.assertEqual(edited.status_code, 201, edited.content)
        self.assertEqual(self.take(self.producer, message, mode="import").status_code, 201)
        self.assertEqual(self.client_for(self.producer).get(f"{API}source/?message={message.pk}").status_code, 200)

    def test_another_producers_message_is_not_disclosed(self):
        theirs = self.saved_draft(self.producer2, self.pa)                          # same content type, other producer
        for url in (f"{RECORDER}?message={theirs.pk}", f"{API}context/?message={theirs.pk}",
                    f"{API}source/?message={theirs.pk}",
                    f"{API}media/{theirs.message_media_id}/?message={theirs.pk}"):
            with self.subTest(url=url):
                self.assertEqual(self.client_for(self.producer).get(url).status_code, 404)
        rows = ProductionMedia.objects.count()
        self.assertEqual(self.take(self.producer, theirs).status_code, 404)
        self.assertEqual(ProductionMedia.objects.count(), rows)                    # refused before intake
        mine = self.recorded(self.producer, self.draft(self.producer, self.pa))
        self.assertEqual(self.save(self.producer, theirs, mine, services.message_revision(theirs)).status_code, 404)
        self.assertEqual(M.objects.get(pk=theirs.pk).message_media_id, theirs.message_media_id)

    def test_control_may_open_any_message_and_preview_its_bound_audio(self):
        theirs = self.saved_draft(self.producer, self.urgent)
        self.assertEqual(self.client_for(self.controller).get(f"{RECORDER}?message={theirs.pk}").status_code, 200)
        preview = self.client_for(self.controller).get(f"{API}media/{theirs.message_media_id}/?message={theirs.pk}")
        self.assertEqual(preview.status_code, 200)
        self.assertNotIn("storage", preview.get("Content-Disposition", ""))
        # ...but not the producer's other, unbound takes.
        unbound = self.recorded(self.producer, theirs)
        self.assertEqual(self.client_for(self.controller).get(f"{API}media/{unbound}/?message={theirs.pk}")
                         .status_code, 404)

    def test_an_active_message_is_read_only(self):
        message = self.saved_draft(self.producer, self.pa)
        revision = services.message_revision(message)
        services.activate_message(message, actor=self.producer)
        context = self.context(self.producer, message).json()["context"]
        self.assertEqual(sorted(context["allowed_operations"]), ["export", "open"])
        self.assertEqual(context["terms"]["restored"], "Draft restored for preview only; this message is read-only.")
        self.assertNotIn("save", context["terms"]["editor_hint"])
        self.assertIn("prepare a replacement", context["blocked_reason"])
        rows = ProductionMedia.objects.count()
        for mode in ("record", "import"):
            self.assertEqual(self.take(self.producer, message, mode=mode).status_code, 403)
        self.assertEqual(ProductionMedia.objects.count(), rows)
        other = self.recorded(self.producer, self.draft(self.producer, self.pa))
        self.assertEqual(self.save(self.producer, message, other, revision).status_code, 403)
        self.assertEqual(M.objects.get(pk=message.pk).message_media_id, message.message_media_id)
        listen = self.client_for(self.producer).get(f"{API}media/{message.message_media_id}/?message={message.pk}")
        self.assertEqual(listen.status_code, 200)                                  # owner may preview
        terminal = services.clear_message(message, actor=self.producer)
        self.assertEqual(self.client_for(self.producer).get(f"{RECORDER}?message={terminal.pk}").status_code, 200)
        self.assertEqual(self.take(self.producer, terminal).status_code, 403)

    def test_a_producer_who_lost_the_assignment_sees_the_draft_read_only(self):
        message = self.draft(self.producer, self.pa)
        ProducedContentTypeAssignment.objects.filter(user=self.producer, content_type=self.pa).delete()
        context = self.context(self.producer, message).json()["context"]
        self.assertIn("no longer assigned", context["blocked_reason"])
        self.assertEqual(self.take(self.producer, message).status_code, 403)

    def test_regular_and_test_types_cannot_enter_the_workflow(self):
        for content_type_ in (self.regular, self.test_type):
            with self.subTest(type=content_type_.key):
                before = M.objects.count()
                response = self.post(self.producer, f"{PAGE}start/", {"content_type": content_type_.key})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(M.objects.count(), before)

    def test_the_media_policy_is_the_shared_one_at_the_current_limit(self):
        message = self.draft(self.producer, self.pa)
        services.set_limits(actor=self.admin, max_message_seconds=2, max_aggregate_seconds=600)
        policy = registry.get(ADAPTER_KEY).media_policy(registry.get(ADAPTER_KEY).resolve({"message": message.pk}))
        self.assertEqual((policy.max_duration_seconds, policy.require_engine_decode), (2.0, True))


class RevisionTests(WorkspaceCase):
    def test_a_stale_second_editor_gets_409_and_the_first_binding_stands(self):
        message = self.draft(self.producer, self.pa)
        first_tab = self.context(self.producer, message).json()["context"]["revision"]
        second_tab = self.context(self.producer, message).json()["context"]["revision"]
        first_take, second_take = self.recorded(self.producer, message), self.recorded(self.producer, message)
        self.assertEqual(self.save(self.producer, message, first_take, first_tab).status_code, 200)
        stale = self.save(self.producer, message, second_take, second_tab)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"], "stale_revision")
        self.assertEqual(str(M.objects.get(pk=message.pk).message_media_id), first_take)

    def test_the_revision_is_checked_under_the_row_lock_inside_the_service(self):
        message = self.draft(self.producer, self.pa)
        stale = services.message_revision(message)
        services.bind_message_media(message, media(self.producer), actor=self.producer)
        with self.assertRaises(services.RevisionConflict):
            services.bind_message_media(message, media(self.producer), actor=self.producer, expected_revision=stale)
        current = services.message_revision(message)
        bound = media(self.producer)
        services.bind_message_media(message, bound, actor=self.producer, expected_revision=current)
        self.assertEqual(M.objects.get(pk=message.pk).message_media_id, bound.pk)

    def test_rebinding_earlier_media_still_changes_the_revision(self):
        message = self.draft(self.producer, self.pa)
        a, b = media(self.producer), media(self.producer)
        services.bind_message_media(message, a, actor=self.producer)
        first = services.message_revision(message)
        services.bind_message_media(message, b, actor=self.producer)
        services.bind_message_media(message, a, actor=self.producer)
        self.assertNotEqual(services.message_revision(message), first)            # A-B-A is not "unchanged"

    def test_a_save_racing_activation_never_overwrites_active_media(self):
        message = self.saved_draft(self.producer, self.pa)
        adapter = registry.get(ADAPTER_KEY)
        subject = adapter.resolve({"message": message.pk})                          # resolved while PREPARED
        revision = services.message_revision(message)
        take = ProductionMedia.objects.get(pk=self.recorded(self.producer, message))
        services.activate_message(message, actor=self.admin)                       # activation wins the race
        from production.recorder.contracts import Conflict
        with self.assertRaises(Conflict):
            adapter.commit(self.producer, subject, take, revision)
        with self.assertRaises(services.InvalidTransition):
            services.bind_message_media(message, take, actor=self.admin)           # even control, no revision
        stored = M.objects.get(pk=message.pk)
        self.assertEqual((stored.state, stored.message_media_id), ("active", message.message_media_id))


class MessagesPageTests(WorkspaceCase):
    def page(self, user):
        response = self.client_for(user).get(PAGE)
        self.assertEqual(response.status_code, 200)
        return response

    def keys(self, response):
        import re
        return set(re.findall(r'data-content-type="([^"]+)"', response.content.decode()))

    def test_a_producer_sees_only_assigned_enabled_pa_and_urgent_types(self):
        # (The shared fixture also assigns both producers to the two compatibility types.)
        self.assertEqual(self.keys(self.page(self.producer)),
                         {"town_notice", "board_notice", "school_urgent", "urgent_pa", "public_address"})
        self.assertEqual(self.keys(self.page(self.producer2)), {"town_notice", "urgent_pa", "public_address"})
        self.assertEqual(self.keys(self.page(self.unassigned)), set())          # regular/test/disabled never shown

    def test_control_sees_every_enabled_pa_and_urgent_type(self):
        self.assertEqual(self.keys(self.page(self.controller)),
                         {"town_notice", "board_notice", "school_urgent", "urgent_pa", "public_address"})

    def test_starting_a_draft_goes_through_the_services_and_opens_the_recorder(self):
        response = self.post(self.producer, f"{PAGE}start/", {"content_type": "town_notice", "title": "Road work"})
        message = M.objects.get(producer=self.producer, content_type=self.pa)
        self.assertRedirects(response, f"{RECORDER}?message={message.pk}", fetch_redirect_response=False)
        self.assertEqual((message.state, message.title, message.kind), ("prepared", "Road work", "public_address"))
        self.assertEqual(list(ActiveBroadcastMessageEvent.objects.filter(message=message)
                              .values_list("event", flat=True)), ["prepared"])
        self.assertContains(self.page(self.producer), f"{RECORDER}?message={message.pk}")     # Continue
        self.assertContains(self.page(self.producer), "Continue")

    def test_several_drafts_are_all_shown(self):
        first, second = self.draft(self.producer, self.pa, "One"), self.draft(self.producer, self.pa, "Two")
        page = self.page(self.producer)
        for message in (first, second):
            self.assertContains(page, f'data-message="{message.pk}"')
        self.assertContains(page, "2 drafts of this type")

    def test_self_publish_on_publishes_the_exact_draft(self):
        message = self.saved_draft(self.producer, self.pa)
        self.assertContains(self.page(self.producer), f"/iportal/messages/{message.pk}/publish/")
        self.assertEqual(self.post(self.producer, f"{PAGE}{message.pk}/publish/").status_code, 302)
        self.assertEqual(M.objects.get(pk=message.pk).state, "active")
        replacement = self.saved_draft(self.producer, self.pa)
        self.assertContains(self.page(self.producer), "Publish replacement")
        self.post(self.producer, f"{PAGE}{replacement.pk}/publish/")
        self.assertEqual((M.objects.get(pk=message.pk).state, M.objects.get(pk=replacement.pk).state),
                         ("cleared", "active"))

    def test_self_publish_off_awaits_operator_approval(self):
        message = self.saved_draft(self.producer, self.reviewed)
        page = self.page(self.producer)
        self.assertContains(page, "Awaiting operator approval")
        self.assertNotContains(page, f"/iportal/messages/{message.pk}/publish/")
        self.post(self.producer, f"{PAGE}{message.pk}/publish/")                   # a crafted request
        self.assertEqual(M.objects.get(pk=message.pk).state, "prepared")
        self.post(self.controller, f"{PAGE}{message.pk}/publish/")
        self.assertEqual(M.objects.get(pk=message.pk).state, "active")              # the operator approves

    def test_a_producer_cannot_publish_or_touch_someone_elses_draft(self):
        theirs = self.saved_draft(self.producer2, self.pa)
        for action in ("publish", "discard"):
            with self.subTest(action=action):
                self.assertEqual(self.post(self.producer, f"{PAGE}{theirs.pk}/{action}/").status_code, 404)
        self.assertEqual(self.client_for(self.producer).get(f"{PAGE}{theirs.pk}/discard/").status_code, 404)
        self.assertEqual(M.objects.get(pk=theirs.pk).state, "prepared")

    def test_the_owner_recalls_a_recallable_active_message_after_confirming(self):
        message = self.saved_draft(self.producer, self.pa)
        services.activate_message(message, actor=self.producer)
        self.assertContains(self.page(self.producer), f"/iportal/messages/{message.pk}/recall/")
        confirm = self.client_for(self.producer).get(f"{PAGE}{message.pk}/recall/")
        self.assertContains(confirm, "town_notice")
        self.assertContains(confirm, "Recall this message?")
        self.assertEqual(M.objects.get(pk=message.pk).state, "active")              # GET changes nothing
        self.post(self.producer, f"{PAGE}{message.pk}/recall/")
        stored = M.objects.get(pk=message.pk)
        self.assertEqual((stored.state, stored.terminal_by_id), ("cleared", self.producer.pk))

    def test_a_non_recallable_active_message_cannot_be_recalled_by_its_owner(self):
        message = self.saved_draft(self.producer, self.urgent)
        services.activate_message(message, actor=self.producer)
        self.assertNotContains(self.page(self.producer), f"/iportal/messages/{message.pk}/recall/")
        self.post(self.producer, f"{PAGE}{message.pk}/recall/")
        self.assertEqual(M.objects.get(pk=message.pk).state, "active")

    def test_discard_closes_exactly_that_draft_and_never_an_activated_one(self):
        keep, drop = self.draft(self.producer, self.pa, "Keep"), self.draft(self.producer, self.pa, "Drop")
        self.post(self.producer, f"{PAGE}{drop.pk}/discard/")
        self.assertEqual((M.objects.get(pk=drop.pk).state, M.objects.get(pk=keep.pk).state), ("cleared", "prepared"))
        live = self.saved_draft(self.producer, self.pa)
        services.activate_message(live, actor=self.producer)                       # activated after the page loaded
        self.post(self.producer, f"{PAGE}{live.pk}/discard/")
        self.assertEqual(M.objects.get(pk=live.pk).state, "active")                # a discard is never a recall

    def test_messages_of_types_no_longer_offered_stay_withdrawable(self):
        message = self.saved_draft(self.producer, self.pa)
        services.activate_message(message, actor=self.producer)
        ProducedContentTypeAssignment.objects.filter(user=self.producer, content_type=self.pa).delete()
        page = self.page(self.producer)
        self.assertNotIn("town_notice", self.keys(page))
        self.assertContains(page, f"/iportal/messages/{message.pk}/recall/")
        self.post(self.producer, f"{PAGE}{message.pk}/recall/")
        self.assertEqual(M.objects.get(pk=message.pk).state, "cleared")

    def test_an_active_message_shows_its_frozen_cadence_beside_the_types_current_setting(self):
        timed = content_type("timed_notice", "public_address", replay_interval_seconds=300)
        assign(self.producer, timed)
        message = self.saved_draft(self.producer, timed)
        services.activate_message(message, actor=self.producer)
        timed.replay_interval_seconds = 900                                         # reconfigured afterwards
        timed.save()
        page = self.page(self.producer)
        section = page.content.decode().split('data-content-type="timed_notice"')[1].split("</section>")[0]
        self.assertIn("New publishes: replay every 900 s (this content type)", section)
        self.assertIn("replay every 300 s (frozen at activation)", section)
        self.assertEqual(M.objects.get(pk=message.pk).replay_interval_seconds, 300)  # cadence itself untouched

    def test_a_policy_default_cadence_is_frozen_the_same_way(self):
        default = services.current_policy().default_replay_interval_seconds
        message = self.saved_draft(self.producer, self.pa)                         # no type override
        services.activate_message(message, actor=self.producer)
        services.set_default_replay_interval(default + 600, actor=self.admin)        # the station default moves
        page = self.page(self.producer)
        section = page.content.decode().split('data-content-type="town_notice"')[1].split("</section>")[0]
        self.assertIn(f"New publishes: replay every {default + 600} s (station default)", section)
        self.assertIn(f"replay every {default} s (frozen at activation)", section)

    def test_csrf_and_authentication_are_enforced(self):
        message = self.draft(self.producer, self.pa)
        no_token = self.client_for(self.producer).post(f"{PAGE}{message.pk}/discard/")
        self.assertEqual(no_token.status_code, 403)
        self.assertEqual(M.objects.get(pk=message.pk).state, "prepared")
        anonymous = Client()
        self.assertEqual(anonymous.get(PAGE).status_code, 302)
        self.assertIn(anonymous.post(f"{PAGE}start/", {"content_type": "town_notice"}).status_code, (302, 403))
        self.assertEqual(self.client_for(self.host).get(PAGE).status_code, 403)    # no active-message capability

    def test_the_workspace_is_dormant_and_dispatches_nothing(self):
        self.assertFalse(services.NATIVE_AIRING_AVAILABLE)
        self.assertContains(self.page(self.producer), NATIVE_AIRING_CLOSED)
        with mock.patch("isadoraair.engine_commands.enqueue_engine_command",
                        side_effect=AssertionError("no engine command")):
            message = self.saved_draft(self.producer, self.pa)
            self.post(self.producer, f"{PAGE}{message.pk}/publish/")
            self.post(self.producer, f"{PAGE}{message.pk}/recall/")
        self.assertEqual(M.objects.get(pk=message.pk).state, "cleared")
        from active_messages.models import ActiveBroadcastExecution
        self.assertFalse(ActiveBroadcastExecution.objects.exists())                  # no airing was even materialized
