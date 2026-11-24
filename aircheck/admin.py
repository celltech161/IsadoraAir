from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from .models import AircheckConfig, AircheckSession


@admin.register(AircheckConfig)
class AircheckConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Format", {"fields": ["audio_format", "bitrate"]}),
        ("Storage", {"fields": ["output_directory", "filename_template"]}),
        ("Source", {
            "fields": ["effective_source"],
            "description": (
                "Aircheck records the same program feed used by the default "
                "Liquidsoap encoder group. There is no independent capture "
                "device selection."
            ),
        }),
    ]
    # AircheckConfig.source_device is a legacy column that nothing reads any
    # more. It is deliberately not an editable field: it is absent from the
    # fieldsets, so Django builds no form field for it and a forged POST value
    # cannot change the stored row.
    readonly_fields = ["effective_source"]

    @admin.display(description="Capture source")
    def effective_source(self, obj):
        # Lazy import: the canonical constant lives with the encoder-group
        # builder. aircheck.views already loads that module in every web
        # worker, so this adds no new dependency, and importing it here
        # opens no device and touches no encoder state.
        from encoders.services.encoder_manager import DEFAULT_INPUT_DEVICE

        return format_html(
            "<strong>{}</strong> \u2014 shared default encoder-group source",
            DEFAULT_INPUT_DEVICE,
        )

    def has_add_permission(self, request):
        return not AircheckConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = AircheckConfig.load()
        return HttpResponseRedirect(
            reverse("admin:aircheck_aircheckconfig_change", args=[obj.pk])
        )


@admin.register(AircheckSession)
class AircheckSessionAdmin(admin.ModelAdmin):
    list_display = ["started_at", "ended_at", "audio_format", "bitrate",
                     "still_running", "size_bytes", "filename"]
    list_filter = ["audio_format", "still_running"]
    ordering = ["-started_at"]
    date_hierarchy = "started_at"
    readonly_fields = [
        "started_at", "ended_at", "filename", "audio_format", "bitrate",
        "source_device", "ffmpeg_pid", "still_running", "size_bytes", "exit_note",
    ]

    def has_add_permission(self, request):
        return False
