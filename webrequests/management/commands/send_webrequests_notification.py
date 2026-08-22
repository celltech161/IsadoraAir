from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from webrequests.models import WebRequestConfig


class Command(BaseCommand):
    """Legacy cutover bridge for standalone-helper failure email.

    Native ingest uses coalesced SystemEvents and the same Django mail setup.
    """

    help = "Legacy bridge: send a web-request failure notification email."

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
