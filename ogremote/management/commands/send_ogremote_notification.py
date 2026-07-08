from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from ogremote.models import OGRemoteConfig


class Command(BaseCommand):
    """Sends a failure notification for the ogremote-ingest pipeline via
    the project's own EMAIL_* settings, invoked by the external cron
    scripts via subprocess (same cross-venv pattern as sync_track_file)
    instead of a separate smtplib/credential-file setup."""

    help = "Send an ogremote-pipeline notification email using OGRemoteConfig.notify_email."

    def add_arguments(self, parser):
        parser.add_argument("subject")
        parser.add_argument("body")

    def handle(self, *args, **options):
        cfg = OGRemoteConfig.load()
        if not cfg.notify_email:
            return
        send_mail(
            options["subject"], options["body"], settings.DEFAULT_FROM_EMAIL,
            [cfg.notify_email], fail_silently=True,
        )
