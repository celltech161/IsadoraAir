from django.conf import settings
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from .models import ListenerPeak, MonitorCheck, NotificationConfig, SystemEvent, TransmitterConfig
from .services.notify import send_test_email


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

    # No Media/js confirm dialog and no save_model()/delete_model()
    # restart here, unlike EncoderAdmin/AudioPipelineAdmin — the poller
    # re-reads MonitorCheck.objects.filter(enabled=True) fresh every
    # cycle (~10s), so a save just takes effect on the next tick with no
    # disruptive side effect to confirm.


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
    fields = ["host", "port", "timeout_seconds", "poll_interval_seconds", "full_power_watts"]


@admin.register(NotificationConfig)
class NotificationConfigAdmin(_SingletonAdmin):
    """SMTP credentials are deliberately NOT shown or editable here --
    they live only in .env/Django settings (EMAIL_HOST, EMAIL_PORT,
    EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, EMAIL_USE_TLS,
    DEFAULT_FROM_EMAIL), same as every other secret in this project.
    smtp_status/send_test_email_button are read-only, settings-derived
    displays -- there is nothing on this model for either of them to
    persist, so no migration is needed for this page's additions."""
    singleton_model = NotificationConfig
    change_url_name = "admin:monitoring_notificationconfig_change"
    readonly_fields = ["smtp_status", "send_test_email_button"]

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
        ("SMTP transport (from .env / Django settings, not editable here)", {
            "fields": ["smtp_status", "send_test_email_button"],
        }),
    ]

    def get_urls(self):
        return [
            path(
                "send-test-email/",
                self.admin_site.admin_view(self.send_test_email_view),
                name="monitoring_notificationconfig_send_test_email",
            ),
            *super().get_urls(),
        ]

    def send_test_email_view(self, request):
        config = NotificationConfig.load()
        ok, message = send_test_email(config)
        self.message_user(request, message, level=messages.SUCCESS if ok else messages.ERROR)
        return HttpResponseRedirect(reverse(self.change_url_name, args=[config.pk]))

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
        return format_html("<table>{}</table>", rows_html)

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
            "any pending changes before testing."
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
