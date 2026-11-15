"""QA helper (not collected by default test discovery): render the dedicated Remote DJ page and
the full Console for representative accounts inside the guarded test sandbox, and print them
between markers, so a real browser OUTSIDE the sandbox can exercise the page's Remote DJ
connection, automatic reconnect and leave-page code with every network request stubbed
(library/tests/browser_remote_dj_reconnect_check.py) -- no server, database or station data.

    run_with_validation_service.sh <python> manage.py test library.tests.render_remote_dj_console_artifact
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from authz.models import ScheduleAccessConfig
from library.tests.test_remote_dj_gate_binding_2_17a import remote_host_group

User = get_user_model()


class RenderArtifact(TestCase):
    def test_render(self):
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        dj = User.objects.create_user("render_217b_dj", password="x", first_name="Remote", last_name="Host")
        dj.groups.add(remote_host_group())
        staff = User.objects.create_superuser("render_217b_staff", password="x")
        pages = {}
        for name, user, url in (("REMOTE_DJ_PAGE", dj, "/remote-dj/"), ("CONSOLE_PAGE", staff, "/")):
            client = Client()
            client.force_login(user)
            response = client.get(url, secure=True)
            self.assertEqual(response.status_code, 200, (name, response.status_code))
            pages[name] = response.content.decode()
        for name, blob in pages.items():
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
        for blob in pages.values():
            self.assertIn('id="rdjLeaveDialog"', blob)
            self.assertIn("function rdjMaybeStartRetry", blob)
