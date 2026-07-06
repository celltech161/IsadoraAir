from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import MonitorCheck, NotificationConfig, TransmitterConfig


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
    singleton_model = NotificationConfig
    change_url_name = "admin:monitoring_notificationconfig_change"
    fields = ["enabled", "recipients", "cooldown_minutes"]
