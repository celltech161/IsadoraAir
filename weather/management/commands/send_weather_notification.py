from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from weather.models import WeatherConfig


class Command(BaseCommand):
    """Sends a failure notification for the weather-ingest pipeline via
    the project's own EMAIL_* settings, invoked by the external cron
    scripts via subprocess (same cross-venv pattern as sync_track_file)
    instead of each script keeping its own smtplib/credential-file
    setup like the original scripts did."""

    help = "Send a weather-pipeline notification email using WeatherConfig.notify_email."

    def add_arguments(self, parser):
        parser.add_argument("subject")
        parser.add_argument("body")

    def handle(self, *args, **options):
        cfg = WeatherConfig.load()
        if not cfg.notify_email:
            return
        send_mail(
            options["subject"], options["body"], settings.DEFAULT_FROM_EMAIL,
            [cfg.notify_email], fail_silently=True,
        )
