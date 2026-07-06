from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the IsadoraAir system/transmitter monitoring poller."

    def handle(self, *args, **options):
        from monitoring.services.monitor import MonitorManager
        manager = MonitorManager()
        manager.start()
