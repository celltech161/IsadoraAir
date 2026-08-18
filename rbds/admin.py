import subprocess

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import RBDSConfig, RBDSMessage, RBDSPSFrame
from .services import dynamic_ps


@admin.register(RBDSConfig)
class RBDSConfigAdmin(admin.ModelAdmin):
    # Fields whose change is a TOPOLOGY change for the engine's one
    # persistent socket (can't hot-swap TCP<->UDP or reconnect with a
    # different site/encoder address mid-stream) -- see save_model()
    # below. Deliberately does NOT include station identity/content
    # fields (PS settings among them, 2026-08-18) -- those are re-read
    # fresh by the manager every ~1s tick with no restart needed.
    RESTART_TOPOLOGY_FIELDS = frozenset({
        "host", "port", "transport", "protocol", "uecp_site_address", "uecp_encoder_address",
    })

    fieldsets = [
        ("Connection", {"fields": ["host", "port", "transport", "protocol"]}),
        ("UECP Addressing", {"fields": ["uecp_site_address", "uecp_encoder_address"]}),
        ("Program Service (PS)", {
            "fields": [
                "station_ps", "ps_mode", "dynamic_ps_text",
                "dynamic_ps_mode", "dynamic_ps_frame_seconds", "generated_ps_preview",
            ],
            "description": (
                "PS Mode controls only the ordinary 8-character PS service -- "
                "Long PS is a separate, later RDS feature and is not one of "
                "these choices.<br><br>"
                "<b>Static PS</b>: sends Station PS above, unchanged, no "
                "rotation.<br>"
                "<b>Manual PS Frames</b>: rotates the enabled records from the "
                "PS Rotation Frames admin page -- Station PS is only used as a "
                "fail-safe fallback if zero frames there are enabled.<br>"
                "<b>Generated Rotating PS</b>: converts Dynamic PS Text into a "
                "sequence of 8-character frames per Dynamic PS Mode (0-3) -- "
                "see the live preview below, which uses the real frame "
                "generator, not a separate reimplementation."
            ),
        }),
        ("Identity", {"fields": ["pi_code", "ecc", "language_code", "pty", "tp", "ta", "ms"]}),
        ("DI Flags", {"fields": ["di_dynamic_pty", "di_compressed", "di_artificial_head", "di_stereo"]}),
        ("AF List", {"fields": ["af_frequencies_mhz"]}),
        ("Clock Time (CT)", {"fields": ["send_ct"]}),
        ("RadioText", {"fields": ["now_playing_format", "use_rt_plus", "nowplaying_min_seconds"]}),
    ]

    readonly_fields = ["generated_ps_preview"]

    class Media:
        js = ["rbds/js/rbds_confirm.js"]

    def has_add_permission(self, request):
        return not RBDSConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = RBDSConfig.load()
        return HttpResponseRedirect(reverse("admin:rbds_rbdsconfig_change", args=[obj.pk]))

    def generated_ps_preview(self, obj):
        """Read-only preview of what Generated Rotating PS would
        actually transmit -- calls the real
        rbds.services.dynamic_ps.generate_ps_frames() (the same
        function rbds_manager.py's live PS resolution calls), never a
        second implementation. Reflects the object's last-SAVED state,
        not in-progress unsaved form edits -- a plain server-rendered
        read-only field is sufficient here, no JS live-preview.
        Never raises: any condition that would prevent a meaningful
        preview (mode not Generated, blank text, an unexpected
        generation failure) is shown as a concise status line instead."""
        if obj is None or obj.pk is None:
            return "(save the config first to preview)"
        if obj.ps_mode != "generated":
            return "Not applicable — PS Mode is not Generated Rotating PS."
        if not obj.dynamic_ps_text.strip():
            return "Not applicable — Dynamic PS Text is blank."
        try:
            frames = dynamic_ps.generate_ps_frames(obj.dynamic_ps_text, obj.dynamic_ps_mode)
        except ValueError as exc:
            return f"Cannot preview: {exc}"
        if not frames:
            return "(generator produced zero frames)"
        # Numbered, pipe-delimited, monospace -- the pipes make
        # leading/trailing spaces and the exact 8-character frame
        # boundary visible, which a plain unbounded string would not.
        lines = format_html_join(
            "\n", "[{}] |{}|",
            ((f"{i + 1:>2}", frame) for i, frame in enumerate(frames)),
        )
        return format_html('<pre style="margin:0;font-family:monospace">{}</pre>', lines)
    generated_ps_preview.short_description = "Generated frame preview"

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Only an actual connection-topology change needs a restart --
        # can't hot-swap TCP<->UDP or reconnect with a different site/
        # encoder address mid-stream, matching EncoderAdmin's precedent
        # for exactly this class of change. A brand-new singleton row
        # (change=False -- only ever happens once, has_add_permission()
        # above blocks a second one) restarts unconditionally too,
        # since there is no previously-running config to compare
        # against and the engine may not even be started yet.
        #
        # 2026-08-18 fix: this used to restart on EVERY save
        # unconditionally, which this module's own prior comment
        # already flagged as only actually required for topology --
        # that became unacceptable once Dynamic PS text/mode/interval
        # started living on this same row: an operator tweaking PS
        # content would bounce the live on-air RBDS connection for no
        # reason. The manager already reloads RBDSConfig.load() fresh
        # every ~1s tick, so content-only changes need no restart at
        # all to take effect.
        if not change or self.RESTART_TOPOLOGY_FIELDS.intersection(form.changed_data):
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
