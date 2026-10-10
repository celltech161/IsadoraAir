"""C2B -- remaining evidence: delivery and relocation across two REAL
filesystems, the read-only health observer, the admin diagnostics."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext

from library.models import Track
from production.services import layout
from spoken import delivery, projection_health
from spoken.models import AssetState, SpokenAsset

from .projection_support import FFMPEG, ProjectionFixtures
from .test_domain import make_category


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class CrossFilesystemTests(ProjectionFixtures):
    def test_promotion_and_relocation_when_the_library_is_another_filesystem(self):
        shm = Path("/dev/shm")
        layout.ensure_layout()
        if not shm.is_dir() or os.stat(shm).st_dev == os.stat(layout.media_root()).st_dev:
            self.skipTest("no second filesystem available")
        library = Path(tempfile.mkdtemp(prefix="spoken-xfs-", dir=shm))
        self.addCleanup(shutil.rmtree, library, True)
        with override_settings(LIBRARY_ROOT=str(library)):
            self.assertNotEqual(os.stat(library).st_dev, os.stat(layout.media_root()).st_dev)
            ctype = self.make_type()
            a, first = self.delivered(ctype)
            b, second = self.delivered(ctype)
            asset = SpokenAsset.objects.get(pk=second.asset_id)
            self.assertEqual((library / asset.library_relpath).read_bytes(),
                             layout.resolve_storage_path(b.rendition_media.storage_key).read_bytes())
            ctype.target_category = make_category("NewsDesk")
            ctype.save()
            delivery.reconcile()
            asset.refresh_from_db()
            self.assertTrue((library / asset.library_relpath).is_file())
            self.assertEqual(sorted(p.parent.name for p in library.rglob("spoken-*.flac")), ["NewsDesk"])
            self.assertEqual(Track.objects.get(pk=asset.track_id).filepath, str(library / asset.library_relpath))


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class HealthAndAdminTests(ProjectionFixtures):
    def test_the_observer_is_read_only_and_reports_what_reconcile_repairs(self):
        ctype = self.make_type()
        item, result = self.delivered(ctype)
        self.assertEqual(projection_health.observe()["issues"], [])
        asset = SpokenAsset.objects.get(pk=result.asset_id)
        self.library_file(asset.library_relpath).unlink()
        with CaptureQueriesContext(connection) as queries:
            observed = projection_health.observe()
        sql = [query["sql"].upper() for query in queries.captured_queries]
        self.assertTrue(sql and all(s.startswith("SELECT") and "FOR UPDATE" not in s for s in sql))  # read-only
        self.assertEqual([i["issue"] for i in observed["issues"]], ["file_missing"])
        self.assertFalse(self.library_file(asset.library_relpath).exists())
        delivery.reconcile()
        self.assertTrue(projection_health.observe()["healthy"])

    def test_admin_diagnostics_render_read_only(self):
        ctype = self.make_type()
        _, result = self.delivered(ctype)
        admin_user = get_user_model().objects.create_superuser("diag", "d@example.invalid", "pw")
        client = Client()
        client.force_login(admin_user)
        page = client.get(f"/admin/spoken/spokenasset/{result.asset_id}/change/", secure=True)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn(SpokenAsset.objects.get(pk=result.asset_id).library_relpath, body)
        self.assertIn(">ok<", body.replace(" ", ""))
        self.assertEqual(client.post(f"/admin/spoken/spokenasset/{result.asset_id}/delete/", secure=True)
                         .status_code, 403)
