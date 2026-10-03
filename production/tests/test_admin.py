"""Read-only operator diagnostics."""
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from production.admin import ProductionMediaAdmin
from production.models import ProductionMedia
from production.services import intake, layout

from .support import IsolatedMediaRootMixin, fixture


class FileLike:
    def __init__(self, data):
        self.data, self.position = data, 0

    def read(self, size=-1):
        chunk = self.data[self.position:self.position + (size if size and size > 0 else len(self.data))]
        self.position += len(chunk)
        return chunk


class AdminDiagnosticsTests(IsolatedMediaRootMixin, TestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.staff = User.objects.create_superuser("opsadmin", "ops@example.com", "pw-not-printed")
        self.client.force_login(self.staff)
        self.media = intake.ingest_stream(FileLike(fixture("flac.flac")), kind="recording", validate=True,
                                          owner=self.staff, original_filename="take 1.flac").media

    # The site redirects plain HTTP (SECURE_SSL_REDIRECT): speak HTTPS like a browser.
    def get(self, url):
        return self.client.get(url, secure=True)

    def post(self, url, data=None):
        return self.client.post(url, data or {}, secure=True)

    def url(self, name, *args):
        return reverse(f"admin:production_productionmedia_{name}", args=args)

    def test_the_list_and_detail_pages_show_the_diagnostic_fields(self):
        listing = self.get(self.url("changelist"))
        self.assertEqual(listing.status_code, 200)
        self.assertContains(listing, str(self.media.pk))
        detail = self.get(self.url("change", self.media.pk))
        self.assertEqual(detail.status_code, 200)
        for expected in (str(self.media.pk), self.media.sha256, self.media.storage_key, "opsadmin", "flac",
                         "take 1.flac", "Valid", "present, size matches"):
            self.assertContains(detail, expected)

    def test_the_storage_status_reflects_the_filesystem(self):
        path = layout.resolve_storage_path(self.media.storage_key)
        admin_instance = ProductionMediaAdmin(ProductionMedia, admin.site)
        self.assertEqual(admin_instance.storage_status(self.media), "present, size matches")
        path.chmod(0o640)
        path.write_bytes(b"x")
        self.assertIn("SIZE MISMATCH", admin_instance.storage_status(self.media))
        path.unlink()
        self.assertEqual(admin_instance.storage_status(self.media), "MISSING")
        ProductionMedia.objects.filter(pk=self.media.pk).update(retention_state="purged", purged_at=self.media.created_at)
        purged = ProductionMedia.objects.get(pk=self.media.pk)
        self.assertEqual(admin_instance.storage_status(purged), "purged (no bytes expected)")

    def test_nothing_can_be_added_changed_or_deleted_even_by_a_superuser(self):
        before = ProductionMedia.objects.values().get(pk=self.media.pk)
        self.assertEqual(self.get(self.url("add")).status_code, 403)
        self.assertEqual(self.post(self.url("add"), {"kind": "upload"}).status_code, 403)
        self.assertEqual(self.post(self.url("change", self.media.pk), {"sha256": "b" * 64}).status_code, 403)
        self.assertEqual(self.get(self.url("delete", self.media.pk)).status_code, 403)
        self.assertEqual(self.post(self.url("delete", self.media.pk), {"post": "yes"}).status_code, 403)
        response = self.post(self.url("changelist"), {
            "action": "delete_selected", "_selected_action": [str(self.media.pk)], "post": "yes",
        })
        self.assertIn(response.status_code, (200, 302))
        self.assertEqual(ProductionMedia.objects.values().get(pk=self.media.pk), before)

    def test_every_displayed_field_is_read_only_and_there_are_no_bulk_actions(self):
        instance = ProductionMediaAdmin(ProductionMedia, admin.site)
        shown = {name for _title, options in instance.fieldsets for name in options["fields"]}
        self.assertEqual(shown - set(instance.readonly_fields), set())
        model_fields = {field.name for field in ProductionMedia._meta.get_fields() if field.concrete}
        self.assertEqual(model_fields - shown, set())               # nothing hidden from the diagnostics
        self.assertEqual(instance.get_actions(None), {})

    def test_a_non_staff_user_cannot_see_it(self):
        self.client.logout()
        user = get_user_model().objects.create_user("viewer", password="pw-not-printed")
        self.client.force_login(user)
        self.assertNotEqual(self.get(self.url("changelist")).status_code, 200)
