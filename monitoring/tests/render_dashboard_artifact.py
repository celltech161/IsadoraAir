"""QA helper (not collected by default test discovery): render /monitoring/ inside the guarded test
sandbox and print it between markers, so a browser outside the sandbox can inspect the real page
with every API request stubbed from synthetic data -- no server, database or station service.

    run_with_validation_service.sh <python> manage.py test monitoring.tests.render_dashboard_artifact
"""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse


@override_settings(SECURE_SSL_REDIRECT=False)
class RenderArtifact(TestCase):
    def test_render(self):
        self.client.force_login(User.objects.create_user("render-dash", password="x", is_staff=True))
        page = self.client.get(reverse("monitoring:dashboard")).content.decode()
        print(f"\n=====BEGIN-PAGE=====\n{page}\n=====END-PAGE=====")
        self.assertIn('id="monGroups"', page)
