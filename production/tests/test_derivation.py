"""Edits and renditions are NEW immutable media derived from the original."""
import hashlib
import os
import stat

from django.test import TestCase

from production.errors import IntakeError, MediaRejected
from production.models import ProductionMedia
from production.services import intake, layout, retention

from .support import IsolatedMediaRootMixin, fixture


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


def original(name="wav16_mono.wav", **kwargs):
    return intake.ingest_stream(FileLike(fixture(name)), kind="recording", validate=True, **kwargs).media


def snapshot(media):
    path = layout.resolve_storage_path(media.storage_key)
    info = path.stat()
    return (hashlib.sha256(path.read_bytes()).hexdigest(), info.st_ino, info.st_mtime_ns, stat.S_IMODE(info.st_mode))


class DerivationTests(IsolatedMediaRootMixin, TestCase):
    def test_a_derivative_is_a_new_media_and_the_original_is_untouched(self):
        parent = original()
        before = snapshot(parent)
        row_before = ProductionMedia.objects.values().get(pk=parent.pk)
        result = intake.ingest_derivative(
            parent, FileLike(fixture("flac.flac")), kind="edit", validate=True,
            recipe_key="trim", recipe_version=2, recipe_params={"start": 1.5, "end": 3.0}, toolchain="ffmpeg 8.0.1",
        )
        child = result.media
        self.assertEqual(child.derived_from_id, parent.pk)
        self.assertEqual(child.kind, "edit")
        self.assertNotEqual(child.pk, parent.pk)
        self.assertNotEqual(child.storage_key, parent.storage_key)
        self.assertNotEqual(child.sha256, parent.sha256)
        self.assertEqual(snapshot(parent), before)                              # bytes, inode, mtime, mode
        self.assertEqual(ProductionMedia.objects.values().get(pk=parent.pk), row_before)
        self.assertEqual(sorted(self.list_files("media")), sorted([parent.storage_key, child.storage_key]))

    def test_recipe_provenance_is_recorded_with_a_canonical_parameter_digest(self):
        parent = original()
        a = intake.ingest_derivative(parent, FileLike(fixture("flac.flac")), kind="rendition", validate=False,
                                     recipe_key="loudnorm", recipe_version=1,
                                     recipe_params={"i": -16, "tp": -1.0, "lra": 5}).media
        b = intake.ingest_derivative(parent, FileLike(fixture("flac.flac")), kind="rendition", validate=False,
                                     recipe_key="loudnorm", recipe_version=1,
                                     recipe_params={"lra": 5, "tp": -1.0, "i": -16}).media      # same, reordered
        self.assertEqual(a.recipe_params_digest, b.recipe_params_digest)
        self.assertEqual(a.recipe_params_digest,
                         hashlib.sha256(b'{"i":-16,"lra":5,"tp":-1.0}').hexdigest())
        self.assertEqual((a.recipe_key, a.recipe_version), ("loudnorm", 1))
        other = intake.ingest_derivative(parent, FileLike(fixture("flac.flac")), kind="rendition", validate=False,
                                         recipe_key="loudnorm", recipe_version=1, recipe_params={"i": -23}).media
        self.assertNotEqual(other.recipe_params_digest, a.recipe_params_digest)

    def test_identical_bytes_still_get_an_independent_identity(self):
        parent = original()
        child = intake.ingest_derivative(parent, FileLike(fixture("wav16_mono.wav")), kind="edit", validate=True).media
        self.assertEqual(child.sha256, parent.sha256)
        self.assertNotEqual((child.pk, child.storage_key), (parent.pk, parent.storage_key))

    def test_lineage_can_be_chained(self):
        first = original()
        second = intake.ingest_derivative(first, FileLike(fixture("flac.flac")), kind="edit", validate=True).media
        third = intake.ingest_derivative(second, FileLike(fixture("mp3.mp3")), kind="rendition", validate=True).media
        self.assertEqual([third.derived_from_id, second.derived_from_id], [second.pk, first.pk])

    def test_a_derivative_is_validated_independently_of_its_parent(self):
        parent = original()
        before = snapshot(parent)
        with self.assertRaises(MediaRejected) as caught:
            intake.ingest_derivative(parent, FileLike(fixture("truncated_flac.flac")), kind="edit", validate=True)
        self.assertEqual(caught.exception.code, "decode_error")
        self.assertEqual(ProductionMedia.objects.count(), 1)
        self.assertEqual(snapshot(parent), before)

    def test_the_parent_must_be_present_and_valid_judged_from_the_database_not_the_instance(self):
        valid = original()
        stale = ProductionMedia.objects.get(pk=valid.pk)                         # a stale "valid, present" copy
        with self.captureOnCommitCallbacks(execute=True):
            retention.purge_media(valid)
        self.assertTrue(stale.is_present)
        with self.assertRaises(IntakeError) as caught:
            intake.ingest_derivative(stale, FileLike(fixture("flac.flac")), kind="edit")
        self.assertEqual(caught.exception.code, "parent_not_usable")
        unvalidated = intake.ingest_stream(FileLike(fixture("flac.flac")), kind="upload", validate=False).media
        with self.assertRaises(IntakeError):
            intake.ingest_derivative(unvalidated, FileLike(fixture("flac.flac")), kind="edit")

    def test_the_request_shape_is_enforced(self):
        parent = original()
        source = lambda: FileLike(b"data")
        cases = [
            dict(kind="edit"),                                                   # derived kind without a parent
            dict(kind="rendition"),
            dict(kind="upload", derived_from=parent),                           # parent on a non-derived kind
            dict(kind="recording", derived_from=parent),
            dict(kind="edit", derived_from=parent, recipe_version=1),           # version without a key
            dict(kind="edit", derived_from=parent, recipe_params={"a": 1}),     # params without a key
            dict(kind="edit", derived_from=parent, recipe_key="k", recipe_version=-1),
            dict(kind="edit", derived_from=parent, recipe_key="k", recipe_version=True),
            dict(kind="edit", derived_from=parent, recipe_key="k", recipe_params={"x": float("nan")}),
            dict(kind="edit", derived_from=parent, recipe_key="k", recipe_params={"x": {1, 2}}),
            dict(kind="unknown"),
        ]
        for kwargs in cases:
            with self.subTest(kwargs={k: v for k, v in kwargs.items() if k != "derived_from"}), \
                    self.assertRaises(IntakeError):
                intake.ingest_stream(source(), validate=False, **kwargs)
        self.assertEqual(ProductionMedia.objects.count(), 1)
        self.assertEqual(self.list_files("incoming"), [])

    def test_an_owner_must_be_a_saved_user(self):
        from django.contrib.auth.models import AnonymousUser
        with self.assertRaises(IntakeError):
            intake.ingest_stream(FileLike(b"data"), kind="upload", validate=False, owner=AnonymousUser())

    def test_a_derived_media_can_never_be_made_without_going_through_a_parent(self):
        from django.db import IntegrityError, transaction
        from .support import make_row
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_row(kind="rendition")
