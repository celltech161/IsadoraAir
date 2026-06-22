from django.contrib import admin

from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import (
    Album,
    AnalysisConfig,
    Artist,
    Category,
    Clock,
    ClockSlot,
    Holiday,
    LogItem,
    PlaylistLog,
    Rotation,
    RotationSlot,
    ScheduleBlock,
    Genre,
    Track,
)


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    search_fields = ["name"]


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ["title", "album_artist", "year"]
    search_fields = ["title", "album_artist"]
    list_filter = ["year"]


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "sort_order"]
    list_editable = ["name", "sort_order"]


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "month", "day", "ramp_in_days", "ramp_out_days"]


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ["title", "artist", "album", "category", "duration_seconds", "ready2air"]
    list_filter = ["ready2air", "category", "energy", "end_type"]
    search_fields = ["title", "artist__name", "album__title"]
    list_editable = ["ready2air", "category"]
    list_per_page = 50
    raw_id_fields = ["artist", "album", "genre"]


class RotationSlotInline(admin.TabularInline):
    model = RotationSlot
    extra = 1


@admin.register(Rotation)
class RotationAdmin(admin.ModelAdmin):
    list_display = ["name", "description"]
    inlines = [RotationSlotInline]


class ClockSlotInline(admin.TabularInline):
    model = ClockSlot
    extra = 1


@admin.register(Clock)
class ClockAdmin(admin.ModelAdmin):
    list_display = ["name", "description"]
    inlines = [ClockSlotInline]


@admin.register(ScheduleBlock)
class ScheduleBlockAdmin(admin.ModelAdmin):
    list_display = ["__str__", "clock", "start_time", "end_time"]
    list_filter = ["day_of_week", "clock"]


class LogItemInline(admin.TabularInline):
    model = LogItem
    extra = 0
    raw_id_fields = ["track"]


@admin.register(PlaylistLog)
class PlaylistLogAdmin(admin.ModelAdmin):
    list_display = ["date", "status", "generated_at"]
    list_filter = ["status"]
    inlines = [LogItemInline]


@admin.register(AnalysisConfig)
class AnalysisConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Detection Thresholds", {
            "fields": [
                "next_start_threshold_db",
                "cue_in_threshold_db",
                "cue_in_min_seconds",
            ],
        }),
        ("Analysis Parameters", {
            "fields": [
                "analysis_sample_rate",
                "analysis_window_seconds",
                "waveform_points",
            ],
        }),
    ]

    def has_add_permission(self, request):
        return not AnalysisConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = AnalysisConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_analysisconfig_change", args=[obj.pk])
        )
