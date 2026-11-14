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
from library.models import UITheme

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

    def test_invalid_outline_colours_are_rejected(self):
        for value in ("white", "red;}", "url(javascript:alert(1))", "rgba(1,2,3,0.5);x", "#12", ""):
            with self.subTest(value=value):
                response = self.save(current_item_outline_color=value)
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["adminform"].form.errors.get("current_item_outline_color"))
                self.assertEqual(UITheme.load().current_item_outline_color, "rgba(255, 255, 255, 1.00)")

    def test_valid_outline_colours_are_accepted(self):
        for value in ("rgba(255, 255, 255, 1.00)", "rgba(0, 0, 0, 0.00)", "rgb(10, 20, 30)", "#fff", "#FFFFFF80",
                      "rgba(12, 34, 56, .5)"):
            with self.subTest(value=value):
                self.assertEqual(self.save(current_item_outline_color=value).status_code, 302)
                self.assertEqual(UITheme.load().current_item_outline_color, value)

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
