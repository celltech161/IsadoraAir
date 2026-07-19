from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import ListenerPeak, MonitorCheck, NotificationConfig, SystemEvent, TransmitterConfig


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
