"""Regression coverage for the FX Cart admin drag-drop/file-picker upload
widget (library/templates/admin/library/fxcart/change_form.html).

The widget's JS depends on document.getElementById('id_filepath') --
Django's own standard field, rendered by {{ block.super }} -- existing at
the time the script runs. The bug: the custom change_form_template ran its
<script> block BEFORE {{ block.super }}, so id_filepath didn't exist yet
and every upload path (drag/drop and Choose file) failed with "Cannot find
the filepath field on this page." These tests assert the fix stays in
place: the upload panel renders, the standard filepath field renders, and
the dependent script appears strictly after it in the rendered response --
not just that all three pieces are present somewhere on the page."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from library.models import FXCart


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class FXCartAdminUploadWidgetTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("admin", "admin@example.invalid", "password")
        self.client.force_login(self.staff)

    def _assert_upload_widget_ordering(self, content):
        """Shared assertion: upload panel < normal filepath field <
        dependent script, all present exactly once each where relevant."""
        panel_pos = content.index('id="fxUploadPanel"')
        filepath_pos = content.index('id="id_filepath"')
        script_pos = content.index("document.getElementById('id_filepath')")
        self.assertLess(
            panel_pos, filepath_pos,
            "upload panel should render above the normal FX Cart fields",
        )
        self.assertLess(
            filepath_pos, script_pos,
            "id_filepath must be rendered before the dependent upload script runs "
            "(this is the exact ordering bug: block.super must precede the <script> block)",
        )

    def test_add_form_renders_upload_panel(self):
        response = self.client.get(reverse("admin:library_fxcart_add"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="fxUploadPanel"')
        self.assertContains(response, 'id="fxUploadInput"')
        self.assertContains(response, 'id="fxUploadPickBtn"')

    def test_add_form_renders_normal_filepath_field(self):
        response = self.client.get(reverse("admin:library_fxcart_add"))
        self.assertContains(response, 'id="id_filepath"')
        self.assertContains(response, 'name="filepath"')

    def test_add_form_renders_upload_script(self):
        response = self.client.get(reverse("admin:library_fxcart_add"))
        self.assertContains(response, "/api/fx/cart-upload/")
        self.assertContains(response, "document.getElementById('id_filepath')")

    def test_add_form_ordering_filepath_field_before_script(self):
        response = self.client.get(reverse("admin:library_fxcart_add"))
        self.assertEqual(response.status_code, 200)
        self._assert_upload_widget_ordering(response.content.decode())

    def test_add_form_block_super_not_duplicated(self):
        # Regression against a specific way this fix can be gotten wrong:
        # leaving the original trailing {{ block.super }} in place while
        # adding a new one earlier would render the standard fieldset
        # (and therefore id="id_filepath") twice.
        response = self.client.get(reverse("admin:library_fxcart_add"))
        content = response.content.decode()
        self.assertEqual(content.count('id="id_filepath"'), 1)

    def test_change_form_renders_upload_panel_and_ordering(self):
        cart = FXCart.objects.create(name="Laugh Track", filepath="/srv/isadoraair/carts/laugh.wav")
        response = self.client.get(reverse("admin:library_fxcart_change", args=[cart.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="fxUploadPanel"')
        self.assertContains(response, 'id="id_filepath"')
        self._assert_upload_widget_ordering(response.content.decode())

    def test_change_form_shows_existing_filepath_value(self):
        cart = FXCart.objects.create(name="Laugh Track", filepath="/srv/isadoraair/carts/laugh.wav")
        response = self.client.get(reverse("admin:library_fxcart_change", args=[cart.pk]))
        self.assertContains(response, "/srv/isadoraair/carts/laugh.wav")

    def test_manual_filepath_entry_still_a_normal_text_input(self):
        # The upload widget is additive -- typing/editing filepath by hand
        # must remain possible. A disabled/readonly input would silently
        # break that even though the other assertions above would still pass.
        response = self.client.get(reverse("admin:library_fxcart_add"))
        content = response.content.decode()
        start = content.index('id="id_filepath"')
        # Look back a short, bounded window for the opening <input ...> tag
        # this id belongs to, without coupling to the whole widget's markup.
        tag_start = content.rindex("<input", 0, start)
        tag_end = content.index(">", start)
        tag = content[tag_start:tag_end]
        self.assertNotIn("readonly", tag)
        self.assertNotIn("disabled", tag)
