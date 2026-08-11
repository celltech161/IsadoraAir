"""Email dispatch for MonitorCheck status transitions, with a per-check
cooldown so a prolonged outage sends one alert, not one every poll cycle.

Also home to send_test_email() -- the operator-triggered "Send Test
Email" action on the Notification Config admin page (see
monitoring/admin.py). It deliberately reuses the exact same recipient-
resolution path (NotificationConfig.recipient_list()) and email
backend real alerts use, so a successful test genuinely answers "would
a real alert reach these addresses" -- not a separate, parallel send
path that could silently disagree with production behavior."""
import time

from monitoring.models import NotificationConfig


def maybe_notify(check, status, detail, prev_status, cooldowns):
    """cooldowns is a dict of {str(check.id): last_notified_at timestamp},
    mutated in place and persisted by the caller across poll cycles."""
    config = NotificationConfig.load()
    if not config.enabled:
        return

    cid = str(check.id)

    if status in ("critical", "warning"):
        if status == "warning" and not check.notify_on_warning:
            return
        if status == "critical" and not check.notify_on_critical:
            return
        now = time.time()
        last = cooldowns.get(cid, 0)
        if now - last < config.cooldown_minutes * 60:
            return
        cooldowns[cid] = now
        _send(config, f"[IsadoraAir] {check.name}: {status.upper()}", _format_body(check, status, detail))
    elif status == "ok" and prev_status in ("critical", "warning"):
        _send(config, f"[IsadoraAir] {check.name}: RECOVERED", _format_body(check, status, detail))
        cooldowns.pop(cid, None)


def _format_body(check, status, detail):
    lines = [f"Check: {check.name}", f"Kind: {check.get_kind_display()}", f"Status: {status.upper()}", ""]
    for key, value in detail.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _safe_exception_detail(exc):
    """{"exception": <class name>, "error": <str(exc)>}, safe for both
    a SystemEvent's JSON detail and an admin-facing message. Strips the
    configured SMTP password if it happens to appear as a literal
    substring -- defense in depth; smtplib exceptions normally only
    ever echo the SERVER's response text, never the client-sent
    credential, but this costs nothing and matches this project's
    existing sanitize-before-surfacing precedent (e.g. encoders/
    services/preflight.py's _sanitize_output for liquidsoap --check
    stderr). Local import: keeps this module import-safe before
    django.setup() has necessarily run, matching every other Django-
    settings access in this file."""
    from django.conf import settings

    text = str(exc)
    password = getattr(settings, "EMAIL_HOST_PASSWORD", "") or ""
    if password:
        text = text.replace(password, "***REDACTED***")
    return {"exception": type(exc).__name__, "error": text}


def _send(config, subject, body):
    recipients = config.recipient_list()
    if not recipients:
        return
    try:
        from django.conf import settings
        from django.core.mail import send_mail
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception as exc:
        # A broken SMTP config must not crash the poll loop -- console
        # logging preserved exactly as before, plus (2026-08-11 SMTP
        # diagnostics pass) a warning SystemEvent so the failure is
        # ALSO visible on /monitoring/'s Recent Events feed, not just
        # whoever happens to be tailing the journal at the time.
        #
        # Fixed, non-variable dedupe_key (no subject/exception text
        # baked in): a broken SMTP transport affects every subsequent
        # alert identically, so every failure while it stays broken
        # must coalesce into ONE repeating row (see emit_event's own
        # dedupe/coalesce window in monitoring/models.py), never one
        # new SystemEvent per monitor tick per distinct check.
        #
        # Cannot recurse: emit_event() only ever writes a SystemEvent
        # row (monitoring/models.py) -- there is no signal, post_save
        # hook, or other code path anywhere in this codebase that sends
        # an email in reaction to a SystemEvent being created (verified
        # by inspection: maybe_notify/_send are only ever called from
        # monitor.py's own check-transition loop). A SystemEvent
        # created because email sending failed therefore cannot itself
        # trigger another email-send attempt.
        # Sanitized once, reused for both surfaces -- the console print
        # is a lower-stakes surface than a DB row any staff user can
        # browse in admin, but "never expose the credential" is a
        # general property of this failure path, not a SystemEvent-
        # only guarantee, so both go through the same redaction rather
        # than the print trusting raw str(exc).
        safe_detail = _safe_exception_detail(exc)
        print(f"  [notify] Failed to send '{subject}': {safe_detail['exception']}: {safe_detail['error']}")
        from monitoring.models import emit_event
        emit_event(
            category="monitoring", level="warning",
            title="Notification email delivery failed",
            detail=safe_detail,
            dedupe_key="monitoring|notify-smtp-failed",
        )


TEST_EMAIL_SUBJECT = "[IsadoraAir] Test notification"


def send_test_email(config):
    """Operator-triggered from the Notification Config admin page's
    "Send Test Email" button -- deliberately independent of maybe_notify/
    _send's own cooldown/check-transition machinery (no check object,
    no cooldown, always attempted when called), but resolves recipients
    through the exact same config.recipient_list() real alerts use.

    Never raises -- returns (ok: bool, message: str) for the admin view
    to turn directly into a Django admin success/error message. Records
    the outcome as its own SystemEvent (success or failure) so the
    attempt is also visible on /monitoring/'s Recent Events feed, not
    just the one-time admin response. Recipient ADDRESSES are
    deliberately not stored in the event detail -- a recipient COUNT is
    sufficient to answer "did this reach anyone" without persisting who,
    matching this project's existing SystemEvent privacy conventions
    elsewhere (destination details are ids/names, never credentials)."""
    recipients = config.recipient_list()
    if not recipients:
        return False, "No recipients configured -- add at least one address to Notification Config first, then save."

    from django.conf import settings
    from django.core.mail import send_mail

    body = (
        "This is a test notification sent from the IsadoraAir admin "
        "(Monitoring -> Notification Config -> Send Test Email).\n\n"
        "If you received this, outgoing email is configured correctly "
        "for Monitoring alerts."
    )
    from monitoring.models import emit_event

    try:
        send_mail(TEST_EMAIL_SUBJECT, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception as exc:
        detail = _safe_exception_detail(exc)
        emit_event(
            category="monitoring", level="warning",
            title="Notification test email failed",
            detail=detail,
            dedupe_key="monitoring|test-email-failed",
        )
        return False, f"Failed to send test email ({detail['exception']}): {detail['error']}"

    emit_event(
        category="monitoring", level="info",
        title="Notification test email sent",
        detail={"recipient_count": len(recipients), "from": settings.DEFAULT_FROM_EMAIL},
        dedupe_key="monitoring|test-email-sent",
    )
    return True, f"Test email sent to {len(recipients)} recipient(s)."
