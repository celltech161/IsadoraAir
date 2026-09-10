"""Explicit root/operator surface for coordinated database rotation."""
from getpass import getpass, GetPassWarning
import sys
import warnings

from django.core.management.base import BaseCommand, CommandError

from isadoraair.db_credentials import (
    CredentialRollbackError,
    CredentialRotationError,
    CredentialRotator,
    generate_password,
)


class Command(BaseCommand):
    help = (
        "Preflight or transactionally rotate PostgreSQL, .env, and updater "
        ".pgpass credentials. The password is generated or read from a hidden prompt."
    )

    def add_arguments(self, parser):
        action = parser.add_mutually_exclusive_group(required=True)
        action.add_argument("--preflight", action="store_true", help="Validate all current authorities without mutation.")
        action.add_argument("--rotate", action="store_true", help="Perform one coordinated credential rotation.")
        parser.add_argument(
            "--prompt", action="store_true",
            help="Read the new password twice from a hidden terminal prompt instead of generating it.",
        )

    def _progress(self, message):
        self.stdout.write(message)

    @staticmethod
    def _prompt_password():
        if not sys.stdin.isatty():
            raise CommandError("--prompt requires an interactive terminal")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                first = getpass("New PostgreSQL password: ")
                second = getpass("Confirm new PostgreSQL password: ")
        except (EOFError, GetPassWarning):
            raise CommandError("--prompt requires a hidden terminal input channel") from None
        if first != second:
            raise CommandError("password entries did not match")
        if not first:
            raise CommandError("password must not be empty")
        return first

    @staticmethod
    def _emit_incomplete_rollback_event(error):
        # Best effort only: database access may itself be one of the authorities
        # that could not be restored. The event contains static, redacted facts.
        try:
            from monitoring.models import emit_event
            emit_event(
                "security",
                "Database credential rotation rollback incomplete",
                level="critical",
                detail=(
                    f"Operator intervention required after phase {error.original_phase}. "
                    "Unrestored authorities: "
                    + ", ".join(error.failed_authorities)
                ),
                source="rotate_database_credentials",
                dedupe_key="security|database-credential-rollback-incomplete",
            )
        except Exception:
            pass

    def handle(self, *args, **options):
        if options["preflight"] and options["prompt"]:
            raise CommandError("--prompt is valid only with --rotate")
        rotator = CredentialRotator(progress=self._progress)
        try:
            if options["preflight"]:
                identity = rotator.preflight()
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Credential preflight: PASS ({identity.host}:{identity.port}/{identity.name}, role {identity.user})"
                    )
                )
                return
            password = self._prompt_password() if options["prompt"] else generate_password()
            identity = rotator.rotate(password)
        except CredentialRollbackError as exc:
            self._emit_incomplete_rollback_event(exc)
            raise CommandError(str(exc)) from None
        except CredentialRotationError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(self.style.SUCCESS("Database credential rotation: COMMITTED"))
        self.stdout.write(
            "Start these persistent DB consumers now: isadoraair-gunicorn, "
            "isadoraair-engine, isadoraair-encoders, isadoraair-monitoring, isadoraair-rbds."
        )
        self.stdout.write(
            f"Final identity: {identity.host}:{identity.port}/{identity.name}, role {identity.user}."
        )
