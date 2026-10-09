"""The spoken domain: type configuration, the transition authority, the
write guards, permanent database invariants, provenance and the media
binding/purge contract.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from production.errors import PurgeRefused
from production.models import ProductionMedia
from production.services import render, retention
from production.tests.support import VALID_FACTS, make_row
from spoken import guard, projection, transitions
from spoken.models import (
    AssetMode, AssetState, EditionState, ItemState, RecallPolicy, SpokenAsset, SpokenAssetEdition,
    SpokenContentType, SpokenItem, SpokenItemEvent,
)

User = get_user_model()
S = ItemState


def make_category(code="LocalNews"):
    from library.models import Category, CategoryKind
    kind, _ = CategoryKind.objects.get_or_create(code="spoken", defaults={"name": "Spoken"})
    category, _ = Category.objects.get_or_create(code=code, defaults={"name": code, "kind": kind})
    return category


def make_type(**overrides):
    values = dict(code="news-test", name="News (test)", delivery_mode="current_edition", recipe_key="lcl_psa",
                  enabled=True, target_category=make_category())
    values.update(overrides)
    return SpokenContentType.objects.create(**values)


def media(kind=ProductionMedia.KIND_RECORDING, **overrides):
    values = {**VALID_FACTS, "validated_at": timezone.now(), **overrides}
    if values.get("validation_state") == "unvalidated":
        values["validated_at"] = None
    return make_row(kind=kind, **values)


class Fixtures(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner")
        self.other = User.objects.create_user("other")
        self.manager = User.objects.create_user("manager", is_staff=True)
        self.ctype = make_type()

    def item(self, ctype=None, actor=None):
        return transitions.create_item(ctype or self.ctype, actor=actor or self.owner, title="t")

    def submitted(self, **media_overrides):
        item = self.item()
        return transitions.submit(item, media(**media_overrides), actor=self.owner)

    def validated(self):
        return transitions.record_validation(self.submitted())

    def processing(self):
        return transitions.start_processing(self.validated(), lease_seconds=60)

    def expected(self, item):
        ctype = SpokenContentType.objects.get(pk=item.content_type_id)
        with transaction.atomic():
            return render.expected(ctype.recipe_key, ctype.recipe_params)

    def rendition_for(self, item, **overrides):
        """A valid rendition row whose stored provenance is exactly what the
        item's type currently specifies (what render() would record)."""
        expected = self.expected(item)
        values = dict(derived_from=item.submission_media, recipe_key=expected.recipe_key,
                      recipe_version=expected.recipe_version, recipe_params_digest=expected.digest)
        values.update(overrides)
        return media(kind=ProductionMedia.KIND_RENDITION, **values)

    @staticmethod
    def evidence(rendition, **overrides):
        values = dict(rendition_id=str(rendition.pk), source_id=str(rendition.derived_from_id),
                      recipe_key=rendition.recipe_key, recipe_version=rendition.recipe_version,
                      provenance_digest=rendition.recipe_params_digest)
        values.update(overrides)
        return render.RenderEvidence(**values)

    def complete(self, item, token, rendition, **overrides):
        return transitions.complete_processing(item, lease_token=token, evidence=self.evidence(rendition, **overrides))

    def processed(self):
        item, token = self.processing()
        return self.complete(item, token, self.rendition_for(item))

    def events(self, item):
        return list(SpokenItemEvent.objects.filter(item=item).values_list("event", "from_state", "to_state"))


class ContentTypeConfigurationTests(Fixtures):
    def test_recipes_and_parameters_resolve_through_the_render_registry(self):
        for recipe_key, params, code in (("nope", {}, "unknown_recipe"),
                                         ("cal_events", {"filter": "x"}, "unknown_parameter"),
                                         ("cal_events", {"bed_gain_db": "-13"}, "wrong_parameter_type"),
                                         ("cal_events", {"bed_gain_db": 9}, "parameter_out_of_bounds"),
                                         ("cal_events", {"bed": "/etc/passwd"}, "invalid_reference_key"),
                                         ("cal_events", [], "invalid_parameters")):
            with self.subTest(recipe=recipe_key, params=params):
                with self.assertRaises(ValidationError) as raised:
                    make_type(code="t-" + code.replace("_", "-"), recipe_key=recipe_key, recipe_params=params)
                codes = [error.code for errors in raised.exception.error_dict.values() for error in errors]
                self.assertEqual(codes, [code])
        make_type(code="good", recipe_key="news_item", recipe_params={"voice_delay_ms": 3000})

    def test_limits_only_tighten_the_platform_ceilings(self):
        from production import policy
        for overrides in ({"max_bytes": policy.PLATFORM_MAX_BYTES + 1},
                          {"max_duration_seconds": Decimal(policy.PLATFORM_MAX_DURATION_SECONDS) + 1},
                          {"min_duration_seconds": Decimal("30"), "max_duration_seconds": Decimal("10")}):
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                make_type(code="limits", **overrides)
        make_type(code="limits", max_bytes=1024 * 1024, max_duration_seconds=Decimal("120"))

    def test_an_enabled_projected_type_needs_a_target_category_but_a_disabled_one_need_not(self):
        with self.assertRaises(ValidationError):
            make_type(code="nocat", target_category=None, enabled=True)
        draft = make_type(code="nocat", target_category=None, enabled=False)
        make_type(code="retain", target_category=None, enabled=True, delivery_mode="retain_only")
        draft.enabled = True
        with self.assertRaises(ValidationError):
            draft.save()

    def test_a_used_code_is_permanent_and_the_type_cannot_be_deleted(self):
        unused = make_type(code="unused")
        unused.code = "renamed"
        unused.save()                                           # fine while unused
        self.item()
        self.ctype.code = "renamed-again"
        with self.assertRaises(guard.SpokenIntegrityError):
            self.ctype.save()
        with self.assertRaises(guard.SpokenIntegrityError):
            SpokenContentType.objects.get(pk=self.ctype.pk).delete()
        with self.assertRaises(guard.SpokenIntegrityError):
            SpokenContentType.objects.filter(pk=self.ctype.pk).update(code="sneaky")

    def test_the_type_media_policy(self):
        from spoken.configuration import media_policy_for
        ctype = make_type(code="bounded", max_bytes=5000, min_duration_seconds=Decimal("1.5"),
                          max_duration_seconds=Decimal("60"))
        policy = media_policy_for(ctype)
        self.assertEqual((policy.max_bytes, policy.min_duration_seconds, policy.max_duration_seconds,
                          policy.require_engine_decode), (5000, 1.5, 60.0, True))


class LifecycleTests(Fixtures):
    def test_the_happy_path_with_review_and_its_authoritative_history(self):
        item = self.processed()
        item = transitions.request_review(item)
        item = transitions.reject(item, actor=self.manager, note="needs a retake")
        self.assertEqual(item.state, S.REJECTED)
        self.assertEqual((item.reviewed_by, item.reviewed_by_username, item.review_note),
                         (self.manager, "manager", "needs a retake"))
        self.assertIsNotNone(item.terminal_at)
        self.assertEqual(self.events(item), [
            ("created", "", "draft"), ("submitted", "draft", "submitted"), ("validated", "submitted", "validated"),
            ("processing_started", "validated", "processing"), ("processed", "processing", "processed"),
            ("review_requested", "processed", "awaiting_review"), ("rejected", "awaiting_review", "rejected"),
        ])
        processed = SpokenItemEvent.objects.get(item=item, event="processed")
        self.assertEqual(processed.media_id, item.rendition_media_id)
        self.assertEqual(processed.actor_kind, "worker")

    def test_a_disabled_type_accepts_no_new_production(self):
        ctype = make_type(code="off", enabled=False)
        with self.assertRaises(transitions.NotPermitted) as raised:
            self.item(ctype)
        self.assertEqual(raised.exception.code, "type_disabled")
        draft = self.item()
        SpokenContentType.objects.filter(pk=self.ctype.pk)          # (update() is refused -- use save)
        self.ctype.enabled = False
        self.ctype.save()
        with self.assertRaises(transitions.NotPermitted):
            transitions.submit(draft, media(), actor=self.owner)
        self.assertFalse(SpokenItem.objects.filter(content_type=ctype).exists())

    def test_invalid_transitions_are_refused_and_change_nothing(self):
        item = self.item()
        for to_state in (S.VALIDATED, S.PROCESSED, S.AWAITING_REVIEW, S.REJECTED, S.SUPERSEDED, S.EXPIRED):
            with self.subTest(to_state=to_state), self.assertRaises(transitions.InvalidTransition):
                transitions._apply(item, to_state, actor=self.manager)
        item.refresh_from_db()
        self.assertEqual(item.state, S.DRAFT)
        self.assertEqual(len(self.events(item)), 1)

    def test_library_projection_transitions_are_reserved_for_c2(self):
        item = self.processed()
        with self.assertRaises(transitions.ProjectionRequired):
            transitions._apply(item, S.DELIVERED, actor=self.manager)
        self.assertTrue(all(rule.c2 for key, rule in transitions.TRANSITIONS.items() if S.DELIVERED in key))
        item.refresh_from_db()
        self.assertEqual(item.state, S.PROCESSED)

    def test_submission_evidence(self):
        item = self.item()
        for bad, code in ((media(kind=ProductionMedia.KIND_BED), "media_kind"),):
            with self.assertRaises(transitions.EvidenceRefused) as raised:
                transitions.submit(item, bad, actor=self.owner)
            self.assertEqual(raised.exception.code, code)
        with self.assertRaises(transitions.NotPermitted):
            transitions.submit(item, media(), actor=self.other)
        small = make_type(code="small", max_bytes=5)
        tiny = self.item(small)
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            transitions.submit(tiny, media(byte_size=6), actor=self.owner)
        self.assertEqual(raised.exception.code, "too_large")
        purged = media(retention_state="purged", purged_at=timezone.now())
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            transitions.submit(item, purged, actor=self.owner)
        self.assertEqual(raised.exception.code, "media_unusable")
        item.refresh_from_db()
        self.assertEqual((item.state, item.submission_media_id), (S.DRAFT, None))

    def test_validation_verdicts_and_type_duration_bounds(self):
        unvalidated = self.submitted(validation_state="unvalidated", container="", codec="", sample_rate=None,
                                     channels=None, decoded_duration_seconds=None, validation_code="")
        with self.assertRaises(transitions.EvidenceRefused):
            transitions.record_validation(unvalidated)
        invalid = self.submitted(validation_state="invalid", validation_code="decode_error",
                                 validated_at=timezone.now())
        invalid = transitions.record_validation(invalid)
        self.assertEqual((invalid.state, invalid.failure_code), (S.INVALID, "decode_error"))
        short = make_type(code="short", max_duration_seconds=Decimal("1.0"))
        too_long = transitions.submit(self.item(short), media(validated_at=timezone.now()), actor=self.owner)
        too_long = transitions.record_validation(too_long)
        self.assertEqual((too_long.state, too_long.failure_code), (S.INVALID, "too_long"))

    def test_processing_leases(self):
        item, token = self.processing()
        self.assertEqual((item.processing_attempts, str(item.lease_token)), (1, str(token)))
        with self.assertRaises(transitions.InvalidTransition) as raised:
            transitions.start_processing(item)                         # still held
        self.assertEqual(raised.exception.code, "lease_held")
        with guard.transition_scope():                                  # the lease expires
            SpokenItem.objects.filter(pk=item.pk).update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        item, new_token = transitions.start_processing(item)
        self.assertEqual(item.processing_attempts, 2)
        self.assertNotEqual(new_token, token)
        rendition = self.rendition_for(item)
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            self.complete(item, token, rendition)       # stale worker
        self.assertEqual(raised.exception.code, "stale_lease")
        unrelated = self.rendition_for(item, derived_from=media())
        with self.assertRaises(transitions.EvidenceRefused) as raised:
            self.complete(item, new_token, unrelated)
        self.assertEqual(raised.exception.code, "rendition_mismatch")
        other_recipe = self.rendition_for(item, recipe_key="cal_events")
        with self.assertRaises(transitions.EvidenceRefused):
            self.complete(item, new_token, other_recipe)
        item = self.complete(item, new_token, rendition)
        self.assertEqual((item.state, item.rendition_media_id, item.lease_token, item.lease_expires_at),
                         (S.PROCESSED, rendition.pk, None, None))

    def test_failure_and_manager_retry(self):
        item, token = self.processing()
        item = transitions.fail_processing(item, lease_token=token, code="render_timeout", detail="x" * 900)
        self.assertEqual((item.state, item.failure_code, len(item.failure_detail)), (S.FAILED, "render_timeout", 500))
        with self.assertRaises(transitions.NotPermitted):
            transitions.retry(item, actor=self.owner)
        item = transitions.retry(item, actor=self.manager)
        self.assertEqual((item.state, item.failure_code), (S.VALIDATED, ""))

    def test_review_only_where_configured(self):
        no_review = make_type(code="direct", requires_review=False)
        item = transitions.submit(self.item(no_review), media(), actor=self.owner)
        item = transitions.record_validation(item)
        item, token = transitions.start_processing(item)
        item = self.complete(item, token, self.rendition_for(item))
        with self.assertRaises(transitions.InvalidTransition):
            transitions.request_review(item)
        reviewed = transitions.request_review(self.processed())
        with self.assertRaises(transitions.NotPermitted):
            transitions.reject(reviewed, actor=self.owner, note="no")

    def test_recall_before_delivery_follows_the_type_policy(self):
        item = self.validated()
        with self.assertRaises(transitions.NotPermitted):
            transitions.recall(item, actor=self.other)
        recalled = transitions.recall(item, actor=self.owner, reason="wrong file")
        self.assertEqual((recalled.state, recalled.recalled_by_username, recalled.recall_reason),
                         (S.RECALLED, "owner", "wrong file"))
        strict = make_type(code="strict", recall_policy=RecallPolicy.MANAGERS_ONLY)
        mine = self.item(strict)
        with self.assertRaises(transitions.NotPermitted):
            transitions.recall(mine, actor=self.owner)
        transitions.recall(mine, actor=self.manager)
        busy, _token = self.processing()
        with self.assertRaises(transitions.InvalidTransition):
            transitions.recall(busy, actor=self.manager)               # not while a worker holds it

    def test_imported_items_are_reserved_for_the_import_tooling(self):
        with self.assertRaises(transitions.NotPermitted):
            transitions.create_item(self.ctype, actor=self.owner, origin="imported")


class AtomicityTests(Fixtures):
    def test_state_and_event_commit_together_or_not_at_all(self):
        item = self.item()
        real_create = SpokenItemEvent.objects.create
        with mock.patch.object(type(SpokenItemEvent.objects), "create", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                transitions.submit(item, media(), actor=self.owner)
        item.refresh_from_db()
        self.assertEqual((item.state, item.submission_media_id, item.submitted_at), (S.DRAFT, None, None))
        self.assertEqual(len(self.events(item)), 1)
        self.assertTrue(real_create)

    def test_a_refused_check_writes_nothing(self):
        item = self.item()
        with self.assertRaises(transitions.NotPermitted):
            transitions.submit(item, media(), actor=self.other)
        self.assertEqual(len(self.events(item)), 1)


class GuardTests(Fixtures):
    def test_ordinary_writes_cannot_change_lifecycle_or_history(self):
        item = self.submitted()
        loaded = SpokenItem.objects.get(pk=item.pk)
        loaded.state = S.PROCESSED
        with self.assertRaises(guard.SpokenIntegrityError):
            loaded.save()
        for columns in ({"state": "delivered"}, {"submission_media": None}, {"lease_token": None},
                        {"failure_code": "x"}):
            with self.subTest(columns=columns), self.assertRaises(guard.SpokenIntegrityError):
                SpokenItem.objects.filter(pk=item.pk).update(**columns)
        with self.assertRaises(guard.SpokenIntegrityError):
            SpokenItem.objects.bulk_update([loaded], ["state"])
        with self.assertRaises(guard.SpokenIntegrityError):
            SpokenItem.objects.create(content_type=self.ctype)
        with self.assertRaises(guard.SpokenIntegrityError):
            item.delete()
        event = SpokenItemEvent.objects.filter(item=item).first()
        event.event = "rewritten"
        for attempt in (event.save, event.delete, lambda: SpokenItemEvent.objects.filter(item=item).delete(),
                        lambda: SpokenItemEvent.objects.filter(item=item).update(event="x"),
                        lambda: SpokenItemEvent.objects.create(item=item, event="forged", actor_kind="user")):
            with self.assertRaises(guard.SpokenIntegrityError):
                attempt()
        item.refresh_from_db()
        self.assertEqual(item.state, S.SUBMITTED)

    def test_editorial_text_is_free(self):
        item = self.item()
        item.title, item.notes = "New title", "notes"
        item.save()
        SpokenItem.objects.filter(pk=item.pk).update(notes="more")
        item.refresh_from_db()
        self.assertEqual((item.title, item.notes, item.state), ("New title", "more", S.DRAFT))


class ProvenanceTests(Fixtures):
    def test_submitter_and_actor_provenance_survive_user_deletion(self):
        item = self.validated()
        self.owner.delete()
        item.refresh_from_db()
        self.assertEqual((item.submitted_by, item.submitted_by_username), (None, "owner"))
        for event in SpokenItemEvent.objects.filter(item=item, actor_kind="user"):
            self.assertEqual((event.actor, event.actor_username), (None, "owner"))

    def test_bound_media_cannot_be_purged(self):
        item = self.processed()
        for bound, field in ((item.submission_media, "submission_media"), (item.rendition_media, "rendition_media")):
            with self.subTest(field=field):
                with self.assertRaises(PurgeRefused):
                    retention.purge_media(bound)
                references = {(ref.model_label.lower(), ref.field_name) for ref in retention.find_references(bound)}
                self.assertIn(("spoken.spokenitem", field), references)
                self.assertIn(("spoken.spokenitemevent", "media"), references)      # the event names it too


class DatabaseInvariantTests(Fixtures):
    """The permanent constraints in spoken.0001 hold even for writes that
    bypass the services."""

    def assert_rejected(self, make):
        with self.assertRaises(IntegrityError), transaction.atomic(), guard.transition_scope():
            make()

    def raw_item(self, **overrides):
        values = dict(content_type=self.ctype, state=S.DRAFT)
        values.update(overrides)
        with guard.transition_scope():
            return SpokenItem.objects.create(**values)

    def test_item_invariants(self):
        self.assert_rejected(lambda: self.raw_item(lease_token="00000000-0000-0000-0000-000000000001"))
        self.assert_rejected(lambda: self.raw_item(lease_expires_at=timezone.now()))
        self.assert_rejected(lambda: self.raw_item(import_source="ogremote"))          # no id / date / origin
        self.assert_rejected(lambda: self.raw_item(origin="imported"))                 # imported needs provenance
        imported = dict(origin="imported", import_source="ogremote", import_external_id="42",
                        imported_at=timezone.now())
        self.raw_item(**imported)
        self.assert_rejected(lambda: self.raw_item(**imported))                        # one item per legacy record
        self.raw_item(**{**imported, "import_external_id": "43"})
        self.raw_item()
        self.raw_item()                                                                # natives never collide

    def asset(self, **overrides):
        import uuid
        asset_id = overrides.pop("id", uuid.uuid4())
        values = dict(id=asset_id, content_type=self.ctype, mode=AssetMode.CURRENT_EDITION, slot_key="",
                      library_relpath=projection.library_relpath("LocalNews", asset_id))
        values.update(overrides)
        return SpokenAsset.objects.create(**values)

    def test_asset_invariants(self):
        self.asset(slot_key="morning")
        self.assert_rejected(lambda: self.asset(slot_key="morning"))                 # one current edition per slot
        self.asset(slot_key="evening")
        self.asset(slot_key="morning-old", state=AssetState.RETIRED, retired_at=timezone.now())
        retired_twin = self.asset(slot_key="noon", state=AssetState.RETIRED, retired_at=timezone.now())
        self.asset(slot_key="noon")                                                 # retired does not block
        self.assertIsNotNone(retired_twin)
        item = self.raw_item()
        self.asset(mode=AssetMode.ACCUMULATING, item=item)
        self.assert_rejected(lambda: self.asset(mode=AssetMode.ACCUMULATING, item=item))   # one per item
        self.assert_rejected(lambda: self.asset(mode=AssetMode.ACCUMULATING))              # needs its item
        self.assert_rejected(lambda: self.asset(item=item))                                # current edition: no item
        self.assert_rejected(lambda: self.asset(state=AssetState.RETIRED, slot_key="x"))   # retired needs a date
        self.assert_rejected(lambda: self.asset(slot_key="x", retired_at=timezone.now()))
        self.assert_rejected(lambda: self.asset(slot_key="Bad Slot"))
        for relpath in ("../x/spoken-" + "a" * 32 + ".flac", "/abs/spoken-" + "a" * 32 + ".flac",
                        "LocalNews/upload.mp3", "LocalNews/../spoken-" + "a" * 32 + ".flac"):
            with self.subTest(relpath=relpath):
                self.assert_rejected(lambda: self.asset(slot_key="r", library_relpath=relpath))

    def test_edition_invariants(self):
        asset = self.asset()
        item = self.processed()
        rendition = item.rendition_media
        now = timezone.now()
        SpokenAssetEdition.objects.create(asset=asset, item=item, rendition=rendition, generation=1,
                                          state=EditionState.ACTIVE, promoted_at=now)
        self.assert_rejected(lambda: SpokenAssetEdition.objects.create(
            asset=asset, item=item, rendition=rendition, generation=2, state=EditionState.ACTIVE, promoted_at=now))
        self.assert_rejected(lambda: SpokenAssetEdition.objects.create(asset=asset, item=item, rendition=rendition,
                                                                       generation=1))
        self.assert_rejected(lambda: SpokenAssetEdition.objects.create(
            asset=asset, item=item, rendition=rendition, generation=3, library_sha256="xyz"))
        self.assert_rejected(lambda: SpokenAssetEdition.objects.create(
            asset=asset, item=item, rendition=rendition, generation=4, state=EditionState.SUPERSEDED))
        with self.assertRaises(PurgeRefused):
            retention.purge_media(rendition)                     # an edition references it too


class ProjectionPlanningTests(Fixtures):
    def test_deterministic_identity_and_fixed_titles(self):
        import uuid
        asset_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
        self.assertEqual(projection.library_relpath("LocalNews", asset_id),
                         "LocalNews/spoken-12345678123456781234567812345678.flac")
        for bad in ("../x", "", "a/b", "x" * 21, ".hidden"):
            with self.subTest(bad=bad), self.assertRaises(projection.ProjectionPlanError):
                projection.library_relpath(bad, asset_id)
        item = self.submitted()
        plan = projection.plan_projection(item)
        self.assertEqual((plan.mode, plan.category_code, plan.ready2air), ("current_edition", "LocalNews", False))
        when = timezone.make_aware(timezone.datetime(2026, 10, 9, 12, 0))
        cases = {"submitted_date": "10/09/2026", "item_title": "t", "item_title_and_date": "t - 10/09/2026",
                 "submitter_and_date": "owner - 10/09/2026"}
        for policy, expected in cases.items():
            self.assertEqual(projection.render_title(policy, item_title="t", submitter="owner", when=when), expected)
        with self.assertRaises(projection.ProjectionPlanError):
            projection.render_title("{title}", item_title="t", submitter="o", when=when)
        retain = make_type(code="retain", delivery_mode="retain_only", target_category=None)
        with self.assertRaises(projection.ProjectionPlanError):
            projection.plan_projection(transitions.submit(self.item(retain), media(), actor=self.owner))
