import json
import subprocess
from pathlib import Path

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from .models import RBDSConfig, RBDSMessage, RBDSPSFrame
from .services import dynamic_ps

# Same path RBDSManager's own NOW_PLAYING_PATH points at (rbds_manager.py,
# read-only, owned by library/services/engine.py) -- deliberately a
# SEPARATE constant, not imported from rbds_manager.py, so this admin
# module never pulls in that whole daemon module (which calls
# django.setup() unconditionally at import time, being meant to run as a
# standalone process -- see its own module docstring) just for a preview
# read. See _read_current_now_playing()'s own docstring for why this
# preview intentionally does NOT reuse RBDSManager's last-good-value
# caching either.
_NOW_PLAYING_PATH = Path("/run/isadoraair/now_playing.json")


def _read_current_now_playing():
    """Best-effort, ONE-SHOT read for the server-rendered Generated PS
    preview only ([P1] 2.3E) -- deliberately NOT the same code path as
    RBDSManager._read_now_playing(), which the live engine tick uses and
    which has its own last-good-value caching ACROSS TICKS so a
    momentarily-missing/torn file degrades to whatever it last saw, not
    to blank (see that method's own docstring). A one-off admin page
    render has no "previous tick" to fall back to and no live on-air
    state to protect -- "current now-playing was unavailable" is a
    perfectly fine, clearly-labeled preview answer that the live engine
    could never accept. Returns None on any read/parse failure (missing
    file, torn non-atomic write -- see NOW_PLAYING_PATH's own comment in
    rbds_manager.py), never raises. Does not instantiate RBDSManager and
    does not touch or weaken its caching behavior in any way."""
    try:
        return json.loads(_NOW_PLAYING_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


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
                "station_ps", "ps_mode", "dynamic_ps_text", "dynamic_ps_format",
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
                "<b>Generated Rotating PS</b>: combines Dynamic PS Text with "
                "live now-playing information per Dynamic PS Format, then "
                "converts the result into a sequence of 8-character frames per "
                "Dynamic PS Mode (0-3) -- see the live preview below, which "
                "uses the real composer and frame generator, not a separate "
                "reimplementation.<br><br>"
                "<b>Dynamic PS Format</b> tokens: <code>{text}</code> = Dynamic "
                "PS Text; <code>{now_playing}</code> = current Artist - Title "
                "(recommended -- falls back cleanly to just Dynamic PS Text "
                "when nothing is playing); <code>{artist}</code>/"
                "<code>{title}</code> = the individual fields, blank when "
                "unknown, no special fallback. Default <code>{text}</code> "
                "preserves the original, now-playing-free behavior exactly. "
                "Examples: <code>{text}</code> or "
                "<code>{text} | Now Playing: {now_playing}</code>. Sourced only "
                "from the actual currently-playing track -- never from "
                "RadioText, RT+ promos, weather, or any other RBDS message "
                "content (those keep rotating completely independently, see "
                "the RadioText fieldset below)."
            ),
        }),
        ("Long PS", {
            "fields": ["long_ps_managed", "long_ps_enabled", "long_ps_source", "long_ps_static_text"],
            "description": (
                "Long PS is a separate RDS service from the ordinary 8-character "
                "Program Service (PS) above -- up to 32 characters, independent of "
                "PS Mode, and only shown by receivers that support it.<br><br>"
                "<b>Manage Long PS with IsadoraAir</b>: when off, IsadoraAir sends no "
                "Long PS commands at all and leaves any existing encoder-local Long PS "
                "configuration untouched -- turn on before the fields below have any "
                "effect.<br>"
                "<b>Static text</b>: always sends Long PS Static Text below.<br>"
                "<b>Now Playing</b>: sends the current track's Artist - Title, "
                "falling back to Long PS Static Text below whenever nothing usable "
                "is currently playing."
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
        rbds.services.dynamic_ps.compose_dynamic_ps_source() and
        generate_ps_frames() (the same two functions rbds_manager.py's
        live PS resolution calls, in the same order, [P1] 2.3E), never a
        second implementation of either. Reflects the object's
        last-SAVED state, not in-progress unsaved form edits -- a plain
        server-rendered read-only field is sufficient here, no JS
        live-preview. Never raises: any condition that would prevent a
        meaningful preview (mode not Generated, blank text, an
        unexpected generation failure) is shown as a concise status line
        instead.

        Uses a CURRENT now-playing reading when one is safely available
        (_read_current_now_playing(), a one-shot read that does NOT
        instantiate RBDSManager or touch its own last-good-value
        caching -- see that function's own docstring) so the preview can
        show real Artist/Title composition, not just the {text}-only
        case. If the file can't be safely read right now, the preview
        clearly says so and falls back to composing with no now-playing
        data at all (which is also exactly what the live engine itself
        would do if it hit the same read failure on a given tick)."""
        if obj is None or obj.pk is None:
            return "(save the config first to preview)"
        if obj.ps_mode != "generated":
            return "Not applicable — PS Mode is not Generated Rotating PS."
        if not obj.dynamic_ps_text.strip():
            return "Not applicable — Dynamic PS Text is blank."

        now_playing = _read_current_now_playing()
        header = ""
        if now_playing is None:
            header = "(current now-playing unavailable — composing with no now-playing data)\n\n"
            now_playing = {"title": "", "artist": ""}

        try:
            source = dynamic_ps.compose_dynamic_ps_source(obj.dynamic_ps_format, obj.dynamic_ps_text, now_playing)
            frames = dynamic_ps.generate_ps_frames(source, obj.dynamic_ps_mode)
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
        return format_html(
            '<pre style="margin:0;font-family:monospace">{}Resolved source:\n{}\n\nFrames:\n{}</pre>',
            header, source, lines,
        )
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
