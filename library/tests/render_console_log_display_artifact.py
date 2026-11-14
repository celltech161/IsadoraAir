"""QA helper (not collected by default test discovery): render the real Console and Remote DJ pages,
with Show played items Off and On, inside the guarded test sandbox, and print them between markers so a
browser outside the sandbox can drive the real page with every API stubbed -- no server, database or
station data involved (Playwright suites cannot run inside the sandbox).

    run_with_validation_service.sh <python> manage.py test library.tests.render_console_log_display_artifact
"""
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from library.models import UITheme


@override_settings(SECURE_SSL_REDIRECT=False)
class RenderArtifact(TestCase):
    def test_render(self):
        self.client.force_login(get_user_model().objects.create_superuser("render_su", "r@example.invalid", "pw"))
        variants = (
            ("OFF", dict(show_played_items=False)),
            ("ON", dict(show_played_items=True)),
            # r0139: non-default appearance, to prove the settings reach the page.
            ("CUSTOM", dict(show_played_items=True, played_item_darkening=0.8,
                            current_item_outline_color="rgba(255, 0, 0, 1.00)")),
        )
        for suffix, values in variants:
            theme = UITheme.load()
            for field, value in values.items():
                setattr(theme, field, value)
            theme.save()
            for name, path in (("console", "/"), ("remote_dj", "/remote-dj/")):
                page = self.client.get(path).content.decode()
                label = f"{name.upper()}-{suffix}"
                print(f"\n=====BEGIN-{label}=====\n{page}\n=====END-{label}=====")
                self.assertIn(f"const SHOW_PLAYED_ITEMS = {'true' if values['show_played_items'] else 'false'};", page)
        # Another page on the shared base template (navigation clock / menu).
        page = self.client.get("/library/").content.decode()
        print(f"\n=====BEGIN-LIBRARY=====\n{page}\n=====END-LIBRARY=====")
        self.assertIn('id="navClock"', page)
