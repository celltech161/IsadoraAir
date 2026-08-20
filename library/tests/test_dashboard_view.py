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

    def test_remote_mic_vu_meter_plumbing_renders(self):
        """Roadmap 4.1 -- the Remote Mic PTT button's VU-meter-as-fill
        plumbing must render on the normal Dashboard: the button itself
        (unchanged id/label/click-handler), the CSS custom property it's
        driven by, the fill pseudo-element rule, and the JS function
        that sets the property from the existing levels poll."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")
        self.assertIn('id="remoteDjGateBtn"', content)
        self.assertIn('onclick="toggleRemoteDjGate()"', content)
        self.assertIn("--remote-dj-vu-pct", content)
        self.assertIn("#remoteDjGateBtn::before", content)
        self.assertIn("function renderRemoteDjVu(remoteDj)", content)
        # Must be wired into the EXISTING poll, not a new one -- the
        # only setInterval driving level polling stays at 100ms, and
        # renderRemoteDjVu is called from inside pollLevels(), not from
        # a second interval of its own.
        self.assertEqual(content.count("setInterval(pollLevels"), 1)
        self.assertIn("renderRemoteDjVu(data.remote_dj)", content)

    def test_remote_mic_ptt_off_clears_vu_fill_immediately(self):
        """Roadmap 4.1 follow-up -- toggleRemoteDjGate() must reset
        --remote-dj-vu-pct to 0% the instant THIS browser's own
        active:false request succeeds, rather than waiting for the
        next ~1Hz /api/engine/status/ poll (verified live via a
        Playwright-driven timing test against the real rendered page
        during development; this is the accompanying static regression
        guard against the fix silently regressing). Must not touch
        .active/LIVE itself -- that stays exclusively driven by
        renderRemoteDjGate()/the status poll."""
        response = self.client.get("/")
        content = response.content.decode("utf-8")
        self.assertIn("} else if (!desiredActive) {", content)
        self.assertIn("btn.style.setProperty('--remote-dj-vu-pct', '0%');", content)
        # The reset call must live inside toggleRemoteDjGate, after the
        # fetch resolves -- not inside renderRemoteDjGate/renderRemoteDjVu
        # (which would make it re-run on every poll instead of once per
        # successful click).
        toggle_fn = content[content.index("async function toggleRemoteDjGate"):content.index("async function rdjRunCalibration")]
        self.assertIn("btn.style.setProperty('--remote-dj-vu-pct', '0%');", toggle_fn)
        self.assertNotIn("classList.toggle('active'", toggle_fn)


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

    def test_remote_mic_vu_meter_plumbing_renders_in_remote_dj_mode(self):
        """Roadmap 4.1 -- implemented once in the shared template, so
        /remote-dj/ must show the identical Remote Mic meter plumbing
        as the full Dashboard (test_dashboard_view.DashboardPageRenderTests
        .test_remote_mic_vu_meter_plumbing_renders), not a second copy or
        a degraded version. The Remote Mic button itself is NOT inside
        the `{% if mode != 'remote_dj' %}` block that hides Studio Mic
        PTT -- it must still be present and clickable here."""
        response = self.client.get("/remote-dj/")
        content = response.content.decode("utf-8")
        self.assertIn('id="remoteDjGateBtn"', content)
        self.assertIn('onclick="toggleRemoteDjGate()"', content)
        self.assertIn("--remote-dj-vu-pct", content)
        self.assertIn("function renderRemoteDjVu(remoteDj)", content)
        # Studio Mic PTT (operator-only) stays hidden in this mode --
        # confirms the shared template's existing mode-gating is
        # undisturbed by this change.
        self.assertNotIn('id="micPttBtn"', content)
