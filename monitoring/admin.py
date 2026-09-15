from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from isadoraair import env_config

from .models import ListenerPeak, MonitorCheck, NotificationConfig, SystemEvent, TransmitterConfig, emit_event
from .services.config_audit import emit_config_change_event
from .services.notify import send_test_email
from .services.transmitters import (
    TransmitterConfigurationError,
    transmitter_type_requires_password,
)


# ``name`` is operational rather than card-only presentation: monitor.py and
# notify.py use it in transition-event titles and notification subjects/bodies.
# ``show_as_card`` and ``sort_order`` are deliberately absent from this list.
_MONITOR_CHECK_AUDIT_FIELDS = (
    "name",
    "kind",
    "enabled",
    "systemd_unit",
    "disk_path",
    "thermal_zone_label",
    "transmitter_parameter",
    "transmitter_indicator",
    "fault_values",
    "warn_values",
    "silence_device_slug",
    "encoder_group_slug",
    "encoder_group_systemd_unit",
    "warning_threshold",
    "critical_threshold",
    "threshold_direction",
    "consecutive_failures_required",
    "notify_on_warning",
    "notify_on_critical",
)
_MONITOR_CHECK_COMMON_EFFECTIVE_FIELDS = frozenset({
    "name",
    "kind",
    "enabled",
    "consecutive_failures_required",
    "notify_on_warning",
    "notify_on_critical",
})
_MONITOR_CHECK_KIND_FIELDS = {
    "systemd": frozenset({"systemd_unit"}),
    "disk": frozenset({"disk_path"}),
    "temperature": frozenset({"thermal_zone_label"}),
    "transmitter_param": frozenset({"transmitter_parameter"}),
    "transmitter_indicator": frozenset({
        "transmitter_indicator", "fault_values", "warn_values",
    }),
    "audio_silence": frozenset({"silence_device_slug"}),
    "encoder_group": frozenset({
        "encoder_group_slug", "encoder_group_systemd_unit",
    }),
}
_THRESHOLD_MONITOR_KINDS = frozenset({
    "disk", "cpu", "memory", "temperature", "transmitter_param",
})
_MONITOR_CHECK_THRESHOLD_FIELDS = frozenset({
    "warning_threshold", "critical_threshold", "threshold_direction",
})
_TRANSMITTER_CONFIG_AUDIT_FIELDS = (
    "transmitter_type",
    "host",
    "port",
    "password",
    "timeout_seconds",
    "poll_interval_seconds",
    "full_power_watts",
)
_NOTIFICATION_CONFIG_AUDIT_FIELDS = (
    "enabled",
    "recipients",
    "cooldown_minutes",
)


def _persisted_config_snapshot(model, obj, fields):
    """Read the exact pre-save values for one explicitly audited model."""

    if not getattr(obj, "pk", None):
        return None
    row = model.objects.filter(pk=obj.pk).values("pk", *fields).first()
    if row is None:
        return None
    return {
        "pk": row["pk"],
        "values": {field: row[field] for field in fields},
    }


def _current_config_snapshot(obj, fields):
    return {
        "pk": obj.pk,
        "values": {field: getattr(obj, field) for field in fields},
    }


def _audit_monitoring_config_change(
    *,
    request,
    action,
    before,
    after,
    fields,
    category,
    title,
    object_type,
    object_name,
    apply_mode,
    private_fields=(),
):
    """Emit one sanitized desired-policy audit for an Admin operation."""

    old_values = (before or {}).get("values", {})
    new_values = (after or {}).get("values", {})
    if before is None or after is None:
        changed_fields = list(fields)
    else:
        changed_fields = [
            field for field in fields if old_values[field] != new_values[field]
        ]
    if not changed_fields:
        return

    private = frozenset(private_fields)
    changes = {}
    redacted_fields = []
    for field in changed_fields:
        if field in private:
            redacted_fields.append(field)
            continue
        changes[field] = {
            "old": old_values.get(field) if before is not None else None,
            "new": new_values.get(field) if after is not None else None,
        }

    identity = after if after is not None else before
    emit_config_change_event(
        category=category,
        title=title,
        action=action,
        object_type=object_type,
        object_id=(identity or {}).get("pk"),
        object_name=object_name,
        changed_fields=changed_fields,
        changes=changes,
        redacted_fields=redacted_fields,
        request=request,
        apply_modes={field: apply_mode for field in changed_fields},
        restart_required=False,
    )


def _monitor_check_effective_fields(snapshot):
    """Return the compact operational configuration active for one check."""

    values = (snapshot or {}).get("values", {})
    kind = values.get("kind")
    selected = set(_MONITOR_CHECK_COMMON_EFFECTIVE_FIELDS)
    selected.update(_MONITOR_CHECK_KIND_FIELDS.get(kind, ()))
    if kind in _THRESHOLD_MONITOR_KINDS:
        selected.update(_MONITOR_CHECK_THRESHOLD_FIELDS)
    return tuple(field for field in _MONITOR_CHECK_AUDIT_FIELDS if field in selected)


def _audit_monitor_check_change(*, request, action, before, after):
    identity = after if after is not None else before
    values = (identity or {}).get("values", {})
    fields = (
        _MONITOR_CHECK_AUDIT_FIELDS
        if before is not None and after is not None
        else _monitor_check_effective_fields(identity)
    )
    _audit_monitoring_config_change(
        request=request,
        action=action,
        before=before,
        after=after,
        fields=fields,
        category="monitor",
        title=f"Monitoring check configuration {action}d",
        object_type="monitoring.MonitorCheck",
        object_name=values.get("name"),
        apply_mode="next_monitoring_poll",
    )


def _audit_transmitter_config_change(*, request, action, before, after):
    _audit_monitoring_config_change(
        request=request,
        action=action,
        before=before,
        after=after,
        fields=_TRANSMITTER_CONFIG_AUDIT_FIELDS,
        category="monitoring",
        title=f"Transmitter configuration {action}d",
        object_type="monitoring.TransmitterConfig",
        object_name="Transmitter Config",
        apply_mode="next_transmitter_poll",
        private_fields={"password"},
    )


def _audit_notification_config_change(*, request, action, before, after):
    _audit_monitoring_config_change(
        request=request,
        action=action,
        before=before,
        after=after,
        fields=_NOTIFICATION_CONFIG_AUDIT_FIELDS,
        category="monitoring",
        title=f"Notification configuration {action}d",
        object_type="monitoring.NotificationConfig",
        object_name="Notification Config",
        apply_mode="next_notification_evaluation",
        private_fields={"recipients"},
    )


class TransmitterConfigAdminForm(forms.ModelForm):
    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=(
            "Required for BW TX300v3. Leave blank while editing to preserve "
            "the saved password; saved values are never displayed."
        ),
    )

    class Meta:
        model = TransmitterConfig
        fields = "__all__"

    def clean_password(self):
        submitted = self.cleaned_data.get("password", "")
        if not submitted and self.instance.pk:
            return self.instance.password
        return submitted

    def clean(self):
        cleaned = super().clean()
        transmitter_type = cleaned.get("transmitter_type")
        try:
            password_required = (
                transmitter_type
                and transmitter_type_requires_password(transmitter_type)
            )
        except TransmitterConfigurationError:
            password_required = False
        if password_required and not cleaned.get("password"):
            self.add_error(
                "password", "A password is required for this transmitter driver."
            )
        return cleaned


@admin.register(MonitorCheck)
class MonitorCheckAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "enabled", "show_as_card", "sort_order"]
    list_editable = ["enabled", "show_as_card", "sort_order"]
    list_filter = ["kind", "enabled", "show_as_card"]
    ordering = ["sort_order", "name"]

    fieldsets = [
        (None, {"fields": ["name", "kind", "enabled", "show_as_card", "sort_order"]}),
        ("Systemd", {"fields": ["systemd_unit"], "classes": ["collapse"]}),
        ("Disk", {"fields": ["disk_path"], "classes": ["collapse"]}),
        ("Temperature", {"fields": ["thermal_zone_label"], "classes": ["collapse"]}),
        ("Transmitter Parameter", {"fields": ["transmitter_parameter"], "classes": ["collapse"]}),
        ("Transmitter Indicator", {
            "fields": ["transmitter_indicator", "fault_values", "warn_values"],
            "classes": ["collapse"],
        }),
        ("Audio Silence", {"fields": ["silence_device_slug"], "classes": ["collapse"]}),
        ("Encoder Stream Health", {
            "fields": ["encoder_group_slug", "encoder_group_systemd_unit"],
            "classes": ["collapse"],
        }),
        ("Thresholds", {"fields": ["warning_threshold", "critical_threshold", "threshold_direction"]}),
        ("Alerting", {"fields": ["consecutive_failures_required", "notify_on_warning", "notify_on_critical"]}),
    ]

    # No Media/js confirm dialog or restart here, unlike
    # EncoderAdmin/AudioPipelineAdmin — the poller
    # re-reads MonitorCheck.objects.filter(enabled=True) fresh every
    # cycle (~10s), so a save just takes effect on the next tick with no
    # disruptive side effect to confirm.

    def save_model(self, request, obj, form, change):
        before = (
            _persisted_config_snapshot(
                MonitorCheck, obj, _MONITOR_CHECK_AUDIT_FIELDS
            )
            if change
            else None
        )
        super().save_model(request, obj, form, change)
        _audit_monitor_check_change(
            request=request,
            action="update" if change else "create",
            before=before,
            after=_current_config_snapshot(obj, _MONITOR_CHECK_AUDIT_FIELDS),
        )

    def delete_model(self, request, obj):
        before = _current_config_snapshot(obj, _MONITOR_CHECK_AUDIT_FIELDS)
        super().delete_model(request, obj)
        _audit_monitor_check_change(
            request=request,
            action="delete",
            before=before,
            after=None,
        )

    def delete_queryset(self, request, queryset):
        snapshots = [
            _current_config_snapshot(obj, _MONITOR_CHECK_AUDIT_FIELDS)
            for obj in queryset
        ]
        super().delete_queryset(request, queryset)
        for before in snapshots:
            _audit_monitor_check_change(
                request=request,
                action="delete",
                before=before,
                after=None,
            )


class _SingletonAdmin(admin.ModelAdmin):
    """Shared has_add/has_delete/changelist-redirect trio for a
    singleton config model — same pattern as hardware.AudioPipelineAdmin."""
    singleton_model = None
    change_url_name = None

    def has_add_permission(self, request):
        return not self.singleton_model.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = self.singleton_model.load()
        return HttpResponseRedirect(reverse(self.change_url_name, args=[obj.pk]))


@admin.register(TransmitterConfig)
class TransmitterConfigAdmin(_SingletonAdmin):
    singleton_model = TransmitterConfig
    change_url_name = "admin:monitoring_transmitterconfig_change"
    form = TransmitterConfigAdminForm
    fields = [
        "transmitter_type",
        "host",
        "port",
        "password",
        "timeout_seconds",
        "poll_interval_seconds",
        "full_power_watts",
    ]

    def save_model(self, request, obj, form, change):
        before = (
            _persisted_config_snapshot(
                TransmitterConfig, obj, _TRANSMITTER_CONFIG_AUDIT_FIELDS
            )
            if change
            else None
        )
        super().save_model(request, obj, form, change)
        _audit_transmitter_config_change(
            request=request,
            action="update" if change else "create",
            before=before,
            after=_current_config_snapshot(
                obj, _TRANSMITTER_CONFIG_AUDIT_FIELDS
            ),
        )


@admin.register(NotificationConfig)
class NotificationConfigAdmin(_SingletonAdmin):
    """SMTP credentials are NOT stored on this model -- they live only
    in .env (EMAIL_HOST, EMAIL_PORT, EMAIL_HOST_USER, EMAIL_HOST_PASSWORD,
    EMAIL_USE_TLS, DEFAULT_FROM_EMAIL), same as every other secret in
    this project. smtp_status/send_test_email_button/smtp_env_link are
    read-only, computed displays -- there is nothing on THIS model for
    any of them to persist, so no migration is needed for this page's
    additions.

    2026-08-11 Phase 1 admin-editable environment layer: the six SMTP
    keys above are now EDITABLE from a dedicated sub-page
    (smtp-settings/, see smtp_env_view below) that reads/writes them
    through isadoraair/env_config.py -- deliberately a SEPARATE
    sub-form/endpoint from this page's own ModelForm save, not a fake
    combined DB+file "save" (see env_view's own docstring). That
    sub-page is the ONLY place in this admin that reads .env -- this
    changeform's own smtp_status/send_test_email_button stay exactly as
    they were (settings.* / RUNNING values only), so none of the tests
    already covering them start touching the real .env file."""
    singleton_model = NotificationConfig
    change_url_name = "admin:monitoring_notificationconfig_change"
    readonly_fields = ["smtp_status", "send_test_email_button", "smtp_env_link"]

    fieldsets = [
        (None, {
            "fields": ["enabled", "recipients", "cooldown_minutes"],
            "description": (
                "Controls Monitoring alert emails only (MonitorCheck warning/critical/"
                "recovery notifications). Weather, OGRemote, and Web Requests each "
                "maintain their own separate pipeline-failure notification address "
                "(their own admin pages) -- this page does not control those."
            ),
        }),
        ("SMTP transport", {
            "fields": ["smtp_status", "smtp_env_link", "send_test_email_button"],
        }),
    ]

    def save_model(self, request, obj, form, change):
        before = (
            _persisted_config_snapshot(
                NotificationConfig, obj, _NOTIFICATION_CONFIG_AUDIT_FIELDS
            )
            if change
            else None
        )
        super().save_model(request, obj, form, change)
        _audit_notification_config_change(
            request=request,
            action="update" if change else "create",
            before=before,
            after=_current_config_snapshot(
                obj, _NOTIFICATION_CONFIG_AUDIT_FIELDS
            ),
        )

    def get_urls(self):
        return [
            path(
                "send-test-email/",
                self.admin_site.admin_view(self.send_test_email_view),
                name="monitoring_notificationconfig_send_test_email",
            ),
            path(
                "smtp-settings/",
                self.admin_site.admin_view(self.smtp_env_view),
                name="monitoring_notificationconfig_smtp_env",
            ),
            *super().get_urls(),
        ]

    def send_test_email_view(self, request):
        config = NotificationConfig.load()
        ok, message = send_test_email(config)
        self.message_user(request, message, level=messages.SUCCESS if ok else messages.ERROR)
        return HttpResponseRedirect(reverse(self.change_url_name, args=[config.pk]))

    # -- SMTP environment sub-form (Phase 1) -----------------------------
    _SMTP_FIELD_KEYS = ["EMAIL_HOST", "EMAIL_PORT", "EMAIL_HOST_USER", "EMAIL_USE_TLS", "DEFAULT_FROM_EMAIL"]
    # All six SMTP keys, including the secret one -- the explicit scope
    # for this page's own saved-vs-running/duplicate-key checks. Must
    # NOT be list(env_config.MANAGED_SETTINGS): now that Phase 2 has
    # registered library/weather/reports keys in the same shared
    # registry, that would incorrectly pull an unrelated key's mismatch
    # (or a duplicate-definition error) into THIS page's restart banner,
    # audit event, and error state.
    _ALL_SMTP_KEYS = _SMTP_FIELD_KEYS + ["EMAIL_HOST_PASSWORD"]

    def smtp_env_view(self, request):
        """GET renders the sub-form pre-filled with the SAVED (on-disk)
        .env values -- never django.conf.settings alone (requirement:
        a change saved to .env but not yet picked up by a restarted
        Gunicorn/Monitoring worker must still show up here immediately).
        POST writes only the keys the operator actually changed, via
        env_config.update_managed_values(), and redirects back to GET --
        deliberately independent of NotificationConfig's own ModelForm
        save, so a DB save succeeding/failing is never conflated with an
        .env write succeeding/failing (no "false atomic operation")."""
        config = NotificationConfig.load()
        if request.method == "POST":
            self._handle_smtp_env_post(request)
            return HttpResponseRedirect(reverse("admin:monitoring_notificationconfig_smtp_env"))
        context = self._smtp_env_context(request, config)
        return TemplateResponse(request, "admin/monitoring/notificationconfig/smtp_env_form.html", context)

    def _handle_smtp_env_post(self, request):
        values = {
            "EMAIL_HOST": request.POST.get("email_host", "").strip(),
            "EMAIL_PORT": request.POST.get("email_port", "").strip(),
            "EMAIL_HOST_USER": request.POST.get("email_host_user", ""),
            "EMAIL_USE_TLS": "True" if request.POST.get("email_use_tls") else "False",
            "DEFAULT_FROM_EMAIL": request.POST.get("default_from_email", "").strip(),
        }
        clear_password = bool(request.POST.get("clear_email_host_password"))
        new_password = request.POST.get("email_host_password", "")
        if clear_password:
            values["EMAIL_HOST_PASSWORD"] = ""
        elif new_password:
            # Deliberately NOT .strip()'d -- a leading/trailing space in
            # a real SMTP password, however unusual, is the operator's
            # to choose; only blank (nothing typed) means "preserve".
            values["EMAIL_HOST_PASSWORD"] = new_password
        # else: key omitted entirely from `values` -- update_managed_values
        # never touches an omitted key, so the current password on disk
        # is left completely unchanged. This is the ONLY mechanism that
        # implements "blank password field means keep the current one" --
        # an accidentally-blank submission can never clear a credential;
        # only the explicit checkbox above can.

        try:
            result = env_config.update_managed_values(values)
        except env_config.EnvConfigError as exc:
            self.message_user(request, f"Could not save SMTP settings: {exc}", level=messages.ERROR)
            return

        if not result.changed_keys:
            self.message_user(
                request, "No changes to save -- submitted values already match what's saved.",
                level=messages.INFO,
            )
            return

        try:
            comparisons = env_config.compare_to_running(self._ALL_SMTP_KEYS)
            restart_required = any(not c.matches for c in comparisons.values())
        except env_config.EnvConfigError:
            restart_required = True  # unknown -- err toward the more visible warning

        changed = sorted(result.changed_keys)
        self.message_user(
            request,
            f"SMTP settings saved ({', '.join(changed)}). "
            + ("Restart required for these changes to take effect."
               if restart_required else "Running configuration matches saved configuration."),
            level=messages.SUCCESS,
        )
        # Audit event: key NAMES only, never a value -- "EMAIL_HOST_PASSWORD
        # changed" is fine to record, the password itself never is.
        emit_event(
            category="monitoring", level="info",
            title="SMTP environment configuration updated",
            detail={
                "changed_keys": changed,
                "changed_by": request.user.get_username(),
                "restart_required": restart_required,
            },
            dedupe_key="monitoring|smtp-env-updated",
        )

    def _smtp_env_context(self, request, config):
        keys = self._ALL_SMTP_KEYS
        env_error = None
        saved = {}
        comparisons = {}
        try:
            saved = env_config.read_managed_values(keys)
            comparisons = env_config.compare_to_running(keys)
        except env_config.EnvConfigError as exc:
            env_error = str(exc)

        fields = []
        for key in self._SMTP_FIELD_KEYS:
            setting = env_config.MANAGED_SETTINGS[key]
            mv = saved.get(key)
            cmp = comparisons.get(key)
            fields.append({
                "key": key,
                "name": key.lower(),
                "label": setting.label,
                "value": mv.display_value if mv is not None else setting.default,
                "matches": cmp.matches if cmp is not None else True,
            })
        tls_field = next(f for f in fields if f["key"] == "EMAIL_USE_TLS")
        # Uses the setting's own registered cast (decouple-compatible
        # bool parsing) rather than a private module helper.
        tls_field["checked"] = env_config.MANAGED_SETTINGS["EMAIL_USE_TLS"].cast(tls_field["value"])

        password_cmp = comparisons.get("EMAIL_HOST_PASSWORD")
        password_configured = False
        if env_error is None:
            try:
                password_configured = env_config.secret_configured("EMAIL_HOST_PASSWORD")
            except env_config.EnvConfigError:
                pass

        return {
            **self.admin_site.each_context(request),
            "title": "SMTP transport settings",
            "opts": self.model._meta,
            "object_id": config.pk,
            "change_url": reverse(self.change_url_name, args=[config.pk]),
            "env_error": env_error,
            "fields": fields,
            "password_configured": password_configured,
            "password_matches": password_cmp.matches if password_cmp is not None else True,
            # Covers all six managed keys (fields only lists the five
            # non-secret ones for rendering; comparisons has all six).
            "restart_required": env_error is None and any(not c.matches for c in comparisons.values()),
            "services_note": env_config.MANAGED_SETTINGS["EMAIL_HOST"].services_note,
        }

    @admin.display(description="SMTP transport status")
    def smtp_status(self, obj):
        # Non-secret only, by design -- see this class's own docstring.
        # Username/password are reported as Yes/No ("configured"),
        # never the actual value; the password is never surfaced here
        # in ANY form (not even masked-but-recoverable).
        rows = [
            ("Email backend", settings.EMAIL_BACKEND),
            ("SMTP host", settings.EMAIL_HOST),
            ("SMTP port", str(settings.EMAIL_PORT)),
            ("TLS enabled", "Yes" if settings.EMAIL_USE_TLS else "No"),
            ("Default From address", settings.DEFAULT_FROM_EMAIL),
            ("SMTP username configured", "Yes" if settings.EMAIL_HOST_USER else "No"),
            ("SMTP password configured", "Yes" if settings.EMAIL_HOST_PASSWORD else "No"),
        ]
        # format_html_join, not "".join(format_html(...) for ...) --
        # the latter loses each row's own SafeString marking the moment
        # it's joined into a plain str, so a SUBSEQUENT format_html()
        # wrapping that joined string would double-escape it (every
        # "<" turning into "&lt;", the table rendering as literal
        # markup text instead of an actual table). format_html_join
        # formats and joins in one safe operation.
        rows_html = format_html_join(
            "", "<tr><th style='text-align:left;padding-right:1em;'>{}</th><td>{}</td></tr>", rows,
        )
        return format_html(
            "<table>{}</table>"
            '<p style="color:#888;font-size:0.85em;margin-top:0.4em;">'
            "This is the RUNNING configuration this Gunicorn/Monitoring worker currently "
            "has loaded -- not necessarily what's saved to .env if it was changed since "
            "the last restart. See SMTP Settings below to view/edit the saved values."
            "</p>",
            rows_html,
        )

    @admin.display(description="SMTP settings (edit)")
    def smtp_env_link(self, obj):
        if obj is None or obj.pk is None:
            return "(save the configuration first)"
        url = reverse("admin:monitoring_notificationconfig_smtp_env")
        return format_html(
            '<a class="button" href="{}">Edit SMTP settings</a> '
            '<span style="color:#888;font-size:0.85em;">'
            "View the values currently saved to .env and change them. Stored in .env, "
            "not this database record -- a separate save from the form above."
            "</span>",
            url,
        )

    @admin.display(description="Test email")
    def send_test_email_button(self, obj):
        if obj is None or obj.pk is None:
            return "(save the configuration first)"
        url = reverse("admin:monitoring_notificationconfig_send_test_email")
        return format_html(
            '<a class="button" href="{}">Send test email</a> '
            '<span style="color:#888;font-size:0.85em;">'
            "Sends a real email to the recipients configured above, using the "
            "SMTP transport shown above. Does not save this form first -- save "
            "any pending changes before testing. Test Email uses the currently loaded "
            "SMTP configuration -- if you've recently changed SMTP Settings, a restart "
            "is required before it will test the newly saved values."
            "</span>",
            url,
        )


@admin.register(ListenerPeak)
class ListenerPeakAdmin(_SingletonAdmin):
    singleton_model = ListenerPeak
    change_url_name = "admin:monitoring_listenerpeak_change"
    fields = ["peak_total", "peak_since_at", "peak_reached_at"]
    readonly_fields = ["peak_since_at", "peak_reached_at"]


@admin.register(SystemEvent)
class SystemEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "level", "category", "title", "repeat_count", "source"]
    list_filter = ["level", "category", "source"]
    search_fields = ["title", "detail", "dedupe_key"]
    ordering = ["-created_at"]
    date_hierarchy = "created_at"
    # These are written by subsystems, not edited by hand; read-only in
    # admin avoids accidental corruption when the operator is just
    # digging through history.
    readonly_fields = [
        "created_at", "last_repeated_at", "category", "level", "title",
        "detail", "source", "dedupe_key", "repeat_count",
    ]

    def has_add_permission(self, request):
        return False
