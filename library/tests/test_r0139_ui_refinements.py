"""r0139 UI refinements, server side: UI Theme admin (played-item darkening slider,
current-item outline colour, deck-transport opacity slider), the values reaching the
Console / Remote DJ pages with r0138's appearance as the default, and the shared base
template's clock-and-hamburger menu markup. Browser behaviour is in
test_console_log_display_browser.py."""
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from library.admin import OpacitySliderWidget, RGBAColorWidget
from library.models import UITheme, validate_css_color

User = get_user_model()
CHANGE = "/admin/library/uitheme/1/change/"
FILE_FIELDS = {"logo", "station_logo", "default_album_art"}


class ThemeFormMixin:
    def setUp(self):
        super().setUp()
        self.admin_user = User.objects.create_superuser("r0139_admin", "a@example.invalid", "pw")
        self.client.force_login(self.admin_user)
        UITheme.load()

    def form_data(self, **overrides):
        """Everything the change form posts, as a browser would send it, with overrides."""
        theme = UITheme.load()
        data = {}
        for field in UITheme._meta.get_fields():
            if not getattr(field, "editable", False) or field.name in FILE_FIELDS or field.name == "id":
                continue
            value = getattr(theme, field.name)
            if isinstance(value, bool):
                if value:
                    data[field.name] = "on"
            else:
                data[field.name] = "" if value is None else str(value)
        data.update({k: v for k, v in overrides.items()})
        return data

    def save(self, **overrides):
        return self.client.post(CHANGE, self.form_data(**overrides))


@override_settings(SECURE_SSL_REDIRECT=False)
class ThemeAdminTests(ThemeFormMixin, TestCase):
    def test_defaults_match_r0138(self):
        theme = UITheme.load()
        self.assertEqual(theme.played_item_darkening, 0.55)                       # was the fixed rgba(0,0,0,0.55)
        self.assertEqual(theme.current_item_outline_color, "rgba(255, 255, 255, 1.00)")   # was the fixed #fff
        self.assertEqual(theme.deck_transport_disabled_opacity, 0.3)
        self.assertIs(theme.show_played_items, False)

    def test_form_renders_the_sliders_and_the_colour_picker_in_the_log_display_fieldset(self):
        html = self.client.get(CHANGE).content.decode()
        fieldset = html[html.index("Console / Remote DJ"):html.index("Deck Transport Buttons")]
        order = [fieldset.index(f'name="{n}"') for n in
                 ("show_played_items", "played_item_darkening", "current_item_outline_color")]
        self.assertEqual(order, sorted(order))                                    # darkening directly under the toggle
        self.assertIn('id="id_played_item_darkening_slider"', fieldset)
        self.assertIn("0% = original category colour", fieldset)
        self.assertIn('id="id_current_item_outline_color_color"', fieldset)     # RGBAColorWidget
        self.assertIn('id="id_current_item_outline_color_alpha"', fieldset)
        transport = html[html.index("Deck Transport Buttons"):]
        self.assertIn('id="id_deck_transport_disabled_opacity_slider"', transport)
        self.assertIn("0% = invisible", transport)
        self.assertIn("100% = fully opaque", transport)
        self.assertNotIn('type="number"', transport[:transport.index("</fieldset>")])

    def test_widgets_are_the_reusable_ones(self):
        form = self.client.get(CHANGE).context["adminform"].form
        self.assertIsInstance(form.fields["played_item_darkening"].widget, OpacitySliderWidget)
        self.assertIsInstance(form.fields["deck_transport_disabled_opacity"].widget, OpacitySliderWidget)
        self.assertIsInstance(form.fields["current_item_outline_color"].widget, RGBAColorWidget)

    def test_saved_values_persist_and_redisplay(self):
        response = self.save(played_item_darkening="0.80", current_item_outline_color="rgba(255, 0, 0, 0.50)",
                             deck_transport_disabled_opacity="0.45")
        self.assertEqual(response.status_code, 302)
        theme = UITheme.load()
        self.assertEqual((theme.played_item_darkening, theme.current_item_outline_color,
                          theme.deck_transport_disabled_opacity), (0.8, "rgba(255, 0, 0, 0.50)", 0.45))
        html = self.client.get(CHANGE).content.decode()
        self.assertRegex(html, r'name="played_item_darkening" id="id_played_item_darkening" value="0\.8"')
        self.assertIn('value="rgba(255, 0, 0, 0.50)"', html)
        self.assertRegex(html, r'name="deck_transport_disabled_opacity" id="[^"]+" value="0\.45"')

    def test_boundaries_are_accepted(self):
        for value in ("0", "0.00", "1", "1.00"):
            with self.subTest(value=value):
                self.assertEqual(self.save(played_item_darkening=value,
                                           deck_transport_disabled_opacity=value).status_code, 302)
                self.assertEqual(UITheme.load().played_item_darkening, float(value))

    def test_out_of_range_darkening_is_rejected_and_nothing_is_stored(self):
        for value in ("1.01", "-0.01", "55", "abc"):
            with self.subTest(value=value):
                response = self.save(played_item_darkening=value)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["adminform"].form.errors.get("played_item_darkening"))
                self.assertEqual(UITheme.load().played_item_darkening, 0.55)

    ACCEPTED = ("#fff", "#fff8", "#FFFFFF", "#FFFFFF80", "#abcdef12", "rgba(255, 255, 255, 1.00)", "rgba(0, 0, 0, 0.00)",
                "rgb(10, 20, 30)", "rgb(255,255,255)", "rgba(12, 34, 56, .5)", "rgba(1, 2, 3, 1)", "rgba(1, 2, 3, 0)",
                "rgba(255, 255, 255, 0.55)")
    REJECTED = ("", "white", "red;}", "url(javascript:alert(1))", "rgba(1,2,3,0.5);x", "#12", "#1", "#12345", "#1234567",
                "#123456789", "#ggg", "#fffffg", "fff", "rgb(999,999,999)", "rgb(256, 0, 0)", "rgba(0, 0, 0, 1.5)",
                "rgba(0, 0, 0, 2)", "rgba(1, 2", "rgb(1, 2)", "rgba(1, 2, 3, )", "rgba(1, 2, 3, 0.5", "rgb(1 2 3)",
                "rgba(1, 2, 3, -0.1)", "rgb(-1, 2, 3)", "hsl(0, 0%, 100%)")

    def test_invalid_outline_colours_are_rejected(self):
        for value in self.REJECTED:
            with self.subTest(value=value):
                response = self.save(current_item_outline_color=value)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["adminform"].form.errors.get("current_item_outline_color"))
                self.assertEqual(UITheme.load().current_item_outline_color, "rgba(255, 255, 255, 1.00)")

    def test_valid_outline_colours_are_accepted_and_stored_exactly(self):
        for value in self.ACCEPTED:
            with self.subTest(value=value):
                self.assertEqual(self.save(current_item_outline_color=value).status_code, 302)
                self.assertEqual(UITheme.load().current_item_outline_color, value)

    def test_the_validator_function_itself(self):
        for value in self.ACCEPTED:
            validate_css_color(value)
        for value in self.REJECTED:
            with self.assertRaises(ValidationError, msg=value):
                validate_css_color(value)

    def test_every_colour_the_widget_can_produce_is_accepted(self):
        # The widget emits rgba(r, g, b, a.toFixed(2)) for any channel 0-255 and alpha 0-100%.
        for r, g, b in ((0, 0, 0), (255, 255, 255), (55, 65, 81), (1, 254, 128)):
            for pct in (0, 1, 25, 50, 55, 99, 100):
                validate_css_color(f"rgba({r}, {g}, {b}, {pct / 100:.2f})")

    def test_model_field_and_unreleased_migration_agree(self):
        import importlib
        migration = importlib.import_module("library.migrations.0102_uitheme_log_display_appearance")
        fields = {op.name: op.field for op in migration.Migration.operations}
        model_field = UITheme._meta.get_field("current_item_outline_color")
        self.assertIn(validate_css_color, model_field.validators)
        self.assertIn(validate_css_color, fields["current_item_outline_color"].validators)
        self.assertNotIn("RegexValidator", [type(v).__name__ for v in model_field.validators])
        self.assertEqual(fields["current_item_outline_color"].default, model_field.default)
        self.assertEqual(fields["played_item_darkening"].default, UITheme._meta.get_field("played_item_darkening").default)
        validate_css_color(model_field.default)

    def test_widget_script_does_not_rewrite_the_stored_value_on_load(self):
        html = RGBAColorWidget().render("c", "#fff", {"id": "id_c"})
        script = html[html.index("<script>"):]
        self.assertEqual(script.count("hidden.value ="), 1)          # only inside update()
        self.assertNotIn("\n    update();", script)                    # no unconditional call at initialisation
        self.assertIn('value="#fff"', html)

    def test_an_existing_station_value_survives_a_save_that_never_touches_the_slider(self):
        theme = UITheme.load()
        theme.deck_transport_disabled_opacity = 0.333
        theme.save()
        self.assertEqual(self.save().status_code, 302)          # the hidden input still carries 0.333
        self.assertEqual(UITheme.load().deck_transport_disabled_opacity, 0.333)

    def test_model_validation(self):
        theme = UITheme.load()
        for field, bad in (("played_item_darkening", 1.5), ("played_item_darkening", -0.2),
                           ("current_item_outline_color", "blue")):
            setattr(theme, field, bad)
            with self.assertRaises(ValidationError, msg=(field, bad)):
                theme.full_clean(exclude=[f for f in (f.name for f in UITheme._meta.fields)
                                          if f != field])
            theme.refresh_from_db()

    def test_deck_transport_field_schema_is_unchanged(self):
        field = UITheme._meta.get_field("deck_transport_disabled_opacity")
        self.assertEqual((type(field).__name__, field.default, field.validators), ("FloatField", 0.3, []))


class SliderWidgetTests(TestCase):
    def render(self, value):
        return OpacitySliderWidget(low_label="invisible", high_label="fully opaque").render("opacity", value, {"id": "id_o"})

    def test_hidden_value_is_the_stored_value_and_slider_shows_percent_on_load(self):
        html = self.render(0.3)
        self.assertIn('name="opacity" id="id_o" value="0.3"', html)
        self.assertIn('type="range" id="id_o_slider" min="0" max="100"', html)
        self.assertIn("Math.round(v * 100)", html)

    def test_blank_and_escaped_values(self):
        self.assertIn('value=""', self.render(None))
        self.assertNotIn("<script>alert", OpacitySliderWidget(low_label="<script>alert(1)</script>").render("o", 0.1))

    def test_slider_only_rewrites_the_value_on_input(self):
        self.assertEqual(len(re.findall(r"hidden\.value =", self.render(0.2))), 1)


@override_settings(SECURE_SSL_REDIRECT=False)
class PageValueTests(ThemeFormMixin, TestCase):
    def test_default_theme_renders_the_r0138_look_on_both_pages(self):
        for path in ("/", "/remote-dj/"):
            html = self.client.get(path).content.decode()
            self.assertIn("--played-item-darkening: 0.550000;", html, path)
            self.assertIn("--current-item-outline: rgba(255, 255, 255, 1.00);", html, path)
            self.assertIn("rgba(0, 0, 0, var(--played-item-darkening))", html)
            self.assertNotIn("linear-gradient(rgba(0, 0, 0, 0.55)", html)
            self.assertIn("--deck-transport-disabled-opacity: 0.3;", html)

    def test_configured_values_reach_both_pages(self):
        self.save(played_item_darkening="0.20", current_item_outline_color="rgba(255, 0, 0, 1.00)")
        for path in ("/", "/remote-dj/"):
            html = self.client.get(path).content.decode()
            self.assertIn("--played-item-darkening: 0.200000;", html, path)
            self.assertIn("--current-item-outline: rgba(255, 0, 0, 1.00);", html, path)

    def test_queue_header_and_markup(self):
        html = self.client.get("/").content.decode()
        for cls in ("qt-th-handle", "qt-th-num", "qt-th-dur", "qt-th-intro", "qt-th-time"):
            self.assertIn(f'class="{cls}"', html)
        self.assertNotIn("&#9776;", html)                    # the hamburger drag glyph is gone
        self.assertIn("const ICON_SORT =", html)
        self.assertIn("ICON_PLAY", html)


@override_settings(SECURE_SSL_REDIRECT=False)
class NavigationMarkupTests(ThemeFormMixin, TestCase):
    def test_clock_is_a_sibling_button_controlling_the_same_menu(self):
        for path in ("/", "/library/", "/remote-dj/"):
            html = self.client.get(path).content.decode()
            clock = re.search(r'<button[^>]*id="navClock"[^>]*>', html).group(0)
            toggle = re.search(r'<button[^>]*id="navToggle"[^>]*>', html).group(0)
            for tag in (clock, toggle):
                self.assertIn('type="button"', tag, path)
                self.assertIn('aria-controls="navLinks"', tag, path)
                self.assertIn('aria-expanded="false"', tag, path)
            # Not nested: the clock button closes before the hamburger opens.
            self.assertRegex(html, r'id="navClock"[^>]*></button>\s*<button[^>]*id="navToggle"')
            self.assertEqual(html.count('id="navClock"'), 1)

    def test_menu_contents_and_logout_are_unchanged(self):
        html = self.client.get("/library/").content.decode()
        self.assertIn('class="nav-logout-form"', html)
        self.assertIn('id="navLinks"', html)


@override_settings(SECURE_SSL_REDIRECT=False)
class OtherColourWidgetConsumerTests(ThemeFormMixin, TestCase):
    """The shared widget's other users render each stored colour untouched into the form
    (the JavaScript preserving it on load is exercised in the browser tests)."""

    def test_category_kind_changelist_carries_stored_colours_verbatim(self):
        from library.models import CategoryKind
        CategoryKind.objects.create(code="ck1", name="Kind One", fill_color="#fff", sort_order=1)
        CategoryKind.objects.create(code="ck2", name="Kind Two", fill_color="rgba(1, 2, 3, 0.4)", sort_order=2)
        html = self.client.get("/admin/library/categorykind/").content.decode()
        hidden = re.findall(r'<input type="hidden" name="form-\d+-fill_color" id="[^"]*" value="([^"]*)">', html)
        self.assertIn("#fff", hidden)
        self.assertIn("rgba(1, 2, 3, 0.4)", hidden)
        self.assertEqual(len(hidden), html.count("_alpha_label\" style"))         # one widget per kind row

    def test_fx_cart_form_carries_both_widgets(self):
        from library.models import FXCart
        cart = FXCart.objects.create(name="Widget Cart", filepath="/tmp/widget-cart.wav", enabled=True,
                                     idle_color="#abc", playing_color="rgba(5, 6, 7, 0.5)")
        html = self.client.get(f"/admin/library/fxcart/{cart.pk}/change/").content.decode()
        self.assertIn('name="idle_color" id="id_idle_color" value="#abc"', html)
        self.assertIn('name="playing_color" id="id_playing_color" value="rgba(5, 6, 7, 0.5)"', html)

    def test_ui_theme_palette_values_render_verbatim(self):
        UITheme.objects.filter(pk=1).update(accent="#4ade80", panel_bg="rgba(15, 23, 42, 0.95)")
        html = self.client.get(CHANGE).content.decode()
        self.assertIn('name="accent" id="id_accent" value="#4ade80"', html)
        self.assertIn('name="panel_bg" id="id_panel_bg" value="rgba(15, 23, 42, 0.95)"', html)


class Migration0102ClassificationTests(TestCase):
    def test_both_operations_classify_as_additive_with_the_shared_validator(self):
        import importlib

        from updatecenter.management.commands.updatecenter_probe import _classify_migration_operation
        migration = importlib.import_module("library.migrations.0102_uitheme_log_display_appearance")
        instance = migration.Migration("0102_uitheme_log_display_appearance", "library")
        results = [_classify_migration_operation(instance, op) for op in instance.operations]
        self.assertEqual([r["classification"] for r in results], ["additive", "additive"], results)
        self.assertEqual([r["operation"] for r in results], ["AddField", "AddField"])
