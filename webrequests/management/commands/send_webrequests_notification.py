from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from webrequests.models import WebRequestConfig


class Command(BaseCommand):
    """Sends a failure notification for the web-requests-ingest pipeline
    via the project's own EMAIL_* settings, invoked by the external
    scripts via subprocess -- same cross-venv pattern as
    send_weather_notification/send_ogremote_notification."""

    help = "Send a web-requests-ingest notification email using WebRequestConfig.notify_email."

    def add_arguments(self, parser):
        parser.add_argument("subject")
        parser.add_argument("body")

    def handle(self, *args, **options):
        cfg = WebRequestConfig.load()
        if not cfg.notify_email:
            return
        send_mail(
            options["subject"], options["body"], settings.DEFAULT_FROM_EMAIL,
            [cfg.notify_email], fail_silently=True,
        )
