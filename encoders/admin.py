import subprocess

from django import forms
from django.contrib import admin, messages
from django.db import transaction

from hardware.devices import list_input_devices
from monitoring.models import emit_event

from .models import Encoder

RESTART_COMMAND = ["sudo", "systemctl", "restart", "isadoraair-encoders"]

# Fields whose change requires restarting isadoraair-encoders. Every
# entry feeds the generated Liquidsoap script directly or changes which
# input_device group a row belongs to -- confirmed by inspecting every
# `encoder.<field>`/`enc.<field>` read in encoders/services/
# encoder_manager.py's build_liquidsoap_script/_output_block
# (2026-08-06). `name` and `sort_order` are deliberately EXCLUDED:
# neither is read anywhere in encoder_manager.py (`name` only ever
# appears in the manager's own log line, never the generated script;
# `sort_order` only affects admin/queryset ordering), so neither
# changes anything the running Liquidsoap process cares about.
# `description` is ALSO excluded -- that same source inspection showed
# it isn't read by encoder_manager.py either, so a description-only
# edit cannot affect the running script. (Whether `description` should
# eventually reach a supported streaming-server protocol field, making
# it genuinely runtime-affecting, is a separate configuration-model
# question for a later pass, not this one.)
RUNTIME_AFFECTING_FIELDS = frozenset({
    "enabled", "protocol", "host", "port", "mount", "username", "password",
    "format", "bitrate_kbps", "input_device", "station_name", "genre",
    "url", "public",
})


def dispatch_encoder_restart():
    """Fire-and-forget isadoraair-encoders restart dispatch.

    Returns True if the restart process was successfully LAUNCHED
    (Popen() didn't raise) -- NOT confirmation the service actually
    completed a healthy restart, only that dispatch itself succeeded.
    Real health is what encoders/services/encoder_manager.py's own
    supervision and monitoring's encoder_group probe exist to confirm,
    not this call (reliably reporting eventual restart completion here
    would require blocking the request, a background job system, or
    other infrastructure deliberately out of scope for this pass).

    Never raises: catches the immediate-launch failure modes (missing
    binary, fork failure -- all surface as OSError) and reports them
    via emit_event, this project's established dependency-safe event
    mechanism (see monitoring/models.py). The restart command itself
    carries no encoder-specific data (no host/port/mount/password --
    just a fixed systemctl invocation), so nothing credential-bearing
    can reach that event detail."""
    try:
        subprocess.Popen(RESTART_COMMAND)
        return True
    except OSError as exc:
        emit_event(
            category="encoder", level="error",
            title="Failed to dispatch isadoraair-encoders restart from admin",
            detail={"error": str(exc)},
            dedupe_key="encoder|admin-restart-dispatch-failed",
        )
        return False


def _restart_encoders_and_report(request):
    """The actual transaction.on_commit() callback -- dispatches the
    restart and, only on immediate dispatch failure, surfaces an
    admin-visible message. Runs after commit, still within the same
    request/response cycle, so a message added here reaches the
    redirect response Django admin returns for a normal save/delete."""
    if not dispatch_encoder_restart():
        messages.error(
            request,
            "The encoder change was saved, but IsadoraAir was unable to launch "
            "the isadoraair-encoders restart. Restart it manually "
            "(sudo systemctl restart isadoraair-encoders) -- see Recent Events "
            "on /monitoring/ for detail.",
        )


def mark_encoder_restart_needed(request):
    """Register, at most once per request, a single isadoraair-encoders
    restart to run after the current database transaction commits.

    Coalescing boundary: a request attribute, not a module-global.
    Django hands each HTTP request its own request object, so this
    flag is naturally isolated per request/thread -- no risk of
    leaking into a concurrent request, and nothing to reset that could
    suppress a restart in some later, unrelated request. Admin hook
    methods can each be called once per ROW touched by a single
    logical operation (a changelist bulk-edit calls save_model once
    per changed row; see EncoderAdmin below) -- transaction.on_commit()
    itself does not deduplicate repeated registrations, so calling it
    unconditionally from every hook invocation would restart once per
    row instead of once per admin operation. This function is the
    single choke point that prevents that: the first call within a
    request wins and registers the on_commit callback; every
    subsequent call in that same request is a no-op.

    Deferred to on_commit(), not dispatched immediately, because the
    admin's own DB write happens inside a transaction.atomic() block
    (Django wraps every changeform, changelist bulk-edit, and delete
    view this way) that has not committed yet at the point
    save_model/delete_model/delete_queryset runs. Dispatching earlier
    risks the freshly-restarted manager reading the database before
    this request's own write is visible to it -- or, if the
    transaction later rolls back, restarting for a change that was
    never actually persisted at all."""
    if getattr(request, "_encoder_restart_scheduled", False):
        return
    request._encoder_restart_scheduled = True
    transaction.on_commit(lambda: _restart_encoders_and_report(request))


@admin.register(Encoder)
class EncoderAdmin(admin.ModelAdmin):
    list_display = ["name", "enabled", "protocol", "host", "port", "format", "bitrate_kbps", "sort_order"]
    list_editable = ["enabled", "sort_order"]
    ordering = ["sort_order", "name"]

    fieldsets = [
        ("Basic", {"fields": ["name", "enabled", "protocol", "sort_order"]}),
        ("Connection", {"fields": ["host", "port", "mount", "username", "password"]}),
        ("Encoding", {"fields": ["format", "bitrate_kbps", "input_device"]}),
        ("Stream Info", {"fields": ["station_name", "genre", "description", "url", "public"]}),
    ]

    class Media:
        js = ["encoders/js/encoder_confirm.js"]

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
            # only an enabled add needs the group (re)started.
            if is_enabled:
                mark_encoder_restart_needed(request)
            return
        # Edit: form.initial reflects the row's DB state as loaded
        # BEFORE this POST's changes were applied (ModelForm seeds it
        # from the instance fetched prior to binding submitted data),
        # so this is a reliable pre-save "was it enabled" snapshot even
        # though obj/the DB row already hold the new values by now.
        # Restart only if a runtime-affecting field actually changed
        # AND the row was enabled before, is enabled now, or both --
        # a disabled row staying disabled never touches the running
        # config, regardless of what else about it changes.
        was_enabled = bool(form.initial.get("enabled"))
        runtime_changed = bool(RUNTIME_AFFECTING_FIELDS & set(form.changed_data))
        if runtime_changed and (was_enabled or is_enabled):
            mark_encoder_restart_needed(request)

    def delete_model(self, request, obj):
        was_enabled = obj.enabled
        super().delete_model(request, obj)
        # Deleting a row that was already disabled doesn't change the
        # running topology -- it was never part of it.
        if was_enabled:
            mark_encoder_restart_needed(request)

    def delete_queryset(self, request, queryset):
        had_enabled_encoder = queryset.filter(enabled=True).exists()
        super().delete_queryset(request, queryset)
        if had_enabled_encoder:
            mark_encoder_restart_needed(request)
