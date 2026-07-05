from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the IsadoraAir stream encoders (Icecast/Shoutcast)."

    def handle(self, *args, **options):
        from encoders.services.encoder_manager import EncoderManager
        manager = EncoderManager()
        manager.start()
