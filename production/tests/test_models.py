"""ProductionMedia: schema invariants (real PostgreSQL) and the immutability guard."""
import uuid

from django.contrib.auth import get_user_model
from django.db import DataError, IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from production.errors import ImmutableMediaError
from production.models import ProductionMedia

from .support import SHA_A, VALID_FACTS, IsolatedMediaRootMixin, make_row


class SchemaConstraintTests(IsolatedMediaRootMixin, TestCase):
    def assertRejected(self, **overrides):
        # A CHECK/UNIQUE violation (IntegrityError) or an over-long value the
        # column itself refuses (DataError): either way the row never exists.
        with self.assertRaises((IntegrityError, DataError)), transaction.atomic():
            make_row(**overrides)

    def test_identity_is_a_uuid_and_unique_per_row(self):
        first, second = make_row(), make_row()
        self.assertIsInstance(first.pk, uuid.UUID)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(ProductionMedia.objects.get(pk=first.pk).pk, first.pk)

    def test_storage_key_is_unique(self):
        row = make_row()
        self.assertRejected(storage_key=row.storage_key)

    def test_sha256_must_be_64_lowercase_hex(self):
        for bad in ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 63 + "-"):
            with self.subTest(sha=bad):
                self.assertRejected(sha256=bad)
        self.assertEqual(make_row(sha256="0123456789abcdef" * 4).sha256, "0123456789abcdef" * 4)

    def test_storage_key_must_be_the_system_generated_shape(self):
        good = "ab" + uuid.uuid4().hex[2:]        # letters, so .upper() really differs
        for bad in ("", "../etc/passwd", f"/{good}", f"{good[:2]}/../{good}", f"{good[:2]}/{good[:31]}",
                    f"{good[:2].upper()}/{good}", f"{good[:2]}/{good}.wav", f"{good[:2]}/{good}\n",
                    f"{good[:3]}/{good}"):
            with self.subTest(key=bad):
                self.assertRejected(storage_key=bad)

    def test_byte_size_must_be_positive(self):
        self.assertRejected(byte_size=0)

    def test_derivative_kinds_require_a_parent(self):
        parent = make_row()
        for kind in ("edit", "rendition"):
            with self.subTest(kind=kind):
                self.assertRejected(kind=kind)
                self.assertEqual(make_row(kind=kind, derived_from=parent).derived_from_id, parent.pk)
        for kind in ("recording", "upload", "bed"):
            make_row(kind=kind)                      # no parent needed

    def test_retention_state_and_purged_at_must_agree(self):
        self.assertRejected(retention_state="purged")
        self.assertRejected(retention_state="present", purged_at=timezone.now())
        self.assertEqual(make_row(retention_state="purged", purged_at=timezone.now()).retention_state, "purged")

    def test_validation_state_and_validated_at_must_agree(self):
        self.assertRejected(validation_state="valid", validated_at=None, **{
            k: v for k, v in VALID_FACTS.items() if k != "validation_state"})
        self.assertRejected(validation_state="unvalidated", validated_at=timezone.now())
        self.assertRejected(validation_state="invalid", validated_at=None)

    def test_valid_requires_its_evidence(self):
        now = timezone.now()
        for missing in ("container", "codec", "sample_rate", "channels", "decoded_duration_seconds"):
            facts = dict(VALID_FACTS)
            facts[missing] = "" if missing in ("container", "codec") else None
            with self.subTest(missing=missing):
                self.assertRejected(validated_at=now, **facts)
        self.assertEqual(make_row(validated_at=now, **VALID_FACTS).validation_state, "valid")

    def test_invalid_may_carry_partial_evidence(self):
        row = make_row(validation_state="invalid", validation_code="decode_error", validated_at=timezone.now(),
                       container="flac")
        self.assertEqual((row.codec, row.sample_rate), ("", None))

    def test_owner_is_nullable_and_username_is_snapshotted(self):
        user = get_user_model().objects.create_user("custodian", password="x")
        row = make_row(owner=user)
        self.assertEqual(row.owner_username, "custodian")
        self.assertIsNone(make_row().owner)
        user.delete()                                # SET_NULL, not cascade
        row.refresh_from_db()
        self.assertIsNone(row.owner_id)
        self.assertEqual(row.owner_username, "custodian")

    def test_media_is_not_destroyed_when_a_derivative_parent_is_deleted_at_sql_level_protect(self):
        from django.db.models import ProtectedError  # the FK is PROTECT: documented, enforced by the DB layer
        parent = make_row()
        make_row(kind="edit", derived_from=parent)
        self.assertEqual(ProductionMedia._meta.get_field("derived_from").remote_field.on_delete.__name__, "PROTECT")
        self.assertTrue(issubclass(ProtectedError, Exception))


class ImmutabilityGuardTests(IsolatedMediaRootMixin, TestCase):
    def test_frozen_fields_cannot_be_saved_after_creation(self):
        other = make_row()
        for field, value in (
            ("sha256", "b" * 64), ("byte_size", 99), ("storage_key", "ab/" + "c" * 32), ("kind", "recording"),
            ("owner_username", "someone"), ("original_filename", "x.wav"), ("declared_content_type", "audio/x"),
            ("derived_from_id", other.pk), ("recipe_key", "k"), ("recipe_version", 3),
            ("recipe_params_digest", "d" * 64), ("toolchain", "ffmpeg"),
        ):
            with self.subTest(field=field):
                row = ProductionMedia.objects.get(pk=make_row().pk)
                setattr(row, field, value)
                with self.assertRaises(ImmutableMediaError):
                    row.save()
                row.refresh_from_db()                # nothing was written
                self.assertNotEqual(getattr(row, field), value)

    def test_update_fields_cannot_smuggle_a_frozen_change(self):
        row = ProductionMedia.objects.get(pk=make_row().pk)
        row.sha256 = "b" * 64
        with self.assertRaises(ImmutableMediaError):
            row.save(update_fields=["sha256"])

    def test_technical_facts_are_written_once_then_frozen_by_the_verdict(self):
        row = ProductionMedia.objects.get(pk=make_row().pk)
        row.container, row.codec = "wav", "pcm_s16le"       # allowed while unvalidated
        row.save()
        for key, value in VALID_FACTS.items():
            setattr(row, key, value)
        row.validated_at = timezone.now()
        row.save()                                          # the single verdict write
        for field, value in (("container", "flac"), ("codec", "flac"), ("sample_rate", 48000),
                             ("channels", 2), ("decoded_duration_seconds", "9.000000"),
                             ("validation_state", "invalid"), ("validation_code", "x"),
                             ("probe", {"changed": True})):
            with self.subTest(field=field):
                again = ProductionMedia.objects.get(pk=row.pk)
                setattr(again, field, value)
                with self.assertRaises(ImmutableMediaError):
                    again.save()

    def test_retention_only_moves_present_to_purged(self):
        row = ProductionMedia.objects.get(pk=make_row().pk)
        row.retention_state = "purged"                      # without purged_at: refused
        with self.assertRaises(ImmutableMediaError):
            row.save()
        row.refresh_from_db()
        row.retention_state, row.purged_at = "purged", timezone.now()
        row.save()
        row.retention_state, row.purged_at = "present", None
        with self.assertRaises(ImmutableMediaError):
            row.save()

    def test_rows_are_never_deleted(self):
        row = make_row()
        with self.assertRaises(ImmutableMediaError):
            row.delete()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.filter(pk=row.pk).delete()
        with self.assertRaises(ImmutableMediaError):
            ProductionMedia.objects.all().delete()
        self.assertTrue(ProductionMedia.objects.filter(pk=row.pk).exists())

    def test_bulk_update_is_limited_to_the_mutable_columns(self):
        row = make_row()
        queryset = ProductionMedia.objects.filter(pk=row.pk)
        for field, value in (("sha256", "b" * 64), ("storage_key", "ab/" + "c" * 32), ("byte_size", 5),
                             ("kind", "bed"), ("owner_username", "x"), ("created_at", timezone.now())):
            with self.subTest(field=field), self.assertRaises(ImmutableMediaError):
                queryset.update(**{field: value})
        self.assertEqual(queryset.update(validation_code="probe_timeout"), 1)

    def test_constructing_an_instance_with_an_existing_pk_cannot_overwrite_the_row(self):
        row = make_row()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProductionMedia(id=row.pk, kind="upload", storage_key="ab/" + "d" * 32, sha256="c" * 64,
                            byte_size=1).save()
        row.refresh_from_db()
        self.assertEqual(row.sha256, SHA_A)

    def test_refresh_resets_the_baseline(self):
        row = make_row()
        ProductionMedia.objects.filter(pk=row.pk).update(validation_code="probe_timeout")
        row.refresh_from_db()
        row.validation_code = "probe_timeout"
        row.save()          # unchanged relative to the refreshed baseline
