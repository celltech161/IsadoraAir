from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the IsadoraAir playback engine."

    def handle(self, *args, **options):
        from library.services.engine import PlaybackEngine
        engine = PlaybackEngine()
        engine.start()
