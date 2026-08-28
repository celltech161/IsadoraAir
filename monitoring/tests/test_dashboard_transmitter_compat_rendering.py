"""r0015 -- restored WRJE legacy TX300v3 references must render using the
existing VSWR / degrees-C card presentation, not fall through to the
generic "key: value" detail formatter.

`renderCardBody()`'s dispatch previously only recognized `psu.vswr` /
`psu.pa_temperature` inside the `pairedIndicatorRef` branch, which the
newly restored `computed:vswr` / `aio.temp.board` / `aio.temp.dsp`
references never enter (they have no paired legacy COBALT-style
indicator check). This proves the dispatch was extended narrowly rather
than routing through the generic fallback, and that the established
renderers (`renderVswrBody` / `renderPaTempBody`) are reused rather than
a new card type being invented.

`monitoring/dashboard.html` renders its check cards from client-side JS
populated by a polled JSON endpoint, so -- matching this project's
existing convention for asserting on template-embedded JS (see
updatecenter's dashboard/updates tests) -- this is a structural
assertion on the rendered JS source, not a browser-driven test."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(SECURE_SSL_REDIRECT=False)
class DashboardTransmitterCompatRenderingTests(TestCase):
    def setUp(self):
        user = User.objects.create_superuser("dashboard-compat-rendering")
        self.client.force_login(user)

    def _dispatch_block(self):
        response = self.client.get(reverse("monitoring:dashboard"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        start = content.index("function renderCardBody(check, byTxRef) {")
        end = content.index("function renderGroups(checks) {")
        return content[start:end]

    def test_computed_vswr_dispatches_to_the_existing_vswr_renderer(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "computed:vswr") return renderVswrBody(check, check.status);',
            block,
        )

    def test_board_and_dsp_temperature_dispatch_to_the_existing_temperature_renderer(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "aio.temp.board" || check.tx_ref === "aio.temp.dsp") {',
            block,
        )
        self.assertIn("return renderPaTempBody(check, check.status);", block)

    def test_new_dispatch_entries_precede_the_generic_fallback(self):
        """The restored references must be routed BEFORE the generic
        switch/default -- otherwise they'd still render as
        "value: 49 · raw: 49 (C)"."""
        block = self._dispatch_block()
        computed_vswr_index = block.index('check.tx_ref === "computed:vswr"')
        board_temp_index = block.index('check.tx_ref === "aio.temp.board"')
        default_index = block.index("default: return `<div class=\"mon-card-detail\">")
        self.assertLess(computed_vswr_index, default_index)
        self.assertLess(board_temp_index, default_index)

    def test_no_new_card_type_invented_for_the_restored_references(self):
        """Only the two existing renderers (renderVswrBody /
        renderPaTempBody) are reused -- no new render*Body function was
        added for these compatibility references."""
        content = self.client.get(reverse("monitoring:dashboard")).content.decode(
            "utf-8"
        )
        self.assertEqual(content.count("function renderVswrBody("), 1)
        self.assertEqual(content.count("function renderPaTempBody("), 1)

    def test_existing_paired_indicator_dispatch_for_psu_vswr_and_psu_pa_temperature_unchanged(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "psu.vswr") return renderVswrBody(check, colorClass);',
            block,
        )
        self.assertIn(
            'if (check.tx_ref === "psu.pa_temperature") return renderPaTempBody(check, colorClass);',
            block,
        )


@override_settings(SECURE_SSL_REDIRECT=False)
class NativeTx300PowerDashboardRenderingTests(TestCase):
    """r0016: native BW TX300v3 forward/reflected power (meters.pafwd /
    meters.parev) must render using the SAME established C300 renderers
    (renderForwardPowerBody / renderReversePowerBody) as psu.fwd_power /
    psu.rev_power -- not a new renderer, and not the generic fallback.
    Unlike the C300 references, there is no paired legacy indicator
    check for these native parameters, so color comes from the check's
    own calculated status, same treatment as r0015's computed:vswr /
    aio.temp.board / aio.temp.dsp."""

    def setUp(self):
        user = User.objects.create_superuser("dashboard-tx300-power-rendering")
        self.client.force_login(user)

    def _dispatch_block(self):
        response = self.client.get(reverse("monitoring:dashboard"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        start = content.index("function renderCardBody(check, byTxRef) {")
        end = content.index("function renderGroups(checks) {")
        return content[start:end]

    def test_native_forward_power_dispatches_to_the_existing_forward_power_renderer(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "meters.pafwd") return renderForwardPowerBody(check, check.status);',
            block,
        )

    def test_native_reflected_power_dispatches_to_the_existing_reverse_power_renderer(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "meters.parev") return renderReversePowerBody(check, check.status);',
            block,
        )

    def test_native_power_dispatch_precedes_the_generic_fallback(self):
        block = self._dispatch_block()
        forward_index = block.index('check.tx_ref === "meters.pafwd"')
        reverse_index = block.index('check.tx_ref === "meters.parev"')
        default_index = block.index("default: return `<div class=\"mon-card-detail\">")
        self.assertLess(forward_index, default_index)
        self.assertLess(reverse_index, default_index)

    def test_no_new_forward_or_reverse_power_renderer_introduced(self):
        content = self.client.get(reverse("monitoring:dashboard")).content.decode(
            "utf-8"
        )
        self.assertEqual(content.count("function renderForwardPowerBody("), 1)
        self.assertEqual(content.count("function renderReversePowerBody("), 1)

    def test_existing_c300_forward_and_reverse_power_dispatch_unchanged(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "psu.fwd_power") return renderForwardPowerBody(check, colorClass);',
            block,
        )
        self.assertIn(
            'if (check.tx_ref === "psu.rev_power") return renderReversePowerBody(check, colorClass);',
            block,
        )

    def test_r0015_dispatch_entries_remain_unchanged(self):
        block = self._dispatch_block()
        self.assertIn(
            'if (check.tx_ref === "computed:vswr") return renderVswrBody(check, check.status);',
            block,
        )
        self.assertIn(
            'if (check.tx_ref === "aio.temp.board" || check.tx_ref === "aio.temp.dsp") {',
            block,
        )
