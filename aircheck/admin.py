from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import AircheckConfig, AircheckSession


@admin.register(AircheckConfig)
class AircheckConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Format", {"fields": ["audio_format", "bitrate"]}),
        ("Storage", {"fields": ["output_directory", "filename_template"]}),
        ("Source", {"fields": ["source_device"]}),
    ]

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
