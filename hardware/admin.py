from django import forms
from django.contrib import admin

from .devices import list_input_devices, list_output_devices
from .models import AudioInput, AudioOutput


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


@admin.register(AudioInput)
class AudioInputAdmin(_DeviceFieldAdmin):
    _enumerate = staticmethod(list_input_devices)
