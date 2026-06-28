from adminsortable2.admin import SortableAdminBase, SortableTabularInline
from django.contrib import admin
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import (
    Album,
    AnalysisConfig,
    Artist,
    RecencyConfig,
    Category,
    Holiday,
    LogItem,
    Playlist,
    PlaylistItem,
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
    list_display = ["code", "name", "kind", "track_count", "recency_mode", "artist_separation", "title_separation", "sort_order"]
    list_editable = ["name", "kind", "sort_order"]
    list_filter = ["kind", "recency_mode"]
    search_fields = ["code", "name"]
    fieldsets = [
        (None, {"fields": ["code", "name", "kind", "description", "color", "sort_order"]}),
        ("Recency Overrides", {
            "fields": ["recency_mode", "artist_separation", "title_separation"],
            "description": "Leave separation fields blank to use global defaults from Recency Configuration.",
        }),
    ]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_track_count=Count("tracks"))

    @admin.display(description="Tracks", ordering="_track_count")
    def track_count(self, obj):
        return obj._track_count


@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "month", "day", "ramp_in_days", "ramp_out_days"]


@admin.action(description="Mark selected tracks as ready to air")
def mark_ready2air(modeladmin, request, queryset):
    queryset.update(ready2air=True)

@admin.action(description="Mark selected tracks as NOT ready to air")
def mark_not_ready2air(modeladmin, request, queryset):
    queryset.update(ready2air=False)

@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    list_display = ["title", "artist", "album", "category", "duration_seconds", "ready2air"]
    list_filter = ["ready2air", "category", "energy", "end_type", "format"]
    search_fields = ["title", "artist__name", "album__title"]
    list_editable = ["ready2air", "category"]
    list_per_page = 50
    list_select_related = ["artist", "album", "category"]
    raw_id_fields = ["artist", "album", "genre"]
    actions = [mark_ready2air, mark_not_ready2air]


class RotationSlotInline(SortableTabularInline):
    model = RotationSlot
    extra = 1
    fields = ["category"]
    autocomplete_fields = ["category"]


@admin.register(Rotation)
class RotationAdmin(SortableAdminBase, admin.ModelAdmin):
    list_display = ["name", "description", "slot_count"]
    search_fields = ["name"]
    inlines = [RotationSlotInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_slot_count=Count("slots"))

    @admin.display(description="Slots", ordering="_slot_count")
    def slot_count(self, obj):
        return obj._slot_count


class PlaylistItemInline(SortableTabularInline):
    model = PlaylistItem
    extra = 1
    fields = ["track"]
    autocomplete_fields = ["track"]


@admin.register(Playlist)
class PlaylistAdmin(SortableAdminBase, admin.ModelAdmin):
    list_display = ["name", "description", "item_count"]
    search_fields = ["name"]
    inlines = [PlaylistItemInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_item_count=Count("items"))

    @admin.display(description="Items", ordering="_item_count")
    def item_count(self, obj):
        return obj._item_count


@admin.register(ScheduleBlock)
class ScheduleBlockAdmin(admin.ModelAdmin):
    list_display = ["__str__", "rotation", "playlist", "start_time", "end_time"]
    list_filter = ["day_of_week", "rotation", "playlist"]
    autocomplete_fields = ["rotation", "playlist"]


class LogItemInline(admin.TabularInline):
    model = LogItem
    extra = 0
    raw_id_fields = ["track"]


@admin.register(PlaylistLog)
class PlaylistLogAdmin(admin.ModelAdmin):
    list_display = ["date", "hour", "status", "generated_at"]
    list_filter = ["status", "date"]
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
        ("Waveform Display", {
            "fields": [
                "waveform_floor_db",
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


@admin.register(RecencyConfig)
class RecencyConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Global Defaults", {
            "fields": ["artist_separation", "title_separation"],
            "description": "These apply to all categories unless overridden on the category itself.",
        }),
    ]

    def has_add_permission(self, request):
        return not RecencyConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = RecencyConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_recencyconfig_change", args=[obj.pk])
        )
