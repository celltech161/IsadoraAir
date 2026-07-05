import json
import subprocess
from pathlib import Path

from django import forms
from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .devices import list_input_devices, list_output_devices
from .models import AudioInput, AudioOutput, AudioPipeline

# Must match engine.py's STUDIO_MONITOR_NAME.
STUDIO_MONITOR_NAME = "Studio Monitor"


@admin.register(AudioPipeline)
class AudioPipelineAdmin(admin.ModelAdmin):
    fields = ["sample_rate"]

    class Media:
        js = ["hardware/js/audio_pipeline_confirm.js"]

    def has_add_permission(self, request):
        return not AudioPipeline.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = AudioPipeline.load()
        return HttpResponseRedirect(
            reverse("admin:hardware_audiopipeline_change", args=[obj.pk])
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Sample rate is baked into the pipeline's topology at build time
        # (the final capsfilter + silence-priming caps in engine.py) —
        # unlike AGC, there's no live-property path for this, it needs a
        # full engine restart. Same fire-and-forget mechanism as the
        # dashboard's "Restart Engine" button (api_engine_restart).
        subprocess.Popen(["sudo", "systemctl", "restart", "isadoraair-engine"])


class _DeviceFieldAdmin(admin.ModelAdmin):
    """Swap the 'device' CharField for a Select populated from the
    runtime hardware enumeration. Subclasses set `_enumerate` to the
    aplay-/arecord-backed discovery function."""
    _enumerate = None

    list_display = ["name", "device", "sort_order"]
    list_editable = ["sort_order"]
    ordering = ["sort_order", "name"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "device" in form.base_fields and callable(self._enumerate):
            choices = list(self._enumerate())
            # preserve current value even if hardware changed since last save
            if obj and obj.device and not any(obj.device == c[0] for c in choices):
                choices = [(obj.device, f"{obj.device} (UNAVAILABLE)")] + choices
            choices = [("", "— not configured —")] + choices
            form.base_fields["device"].widget = forms.Select(choices=choices)
        return form


@admin.register(AudioOutput)
class AudioOutputAdmin(_DeviceFieldAdmin):
    _enumerate = staticmethod(list_output_devices)

    def get_fieldsets(self, request, obj=None):
        fieldsets = [(None, {"fields": ["name", "device", "sort_order"]})]
        if obj and obj.name == STUDIO_MONITOR_NAME:
            fieldsets.append(("AGC (Studio Monitor Leveling)", {
                "fields": ["agc_enabled", "agc_ratio", "agc_threshold", "agc_soft_knee", "agc_makeup_gain_db"],
                "description": "Interim leveling for this output only "
                                "(compressor + makeup gain + safety limiter). "
                                "Not the transmitter feed — StereoTool will "
                                "handle that separately. Changes apply live "
                                "on Save, no engine restart needed.",
            }))
        return fieldsets

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.name == STUDIO_MONITOR_NAME:
            Path("/run/isadoraair/engine_cmd.json").write_text(
                json.dumps({"command": "reload_agc_config"}), encoding="utf-8"
            )


@admin.register(AudioInput)
class AudioInputAdmin(_DeviceFieldAdmin):
    _enumerate = staticmethod(list_input_devices)
