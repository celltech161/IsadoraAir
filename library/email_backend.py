from django.core.mail.backends.smtp import EmailBackend

# Cap logged bodies so a runaway alert (long traceback attached to a
# monitoring email, verbose HTML template, etc.) can't bloat the log
# table row-by-row. 10k characters is enough to capture the substance
# of any real email we send; anything longer gets a visible marker so
# the truncation is obvious to whoever reads the log.
BODY_MAX_CHARS = 10000
BODY_TRUNCATION_MARKER = "\n\n[...truncated by LoggingSMTPBackend...]"


def _truncated(body):
    if not body or len(body) <= BODY_MAX_CHARS:
        return body or ""
    return body[: BODY_MAX_CHARS - len(BODY_TRUNCATION_MARKER)] + BODY_TRUNCATION_MARKER


class LoggingSMTPBackend(EmailBackend):
    """Wraps the stock SMTP backend so every message sent through Django's
    normal mail API -- password resets, admin invites, monitoring alerts,
    anything -- leaves a row in EmailLog. Added after a real incident: an
    admin-triggered password-reset invite went out with a broken link and
    there was no way to see what had actually been sent without re-deriving
    it by hand. Logs one row per message, individually, so a failure on one
    recipient doesn't hide whether others succeeded."""

    def send_messages(self, email_messages):
        from library.models import EmailLog

        if not email_messages:
            return 0
        num_sent = 0
        for message in email_messages:
            error = ""
            exc_to_reraise = None
            try:
                sent = super().send_messages([message])
                success = bool(sent)
            except Exception as exc:
                success = False
                error = str(exc)
                sent = 0
                exc_to_reraise = exc
            num_sent += sent or 0
            EmailLog.objects.create(
                to=", ".join(message.to),
                from_email=message.from_email or "",
                subject=message.subject or "",
                body=_truncated(message.body),
                success=success,
                error=error,
            )
            if exc_to_reraise is not None:
                raise exc_to_reraise
        return num_sent
