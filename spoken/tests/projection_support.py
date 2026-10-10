"""Fixtures for the C2B projection tests: a private LIBRARY_ROOT and media
root, content types per delivery mode, and processed items whose renditions
have REAL FLAC bytes in production storage (raw-inserted executor-shaped
rows -- the C2A render path itself is exercised by the end-to-end tests)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import uuid
from datetime import timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone

from production.models import ProductionMedia
from production.services import layout, render
from production.tests.support import IsolatedMediaRootMixin, make_row
from spoken import delivery, library_files, transitions
from spoken.models import DeliveryMode, ItemState, SpokenContentType, SpokenItem

from .test_domain import make_category, media

User = get_user_model()
S = ItemState
FFMPEG = shutil.which("ffmpeg")


@lru_cache(maxsize=64)
def flac(milliseconds: int) -> bytes:
    """A distinct small FLAC per length (48 kHz stereo)."""
    with tempfile.TemporaryDirectory(prefix="spoken-flac-") as directory:
        path = os.path.join(directory, "x.flac")
        subprocess.run([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=440:duration={milliseconds / 1000}:sample_rate=48000", "-ac", "2",
                        "-c:a", "flac", path], check=True)
        return Path(path).read_bytes()


class IsolatedLibraryRootMixin:
    def setUp(self):
        super().setUp()
        self._library_dir = tempfile.TemporaryDirectory(prefix="spoken-library-")
        self.library = Path(self._library_dir.name) / "music"
        self.library.mkdir()
        override = override_settings(LIBRARY_ROOT=str(self.library))
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self._library_dir.cleanup)

    def library_file(self, relpath) -> Path:
        return self.library / relpath

    def sha_on_disk(self, relpath):
        path = self.library_file(relpath)
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


class ProjectionMixin(IsolatedLibraryRootMixin, IsolatedMediaRootMixin):
    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user(f"owner-{uuid.uuid4().hex[:6]}")
        self.manager = User.objects.create_user(f"manager-{uuid.uuid4().hex[:6]}", is_staff=True)
        self._lengths = iter(range(700, 100000, 37))

    def make_type(self, mode=DeliveryMode.CURRENT_EDITION, *, code=None, category="LocalNews", **overrides):
        values = dict(code=code or f"t-{uuid.uuid4().hex[:8]}", name="News (test)", delivery_mode=mode,
                      recipe_key="lcl_psa", enabled=True, requires_review=False, deliver_ready_to_air=True,
                      artist_label="KOGR News", target_category=make_category(category) if category else None)
        values.update(overrides)
        return SpokenContentType.objects.create(**values)

    def stored_rendition(self, item, data: bytes) -> ProductionMedia:
        media_id = uuid.uuid4()
        key = layout.storage_key_for(media_id)
        layout.ensure_layout()
        path = layout.resolve_storage_path(key)
        layout.ensure_durable_dir(path.parent)
        path.write_bytes(data)
        os.chmod(path, layout.MEDIA_FILE_MODE)
        ctype = SpokenContentType.objects.get(pk=item.content_type_id)
        with transaction.atomic():
            expected = render.expected(ctype.recipe_key, ctype.recipe_params)
        return make_row(id=media_id, kind=ProductionMedia.KIND_RENDITION, storage_key=key,
                        sha256=hashlib.sha256(data).hexdigest(), byte_size=len(data),
                        derived_from=item.submission_media, recipe_key=expected.recipe_key,
                        recipe_version=expected.recipe_version, recipe_params_digest=expected.digest,
                        container="flac", codec="flac", sample_rate=48000, channels=2,
                        decoded_duration_seconds=Decimal("1.500000"), validation_state="valid",
                        validation_code="ok", validated_at=timezone.now())

    def processed(self, ctype, *, title="t", data=None, owner=None) -> SpokenItem:
        """A processed item of ``ctype`` whose rendition has real bytes."""
        item = transitions.create_item(ctype, actor=owner or self.owner, title=title)
        item = transitions.record_validation(transitions.submit(item, media(), actor=owner or self.owner))
        item, token = transitions.start_processing(item, lease_seconds=600)
        rendition = self.stored_rendition(item, data or flac(next(self._lengths)))
        evidence = render.RenderEvidence(rendition_id=str(rendition.pk), source_id=str(rendition.derived_from_id),
                                         recipe_key=rendition.recipe_key, recipe_version=rendition.recipe_version,
                                         provenance_digest=rendition.recipe_params_digest)
        return transitions.complete_processing(item, lease_token=token, evidence=evidence)

    def delivered(self, ctype, **kwargs):
        item = self.processed(ctype, **kwargs)
        return item, delivery.deliver_item(item)

    def expire_soon(self, ctype, seconds=60):
        ctype.default_lifetime = timedelta(seconds=seconds)
        ctype.save()


class ProjectionFixtures(ProjectionMixin, TestCase):
    pass


def later(seconds):
    return timezone.now() + timedelta(seconds=seconds)
