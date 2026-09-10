"""Narrow child-process probe used by credential rotation validation."""
from django.core.management.base import BaseCommand, CommandError
from django.db import connection


class Command(BaseCommand):
    help = "Verify that Django can authenticate with its current database settings."

    def handle(self, *args, **options):
        try:
            connection.close()
            connection.ensure_connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                if cursor.fetchone() != (1,):
                    raise CommandError("database probe returned an unexpected result")
        except CommandError:
            raise
        except Exception:
            raise CommandError("Django database authentication failed") from None
        self.stdout.write("Django database authentication: PASS")
