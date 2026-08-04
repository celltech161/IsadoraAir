import subprocess

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import RBDSConfig, RBDSMessage, RBDSPSFrame


@admin.register(RBDSConfig)
class RBDSConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Connection", {"fields": ["host", "port", "transport", "protocol"]}),
        ("UECP Addressing", {"fields": ["uecp_site_address", "uecp_encoder_address"]}),
        ("Identity", {"fields": ["pi_code", "ecc", "language_code", "station_ps", "pty", "tp", "ta", "ms"]}),
        ("DI Flags", {"fields": ["di_dynamic_pty", "di_compressed", "di_artificial_head", "di_stereo"]}),
        ("AF List", {"fields": ["af_frequencies_mhz"]}),
        ("Clock Time (CT)", {"fields": ["send_ct"]}),
        ("RadioText", {"fields": ["now_playing_format", "use_rt_plus", "nowplaying_min_seconds"]}),
    ]

    class Media:
        js = ["rbds/js/rbds_confirm.js"]

    def has_add_permission(self, request):
        return not RBDSConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = RBDSConfig.load()
        return HttpResponseRedirect(reverse("admin:rbds_rbdsconfig_change", args=[obj.pk]))

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Network/protocol/addressing are topology for the engine's one
        # persistent socket (can't hot-swap TCP<->UDP or reconnect with a
        # different site/encoder address mid-stream) -- restart, matching
        # EncoderAdmin's precedent for exactly this class of change.
        subprocess.Popen(["sudo", "systemctl", "restart", "isadoraair-rbds"])


@admin.register(RBDSPSFrame)
class RBDSPSFrameAdmin(admin.ModelAdmin):
    list_display = ["text", "enabled", "hold_seconds", "sort_order"]
    list_editable = ["enabled", "hold_seconds", "sort_order"]
    ordering = ["sort_order", "id"]
    # No save_model()/delete_model() restart hook -- pure rotation
    # content, the engine re-polls enabled rows fresh every ~1s tick,
    # matching how MonitorCheck content changes never need a restart.


@admin.register(RBDSMessage)
class RBDSMessageAdmin(admin.ModelAdmin):
    list_display = ["name", "source_type", "enabled", "display_seconds", "start_date", "end_date", "sort_order"]
    list_editable = ["enabled", "display_seconds", "sort_order"]
    list_filter = ["source_type", "enabled"]
    ordering = ["sort_order", "id"]
    fieldsets = [
        ("Basic", {"fields": ["name", "enabled", "sort_order", "display_seconds"]}),
        ("Content Source", {
            "fields": ["source_type", "text", "file_path", "source_url", "poll_interval_seconds"],
            "description": "Fill in only the field matching Source Type above.",
        }),
        ("RT+ Tagging", {"fields": ["rt_plus_delimiter"]}),
        ("Date Range", {"fields": ["start_date", "end_date"]}),
    ]
    # No restart hook here either -- same reasoning as RBDSPSFrameAdmin.
