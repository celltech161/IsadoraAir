import json
import subprocess

from django import forms
from django.contrib import admin, messages
from django.db import transaction

from hardware.devices import list_input_devices
from monitoring.models import emit_event

from .models import Encoder, RUNTIME_AFFECTING_FIELDS

RESTART_COMMAND = ["sudo", "systemctl", "restart", "isadoraair-encoders"]


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


def _run_predispatch_preflight():
    """Phase 2 gap-closing pass (post-review): run the exact same
    validate -> render -> static-preflight sequence
    EncoderManager._launch_group() runs, for EVERY currently-enabled
    encoder group, BEFORE this admin dispatches an isadoraair-encoders
    restart -- so an operator's own bad edit is caught and rejected
    WITHOUT ever interrupting a currently-healthy stream.

    Why this was missing before: without it, the sequence for an
    admin change was "DB commits -> systemctl restart kills the
    current known-good process -> new manager starts -> validates ->
    rejects -> relaunches the LKG" -- the LKG still protects against
    staying down, but a bad admin edit could still cause an
    unnecessary on-air interruption while that whole cycle plays out.
    This closes that gap for the specific, common case this admin can
    control: it cannot help for OTHER paths that change the DB
    (migrations, fixtures, another admin process, direct ORM/shell
    writes) -- which is exactly why encoder_manager.py's own
    _launch_group() still repeats these same checks at startup
    regardless of who or what changed the DB. This function is a
    front-line filter, not a replacement for that authoritative check.

    Deliberately whole-service, not per-group: `systemctl restart
    isadoraair-encoders` restarts every group in one process -- if
    ANY currently-enabled group fails static checks, no restart is
    dispatched at all, even for other groups whose own changes are
    fine. The tradeoff (a valid change to group A is held up by a
    still-broken group B until B is also fixed) is the safer one
    given the invariant this whole effort exists to protect.

    Reuses the SAME centralized functions (validation.py,
    build_liquidsoap_script, lkg.write_candidate, preflight.
    run_preflight) the manager's own _launch_group() uses -- not a
    second, parallel implementation that could disagree with it.

    Returns (ok, failures) -- failures is a list of
    (input_device, reason) tuples for every group that failed static
    checks; empty list (with ok=True) covers both "everything passed"
    and "no enabled encoders exist at all" (trivially fine, matches
    pre-Phase-2 behavior for an empty topology)."""
    from encoders.models import Encoder

    from .services import lkg as lkg_module
    from .services import preflight as preflight_module
    from .services.encoder_manager import DEFAULT_INPUT_DEVICE, _group_by_input_device, _slug, build_liquidsoap_script
    from .services.validation import validate_full_configuration

    failures = []
    groups = _group_by_input_device(Encoder.objects.filter(enabled=True))
    for input_device, encoders in groups.items():
        errors = validate_full_configuration(encoders)
        if errors:
            failures.append((input_device, "; ".join(errors)))
            continue
        slug = _slug(input_device)
        host_aircheck = input_device == DEFAULT_INPUT_DEVICE
        script = build_liquidsoap_script(input_device, encoders, host_aircheck=host_aircheck, generation="admin-predispatch-check")
        try:
            candidate_path = lkg_module.write_candidate(slug, script)
        except OSError as exc:
            # Phase 2 review-fix pass 2, Issue 3: a filesystem failure
            # here (ENOSPC, EACCES, ...) must become an ordinary
            # rejection -- no restart dispatched, current stream
            # untouched, admin error displayed -- never an exception
            # escaping the request (this runs inside transaction.
            # on_commit(), with nothing above it to catch a stray
            # exception before it reaches the response cycle).
            failures.append((input_device, f"failed to write candidate script: {exc}"))
            continue
        try:
            result = preflight_module.run_preflight(candidate_path, encoders)
        finally:
            lkg_module.cleanup_candidate(candidate_path)
        if not result.ok:
            failures.append((input_device, result.reason))
    return (not failures), failures


def _restart_encoders_and_report(request):
    """The actual transaction.on_commit() callback. Runs after commit,
    still within the same request/response cycle, so a message added
    here reaches the redirect response Django admin returns for a
    normal save/delete.

    Gates on _run_predispatch_preflight() first: a failure there means
    the desired (just-saved) configuration cannot be trusted to run,
    so no restart is dispatched at all -- the currently-running
    encoder process (whatever it is) is left completely untouched.
    The saved DB values remain visible in the admin for diagnosis
    (same "desired vs accepted" principle as _describe_group_status
    above); they are never silently overwritten or discarded here."""
    ok, failures = _run_predispatch_preflight()
    if not ok:
        for input_device, reason in failures:
            messages.error(
                request,
                f"Encoder group '{input_device}': the saved configuration failed pre-restart checks "
                f"({reason}) -- isadoraair-encoders was NOT restarted, so the currently-running "
                f"configuration is untouched. Fix the saved values and save again.",
            )
            emit_event(
                category="encoder", level="error",
                title=f"Encoder group '{input_device}' rejected before restart dispatch (admin pre-check)",
                detail={"input_device": input_device, "reason": reason},
                dedupe_key=f"encoder|admin-predispatch-rejected|{input_device}",
            )
        return
    if not dispatch_encoder_restart():
        messages.error(
            request,
            "The encoder change was saved, but IsadoraAir was unable to launch "
            "the isadoraair-encoders restart. Restart it manually "
            "(sudo systemctl restart isadoraair-encoders) -- see Recent Events "
            "on /monitoring/ for detail.",
        )


def _describe_group_status(slug, input_device, encoders):
    """Phase 2M: desired (database) vs accepted (last-known-good)
    surfacing. Reads only on-disk state -- this admin runs under
    gunicorn as a separate process from the encoder manager
    (isadoraair-encoders.service, its own systemd unit); there is no
    shared memory to read self._launch_kind from directly, so the
    group-state JSON file (EncoderManager._write_group_state) and the
    LKG metadata sidecar (encoders/services/lkg.py, written at
    promotion) are the only channel. Only the non-secret LKG metadata
    is read here -- never the LKG script itself (it holds credentials).

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
        return {"level": "warning", "message": (
            f"{label}: the SAVED configuration does not match what's actually ACCEPTED and on-air "
            f"(last-known-good) -- it was likely rejected by validation, preflight, or live "
            f"qualification. The saved values remain visible here for diagnosis, but IsadoraAir is "
            f"running the last-known-good configuration operationally, not this one."
        )}
    return {"level": None, "message": ""}


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
