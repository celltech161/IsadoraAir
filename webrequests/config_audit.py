"""Privacy-safe configuration audit snapshots for Web Requests write paths."""

from monitoring.services.config_audit import emit_config_change_event

from .models import WebRequestConfig


WEB_REQUEST_CONFIG_AUDIT_FIELDS = (
    "enabled",
    "open_slots",
    "max_fulfilled_per_hour",
    "lookahead_warning_minutes",
    "expire_after_hours",
    "notify_email",
    "dedication_tts_voice",
    "dedication_tts_timeout_seconds",
    "dedication_named_message_template",
    "dedication_named_request_template",
    "dedication_anonymous_message_template",
    "dedication_anonymous_request_template",
    "dedication_message_spoken_limit",
)

WEB_REQUEST_STAFF_API_FIELDS = (
    "enabled",
    "max_fulfilled_per_hour",
    "lookahead_warning_minutes",
    "expire_after_hours",
)

WEB_REQUEST_TEMPLATE_FIELDS = frozenset({
    "dedication_named_message_template",
    "dedication_named_request_template",
    "dedication_anonymous_message_template",
    "dedication_anonymous_request_template",
})

WEB_REQUEST_REDACTED_FIELDS = frozenset(
    {"notify_email"} | WEB_REQUEST_TEMPLATE_FIELDS
)

WEB_REQUEST_CONFIG_APPLY_MODES = {
    "enabled": "next_web_request_evaluation",
    "open_slots": "next_request_window_evaluation",
    "max_fulfilled_per_hour": "next_web_request_evaluation",
    "lookahead_warning_minutes": "next_web_request_evaluation",
    "expire_after_hours": "next_web_request_evaluation",
    "notify_email": "next_web_request_notification_attempt",
    "dedication_tts_voice": "next_dedication_tts_attempt",
    "dedication_tts_timeout_seconds": "next_dedication_tts_attempt",
    "dedication_named_message_template": "next_dedication_tts_attempt",
    "dedication_named_request_template": "next_dedication_tts_attempt",
    "dedication_anonymous_message_template": "next_dedication_tts_attempt",
    "dedication_anonymous_request_template": "next_dedication_tts_attempt",
    "dedication_message_spoken_limit": "next_dedication_tts_attempt",
}

DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _normalized_open_slots(raw):
    """Return only the effective weekly slots, deduplicated and ordered."""

    return tuple(sorted({
        slot for slot in (raw or ())
        if isinstance(slot, int) and not isinstance(slot, bool) and 0 <= slot <= 167
    }))


def web_request_config_snapshot(obj, fields=WEB_REQUEST_CONFIG_AUDIT_FIELDS):
    comparison = {}
    audit = {}
    for field in fields:
        if field == "open_slots":
            slots = _normalized_open_slots(obj.open_slots)
            comparison[field] = slots
            audit[field] = {"open_slot_count": len(slots)}
        elif field == "dedication_tts_voice":
            comparison[field] = obj.dedication_tts_voice_id
            audit[field] = {
                "id": obj.dedication_tts_voice_id,
                "name": (
                    obj.dedication_tts_voice.name
                    if obj.dedication_tts_voice_id is not None
                    else None
                ),
            }
        elif field in WEB_REQUEST_REDACTED_FIELDS:
            # Raw values exist only in this synchronous comparison snapshot.
            # They never enter changes or the deferred on_commit callback.
            comparison[field] = getattr(obj, field)
            audit[field] = None
        else:
            comparison[field] = getattr(obj, field)
            audit[field] = getattr(obj, field)
    return {"pk": obj.pk, "comparison": comparison, "audit": audit}


def persisted_web_request_config_snapshot(
    obj, fields=WEB_REQUEST_CONFIG_AUDIT_FIELDS
):
    if not getattr(obj, "pk", None):
        return None
    persisted = WebRequestConfig.objects.select_related(
        "dedication_tts_voice"
    ).filter(pk=obj.pk).first()
    return web_request_config_snapshot(persisted, fields) if persisted else None


def _schedule_summary(before, after):
    old_slots = set(before["comparison"]["open_slots"]) if before else set()
    new_slots = set(after["comparison"]["open_slots"])
    return {
        "old_open_slot_count": len(old_slots) if before else None,
        "new_open_slot_count": len(new_slots),
        "changed_slot_count": len(old_slots.symmetric_difference(new_slots)),
    }


def emit_web_request_config_change(
    *,
    request,
    action,
    before,
    after,
    fields=WEB_REQUEST_CONFIG_AUDIT_FIELDS,
    change_source="django_admin",
    schedule_change=None,
):
    """Queue one config event containing only explicitly safe evidence."""

    old_comparison = (before or {}).get("comparison", {})
    changed_fields = (
        list(fields)
        if before is None
        else [
            field
            for field in fields
            if old_comparison[field] != after["comparison"][field]
        ]
    )
    if not changed_fields:
        return False

    changes = {}
    for field in changed_fields:
        if field in WEB_REQUEST_REDACTED_FIELDS:
            continue
        if field == "open_slots":
            changes[field] = schedule_change or _schedule_summary(before, after)
        else:
            changes[field] = {
                "old": before["audit"][field] if before else None,
                "new": after["audit"][field],
            }

    return emit_config_change_event(
        category="webrequests",
        title="Web request configuration updated",
        action=action,
        object_type="webrequests.WebRequestConfig",
        object_id=after["pk"],
        object_name="Web Request Configuration",
        changed_fields=changed_fields,
        changes=changes,
        redacted_fields=[
            field for field in changed_fields
            if field in WEB_REQUEST_REDACTED_FIELDS
        ],
        request=request,
        change_source=change_source,
        apply_modes={
            field: WEB_REQUEST_CONFIG_APPLY_MODES[field]
            for field in changed_fields
        },
        restart_required=False,
    )
