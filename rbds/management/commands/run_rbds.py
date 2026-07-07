from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Start the IsadoraAir RBDS (RDS/RBDS UECP/ASCII) client."

    def handle(self, *args, **options):
        from rbds.services.rbds_manager import RBDSManager
        RBDSManager().start()
