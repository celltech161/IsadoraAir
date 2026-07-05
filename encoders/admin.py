import subprocess

from django import forms
from django.contrib import admin

from hardware.devices import list_input_devices

from .models import Encoder


@admin.register(Encoder)
class EncoderAdmin(admin.ModelAdmin):
    list_display = ["name", "enabled", "protocol", "host", "port", "format", "bitrate_kbps", "sort_order"]
    list_editable = ["enabled", "sort_order"]
    ordering = ["sort_order", "name"]

    fieldsets = [
        ("Basic", {"fields": ["name", "enabled", "protocol", "sort_order"]}),
        ("Connection", {"fields": ["host", "port", "mount", "username", "password"]}),
        ("Encoding", {"fields": ["format", "bitrate_kbps", "input_device"]}),
        ("Stream Info", {"fields": ["station_name", "genre", "description", "url", "public"]}),
    ]

    class Media:
        js = ["encoders/js/encoder_confirm.js"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "input_device" in form.base_fields:
            choices = list(list_input_devices())
            if obj and obj.input_device and not any(obj.input_device == c[0] for c in choices):
                choices = [(obj.input_device, f"{obj.input_device} (UNAVAILABLE)")] + choices
            choices = [("", "— default (StereoTool HD Output bridge) —")] + choices
            form.base_fields["input_device"].widget = forms.Select(choices=choices)
        return form

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        self._restart_encoders()

    def delete_model(self, request, obj):
        super().delete_model(request, obj)
        self._restart_encoders()

    def delete_queryset(self, request, queryset):
        super().delete_queryset(request, queryset)
        self._restart_encoders()

    def _restart_encoders(self):
        # Add/edit/remove are all topology changes for the encoders
        # manager process (each enabled row gets its own independent
        # Liquidsoap subprocess) — no live-reload path, matches
        # AudioPipelineAdmin's restart-on-save convention.
        subprocess.Popen(["sudo", "systemctl", "restart", "isadoraair-encoders"])
