import json

from django import forms
from django.contrib import admin, messages

from hardware.devices import list_input_devices

from .models import Encoder, RUNTIME_AFFECTING_FIELDS


def _notify_reconciliation_pending(request):
    """Phase 3A: replaces the old dispatch_encoder_restart/
    mark_encoder_restart_needed pair. Admin no longer triggers
    anything -- it only commits the desired configuration (the ORM
    save already happened by the time this is called) and tells the
    operator the encoder manager will pick the change up on its own,
    on its existing ~5s reconciliation cadence (see encoders/services/
    encoder_manager.py's _reconcile). No subprocess, no sudo, nothing
    to defer to transaction.on_commit() -- there is no dispatch left
    to race the DB write becoming visible; the manager only ever reads
    fully-committed state on its own schedule regardless of exactly
    when in a future tick it looks.

    Coalescing: a request attribute, same pattern the old
    mark_encoder_restart_needed used -- a changelist bulk-edit touching
    several rows in one logical operation should show this message
    once, not once per row."""
    if getattr(request, "_encoder_reconcile_message_shown", False):
        return
    request._encoder_reconcile_message_shown = True
    messages.info(
        request,
        "Encoder configuration saved. The encoder manager will reconcile the affected group "
        "automatically -- this can take up to about a minute while the new configuration is "
        "validated and proven healthy. See the status banner above (or /monitoring/) for progress.",
    )


def _describe_group_status(slug, input_device, encoders):
    """Phase 2M, extended for Phase 3O: desired (database) vs running
    vs accepted (last-known-good) surfacing. Reads only on-disk state
    -- this admin runs under gunicorn as a separate process from the
    encoder manager (isadoraair-encoders.service, its own systemd
    unit); there is no shared memory to read self._launch_kind from
    directly, so the group-state JSON file
    (EncoderManager._write_group_state) and the LKG metadata sidecar
    (encoders/services/lkg.py, written at promotion) are the only
    channel. Only the non-secret LKG metadata is read here -- never
    the LKG script itself (it holds credentials).

    Imports encoder_manager/lkg locally, not at module level:
    encoder_manager.py doubles as a standalone script (it's the actual
    isadoraair-encoders.service entry point) and unconditionally calls
    django.setup() at import time for that reason -- importing it at
    admin.py's own module level triggers that mid-app-registry-
    population (Django's admin autodiscover imports every app's
    admin.py while apps.populate() is still running) and fails with
    "populate() isn't reentrant" (confirmed via `manage.py check`).
    By the time this function actually runs (a real request), Django
    startup is long finished and the same import is harmless.

    Returns {"level": "error"|"warning"|"info"|None, "message": str}
    -- level=None means nothing worth surfacing (desired matches
    accepted and the group isn't on probation/rollback/critical-stop)."""
    from .services import lkg
    from .services.encoder_manager import _group_state_path_for_slug

    desired_fp = lkg.compute_fingerprint(input_device, encoders)
    accepted_fp = (lkg.read_lkg_meta(slug) or {}).get("fingerprint")

    group_state = {}
    group_state_path = _group_state_path_for_slug(slug)
    if group_state_path.is_file():
        try:
            group_state = json.loads(group_state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            group_state = {}
    launch_kind = group_state.get("launch_kind", "accepted")
    critical_stopped = bool(group_state.get("critical_stopped"))
    last_reconcile_result = group_state.get("last_reconcile_result") or ""
    last_reconcile_error = group_state.get("last_reconcile_error") or ""

    label = f"Encoder group '{input_device}'"

    if critical_stopped:
        return {"level": "error", "message": (
            f"{label}: automatic configuration switching is STOPPED -- a candidate configuration "
            f"and the rollback to last-known-good both failed to establish health. IsadoraAir is "
            f"only retrying infrastructure recovery of the last-known-good configuration now, not "
            f"switching configurations. Check Recent Events on /monitoring/, fix the underlying "
            f"problem, then save a corrected configuration to try again."
        )}
    if launch_kind == "candidate":
        return {"level": "warning", "message": (
            f"{label}: the saved configuration is running on PROBATION, not yet accepted -- "
            f"IsadoraAir is confirming it stays healthy before treating it as last-known-good. "
            f"If it fails, IsadoraAir automatically rolls back to the previous last-known-good "
            f"configuration."
        )}
    if launch_kind == "rollback":
        return {"level": "warning", "message": (
            f"{label}: a candidate configuration failed and IsadoraAir automatically rolled back "
            f"to the last-known-good configuration, which is itself still being re-qualified."
        )}
    if accepted_fp is None:
        return {"level": "info", "message": (
            f"{label}: no last-known-good configuration exists yet for this group (bootstrap) -- "
            f"the saved configuration becomes last-known-good once it passes live qualification."
        )}
    if desired_fp != accepted_fp:
        if last_reconcile_result == "blocked_cross_group_collision":
            return {"level": "warning", "message": (
                f"{label}: the saved configuration is blocked -- it targets a streaming destination "
                f"already in use by another encoder group ({last_reconcile_error or 'see Recent Events'}). "
                f"IsadoraAir keeps running the last-known-good configuration here until the "
                f"conflicting destination is resolved on one side or the other."
            )}
        if last_reconcile_result.endswith("_rejected"):
            reason = f" ({last_reconcile_error})" if last_reconcile_error else ""
            return {"level": "error", "message": (
                f"{label}: the saved configuration was rejected{reason} and will NOT be retried "
                f"automatically -- IsadoraAir keeps running the last-known-good configuration. "
                f"Fix the saved values and save again to try a fresh reconciliation attempt."
            )}
        return {"level": "warning", "message": (
            f"{label}: the SAVED configuration does not match what's actually ACCEPTED and on-air "
            f"(last-known-good) yet -- the encoder manager reconciles this automatically, usually "
            f"within about a minute, without restarting any other group. The saved values remain "
            f"visible here for diagnosis in the meantime."
        )}
    return {"level": None, "message": ""}


@admin.register(Encoder)
class EncoderAdmin(admin.ModelAdmin):
    list_display = ["name", "enabled", "protocol", "host", "port", "format", "bitrate_kbps", "sort_order"]
    list_editable = ["enabled", "sort_order"]
    ordering = ["sort_order", "name"]

    fieldsets = [
        ("Basic", {"fields": ["name", "enabled", "provider", "protocol", "sort_order"], "description": (
            "<p><strong>Provider</strong> is a preset layered on top of the generic Icecast/"
            "Shoutcast fields below -- it narrows which Protocol/Format/MP3 Rate Mode "
            "combinations are accepted and how IsadoraAir verifies this destination is "
            "actually connected, it does not add a new streaming protocol.</p>"
            "<p><strong>Live365</strong>: fundamentally an Icecast source destination. Set "
            "Protocol to Icecast. Get Host/Port/Mount/Username/Password from your Live365 "
            "LiveDJ/source credentials (Live365 dashboard -> Broadcasting/Source setup). "
            "MP3 or AAC only, and MP3 must be CBR (MP3 Rate Mode = CBR, or Auto at 192 kbps "
            "or higher). Match the configured Bitrate to what your Live365 account expects.</p>"
            "<p><strong>Radio.co</strong>: fundamentally a Shoutcast-1-style external-"
            "broadcaster source connection. Set Protocol to Shoutcast 1. Get Host/Port/"
            "Password from your Radio.co station's external broadcaster / source settings "
            "(no mount or username needed for Shoutcast 1). MP3 only, always CBR. Your "
            "Radio.co station must be configured to permit this external source -- typically "
            "a Live DJ event, or Live Anytime for continuous IsadoraAir operation. IsadoraAir "
            "remains the schedule/automation authority; Radio.co is downstream distribution/"
            "fallback, not a mirrored schedule.</p>"
        )}),
        ("Connection", {"fields": ["host", "port", "mount", "username", "password"]}),
        ("Encoding", {"fields": ["format", "bitrate_kbps", "mp3_rate_mode", "input_device"]}),
        ("Stream Info", {"fields": ["station_name", "genre", "description", "url", "public"]}),
    ]

    class Media:
        js = ["encoders/js/encoder_confirm.js"]

    def changelist_view(self, request, extra_context=None):
        self._add_group_status_messages(request)
        return super().changelist_view(request, extra_context=extra_context)

    def _add_group_status_messages(self, request):
        """Phase 2M: one Django admin message per enabled encoder group
        whose desired (database) configuration doesn't match what's
        actually accepted and on-air (last-known-good), or that's
        currently on probation/rollback/critical-stop -- see
        _describe_group_status. Best-effort: any failure reading the
        on-disk state must never break the changelist page itself, only
        skip the banner for that one group (the row data underneath is
        completely unaffected either way)."""
        from .services.encoder_manager import _group_by_input_device, _slug

        try:
            groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
        except Exception:
            return
        for input_device, encoders in groups.items():
            try:
                status = _describe_group_status(_slug(input_device), input_device, encoders)
            except Exception:
                continue
            if status["level"] == "error":
                messages.error(request, status["message"])
            elif status["level"] == "warning":
                messages.warning(request, status["message"])
            elif status["level"] == "info":
                messages.info(request, status["message"])

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "input_device" in form.base_fields:
            choices = list(list_input_devices())
            if obj and obj.input_device and not any(obj.input_device == c[0] for c in choices):
                choices = [(obj.input_device, f"{obj.input_device} (UNAVAILABLE)")] + choices
            choices = [("", "— default (StereoTool HD Output bridge) —")] + choices
            form.base_fields["input_device"].widget = forms.Select(choices=choices)
        return form

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        is_enabled = bool(obj.enabled)
        if not change:
            # A disabled row was never part of the running topology --
            # only an enabled add is something for the manager to
            # reconcile.
            if is_enabled:
                _notify_reconciliation_pending(request)
            return
        # Edit: form.initial reflects the row's DB state as loaded
        # BEFORE this POST's changes were applied (ModelForm seeds it
        # from the instance fetched prior to binding submitted data),
        # so this is a reliable pre-save "was it enabled" snapshot even
        # though obj/the DB row already hold the new values by now.
        # Notify only if a runtime-affecting field actually changed AND
        # the row was enabled before, is enabled now, or both -- a
        # disabled row staying disabled never touches the running
        # config, regardless of what else about it changes.
        was_enabled = bool(form.initial.get("enabled"))
        runtime_changed = bool(RUNTIME_AFFECTING_FIELDS & set(form.changed_data))
        if runtime_changed and (was_enabled or is_enabled):
            _notify_reconciliation_pending(request)

    def delete_model(self, request, obj):
        was_enabled = obj.enabled
        super().delete_model(request, obj)
        # Deleting a row that was already disabled doesn't change the
        # running topology -- it was never part of it.
        if was_enabled:
            _notify_reconciliation_pending(request)

    def delete_queryset(self, request, queryset):
        had_enabled_encoder = queryset.filter(enabled=True).exists()
        super().delete_queryset(request, queryset)
        if had_enabled_encoder:
            _notify_reconciliation_pending(request)
