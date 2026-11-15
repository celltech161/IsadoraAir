"""QA helper (not collected by default test discovery): render the Schedule page, the full Console and
the dedicated Remote DJ page for representative accounts inside the guarded test sandbox and print
them between markers, so a real browser OUTSIDE the sandbox can exercise their scripts with every
network request stubbed (library/tests/browser_schedule_past_overrides_check.py,
library/tests/browser_console_queue_swap_check.py) -- no server, database or station data.

    run_with_validation_service.sh <python> manage.py test library.tests.render_schedule_console_artifact
"""
from django.contrib.auth import get_user_model
from django.test import Client, TestCase

from authz.models import ScheduleAccessConfig
from library.models import Rotation
from library.tests.schedule_profile_helpers import ensure_schedule_profile_state
from library.tests.test_remote_dj_gate_binding_2_17a import remote_host_group

User = get_user_model()


class RenderArtifact(TestCase):
    def test_render(self):
        ensure_schedule_profile_state()
        Rotation.objects.create(name="Jeff Memorial")
        cfg = ScheduleAccessConfig.load()
        cfg.scheduled_enforcement_enabled = False
        cfg.save()
        staff = User.objects.create_superuser("render_sched_staff", password="x")
        dj = User.objects.create_user("render_sched_dj", password="x", first_name="Remote", last_name="Host")
        dj.groups.add(remote_host_group())
        pages = {}
        for name, user, url in (("SCHEDULE_PAGE", staff, "/schedule/"), ("CONSOLE_PAGE", staff, "/"),
                                ("REMOTE_DJ_PAGE", dj, "/remote-dj/")):
            client = Client()
            client.force_login(user)
            response = client.get(url, secure=True)
            self.assertEqual(response.status_code, 200, (name, response.status_code))
            pages[name] = response.content.decode()
        for name, blob in pages.items():
            print(f"\n=====BEGIN-{name}=====\n{blob}\n=====END-{name}=====")
        self.assertIn('id="pastOverridesButton"', pages["SCHEDULE_PAGE"])
