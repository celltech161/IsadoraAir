"""iPortal D3D: the Active Messages Operator Board.

Control-only access; every action on one exact message UUID through the
existing services (publish = activate_message with the reviewed revision,
clear/decline = clear_message with the expected state, expiry =
set_message_expiry with the expected expiry, Replay Now =
request_manual_replay with the page's token); filter/search/page preserved;
ACTIVE is never shown as ON AIR without air evidence; and with the
native-airing gate closed nothing airs, no airing is created and no engine
command is sent."""
import re
import uuid
from datetime import date, datetime, timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone

from active_messages import board as board_module
from active_messages import services
from active_messages.models import (
    ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastMessage, ActiveBroadcastMessageEvent,
    ActiveBroadcastPublication,
)
from produced_content.models import ProducedContentType
from production.models import ProductionMedia

from .support import assign, media, raw, user_with_role
from .test_iportal import API, RECORDER, WorkspaceCase

M = ActiveBroadcastMessage
X = ActiveBroadcastExecution
BOARD = "/iportal/messages/board/"


class BoardCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.staff = user_with_role("staffer", is_staff=True, is_superuser=True)

    def board(self, user=None, **query):
        response = self.client_for(user or self.controller).get(BOARD, query)
        self.assertEqual(response.status_code, 200)
        return response

    def row(self, response, message) -> str:
        """The exact message's row on the board (empty when it is not listed)."""
        html = response.content.decode()
        match = re.search(rf'<li class="amb-item" id="m-{message.pk}".*?</details>\s*</li>', html, re.S)
        return match.group(0) if match else ""

    def markup(self, response) -> str:
        """The page without its script (whose strings are not what is shown)."""
        return re.sub(r"<script>.*?</script>", "", response.content.decode(), flags=re.S)

    def listed(self, response) -> list:
        return re.findall(r'<li class="amb-item" id="m-([0-9a-f-]{36})"', response.content.decode())

    def revision(self, response, message) -> str:
        found = re.search(r'name="revision" value="([0-9a-f]+)"', self.row(response, message))
        return found.group(1) if found else ""

    def follow(self, user, response):
        self.assertEqual(response.status_code, 302, response.content[:500])
        return self.client_for(user).get(response["Location"])

    def flash(self, response) -> str:
        found = re.search(r'<ul class="amb-flash"[^>]*>(.*?)</ul>', response.content.decode(), re.S)
        return re.sub(r"\s+", " ", found.group(1)) if found else ""

    def ready(self, producer, content_type_, *, title="", seconds="30.000000"):
        """A PREPARED draft with bound audio (rows only; no bytes)."""
        message = services.prepare_message(actor=producer, content_type=content_type_, title=title)
        return services.bind_message_media(message, media(producer, seconds=seconds), actor=producer)

    def live(self, producer, content_type_, *, title="", seconds="30.000000"):
        return services.activate_message(self.ready(producer, content_type_, title=title, seconds=seconds),
                                         actor=self.admin)

    def publish(self, user, message, revision, back=""):
        return self.post(user, f"{BOARD}{message.pk}/publish/", {"revision": revision, "return": back})


class BoardAccessTests(BoardCase):
    def pages(self, message, execution):
        return [BOARD, f"{BOARD}status/", f"{BOARD}{message.pk}/", f"{BOARD}airings/{execution.pk}/",
                f"{BOARD}{message.pk}/clear/", f"{BOARD}{message.pk}/decline/"]

    def test_only_control_opens_the_board_and_its_pages(self):
        message = self.live(self.producer, self.pa)
        execution = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        for url in self.pages(message, execution):
            for user in (self.controller, self.admin, self.staff):
                self.assertIn(self.client_for(user).get(url).status_code, (200, 302), (url, user))
            for user in (self.producer, self.host):
                self.assertEqual(self.client_for(user).get(url).status_code, 403, (url, user))
            nobody = self.client_for(self.nobody).get(url)                          # no group: GroupAccess
            self.assertTrue(nobody.status_code == 403 or nobody["Location"].startswith("/welcome/"), url)
            self.assertEqual(Client().get(url).status_code, 302, url)                 # login first

    def test_every_action_is_post_only_csrf_protected_and_control_only(self):
        draft = self.ready(self.producer, self.reviewed)
        active = self.live(self.producer, self.pa)
        actions = {f"{BOARD}{draft.pk}/publish/": {"revision": services.message_revision(draft)},
                   f"{BOARD}{draft.pk}/decline/": {}, f"{BOARD}{active.pk}/clear/": {},
                   f"{BOARD}{active.pk}/expiry/": {"remove": "1"},
                   f"{BOARD}replay/": {"request_token": str(uuid.uuid4())}}
        for url in (f"{BOARD}{draft.pk}/publish/", f"{BOARD}{active.pk}/expiry/", f"{BOARD}replay/"):
            self.assertEqual(self.client_for(self.controller).get(url).status_code, 405, url)
        for url, data in actions.items():
            self.assertEqual(self.client_for(self.controller).post(url, data).status_code, 403, url)   # no CSRF token
            self.assertEqual(self.post(self.producer, url, data).status_code, 403, url)                # not control
            self.assertEqual(self.post(self.host, url, data).status_code, 403, url)
        self.assertEqual(M.objects.get(pk=draft.pk).state, "prepared")
        self.assertEqual(M.objects.get(pk=active.pk).state, "active")
        self.assertFalse(X.objects.exists())

    def test_the_hub_offers_the_board_once_and_only_to_control(self):
        hub = self.client_for(self.controller).get("/iportal/").content.decode()
        self.assertEqual(hub.count(f'href="{BOARD}"'), 1)
        self.assertEqual(hub.count("Urgent &amp; Public Address Messages"), 1)       # still one entry
        producer_hub = self.client_for(self.producer).get("/iportal/").content.decode()
        self.assertIn("Urgent &amp; Public Address Messages", producer_hub)
        self.assertNotIn(BOARD, producer_hub)
        self.assertContains(self.client_for(self.controller).get("/iportal/messages/"), f'href="{BOARD}"')
        self.assertNotContains(self.client_for(self.producer).get("/iportal/messages/"), BOARD)

    def test_unknown_messages_and_airings_are_404(self):
        for url in (f"{BOARD}{uuid.uuid4()}/", f"{BOARD}airings/{uuid.uuid4()}/", f"{BOARD}{uuid.uuid4()}/clear/"):
            self.assertEqual(self.client_for(self.controller).get(url).status_code, 404, url)


class ReviewAndPublishTests(BoardCase):
    def test_cross_producer_review_shows_identity_and_previews_the_exact_bound_audio(self):
        draft = self.saved_draft(self.producer, self.reviewed)                   # real bytes, another producer
        page = self.board()
        row = self.row(page, draft)
        for text in ("board_notice", "producer", "REVIEW", "needs an operator", str(draft.pk), "Publish</button>"):
            self.assertIn(text, row)
        preview = re.search(r'<audio[^>]+src="([^"]+)"', row).group(1).replace("&amp;", "&")
        self.assertEqual(preview, f"{API}media/{draft.message_media_id}/?message={draft.pk}")
        response = self.client_for(self.controller).get(preview)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("never reaches the station output", row)

    def test_the_operator_publishes_the_exact_reviewed_draft(self):
        draft = self.ready(self.producer, self.reviewed, title="Board meeting")
        page = self.board(filter="review", q="Board")
        response = self.publish(self.controller, draft, self.revision(page, draft), back="filter=review&q=Board")
        self.assertEqual(response["Location"], f"{BOARD}?filter=review&q=Board&open={draft.pk}#m-{draft.pk}")
        stored = M.objects.get(pk=draft.pk)
        self.assertEqual((stored.state, stored.activated_by_id), ("active", self.controller.pk))
        notice = self.flash(self.follow(self.controller, response))
        self.assertIn("Published", notice)
        self.assertIn("does not air: native airing is closed", notice)
        self.assertEqual(self.events(stored)[-1], "activated")

    def test_audio_saved_after_the_review_is_never_published_unreviewed(self):
        draft = self.ready(self.producer, self.reviewed)
        reviewed = self.revision(self.board(), draft)
        newer = media(self.producer)
        services.bind_message_media(draft, newer, actor=self.producer)            # the producer saves again
        response = self.publish(self.controller, draft, reviewed)
        self.assertIn("changed since you reviewed it", self.flash(self.follow(self.controller, response)))
        stored = M.objects.get(pk=draft.pk)
        self.assertEqual((stored.state, stored.message_media_id), ("prepared", newer.pk))
        # ...and a publication without a reviewed revision is refused too.
        self.publish(self.controller, draft, "")
        self.assertEqual(M.objects.get(pk=draft.pk).state, "prepared")
        self.publish(self.controller, draft, self.revision(self.board(), draft))
        self.assertEqual(M.objects.get(pk=draft.pk).state, "active")

    def test_a_repeated_or_concurrent_publication_is_reported_not_repeated(self):
        draft = self.ready(self.producer, self.reviewed)
        revision = self.revision(self.board(), draft)
        self.publish(self.admin, draft, revision)                                 # another operator was first
        response = self.publish(self.controller, draft, revision)
        self.assertIn("Already active", self.flash(self.follow(self.controller, response)))
        stored = M.objects.get(pk=draft.pk)
        self.assertEqual(stored.activated_by_id, self.admin.pk)
        self.assertEqual(self.events(stored).count("activated"), 1)

    def test_a_replacement_supersedes_only_the_same_producer_and_exact_type(self):
        old = self.live(self.producer, self.pa, title="Old notice")
        other_type = self.live(self.producer, self.reviewed, title="Other type, same class")
        other_producer = self.live(self.producer2, self.pa, title="Someone else's")
        replacement = self.ready(self.producer, self.pa, title="New notice")
        page = self.board()
        self.assertIn("Publish replacement", self.row(page, replacement))
        self.assertIn("publishing clears “Old notice”", self.row(page, replacement))
        response = self.publish(self.controller, replacement, self.revision(page, replacement))
        self.assertIn("replaces “Old notice”", self.flash(self.follow(self.controller, response)))
        states = dict(M.objects.values_list("id", "state"))
        self.assertEqual(states[old.pk], "cleared")
        self.assertEqual(M.objects.get(pk=old.pk).superseded_by_id, replacement.pk)
        self.assertEqual((states[other_type.pk], states[other_producer.pk], states[replacement.pk]),
                         ("active", "active", "active"))

    def test_missing_and_invalid_audio_cannot_be_published(self):
        empty = services.prepare_message(actor=self.producer, content_type=self.reviewed, title="No audio yet")
        broken = self.ready(self.producer, self.reviewed, title="Broken audio")
        raw(ProductionMedia).filter(pk=broken.message_media_id).update(validation_state="invalid")
        self.assertNotIn(str(empty.pk), self.listed(self.board()))                   # current: drafts with audio
        page = self.board(filter="review")
        self.assertEqual(self.listed(page)[-1], str(empty.pk))                       # work in progress sorts last
        for message, text in ((empty, "No audio"), (broken, "Audio not valid")):
            row = self.row(page, message)
            self.assertIn(f"Cannot publish: {text}", row)
            self.assertNotIn('data-action="publish"', row)
            self.publish(self.controller, message, services.message_revision(message))
            self.assertEqual(M.objects.get(pk=message.pk).state, "prepared")
        self.assertNotIn("<audio", self.row(page, empty))
        self.assertNotIn("<audio", self.row(page, broken))

    def test_test_media_is_flagged_and_never_publishable(self):
        audio = board_module._audio(media(self.producer, airing_prohibited=True), services.current_policy())
        self.assertEqual((audio["ok"], audio["code"]), (False, "test_media"))

    def test_a_disabled_type_refuses_publication(self):
        draft = self.ready(self.producer, self.reviewed)
        revision = services.message_revision(draft)
        ProducedContentType.objects.filter(pk=self.reviewed.pk).update(enabled=False)
        row = self.row(self.board(), draft)
        self.assertIn("is disabled", row)
        self.assertNotIn('data-action="publish"', row)
        response = self.publish(self.controller, draft, revision)
        self.assertIn("is disabled", self.flash(self.follow(self.controller, response)))
        self.assertEqual(M.objects.get(pk=draft.pk).state, "prepared")

    def test_a_composition_limit_refusal_is_reported(self):
        self.live(self.producer, self.pa)
        services.set_limits(actor=self.admin, max_active_messages=1)
        draft = self.ready(self.producer2, self.pa)
        response = self.publish(self.controller, draft, services.message_revision(draft))
        notice = self.flash(self.follow(self.controller, response))
        self.assertIn("Not published", notice)
        self.assertIn("the limit is 1", notice)
        self.assertEqual(M.objects.get(pk=draft.pk).state, "prepared")


class ClearAndDeclineTests(BoardCase):
    def test_clear_confirms_the_exact_message_and_producer_first(self):
        message = self.live(self.producer, self.urgent, title="Boil order")
        confirm = self.client_for(self.controller).get(f"{BOARD}{message.pk}/clear/?filter=active")
        for text in ("Boil order", "producer", "school_urgent", str(message.pk), "never cut",
                     'name="return" value="filter=active"'):
            self.assertContains(confirm, text)
        self.assertEqual(M.objects.get(pk=message.pk).state, "active")              # GET changed nothing
        response = self.post(self.controller, f"{BOARD}{message.pk}/clear/", {"reason": "all clear",
                                                                              "return": "filter=active"})
        self.assertTrue(response["Location"].startswith(f"{BOARD}?filter=active&open={message.pk}"))
        stored = M.objects.get(pk=message.pk)
        self.assertEqual((stored.state, stored.terminal_reason, stored.terminal_by_id),
                         ("cleared", "all clear", self.controller.pk))
        self.assertEqual(self.events(stored)[-1], "cleared")

    def test_a_stale_clear_never_touches_the_newer_replacement(self):
        old = self.live(self.producer, self.pa, title="Old")
        self.client_for(self.controller).get(f"{BOARD}{old.pk}/clear/")           # the operator's page
        replacement = self.live(self.producer, self.pa, title="New")              # meanwhile, a replacement
        response = self.post(self.controller, f"{BOARD}{old.pk}/clear/")
        self.assertIn("Already cleared", self.flash(self.follow(self.controller, response)))
        self.assertEqual(M.objects.get(pk=replacement.pk).state, "active")
        self.assertEqual(M.objects.get(pk=old.pk).terminal_reason, "superseded")

    def test_decline_closes_exactly_that_draft_and_never_one_activated_meanwhile(self):
        declined = self.ready(self.producer, self.reviewed, title="Declined")
        self.assertContains(self.client_for(self.controller).get(f"{BOARD}{declined.pk}/decline/"),
                            "can never be published")
        self.post(self.controller, f"{BOARD}{declined.pk}/decline/", {"reason": "duplicate"})
        stored = M.objects.get(pk=declined.pk)
        self.assertEqual((stored.state, stored.terminal_reason), ("cleared", "duplicate"))
        self.assertEqual(self.events(stored)[-1], "discarded")
        raced = self.ready(self.producer, self.pa)
        services.activate_message(raced, actor=self.producer)                     # self-published meanwhile
        response = self.post(self.controller, f"{BOARD}{raced.pk}/decline/")
        self.assertIn("not prepared", self.flash(self.follow(self.controller, response)))
        self.assertEqual(M.objects.get(pk=raced.pk).state, "active")
        # A stale confirmation link explains instead of offering the wrong action.
        stale = self.client_for(self.controller).get(f"{BOARD}{raced.pk}/decline/")
        self.assertEqual(stale.status_code, 302)


class ExpiryTests(BoardCase):
    def local_input(self, when) -> str:
        return timezone.localtime(when).strftime("%Y-%m-%dT%H:%M")

    def set(self, message, value, expected="", **extra):
        return self.post(self.controller, f"{BOARD}{message.pk}/expiry/",
                         {"expires_at": value, "expected": expected, **extra})

    def test_set_change_and_remove_in_station_time(self):
        message = self.live(self.producer, self.pa)
        target = timezone.localtime(timezone.now() + timedelta(days=2)).replace(second=0, microsecond=0)
        self.set(message, self.local_input(target))
        stored = M.objects.get(pk=message.pk)
        self.assertEqual(stored.expires_at, target)
        page = self.board()
        self.assertIn(f'value="{self.local_input(target)}"', self.row(page, message))
        later = target + timedelta(hours=3)
        self.set(message, self.local_input(later), expected=stored.expires_at.isoformat())
        self.assertEqual(M.objects.get(pk=message.pk).expires_at, later)
        self.set(message, "", expected=later.isoformat(), remove="1")
        self.assertIsNone(M.objects.get(pk=message.pk).expires_at)
        self.assertEqual(self.events(stored).count("expiry_set"), 3)

    def test_a_stale_expiry_form_never_overwrites_a_newer_change(self):
        message = self.live(self.producer, self.pa)
        newer = timezone.now() + timedelta(days=1)
        services.set_message_expiry(message, newer, actor=self.admin)             # another operator, meanwhile
        response = self.set(message, self.local_input(newer + timedelta(days=1)), expected="")
        self.assertIn("changed it since this page was loaded", self.flash(self.follow(self.controller, response)))
        self.assertEqual(M.objects.get(pk=message.pk).expires_at, newer)

    def test_invalid_times_are_refused_never_guessed(self):
        message = self.live(self.producer, self.pa)
        year = timezone.localdate().year + 1
        march = date(year, 3, 1)
        spring = march + timedelta(days=(6 - march.weekday()) % 7 + 7)            # second Sunday of March
        november = date(year, 11, 1)
        fall = november + timedelta(days=(6 - november.weekday()) % 7)           # first Sunday of November
        cases = {"": "Enter a date and time", "tomorrow": "Enter a date and time",
                 self.local_input(timezone.now() - timedelta(hours=1)): "must be in the future",
                 f"{spring:%Y-%m-%d}T02:30": "does not exist in station time",
                 f"{fall:%Y-%m-%d}T01:30": "happens twice in station time"}
        for value, text in cases.items():
            response = self.set(message, value)
            self.assertIn(text, self.flash(self.follow(self.controller, response)), value)
        self.assertIsNone(M.objects.get(pk=message.pk).expires_at)
        self.assertEqual(self.events(message).count("expiry_set"), 0)

    def test_an_approaching_or_passed_expiry_is_distinct_from_the_expired_state(self):
        now = timezone.now()
        soon = self.live(self.producer, self.pa, title="Soon")
        services.set_message_expiry(soon, now + timedelta(minutes=30), actor=self.admin)
        passed = self.live(self.producer2, self.pa, title="Passed")
        services.set_message_expiry(passed, now + timedelta(minutes=5), actor=self.admin)
        raw(M).filter(pk=passed.pk).update(expires_at=now - timedelta(minutes=1))    # the sweep has not run yet
        page = self.board()
        self.assertRegex(self.row(page, soon), r"Expires in (29|30) min")
        self.assertIn("not yet expired by the sweep", self.row(page, passed))
        self.assertIn("ACTIVE", self.row(page, passed))
        services.expire_due_messages()
        history = self.board(filter="history")
        self.assertIn("EXPIRED", self.row(history, passed))
        self.assertIn("Expired at", self.row(history, passed))
        self.assertNotIn("<form", self.row(history, passed).split('class="amb-actions"')[1].split("</div>")[0])


class FilterAndHistoryTests(BoardCase):
    def test_the_current_view_puts_review_first_and_urgent_before_public_address(self):
        pa_active = self.live(self.producer, self.pa)
        urgent_active = self.live(self.producer, self.urgent)
        pa_review = self.ready(self.producer2, self.pa)
        urgent_review = self.ready(self.producer, self.urgent)
        cleared = self.live(self.producer, self.reviewed)
        services.clear_message(cleared, actor=self.admin)
        self.assertEqual(self.listed(self.board()),
                         [str(urgent_review.pk), str(pa_review.pk), str(urgent_active.pk), str(pa_active.pk)])
        self.assertEqual(self.listed(self.board(filter="active")), [str(urgent_active.pk), str(pa_active.pk)])
        self.assertEqual(self.listed(self.board(filter="history")), [str(cleared.pk)])

    def test_search_matches_title_producer_type_and_exact_id(self):
        water = self.ready(self.producer, self.pa, title="Water main break")
        assign(self.producer2, self.reviewed)
        other = self.ready(self.producer2, self.reviewed, title="Library hours")
        for query, expected in (("water", [water]), ("producer2", [other]), ("board_notice", [other]),
                                (str(water.pk), [water]), ("nothing-like-this", [])):
            self.assertEqual(self.listed(self.board(q=query)), [str(m.pk) for m in expected], query)

    def test_the_return_target_is_whitelisted_and_never_an_external_url(self):
        draft = self.ready(self.producer, self.reviewed)
        response = self.publish(self.controller, draft, services.message_revision(draft),
                                back="filter=bogus&next=https://evil.example/&q=x&page=-4")
        self.assertEqual(response["Location"], f"{BOARD}?q=x&open={draft.pk}#m-{draft.pk}")

    def test_history_is_paginated_newest_first(self):
        made = []
        for index in range(board_module.PAGE_SIZE + 5):
            message = services.prepare_message(actor=self.producer, content_type=self.pa, title=f"old {index}")
            made.append(services.clear_message(message, actor=self.producer))
        first = self.board(filter="history")
        self.assertEqual(len(self.listed(first)), board_module.PAGE_SIZE)
        self.assertEqual(self.listed(first)[0], str(made[-1].pk))
        self.assertContains(first, "Older ›")
        second = self.board(filter="history", page=2)
        self.assertEqual(len(self.listed(second)), 5)
        self.assertEqual(self.listed(second)[-1], str(made[0].pk))
        self.assertEqual(self.listed(self.board(filter="history", page=99)), self.listed(second))

    def test_long_names_are_kept_whole_for_assistive_text(self):
        long_type = ProducedContentType.objects.create(key="long_" + "x" * 50, delivery_mode="public_address",
                                                       name=("A very long content type " * 4).strip())
        assign(self.producer, long_type)
        message = self.ready(self.producer, long_type, title="T" * 200)
        row = self.row(self.board(), message)
        self.assertIn(f'title="{"T" * 200}"', row)
        self.assertIn("A very long content type", row)

    def test_message_detail_has_full_history_and_airings(self):
        message = self.live(self.producer, self.pa)
        services.set_message_expiry(message, timezone.now() + timedelta(days=1), actor=self.admin)
        execution = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        page = self.client_for(self.controller).get(f"{BOARD}{message.pk}/?filter=active")
        for event in ("prepared", "media_bound", "activated", "expiry_set"):
            self.assertContains(page, f'data-event="{event}"')
        self.assertContains(page, f"{BOARD}airings/{execution.pk}/")
        self.assertContains(page, f'href="{BOARD}?filter=active&amp;open={message.pk}#m-{message.pk}"')


class AiringVisibilityTests(BoardCase):
    def test_active_is_not_on_air_without_air_evidence(self):
        message = self.live(self.producer, self.pa)
        page = self.board()
        self.assertNotIn("ON AIR", self.markup(page))
        self.assertNotIn("data-onair", self.markup(page))
        self.assertIn("not airing (native airing closed)", self.row(page, message))
        self.assertIn('data-gate="closed"', page.content.decode())

    def test_on_air_is_shown_only_for_the_components_of_a_started_airing(self):
        aired = self.live(self.producer, self.pa)
        execution = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        self.assertNotIn("ON AIR", self.markup(self.board()))                         # pending render: not air
        self.put_on_air(execution)
        later = self.live(self.producer2, self.pa)                                     # not in that airing
        page = self.board()
        self.assertIn("data-onair", self.row(page, aired))
        self.assertNotIn("data-onair", self.row(page, later))
        self.assertIn("● ON AIR", page.content.decode().split('id="ambAiring"')[1][:200])

    def test_the_airing_page_shows_identity_events_and_the_evidence_distinction(self):
        message = self.live(self.producer, self.urgent, title="Shelter in place")
        execution = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        page = self.client_for(self.controller).get(f"{BOARD}airings/{execution.pk}/")
        for text in (execution.composition_digest, str(execution.pk), "Replay Now", "Shelter in place",
                     f"{BOARD}{message.pk}/", "No air evidence", 'data-event="replay_requested"'):
            self.assertContains(page, text)
        self.put_on_air(execution)
        self.assertContains(self.client_for(self.controller).get(f"{BOARD}airings/{execution.pk}/"), "Air evidence")

    def test_the_status_endpoint_is_display_only_and_reports_changes(self):
        response = self.client_for(self.controller).get(f"{BOARD}status/")
        status = response.json()
        self.assertEqual((status["urgent"], status["public_address"], status["review"], status["gate_open"]),
                         (0, 0, 0, False))
        self.assertEqual(response["Cache-Control"], "no-store")
        self.live(self.producer, self.urgent)
        changed = self.client_for(self.controller).get(f"{BOARD}status/").json()
        self.assertEqual(changed["urgent"], 1)
        self.assertNotEqual(changed["version"], status["version"])
        self.assertEqual(self.post(self.controller, f"{BOARD}status/").status_code, 405)


class ReplayNowClosedTests(BoardCase):
    def test_replay_now_is_unavailable_while_native_airing_is_closed(self):
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)
        self.live(self.producer, self.pa)
        page = self.board()
        button = re.search(r'<button[^>]*id="ambReplay"[^>]*>', page.content.decode()).group(0)
        self.assertIn("disabled", button)
        self.assertIn("native airing is closed", button)
        token = re.search(r'name="request_token" value="([0-9a-f-]{36})"', page.content.decode()).group(1)
        with mock.patch.object(services, "request_manual_replay", side_effect=AssertionError("never called")):
            response = self.post(self.controller, f"{BOARD}replay/", {"request_token": token})
        self.assertIn("Replay Now is unavailable", self.flash(self.follow(self.controller, response)))
        self.assertFalse(X.objects.exists())                                          # no dormant airing
        self.assertFalse(ActiveBroadcastMessageEvent.objects.filter(request_token=token).exists())

    def test_the_board_cannot_open_native_airing(self):
        source = (board_module.__file__ and open(board_module.__file__).read())
        self.assertNotIn("set_native_airing", source)
        self.assertNotIn("NATIVE_AIRING_AVAILABLE =", source)
        message = self.ready(self.producer, self.reviewed)
        with mock.patch.object(services, "set_native_airing", side_effect=AssertionError("never")):
            self.publish(self.controller, message, services.message_revision(message))
            self.post(self.controller, f"{BOARD}replay/", {"request_token": str(uuid.uuid4()),
                                                           "native_airing_enabled": "1", "enabled": "true"})
        self.assertFalse(services.current_policy().native_airing_enabled)
        self.assertIs(services.NATIVE_AIRING_AVAILABLE, False)

    def test_ordinary_browsing_and_actions_dispatch_nothing_to_the_engine(self):
        from library.models import LogItem, PlayEvent, Track
        before = (Track.objects.count(), LogItem.objects.count(), PlayEvent.objects.count())
        with mock.patch("isadoraair.engine_commands.enqueue_engine_command",
                        side_effect=AssertionError("no engine command")):
            draft = self.ready(self.producer, self.reviewed)
            self.publish(self.controller, draft, services.message_revision(draft))
            self.post(self.controller, f"{BOARD}{draft.pk}/expiry/",
                      {"expires_at": timezone.localtime(timezone.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")})
            declined = self.ready(self.producer2, self.pa)
            self.post(self.controller, f"{BOARD}{declined.pk}/decline/")
            self.post(self.controller, f"{BOARD}replay/", {"request_token": str(uuid.uuid4())})
            for query in ({}, {"filter": "active"}, {"filter": "review"}, {"filter": "history"}):
                self.board(**query)
            self.client_for(self.controller).get(f"{BOARD}{draft.pk}/")
            self.client_for(self.controller).get(f"{BOARD}status/")
            self.post(self.controller, f"{BOARD}{draft.pk}/clear/")
        self.assertEqual(M.objects.get(pk=draft.pk).state, "cleared")
        self.assertEqual((Track.objects.count(), LogItem.objects.count(), PlayEvent.objects.count()), before)
        self.assertFalse(X.objects.exists())
        self.assertFalse(ActiveBroadcastPublication.objects.exists())
        self.assertFalse(ActiveBroadcastInsertion.objects.exists())


class ReplayNowGateOpenTests(BoardCase):
    """Replay Now's request/idempotency integration with the gate opened under
    controlled test conditions only (the patch is the cutover Phase G would
    make). The worker never runs here: nothing renders, publishes or airs."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(services, "NATIVE_AIRING_AVAILABLE", True)
        patcher.start()
        self.addCleanup(patcher.stop)
        services.set_native_airing(True, actor=self.admin)

    def token(self, page) -> str:
        return re.search(r'name="request_token" value="([0-9a-f-]{36})"', page.content.decode()).group(1)

    def replay(self, token, user=None):
        user = user or self.controller
        return self.flash(self.follow(user, self.post(user, f"{BOARD}replay/", {"request_token": token})))

    def test_one_request_creates_one_airing_and_a_repeated_submission_resolves_to_it(self):
        self.live(self.producer, self.pa)
        page = self.board()
        self.assertNotIn("disabled", re.search(r'<button[^>]*id="ambReplay"[^>]*>', page.content.decode()).group(0))
        token = self.token(page)
        with mock.patch("isadoraair.engine_commands.enqueue_engine_command",
                        side_effect=AssertionError("no engine command")):
            first = self.replay(token)
            again = self.replay(token)                                       # double click / Back + resubmit
        execution = X.objects.get()
        self.assertEqual((execution.reason, execution.state, execution.requested_by_id),
                         ("manual", "pending_render", self.controller.pk))
        self.assertIn(f"new airing {str(execution.pk)[:8]}", first)
        self.assertIn("not on air until the engine starts it", first)
        self.assertIn("already requested", again)
        self.assertEqual(ActiveBroadcastMessageEvent.objects.filter(request_token=token).count(), 1)
        self.assertNotEqual(self.token(self.board()), token)                   # every page issues a fresh token

    def test_an_unfinished_airing_absorbs_a_new_request(self):
        self.live(self.producer, self.pa)
        services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        notice = self.replay(str(uuid.uuid4()))
        self.assertIn("absorbed by the airing already in progress", notice)
        self.assertEqual(X.objects.count(), 1)
        self.assertIn("replay_coalesced", self.events(execution=X.objects.get()))

    def test_refusals_are_reported_and_create_nothing(self):
        self.assertIn("no message is active", self.replay(str(uuid.uuid4())))
        self.assertFalse(X.objects.exists())
        message = self.live(self.producer, self.pa, seconds="90.000000")
        services.set_limits(actor=self.admin, max_aggregate_seconds=60, max_message_seconds=60)
        self.assertIn("limit", self.replay(str(uuid.uuid4())))
        self.assertFalse(X.objects.exists())
        services.set_limits(actor=self.admin, max_aggregate_seconds=600, max_message_seconds=300)
        execution = services.request_manual_replay(actor=self.admin, request_token=uuid.uuid4())
        self.put_on_air(execution)
        services.set_limits(actor=self.admin, max_active_messages=10)            # composition moves on
        self.assertIn("still with the engine", self.replay(str(uuid.uuid4())))
        self.assertEqual(X.objects.count(), 1)
        token = str(uuid.uuid4())
        services.clear_message(message, actor=self.admin)
        self.replay(token, user=self.admin)
        self.assertIn("Replay Now refused", self.replay(token))                 # another operator's token

    def test_replay_now_still_needs_control(self):
        self.live(self.producer, self.pa)
        self.assertEqual(self.post(self.producer, f"{BOARD}replay/", {"request_token": str(uuid.uuid4())}).status_code,
                         403)
        self.assertFalse(X.objects.exists())


class RecorderFromBoardTests(BoardCase):
    def test_the_recorder_opens_the_exact_message_and_returns_to_the_board(self):
        draft = self.saved_draft(self.producer, self.reviewed)
        row = self.row(self.board(), draft)
        self.assertIn(f"{RECORDER}?message={draft.pk}&amp;from=board", row)
        page = self.client_for(self.controller).get(f"{RECORDER}?message={draft.pk}&from=board")
        self.assertContains(page, f'href="{BOARD}"')
        # A producer cannot borrow the board's return target.
        own = self.client_for(self.producer).get(f"{RECORDER}?message={draft.pk}&from=board")
        self.assertNotContains(own, f'href="{BOARD}"')
        self.assertContains(own, 'href="/iportal/messages/"')


class ServiceGuardTests(BoardCase):
    """The two optional D3D guards, in the services themselves."""

    def test_activation_with_a_stale_revision_changes_nothing(self):
        draft = self.ready(self.producer, self.reviewed)
        stale = services.message_revision(draft)
        services.bind_message_media(draft, media(self.producer), actor=self.producer)
        generation = services.current_policy().generation
        with self.assertRaises(services.RevisionConflict):
            services.activate_message(draft, actor=self.admin, expected_revision=stale)
        self.assertEqual(M.objects.get(pk=draft.pk).state, "prepared")
        self.assertEqual(services.current_policy().generation, generation)
        self.assertNotIn("activated", self.events(draft))
        services.activate_message(draft, actor=self.admin, expected_revision=services.message_revision(draft))
        self.assertEqual(M.objects.get(pk=draft.pk).state, "active")
        # Without the guard the D1-D3C behaviour is unchanged (idempotent).
        self.assertEqual(services.activate_message(draft, actor=self.admin).state, "active")

    def test_expiry_with_a_stale_expected_value_changes_nothing(self):
        message = self.live(self.producer, self.pa)
        first = timezone.now() + timedelta(days=1)
        services.set_message_expiry(message, first, actor=self.admin, expected_expires_at=None)
        with self.assertRaises(services.InvalidTransition) as caught:
            services.set_message_expiry(message, first + timedelta(hours=1), actor=self.admin,
                                        expected_expires_at=None)
        self.assertEqual(caught.exception.code, "expiry_changed")
        self.assertEqual(M.objects.get(pk=message.pk).expires_at, first)
        services.set_message_expiry(message, None, actor=self.admin, expected_expires_at=first)
        self.assertIsNone(M.objects.get(pk=message.pk).expires_at)
        services.set_message_expiry(message, first, actor=self.admin)               # unguarded: as before
        self.assertEqual(M.objects.get(pk=message.pk).expires_at, first)


class LocalTimeTests(BoardCase):
    def test_local_time_is_station_time(self):
        value = board_module._local_time("2031-06-01T12:00")
        self.assertEqual(value.utcoffset(), timedelta(hours=-5))                      # America/Chicago, CDT
        self.assertEqual(value.replace(tzinfo=None), datetime(2031, 6, 1, 12, 0))
