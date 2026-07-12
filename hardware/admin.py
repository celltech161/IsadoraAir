import json
import re
import subprocess
from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse

from .devices import list_input_devices, list_mixer_controls, list_output_devices
from .models import AudioInput, AudioOutput, AudioPipeline, DuckingConfig, RemoteDJAudioInput

# Must match engine.py's STUDIO_MONITOR_NAME.
STUDIO_MONITOR_NAME = "Studio Monitor"

_CARD_RE = re.compile(r"plughw:(\d+),")


def _parse_card_number(device):
    m = _CARD_RE.match(device or "")
    return int(m.group(1)) if m else None


def _mixer_controls_for(obj):
    if not obj or not obj.device:
        return []
    card = _parse_card_number(obj.device)
    if card is None:
        return []
    return list_mixer_controls(card)


@admin.register(AudioPipeline)
class AudioPipelineAdmin(admin.ModelAdmin):
    fields = ["sample_rate", "program_gain_db"]

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

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            (None, {"fields": ["name", "device", "sort_order"]}),
            ("Software Gain", {
                "fields": ["gain_db"],
                "description": "Applied in the on-air mix, independent of the "
                                "hardware controls below.",
            }),
        ]
        controls = _mixer_controls_for(obj)
        if controls:
            fieldsets.append(("Hardware Mixer Controls", {
                "fields": [f"mixer_{i}" for i in range(len(controls))],
                "description": "Real ALSA mixer controls discovered live for "
                                "this input's card (via amixer) -- exactly what "
                                "the connected hardware actually exposes, nothing "
                                "hardcoded. Changes apply immediately to the "
                                "hardware on Save.",
            }))
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        # _changeform_view calls get_form(fields=flatten_fieldsets(...)).
        # Django's OWN base get_form() ALSO internally derives `fields`
        # from get_fieldsets() whenever it isn't explicitly passed at
        # all (confirmed live -- calling get_form() directly with no
        # `fields` kwarg hit the exact same FieldError as the admin
        # view path). Either way, whatever `fields` ends up being,
        # our own synthetic "mixer_N" names from get_fieldsets() above
        # must never reach modelform_factory (it validates `fields`
        # against real model fields) -- so always compute and filter it
        # explicitly here rather than only when a caller happens to
        # pass one in.
        fields = kwargs.pop("fields", None)
        if fields is None:
            from django.contrib.admin.utils import flatten_fieldsets
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        kwargs["fields"] = [f for f in fields if not f.startswith("mixer_")]
        form = super().get_form(request, obj, **kwargs)
        controls = _mixer_controls_for(obj)
        existing = (obj.mixer_control_values if obj else {}) or {}
        control_map = {}
        for idx, control in enumerate(controls):
            field_name = f"mixer_{idx}"
            control_map[field_name] = control
            current = existing.get(control["control_id"])
            if control["has_enum"]:
                choices = [(item, item) for item in control["enum_items"]]
                initial = current if current in control["enum_items"] else control["enum_value"]
                form.base_fields[field_name] = forms.ChoiceField(
                    label=control["label"], choices=choices, initial=initial, required=False,
                )
            elif control["has_switch"] and not control["has_volume"]:
                initial = current if current is not None else control["on"]
                form.base_fields[field_name] = forms.BooleanField(
                    label=control["label"], initial=bool(initial), required=False,
                )
            elif control["has_volume"]:
                initial = current if current is not None else control["value_pct"]
                form.base_fields[field_name] = forms.IntegerField(
                    label=f"{control['label']} (%)", initial=initial, required=False,
                    min_value=0, max_value=100,
                )
        form._mixer_control_map = control_map
        return form

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        control_map = getattr(form, "_mixer_control_map", {})
        if not control_map:
            return
        card = _parse_card_number(obj.device)
        values = dict(obj.mixer_control_values or {})
        changed = False
        for field_name, control in control_map.items():
            if field_name not in form.cleaned_data:
                continue
            new_value = form.cleaned_data[field_name]
            control_id = control["control_id"]
            if values.get(control_id) == new_value:
                continue
            values[control_id] = new_value
            changed = True
            if control["has_enum"]:
                amixer_value = new_value
            elif control["has_switch"] and not control["has_volume"]:
                amixer_value = "on" if new_value else "off"
            else:
                amixer_value = f"{new_value}%"
            try:
                subprocess.run(
                    ["amixer", "-c", str(card), "sset", control_id, str(amixer_value)],
                    capture_output=True, text=True, timeout=5, check=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
                messages.error(request, f"Failed to apply '{control['label']}': {exc}")
        if changed:
            obj.mixer_control_values = values
            obj.save(update_fields=["mixer_control_values"])


@admin.register(DuckingConfig)
class DuckingConfigAdmin(admin.ModelAdmin):
    fields = ["enabled", "duck_level_db"]

    def has_add_permission(self, request):
        return not DuckingConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = DuckingConfig.load()
        return HttpResponseRedirect(
            reverse("admin:hardware_duckingconfig_change", args=[obj.pk])
        )


@admin.register(RemoteDJAudioInput)
class RemoteDJAudioInputAdmin(admin.ModelAdmin):
    fields = ["gain_db"]

    def has_add_permission(self, request):
        return not RemoteDJAudioInput.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = RemoteDJAudioInput.load()
        return HttpResponseRedirect(
            reverse("admin:hardware_remotedjaudioinput_change", args=[obj.pk])
        )
