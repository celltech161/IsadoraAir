"""r0083 -- branding semantics cleanup: UITheme.logo is the IsadoraAir/
Administration identity (Django Admin header only); UITheme.station_logo
is the ordinary front-end/login/welcome brand. Real Django template/
client rendering tests, not source-string matching."""
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string
from django.test import Client, TestCase

from library.models import UITheme

User = get_user_model()
BUNDLED_LOGO_PATH = "library/img/isadoraair_logo.png"


def _make_theme_image(name):
    from django.core.files.uploadedfile import SimpleUploadedFile

    # Minimal valid 1x1 PNG -- real bytes, real ImageField validation,
    # no external fixture file needed.
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415408d763f8ffff3f0005fe02fea7f2c8400000000049"
        "454e44ae426082"
    )
    return SimpleUploadedFile(name, png_bytes, content_type="image/png")


class FrontEndBrandTests(TestCase):
    def setUp(self):
        UITheme.objects.all().delete()

    def test_configured_station_logo_is_used(self):
        theme = UITheme.load()
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        rendered = render_to_string("base.html", {"ui_theme": theme, "nav_menu_items": []})
        self.assertIn(theme.station_logo.url, rendered)

    def test_logo_field_is_never_used_as_the_front_end_brand(self):
        theme = UITheme.load()
        theme.logo = _make_theme_image("product.png")
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        rendered = render_to_string("base.html", {"ui_theme": theme, "nav_menu_items": []})
        self.assertNotIn(theme.logo.url, rendered)
        self.assertIn(theme.station_logo.url, rendered)

    def test_blank_station_logo_falls_back_to_bundled_logo(self):
        theme = UITheme.load()
        rendered = render_to_string("base.html", {"ui_theme": theme, "nav_menu_items": []})
        self.assertIn(BUNDLED_LOGO_PATH, rendered)


class LoginAuthWelcomeBrandTests(TestCase):
    def setUp(self):
        UITheme.objects.all().delete()
        self.user = User.objects.create_superuser(
            username="brandtest", email="brandtest@example.com", password="pw12345!",
        )
        self.client = Client()

    def test_configured_station_logo_appears_once_on_login(self):
        theme = UITheme.load()
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        resp = self.client.get("/login/", follow=True)
        content = resp.content.decode()
        self.assertEqual(content.count(theme.station_logo.url), 1)

    def test_product_logo_not_additionally_stacked_on_login(self):
        theme = UITheme.load()
        theme.logo = _make_theme_image("product.png")
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        resp = self.client.get("/login/", follow=True)
        content = resp.content.decode()
        self.assertNotIn(theme.logo.url, content)
        self.assertEqual(content.count(theme.station_logo.url), 1)

    def test_blank_station_logo_uses_bundled_fallback_on_login(self):
        resp = self.client.get("/login/", follow=True)
        content = resp.content.decode()
        self.assertIn(BUNDLED_LOGO_PATH, content)
        self.assertEqual(content.count(BUNDLED_LOGO_PATH), 1)

    def test_configured_station_logo_appears_once_on_welcome(self):
        theme = UITheme.load()
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        self.client.force_login(self.user)
        resp = self.client.get("/welcome/", follow=True)
        content = resp.content.decode()
        self.assertEqual(content.count(theme.station_logo.url), 1)

    def test_product_logo_not_additionally_stacked_on_welcome(self):
        theme = UITheme.load()
        theme.logo = _make_theme_image("product.png")
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        self.client.force_login(self.user)
        resp = self.client.get("/welcome/", follow=True)
        content = resp.content.decode()
        self.assertNotIn(theme.logo.url, content)
        self.assertEqual(content.count(theme.station_logo.url), 1)

    def test_blank_station_logo_uses_bundled_fallback_on_welcome(self):
        self.client.force_login(self.user)
        resp = self.client.get("/welcome/", follow=True)
        content = resp.content.decode()
        self.assertIn(BUNDLED_LOGO_PATH, content)


class DjangoAdminBrandingTests(TestCase):
    def setUp(self):
        UITheme.objects.all().delete()
        self.user = User.objects.create_superuser(
            username="admintest", email="admintest@example.com", password="pw12345!",
        )
        self.client = Client()
        self.client.force_login(self.user)

    def test_default_django_administration_text_is_absent(self):
        resp = self.client.get("/admin/", follow=True)
        self.assertNotIn("Django administration", resp.content.decode())

    def test_administration_text_is_present(self):
        resp = self.client.get("/admin/", follow=True)
        self.assertIn("Administration", resp.content.decode())

    def test_configured_ui_theme_logo_is_used_by_admin(self):
        theme = UITheme.load()
        theme.logo = _make_theme_image("product.png")
        theme.save()
        resp = self.client.get("/admin/", follow=True)
        self.assertIn(theme.logo.url, resp.content.decode())

    def test_blank_ui_theme_logo_uses_bundled_fallback_in_admin(self):
        resp = self.client.get("/admin/", follow=True)
        self.assertIn(BUNDLED_LOGO_PATH, resp.content.decode())

    def test_station_logo_is_never_substituted_into_admin_identity(self):
        theme = UITheme.load()
        theme.station_logo = _make_theme_image("station.png")
        theme.save()
        resp = self.client.get("/admin/", follow=True)
        self.assertNotIn(theme.station_logo.url, resp.content.decode())
        # Falls back to the bundled product logo, never the station one.
        self.assertIn(BUNDLED_LOGO_PATH, resp.content.decode())

    def test_admin_login_page_also_carries_the_override(self):
        """The admin login page (rendered while anonymous) uses the same
        admin/base_site.html override, proven separately from the
        already-authenticated /admin/ index above."""
        self.client.logout()
        resp = self.client.get("/admin/login/", follow=True)
        content = resp.content.decode()
        self.assertNotIn("Django administration", content)
        self.assertIn("Administration", content)
        self.assertIn(BUNDLED_LOGO_PATH, content)


class UIThemeFieldLabelTests(TestCase):
    def test_logo_help_text_describes_administration_identity(self):
        field = UITheme._meta.get_field("logo")
        self.assertIn("Administration", field.help_text)
        self.assertIn("bundled IsadoraAir logo", field.help_text)

    def test_station_logo_help_text_describes_front_end_use(self):
        field = UITheme._meta.get_field("station_logo")
        self.assertIn("Station-facing", field.help_text)
        self.assertIn("bundled IsadoraAir logo", field.help_text)

    def test_fields_are_not_renamed(self):
        names = {f.name for f in UITheme._meta.get_fields()}
        self.assertIn("logo", names)
        self.assertIn("station_logo", names)
