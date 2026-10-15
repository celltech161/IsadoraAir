"""iPortal D3C: the producer's My Content workspace and the REGULAR recorder
(adapter ``produced-content``; subject = one exact SpokenItem UUID).

Producer authority per exact assigned enabled type; a submission is one
DRAFT SpokenItem created through spoken.transitions; the recorder's save IS
the explicit submission (revision under the row lock; submit + validation in
one transaction; never a render or projection in the request); submitted
items are read-only; the owner abandons / withdraws through the existing
spoken lifecycle; another producer's item is never disclosed."""
from unittest import mock

from django.test import TestCase, override_settings

from produced_content import backend, regular
from produced_content.iportal import ADAPTER_KEY, ProducedContentAdapter
from produced_content.models import ProducedContentType, ProducedContentTypeAssignment
from production.recorder import registry
from spoken import transitions
from spoken.models import ItemState, SpokenAsset, SpokenContentType, SpokenItem

from .support import API, PAGE, RECORDER, People, assign, media_row, no_air_type, regular_type

S = ItemState


def no_render(*args, **kwargs):
    raise AssertionError("nothing renders in the web request")


@override_settings(SECURE_SSL_REDIRECT=False)
class WorkspaceCase(People, TestCase):
    def setUp(self):
        super().setUp()
        self.news = regular_type("town_news", output_mode="single", recipe_key="lcl_psa", name="Town News")
        self.psa = regular_type("local_psa", output_mode="accumulating", recipe_key="lcl_psa", name="Local PSA",
                                category_code="PCPSA")
        self.practice = no_air_type("practice", name="Practice")
        self.old = regular_type("old_news", name="Old News", enabled=False)
        self.elsewhere = regular_type("other_news", name="Other News")
        self.notice = ProducedContentType.objects.create(key="town_notice", name="Town Notice",
                                                         delivery_mode="public_address")
        assign(self.producer, self.news, self.psa, self.practice, self.old, self.notice)
        assign(self.producer2, self.news, self.practice)
        assign(self.unassigned, self.notice)                      # a PA producer only

    def start(self, user, content_type, title=""):
        return self.post(user, f"{PAGE}start/", {"content_type": content_type.key, "title": title})

    def draft(self, user=None, content_type=None, title="Lunch menu"):
        response = self.start(user or self.producer, content_type or self.news, title)
        self.assertEqual(response.status_code, 302, response.content)
        return SpokenItem.objects.filter(submitted_by=user or self.producer).order_by("-created_at").first()

    def query(self, item):
        return f"item={item.pk}"

    def revision(self, user, item):
        response = self.context(user, self.query(item))
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["context"]["revision"]

    def submit(self, user, item, media_id, revision):
        return self.commit(user, {"item": str(item.pk)}, media_id, revision)


class ProducerAuthorityTests(WorkspaceCase):
    def test_the_adapter_is_registered_and_listed_only_for_regular_or_test_producers_and_control(self):
        self.assertIsInstance(registry.get(ADAPTER_KEY), ProducedContentAdapter)
        for user, shown in ((self.producer, True), (self.controller, True), (self.unassigned, False),
                            (self.host, False)):
            with self.subTest(user=user.username):
                page = self.client_for(user).get("/iportal/")
                (self.assertContains if shown else self.assertNotContains)(page, PAGE)

    def test_my_content_lists_exactly_the_assigned_enabled_regular_and_test_types(self):
        page = self.client_for(self.producer).get(PAGE)
        self.assertEqual(page.status_code, 200)
        for shown in ("town_news", "local_psa", "practice"):
            self.assertContains(page, f'data-content-type="{shown}"')
        for hidden in ("old_news", "other_news", "town_notice"):              # disabled, unassigned, PA
            self.assertNotContains(page, f'data-content-type="{hidden}"')
        self.assertContains(page, "TEST / NO AIR")
        self.assertContains(page, "PCNEWS")                                   # the target category
        control = self.client_for(self.controller).get(PAGE)
        for shown in ("town_news", "local_psa", "practice", "other_news"):    # every enabled regular/test type
            self.assertContains(control, f'data-content-type="{shown}"')
        self.assertNotContains(control, 'data-content-type="old_news"')
        self.assertNotContains(control, 'data-content-type="town_notice"')

    def test_regular_and_test_types_stay_off_my_messages_and_pa_stays_off_my_content(self):
        messages = self.client_for(self.producer).get("/iportal/messages/")
        self.assertContains(messages, 'data-content-type="town_notice"')
        for key in ("town_news", "local_psa", "practice"):
            self.assertNotContains(messages, f'data-content-type="{key}"')
        self.assertNotContains(self.client_for(self.unassigned).get(PAGE), 'data-content-type="town_notice"')

    def test_no_capability_no_workspace(self):
        self.assertEqual(self.client_for(self.host).get(PAGE).status_code, 403)
        self.assertEqual(self.start(self.host, self.news).status_code, 403)
        self.assertEqual(SpokenItem.objects.count(), 0)

    def test_starting_creates_exactly_one_draft_through_spoken_and_opens_the_recorder(self):
        with mock.patch("production.services.render.render", side_effect=no_render):
            response = self.start(self.producer, self.news, "Lunch menu")
        item = SpokenItem.objects.get()
        self.assertRedirects(response, f"{RECORDER}?item={item.pk}", fetch_redirect_response=False)
        backing = ProducedContentType.objects.get(pk=self.news.pk).spoken_content_type
        self.assertEqual((item.state, item.content_type_id, item.submitted_by_id, item.title),
                         (S.DRAFT, backing.pk, self.producer.pk, "Lunch menu"))
        self.assertEqual(list(item.events.values_list("event", flat=True)), ["created"])
        self.assertEqual(SpokenAsset.objects.count(), 0)                  # no projection, no Track
        self.draft()                                                       # another submission is fine
        self.assertEqual(SpokenItem.objects.filter(state=S.DRAFT).count(), 2)

    def test_unassigned_disabled_and_non_regular_types_refuse_new_production(self):
        for user, content_type in ((self.producer, self.elsewhere), (self.producer, self.old),
                                   (self.unassigned, self.news), (self.producer, self.notice),
                                   (self.producer, self.practice)):
            with self.subTest(user=user.username, type=content_type.key):
                response = self.start(user, content_type)
                self.assertRedirects(response, PAGE, fetch_redirect_response=False)
        self.assertEqual(SpokenItem.objects.count(), 0)

    def test_a_type_that_is_not_runnable_is_shown_unavailable_and_offers_no_new_submission(self):
        broken = regular_type("broken_news", recipe_key="", name="Broken News")
        drifted = regular_type("drifted_news", recipe_key="lcl_psa", name="Drifted News")
        assign(self.producer, broken, drifted)
        backing = backend.ensure_regular_backend(drifted)
        backing.requires_review = True
        backing.save()                                     # drift alone never blocks a producer
        page = self.client_for(self.producer).get(PAGE).content.decode()
        broken_section = page.split('data-content-type="broken_news"')[1].split("</section>")[0]
        drifted_section = page.split('data-content-type="drifted_news"')[1].split("</section>")[0]
        self.assertIn('data-health="bad"', broken_section)
        self.assertIn("no processing recipe", broken_section)
        self.assertNotIn("New submission", broken_section)
        self.assertNotIn('data-health="bad"', drifted_section)
        self.assertIn("New submission", drifted_section)

    def test_control_produces_any_enabled_regular_type_without_an_assignment(self):
        response = self.start(self.controller, self.elsewhere, "Board minutes")
        item = SpokenItem.objects.get(submitted_by=self.controller)
        self.assertRedirects(response, f"{RECORDER}?item={item.pk}", fetch_redirect_response=False)
        # ... and is the owner of what it produces (no spoken manager predicate is involved).
        take = self.recorded(self.controller, self.query(item))
        self.assertEqual(self.submit(self.controller, item, take, self.revision(self.controller, item)).status_code,
                         200)
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.VALIDATED)

    def test_a_disabled_type_refuses_production_even_through_a_stale_page(self):
        draft = self.draft()
        self.news.enabled = False
        self.news.save()
        backend.synchronize(self.news)
        self.assertEqual(self.start(self.producer, self.news).status_code, 302)
        self.assertEqual(SpokenItem.objects.count(), 1)
        context = self.context(self.producer, self.query(draft)).json()["context"]
        self.assertNotIn("save", context["allowed_operations"])
        self.assertIn("disabled", context["blocked_reason"])


class RegularRecorderTests(WorkspaceCase):
    def test_the_subject_is_one_exact_item_uuid(self):
        item = self.draft()
        page = self.client_for(self.producer).get(f"{RECORDER}?{self.query(item)}")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Town News")
        context = self.context(self.producer, self.query(item)).json()["context"]
        self.assertEqual(context["subject"], {"item": str(item.pk)})
        for bad in ("", "not-a-uuid", "00000000-0000-0000-0000-000000000000"):
            with self.subTest(bad=bad):
                self.assertEqual(self.context(self.producer, f"item={bad}").status_code, 404)
        # No "current item", category, path or type name resolves a subject.
        for query in ("content_type=town_news", "category=PCNEWS", "path=/srv/isadoraair/music",
                      "test_type=town_news"):
            with self.subTest(query=query):
                self.assertEqual(self.context(self.producer, query).status_code, 404)
        # A standalone Phase-C item is not a produced-content subject.
        standalone = SpokenContentType.objects.create(code="standalone", name="S", delivery_mode="accumulating",
                                                      recipe_key="lcl_psa", enabled=True,
                                                      target_category=self.news.target_category)
        stray = transitions.create_item(standalone, actor=self.producer)
        self.assertEqual(self.context(self.producer, self.query(stray)).status_code, 404)

    def test_another_producer_never_sees_the_item_and_control_sees_it_read_only(self):
        item = self.draft()
        take = self.recorded(self.producer, self.query(item))
        self.assertEqual(self.context(self.producer2, self.query(item)).status_code, 404)
        self.assertEqual(self.take(self.producer2, self.query(item)).status_code, 404)
        self.assertEqual(self.submit(self.producer2, item, take, "x").status_code, 404)
        self.assertEqual(self.client_for(self.producer2).get(f"{API}media/{take}/?{self.query(item)}").status_code,
                         404)
        # Station control: visible, read-only -- only its producer records and submits it.
        context = self.context(self.controller, self.query(item)).json()["context"]
        self.assertEqual(set(context["allowed_operations"]), {"open", "export"})
        self.assertIn("Only its own producer", context["blocked_reason"])
        self.assertEqual(dict(context["display"])["Producer"], "producer")
        self.assertEqual(self.take(self.controller, self.query(item)).status_code, 403)
        revision = self.revision(self.producer, item)
        self.assertEqual(self.submit(self.controller, item, take, revision).status_code, 403)
        self.assertEqual(self.client_for(self.controller).get(f"{API}media/{take}/?{self.query(item)}").status_code,
                         404)                                             # an unbound take of another's
        self.assertEqual(self.context(self.host, self.query(item)).status_code, 403)     # no capability at all
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.DRAFT)

    def test_the_draft_context_names_submit_explicitly(self):
        item = self.draft()
        context = self.context(self.producer, self.query(item)).json()["context"]
        self.assertEqual(context["terms"]["save"], "Submit for processing")
        self.assertEqual(context["terms"]["editor_hint"],
                         "browser only; nothing is submitted or on air until you submit")
        self.assertIn("nothing is submitted or on air until you submit", context["purpose"])
        self.assertEqual(set(context["allowed_operations"]), {"open", "export", "record", "import", "save"})
        display = dict(context["display"])
        self.assertEqual((display["Content type"], display["Output"], display["Processing recipe"],
                          display["State"]), ("Town News (town_news)", "Single (replaces the previous item)",
                                              "lcl_psa", "Draft"))
        self.assertEqual(context["current"], None)                         # no persisted draft audio

    def test_submitting_validates_in_one_request_and_renders_nothing(self):
        item = self.draft()
        take = self.recorded(self.producer, self.query(item))
        with mock.patch("production.services.render.render", side_effect=no_render), \
                mock.patch("spoken.delivery.deliver_item", side_effect=no_render):
            response = self.submit(self.producer, item, take, self.revision(self.producer, item))
        self.assertEqual(response.status_code, 200, response.content)
        item.refresh_from_db()
        self.assertEqual((item.state, str(item.submission_media_id)), (S.VALIDATED, take))
        self.assertEqual(list(item.events.values_list("event", flat=True)), ["created", "submitted", "validated"])
        self.assertEqual((item.rendition_media_id, item.lease_token), (None, None))
        self.assertEqual(SpokenAsset.objects.count(), 0)
        context = response.json()["context"]                              # now read-only
        self.assertEqual(set(context["allowed_operations"]), {"open", "export"})
        self.assertEqual(context["terms"]["badge"], "VALIDATED")
        self.assertTrue(context["terms"]["saved"].startswith("Submitted."))      # the post-submit confirmation
        self.assertEqual(context["current"]["media_id"], take)
        self.assertEqual(self.client_for(self.producer).get(context["current"]["preview_url"]).status_code, 200)

    def test_a_stale_second_tab_gets_a_conflict_not_a_silent_replacement(self):
        item = self.draft()
        revision = self.revision(self.producer, item)                     # both tabs opened the same draft
        first = self.recorded(self.producer, self.query(item))
        second = self.recorded(self.producer, self.query(item))
        self.assertEqual(self.submit(self.producer, item, first, revision).status_code, 200)
        response = self.submit(self.producer, item, second, revision)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"], "stale_revision")
        item.refresh_from_db()
        self.assertEqual(str(item.submission_media_id), first)

    def test_a_revision_mismatch_is_refused_under_the_row_lock(self):
        item = self.draft()
        take = self.recorded(self.producer, self.query(item))
        with self.assertRaises(regular.RevisionConflict):
            regular.submit(self.producer, item, take, expected_revision="")
        transitions.SpokenItem.objects.filter(pk=item.pk).update(title="renamed")   # editorial: same revision
        self.assertEqual(regular.submit(self.producer, item, take,
                                        expected_revision=regular.item_revision(item)).state, S.VALIDATED)

    def test_a_submitted_item_is_read_only(self):
        item = self.draft()
        take = self.recorded(self.producer, self.query(item))
        self.submit(self.producer, item, take, self.revision(self.producer, item))
        revision = self.revision(self.producer, item)
        response = self.take(self.producer, self.query(item))
        self.assertEqual((response.status_code, response.json()["error"]), (403, "read_only"))
        response = self.submit(self.producer, item, take, revision)
        self.assertEqual((response.status_code, response.json()["error"]), (409, "stale_revision"))
        self.assertEqual(list(SpokenItem.objects.get(pk=item.pk).events.values_list("event", flat=True)),
                         ["created", "submitted", "validated"])

    def test_media_that_is_not_valid_or_not_yours_is_refused_and_the_draft_stays(self):
        item = self.draft()
        revision = self.revision(self.producer, item)
        invalid = media_row(self.producer, validation_state="invalid", validation_code="decode_failed")
        pending = media_row(self.producer, validation_state="unvalidated")
        for bad in (invalid, pending):
            with self.subTest(state=bad.validation_state):
                response = self.submit(self.producer, item, str(bad.pk), revision)
                self.assertEqual((response.status_code, response.json()["error"]), (422, "media_not_valid"))
        theirs = media_row(self.producer2)
        self.assertEqual(self.submit(self.producer, item, str(theirs.pk), revision).status_code, 404)
        bed = media_row(self.producer, kind="bed")
        self.assertEqual(self.submit(self.producer, item, str(bed.pk), revision).json()["error"], "media_kind")
        item.refresh_from_db()
        self.assertEqual((item.state, item.submission_media_id), (S.DRAFT, None))

    def test_a_valid_take_outside_the_types_duration_policy_reaches_invalid(self):
        """The existing validation contract decides: submit, then the type's
        duration bounds -> INVALID (recorded in history)."""
        item = self.draft()
        take = media_row(self.producer, seconds="3.000000")
        backing = SpokenContentType.objects.get(pk=item.content_type_id)
        with mock.patch("spoken.configuration.media_policy_for") as policy:
            from production.policy import MediaPolicy
            policy.return_value = MediaPolicy(max_duration_seconds=1.0)
            result = regular.submit(self.producer, item, take, expected_revision=regular.item_revision(item))
        self.assertEqual((result.state, result.failure_code), (S.INVALID, "too_long"))
        self.assertEqual(backing.pk, result.content_type_id)

    def test_losing_the_assignment_makes_a_draft_read_only_but_it_can_still_be_abandoned(self):
        item = self.draft()
        ProducedContentTypeAssignment.objects.filter(user=self.producer, content_type=self.news).delete()
        context = self.context(self.producer, self.query(item)).json()["context"]
        self.assertNotIn("save", context["allowed_operations"])
        self.assertIn("not assigned", context["blocked_reason"])
        self.assertEqual(self.take(self.producer, self.query(item)).status_code, 403)
        page = self.client_for(self.producer).get(PAGE)
        self.assertContains(page, 'id="pcOtherItems"')
        self.assertContains(page, f'data-item="{item.pk}"')
        response = self.post(self.producer, f"{PAGE}{item.pk}/withdraw/", {"expected_state": "draft"})
        self.assertRedirects(response, PAGE, fetch_redirect_response=False)
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.RECALLED)


class WithdrawTests(WorkspaceCase):
    def test_the_owner_abandons_a_draft_after_a_confirmation(self):
        item = self.draft()
        confirm = self.client_for(self.producer).get(f"{PAGE}{item.pk}/withdraw/")
        self.assertContains(confirm, "Abandon this item?")
        self.assertEqual(self.post(self.producer2, f"{PAGE}{item.pk}/withdraw/").status_code, 404)
        response = self.post(self.producer, f"{PAGE}{item.pk}/withdraw/", {"expected_state": "draft"})
        self.assertRedirects(response, PAGE, fetch_redirect_response=False)
        item = SpokenItem.objects.get(pk=item.pk)
        self.assertEqual((item.state, item.recalled_by_id), (S.RECALLED, self.producer.pk))
        self.assertEqual(list(item.events.values_list("event", flat=True)), ["created", "recalled"])
        self.assertTrue(SpokenItem.objects.filter(pk=item.pk).exists())          # history is kept

    def test_a_submitted_item_can_be_withdrawn_but_processing_is_never_interrupted(self):
        item = self.draft()
        take = self.recorded(self.producer, self.query(item))
        self.submit(self.producer, item, take, self.revision(self.producer, item))
        other = self.draft(title="second")
        self.submit(self.producer, other, self.recorded(self.producer, self.query(other)),
                    self.revision(self.producer, other))
        transitions.start_processing(other.pk, lease_seconds=600)            # the worker holds it
        self.post(self.producer, f"{PAGE}{other.pk}/withdraw/", {"expected_state": "processing"})
        self.assertEqual(SpokenItem.objects.get(pk=other.pk).state, S.PROCESSING)
        page = self.client_for(self.producer).get(PAGE)
        self.assertNotContains(page, f"{other.pk}/withdraw/")                # not offered either
        self.post(self.producer, f"{PAGE}{item.pk}/withdraw/", {"expected_state": "validated"})
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.RECALLED)

    def test_a_stale_confirmation_changes_nothing(self):
        item = self.draft()
        take = self.recorded(self.producer, self.query(item))
        self.submit(self.producer, item, take, self.revision(self.producer, item))
        self.post(self.producer, f"{PAGE}{item.pk}/withdraw/", {"expected_state": "draft"})
        self.assertEqual(SpokenItem.objects.get(pk=item.pk).state, S.VALIDATED)
