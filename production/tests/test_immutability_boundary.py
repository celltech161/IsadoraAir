"""Codex Blocker 2: every ordinary Django ORM write path fails closed.

One test per bypass Codex demonstrated against 77792c2, plus the paths that
must keep working: Django's own SET_NULL when a User is deleted, and the
explicit state-qualified transitions in production.transitions.
"""
from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from production import transitions
from production.errors import ImmutableMediaError
from production.models import ProductionMedia

from .support import SHA_A, VALID_FACTS, IsolatedMediaRootMixin, make_row


def validated_row(**overrides):
    return make_row(validated_at=timezone.now(), **{**VALID_FACTS, **overrides})


class CodexBypassRegressionTests(IsolatedMediaRootMixin, TestCase):
    def assertUnchanged(self, row, **expected):
        current = ProductionMedia._base_manager.filter(pk=row.pk).values(*expected).get()
        self.assertEqual(current, expected)

    # 1 -- post-validation QuerySet.update(codec=...)
    def test_1_queryset_update_cannot_rewrite_a_validated_codec(self):
        row = validated_row()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.filter(pk=row.pk).update(codec="flac")
        self.assertUnchanged(row, codec="pcm_s16le")

    # 2 -- retention resurrection
    def test_2_purged_cannot_be_resurrected_by_any_generic_path(self):
        row = make_row(retention_state="purged", purged_at=timezone.now())
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.filter(pk=row.pk).update(retention_state="present", purged_at=None)
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia._base_manager.filter(pk=row.pk).update(retention_state="present", purged_at=None)
        instance = ProductionMedia.objects.get(pk=row.pk)
        instance.retention_state, instance.purged_at = "present", None
        with self.assertRaises(ImmutableMediaError):
            instance.save()
        self.assertUnchanged(row, retention_state="purged")

    # 3 -- deferred instance
    def test_3_a_deferred_instance_cannot_change_a_field_it_did_not_load(self):
        row = make_row()
        for loader in (lambda: ProductionMedia.objects.only("id").get(pk=row.pk),
                       lambda: ProductionMedia.objects.defer("sha256", "codec").get(pk=row.pk)):
            instance = loader()
            self.assertNotIn("sha256", instance.__dict__)
            instance.sha256 = "b" * 64
            with self.assertRaises(ImmutableMediaError):
                instance.save()
            instance = loader()
            instance.sha256 = "b" * 64
            with self.assertRaises(ImmutableMediaError):
                instance.save(update_fields=["sha256"])
        self.assertUnchanged(row, sha256=SHA_A)

    # 4 -- reverse related managers
    def test_4_reverse_related_managers_cannot_update(self):
        user = get_user_model().objects.create_user("custody-owner", password="x")
        parent = validated_row()
        row = validated_row(owner=user)
        child = make_row(kind="edit", derived_from=parent)
        with self.assertRaises(ImmutableMediaError):
            user.production_media.update(codec="flac")
        with self.assertRaises(ImmutableMediaError):
            parent.derivatives.update(sha256="c" * 64)
        with self.assertRaises(ImmutableMediaError):
            parent.derivatives.clear()                       # would rewrite derived_from
        other = get_user_model().objects.create_user("thief", password="x")
        with self.assertRaises(ImmutableMediaError):
            other.production_media.add(row)                  # custody reassignment
        self.assertUnchanged(row, codec="pcm_s16le", owner_id=user.pk)
        self.assertUnchanged(child, sha256=SHA_A, derived_from_id=parent.pk)

    # 5 -- bulk_update
    def test_5_bulk_update_is_refused(self):
        row = validated_row()
        instance = ProductionMedia.objects.get(pk=row.pk)
        instance.codec = "flac"
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.bulk_update([instance], ["codec"])
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia._base_manager.bulk_update([instance], ["codec"])
        self.assertUnchanged(row, codec="pcm_s16le")

    # 6 -- _base_manager.update
    def test_6_base_manager_update_is_refused(self):
        row = make_row()
        self.assertIs(type(ProductionMedia._base_manager.all()), type(ProductionMedia.objects.all()))
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia._base_manager.filter(pk=row.pk).update(sha256="b" * 64)
        self.assertUnchanged(row, sha256=SHA_A)

    # 7 -- _base_manager.delete
    def test_7_base_manager_delete_is_refused(self):
        row = make_row()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia._base_manager.filter(pk=row.pk).delete()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia._base_manager.all().delete()
        self.assertTrue(ProductionMedia._base_manager.filter(pk=row.pk).exists())

    # 8 -- update_or_create / bulk_create upsert
    def test_8_update_or_create_and_upserts_cannot_mutate_an_existing_row(self):
        row = validated_row()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.update_or_create(pk=row.pk, defaults={"codec": "flac"})
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.update_or_create(pk=row.pk, defaults={"sha256": "b" * 64})
        clone = ProductionMedia(id=row.pk, kind="upload", storage_key=row.storage_key, sha256="d" * 64,
                                byte_size=1)
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.bulk_create([clone], update_conflicts=True, unique_fields=["id"],
                                                update_fields=["sha256"])
        self.assertUnchanged(row, codec="pcm_s16le", sha256=SHA_A)
        # get_or_create on an existing row is a read: allowed and harmless.
        found, created = ProductionMedia.objects.get_or_create(pk=row.pk, defaults={"codec": "flac"})
        self.assertFalse(created)
        self.assertEqual(found.codec, "pcm_s16le")

    def test_owner_cannot_be_reassigned_by_save_or_update(self):
        user = get_user_model().objects.create_user("first", password="x")
        other = get_user_model().objects.create_user("second", password="x")
        row = make_row(owner=user)
        instance = ProductionMedia.objects.get(pk=row.pk)
        instance.owner = other
        with self.assertRaises(ImmutableMediaError):
            instance.save()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.filter(pk=row.pk).update(owner=other)
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.filter(pk=row.pk).update(owner=None, codec="x")   # SET_NULL plus anything
        self.assertUnchanged(row, owner_id=user.pk)


class LegitimatePathsStillWorkTests(IsolatedMediaRootMixin, TestCase):
    # 9 -- Django's own SET_NULL on User deletion
    def test_9_deleting_a_user_nulls_custody_and_keeps_the_snapshot(self):
        user = get_user_model().objects.create_user("departing", password="x")
        rows = [validated_row(owner=user), make_row(owner=user)]
        user.delete()                                     # the collector's update(owner=None)
        for row in rows:
            current = ProductionMedia.objects.get(pk=row.pk)
            self.assertIsNone(current.owner_id)
            self.assertEqual(current.owner_username, "departing")
            self.assertEqual(current.sha256, SHA_A)

    def test_9b_a_deleted_user_with_loaded_related_rows_also_works(self):
        user = get_user_model().objects.create_user("cached", password="x")
        row = make_row(owner=user)
        list(user.production_media.all())                 # populate caches the collector may use
        user.delete()
        self.assertIsNone(ProductionMedia.objects.get(pk=row.pk).owner_id)

    def test_9c_reverse_manager_clear_only_unlinks_custody(self):
        user = get_user_model().objects.create_user("clearing", password="x")
        row = make_row(owner=user)
        user.production_media.clear()
        current = ProductionMedia.objects.get(pk=row.pk)
        self.assertEqual((current.owner_id, current.owner_username), (None, "clearing"))

    # 10 -- the explicit, state-qualified transitions
    def test_10a_a_verdict_is_recorded_exactly_once(self):
        row = make_row()
        now = timezone.now()
        verdict = dict(VALID_FACTS, validated_at=now)
        self.assertEqual(transitions.record_verdict(row.pk, verdict), 1)
        self.assertEqual(transitions.record_verdict(row.pk, dict(verdict, codec="flac")), 0)   # already judged
        self.assertEqual(ProductionMedia.objects.get(pk=row.pk).codec, "pcm_s16le")

    def test_10b_a_verdict_may_write_only_fact_and_verdict_columns(self):
        row = make_row()
        for illegal in ({"sha256": "b" * 64}, {"retention_state": "purged"}, {"owner_id": None},
                        {"storage_key": "ab/" + "c" * 32}):
            with self.subTest(illegal=illegal), self.assertRaises(ValueError):
                transitions.record_verdict(row.pk, dict(VALID_FACTS, validated_at=timezone.now(), **illegal))
        with self.assertRaises(ValueError):
            transitions.record_verdict(row.pk, dict(VALID_FACTS, validation_state="unvalidated",
                                                    validated_at=timezone.now()))
        self.assertEqual(ProductionMedia.objects.get(pk=row.pk).validation_state, "unvalidated")

    def test_10c_no_verdict_on_a_purged_row(self):
        row = make_row(retention_state="purged", purged_at=timezone.now())
        self.assertEqual(transitions.record_verdict(row.pk, dict(VALID_FACTS, validated_at=timezone.now())), 0)

    def test_10d_an_infrastructure_note_only_while_unvalidated(self):
        row = make_row()
        self.assertEqual(transitions.record_infrastructure_attempt(row.pk, "probe_timeout"), 1)
        self.assertEqual(ProductionMedia.objects.get(pk=row.pk).validation_code, "probe_timeout")
        judged = validated_row()
        self.assertEqual(transitions.record_infrastructure_attempt(judged.pk, "probe_timeout"), 0)
        self.assertEqual(ProductionMedia.objects.get(pk=judged.pk).validation_code, "ok")

    def test_10e_present_to_purged_once_and_never_back(self):
        row = make_row()
        when = timezone.now()
        self.assertEqual(transitions.mark_purged(row.pk, when), 1)
        self.assertEqual(transitions.mark_purged(row.pk, timezone.now()), 0)
        current = ProductionMedia.objects.get(pk=row.pk)
        self.assertEqual((current.retention_state, current.purged_at), ("purged", when))
        self.assertFalse(hasattr(transitions, "mark_present"))
        with self.assertRaises(ValueError):
            transitions.mark_purged(make_row().pk, None)

    def test_10f_transitions_are_atomic_with_their_callers_transaction(self):
        row = make_row()
        try:
            with transaction.atomic():
                transitions.mark_purged(row.pk, timezone.now())
                raise RuntimeError("caller failed")
        except RuntimeError:
            pass
        self.assertEqual(ProductionMedia.objects.get(pk=row.pk).retention_state, "present")
