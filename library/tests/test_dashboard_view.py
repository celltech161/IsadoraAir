"""Regression coverage for dashboard.html itself -- both the FX-Cart/
Coming-Up large-display layout added 2026-08 (has-fx-carts grid, the
new FX Carts card label) and the TLH listener-widget addition.

The primary risk this guards against is a real bug hit while building
this: a CSS comment containing the literal text "{% if fx_carts %}"
(explaining WHEN a rule applies) was parsed by Django's template
engine as a genuine tag, since Django parses `{% %}` everywhere in the
file, including inside <style> blocks/CSS comments -- it left an
unclosed if-block that made the whole template fail to render with a
TemplateSyntaxError. A plain "does dashboard.html render" test would
have caught that immediately; none existed. It does now."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from library.models import FXCart


@override_settings(SECURE_SSL_REDIRECT=False)  # project-wide prod setting; the
# plain-HTTP Django test client would otherwise get a 301 on every request
class DashboardPageRenderTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser("dashuser", "dash@example.invalid", "password")
        self.client.force_login(self.staff)

    def test_renders_with_no_fx_carts(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("coming-up-card", content)
        self.assertIn("listenerTlh", content)
        self.assertIn("resetListenerTlh", content)
        # No carts configured -- the FX panel, its card label, and the
        # has-fx-carts class on the actual queue-panel element must all
        # be absent, not rendered-but-empty. Checked against the exact
        # class= attribute string, not a bare substring search -- the
        # CSS ruleset in <style> legitimately contains the literal text
        # ".queue-panel.has-fx-carts" regardless of whether any cart
        # exists, so a plain "has-fx-carts" in content check would
        # always find that selector and never actually catch a bug here.
        self.assertIn('class="queue-panel" id="queuePanel"', content)
        self.assertNotIn('class="queue-panel has-fx-carts" id="queuePanel"', content)
        self.assertNotIn('<h3 class="fx-panel-label">', content)
        self.assertNotIn('id="fxPanel"', content)

    def test_renders_with_fx_carts(self):
        FXCart.objects.create(name="Test Cart", filepath="/tmp/test-cart.wav", enabled=True)
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn('class="queue-panel has-fx-carts" id="queuePanel"', content)
        self.assertIn('id="fxPanel"', content)
        self.assertIn("coming-up-card", content)
        # The label text itself, matching Coming Up's own label exactly.
        self.assertIn('<h3 class="fx-panel-label">FX Carts</h3>', content)

    def test_disabled_cart_does_not_trigger_fx_layout(self):
        # dashboard_page filters FXCart.objects.filter(enabled=True) --
        # a disabled-only library should render like the no-carts case.
        FXCart.objects.create(name="Disabled Cart", filepath="/tmp/disabled.wav", enabled=False)
        response = self.client.get("/")
        content = response.content.decode("utf-8")
        self.assertNotIn('class="queue-panel has-fx-carts" id="queuePanel"', content)


@override_settings(SECURE_SSL_REDIRECT=False)
class RemoteDjPageRenderTests(TestCase):
    def setUp(self):
        from django.contrib.auth.models import Group
        self.user = User.objects.create_user("dj1", "dj1@example.invalid", "password")
        group, _ = Group.objects.get_or_create(name="remote_dj")
        self.user.groups.add(group)
        self.client.force_login(self.user)

    def test_renders_same_shared_template_successfully(self):
        # /remote-dj/ reuses dashboard.html with mode='remote_dj' --
        # confirms the shared template renders cleanly for that mode
        # too, not just the full console.
        response = self.client.get("/remote-dj/")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("coming-up-card", content)
        self.assertIn("listenerTlh", content)
