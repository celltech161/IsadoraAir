"""Email dispatch for MonitorCheck status transitions, with a per-check
cooldown so a prolonged outage sends one alert, not one every poll cycle."""
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


def _send(config, subject, body):
    recipients = config.recipient_list()
    if not recipients:
        return
    try:
        from django.conf import settings
        from django.core.mail import send_mail
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, recipients, fail_silently=False)
    except Exception as exc:
        # A broken SMTP config must not crash the poll loop.
        print(f"  [notify] Failed to send '{subject}': {exc}")
