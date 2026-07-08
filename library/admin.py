from pathlib import Path

from adminsortable2.admin import SortableAdminBase, SortableTabularInline
from django import forms
from django.contrib import admin, messages
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from .models import (
    Album,
    AnalysisConfig,
    Artist,
    RecencyConfig,
    Category,
    CategoryKind,
    DuplicateCandidate,
    Holiday,
    LogFillConfig,
    LogItem,
    Playlist,
    PlaylistItem,
    PlaylistLog,
    Rotation,
    RotationSlot,
    ScheduleBlock,
    Genre,
    Track,
    UITheme,
    UploadConfig,
)


# The `library` app has accumulated models that aren't really "song library"
# related (scheduling/traffic, site-wide config). Rather than a real Django
# app split (would mean renaming app_labels and migrating tables), this
# just regroups the admin index/sidebar display into clearer sections.
# Every model keeps living in `library` underneath — only the admin UI
# grouping changes.
_TRAFFIC_MODELS = {"playlist", "rotation", "scheduleblock", "playlistlog"}
_CONFIG_MODELS = {"analysisconfig", "recencyconfig", "uitheme", "logfillconfig", "uploadconfig"}


class SectionedAdminSite(admin.AdminSite):
    def get_app_list(self, request, app_label=None):
        app_dict = self._build_app_dict(request, app_label)
        library_app = app_dict.pop("library", None)
        if library_app:
            traffic_app = {**library_app, "name": "Traffic", "models": []}
            config_app = {**library_app, "name": "Config", "models": []}
            plain_models = []
            for model in library_app["models"]:
                key = model["object_name"].lower()
                if key in _TRAFFIC_MODELS:
                    traffic_app["models"].append(model)
                elif key in _CONFIG_MODELS:
                    config_app["models"].append(model)
                else:
                    plain_models.append(model)
            library_app["models"] = plain_models

            app_dict["library"] = library_app
            if traffic_app["models"]:
                app_dict["library_traffic"] = traffic_app
            if config_app["models"]:
                app_dict["library_config"] = config_app

        app_list = sorted(app_dict.values(), key=lambda x: x["name"].lower())
        for app in app_list:
            app["models"].sort(key=lambda x: x["name"])
        return app_list


admin.site.__class__ = SectionedAdminSite


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ["name", "created_at"]
    search_fields = ["name"]
    fields = ["name", "cover_art"]


@admin.register(Album)
class AlbumAdmin(admin.ModelAdmin):
    list_display = ["title", "album_artist", "year"]
    search_fields = ["title", "album_artist"]
    list_filter = ["year"]
    fields = ["title", "album_artist", "year", "cover_art"]


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


class RGBAColorWidget(forms.TextInput):
    """A native color picker (hue/sat/lightness) plus a separate opacity
    slider, combined client-side into the rgba() string this field
    actually stores. No JS library/CDN dependency — this box is LAN-only
    with no guaranteed internet access, so everything here is stock
    browser widgets + vanilla JS."""

    _TEMPLATE = """
<input type="hidden" name="__NAME__" id="__ID__" value="__VALUE__">
<input type="color" id="__ID___color" style="vertical-align:middle;">
<input type="range" id="__ID___alpha" min="0" max="100" step="1" style="vertical-align:middle; width:110px; margin-left:6px;">
<span id="__ID___alpha_label" style="display:inline-block; width:2.5em; font-size:0.85em;"></span>
<span id="__ID___preview" style="display:inline-block; width:22px; height:22px; border:1px solid #999; vertical-align:middle; margin-left:4px;"></span>
<script>
(function () {
    function parseColor(v) {
        v = (v || '').trim();
        var m = v.match(/^rgba?\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)\\s*(?:,\\s*([\\d.]+))?\\s*\\)$/i);
        if (m) return {r: +m[1], g: +m[2], b: +m[3], a: m[4] !== undefined ? parseFloat(m[4]) : 1};
        m = v.match(/^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i);
        if (m) return {r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16), a: 1};
        return {r: 55, g: 65, b: 81, a: 1};
    }
    function toHex(n) { return n.toString(16).padStart(2, '0'); }

    var hidden = document.getElementById('__ID__');
    var colorInput = document.getElementById('__ID___color');
    var alphaInput = document.getElementById('__ID___alpha');
    var alphaLabel = document.getElementById('__ID___alpha_label');
    var preview = document.getElementById('__ID___preview');

    var initial = parseColor(hidden.value);
    colorInput.value = '#' + toHex(initial.r) + toHex(initial.g) + toHex(initial.b);
    alphaInput.value = Math.round(initial.a * 100);

    function update() {
        var hex = colorInput.value;
        var r = parseInt(hex.substr(1, 2), 16);
        var g = parseInt(hex.substr(3, 2), 16);
        var b = parseInt(hex.substr(5, 2), 16);
        var a = alphaInput.value / 100;
        var rgba = 'rgba(' + r + ', ' + g + ', ' + b + ', ' + a.toFixed(2) + ')';
        hidden.value = rgba;
        preview.style.background = rgba;
        alphaLabel.textContent = alphaInput.value + '%';
    }
    colorInput.addEventListener('input', update);
    alphaInput.addEventListener('input', update);
    update();
})();
</script>
"""

    def render(self, name, value, attrs=None, renderer=None):
        widget_id = (attrs or {}).get("id", f"id_{name}")
        html = self._TEMPLATE
        html = html.replace("__NAME__", escape(name))
        html = html.replace("__ID__", escape(widget_id))
        html = html.replace("__VALUE__", escape(value or ""))
        return mark_safe(html)


@admin.register(CategoryKind)
class CategoryKindAdmin(admin.ModelAdmin):
    list_display = ["name", "code", "fill_color", "sort_order"]
    list_editable = ["fill_color", "sort_order"]
    ordering = ["sort_order", "name"]

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Covers both the regular add/change form and the changelist's
        # list_editable formset — both route through this hook, unlike
        # `ModelAdmin.form`, which only affects the former.
        if db_field.name == "fill_color":
            kwargs["widget"] = RGBAColorWidget
        return super().formfield_for_dbfield(db_field, request, **kwargs)


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

class BlockedSlotsWidget(forms.Widget):
    """Renders the same 7x24 grid partial used on the public track detail
    page (library/_blocked_slots_grid.html) inline in the admin change
    form. Each cell saves itself immediately via its own AJAX toggle
    endpoint (api_track_blocked_slot_toggle) -- it is NOT part of the
    surrounding form's normal POST data at all (no <input name=...> is
    ever rendered), so value_from_datadict() below always re-reads the
    live DB value rather than trusting the submitted form data. Without
    that, clicking the main admin "Save" button for any unrelated reason
    (e.g. fixing a typo in the title) would silently wipe out every
    previously-toggled slot back to Django's default empty-list value."""

    def __init__(self, track_id=None, *args, **kwargs):
        self.track_id = track_id
        super().__init__(*args, **kwargs)

    def render(self, name, value, attrs=None, renderer=None):
        if not self.track_id:
            return mark_safe("<p>(save the track first)</p>")
        # SimpleArrayField's own prepare_value() pre-stringifies the list
        # into a comma-joined string (e.g. "10,50") before it ever reaches
        # this widget, since that's what its default text-input widget
        # expects -- confirmed live (the admin path produced a broken
        # `new Set(10,50)` in the rendered JS before this normalization,
        # while the plain track_detail.html include path, which passes
        # the real Python list directly, never hit this).
        if isinstance(value, str):
            blocked_slots = [int(v) for v in value.split(",") if v.strip()]
        else:
            blocked_slots = list(value or [])
        return mark_safe(render_to_string("library/_blocked_slots_grid.html", {
            "track_id": self.track_id,
            "blocked_slots": blocked_slots,
        }))

    def value_from_datadict(self, data, files, name):
        if not self.track_id:
            return []
        return Track.objects.filter(pk=self.track_id).values_list(
            "blocked_slots", flat=True
        ).first() or []


class TrackAdminForm(forms.ModelForm):
    class Meta:
        model = Track
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["blocked_slots"].widget = BlockedSlotsWidget(track_id=self.instance.pk)


@admin.register(Track)
class TrackAdmin(admin.ModelAdmin):
    form = TrackAdminForm
    list_display = ["title", "artist", "album", "category", "duration_seconds", "ready2air"]
    list_filter = ["ready2air", "category", "additional_categories", "energy", "end_type", "format"]
    search_fields = ["title", "artist__name", "album__title"]
    list_editable = ["ready2air", "category"]
    list_per_page = 50
    list_select_related = ["artist", "album", "category"]
    raw_id_fields = ["artist", "album", "genre"]
    # 108 categories -- the dual-list-with-search widget is far more usable
    # here than Django's plain multi-select for a field with this many options.
    filter_horizontal = ["additional_categories"]
    actions = [mark_ready2air, mark_not_ready2air]
    readonly_fields = ["audio_preview"]

    @admin.display(description="Preview")
    def audio_preview(self, obj):
        # Plays through the browser's own output device, not the studio
        # monitor -- just a quick listen while reviewing, not a
        # broadcast-chain feature. Native browser codec support varies
        # (aiff/mp2 may not play in every browser even though the file is
        # served correctly) -- not fixable without on-the-fly transcoding.
        if not obj.pk or not obj.filepath:
            return "(save first)"
        url = reverse("library:api-track-audio", args=[obj.pk])
        return format_html('<audio controls preload="none" src="{}"></audio>', url)

    def delete_model(self, request, obj):
        # Single-object delete (after Django's own confirmation page).
        self._delete_file(request, obj)
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        # Bulk "Delete selected" action (after Django's own confirmation
        # page, which already blocks the whole batch if anything in it is
        # PROTECTed -- e.g. still referenced by an active Playlist). Same
        # file-then-row order as apply_duplicate_resolutions.
        for obj in queryset:
            self._delete_file(request, obj)
        super().delete_queryset(request, queryset)

    def _delete_file(self, request, obj):
        if not obj.filepath:
            return
        fp = Path(obj.filepath)
        if not fp.is_file():
            return
        try:
            fp.unlink()
        except OSError as exc:
            messages.error(request, f"Failed to delete file for '{obj}': {exc}")


@admin.register(DuplicateCandidate)
class DuplicateCandidateAdmin(admin.ModelAdmin):
    """Review-only: setting `resolution` here just records the decision --
    nothing gets deleted until `apply_duplicate_resolutions` is run
    separately (dry-run by default, needs --apply to actually touch
    anything). See find_duplicate_tracks for how these get populated."""
    list_display = ["track_a", "track_b", "confidence", "resolution", "applied", "created_at"]
    list_editable = ["resolution"]
    list_filter = ["confidence", "resolution", "applied"]
    search_fields = ["track_a__title", "track_a__artist__name", "track_b__title", "track_b__artist__name"]
    list_select_related = ["track_a", "track_a__artist", "track_b", "track_b__artist"]
    readonly_fields = ["applied", "created_at", "resolved_at", "track_a_file_info", "track_b_file_info"]
    # Without this, Django's default FK widget renders every Track (~29k)
    # as a <select> option for both track_a and track_b -- confirmed live,
    # this made the change page take 27s and return a 4.2MB response.
    raw_id_fields = ["track_a", "track_b"]
    fields = [
        "track_a", "track_a_file_info",
        "track_b", "track_b_file_info",
        "confidence", "resolution", "applied", "created_at", "resolved_at",
    ]

    @admin.display(description="Track A directory / file size")
    def track_a_file_info(self, obj):
        return self._file_info(obj.track_a)

    @admin.display(description="Track B directory / file size")
    def track_b_file_info(self, obj):
        return self._file_info(obj.track_b)

    def _file_info(self, track):
        # Real directory + size on disk, not just the DB's category
        # assignment -- lets you tell at a glance whether two candidates
        # are the literal same file (identical size) or different
        # versions/encodes of the same song (different size), per the
        # user's actual question.
        import os
        from pathlib import Path

        if not track or not track.filepath:
            return "(no filepath)"
        fp = Path(track.filepath)
        if not fp.is_file():
            return f"{fp.parent} -- file missing on disk"
        size = os.path.getsize(fp)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                size_str = f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
                break
            size /= 1024
        return f"{fp.parent} -- {size_str}"


class RotationSlotInline(SortableTabularInline):
    model = RotationSlot
    extra = 1
    fields = ["category", "track"]
    autocomplete_fields = ["category", "track"]


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
    readonly_fields = ["track_title", "track_artist"]


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


_UITHEME_COLOR_FIELDS = {
    "bg_dark", "bg_darker", "panel_bg", "accent", "accent_soft",
    "text_main", "text_muted", "danger", "border_subtle", "nav_clock_color",
    "deck_text_shadow_color", "deck_startsat_color", "deck_pill_text_color",
}


@admin.register(UITheme)
class UIThemeAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Branding", {
            "fields": ["logo", "station_logo"],
        }),
        ("Palette", {
            "fields": [
                "bg_dark", "bg_darker", "panel_bg",
                "accent", "accent_soft",
                "text_main", "text_muted",
                "danger", "border_subtle",
            ],
            "description": "Any valid CSS color value (hex, rgb(), or rgba()).",
        }),
        ("Nav Bar Clock", {
            "fields": ["nav_clock_font_size", "nav_clock_font_weight", "nav_clock_color"],
        }),
        ("Deck Overlay (album art)", {
            "fields": ["deck_text_shadow_color", "deck_startsat_color", "deck_pill_text_color", "default_album_art"],
            "description": "Deck title/artist/pills/etc. now render on top of "
                            "album art — these control the drop shadow that "
                            "keeps them readable, plus dedicated colors for "
                            "the \"Starts at\" text and the pill row. "
                            "default_album_art shows on a deck when no art is "
                            "found anywhere in the lookup chain.",
        }),
    ]

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in _UITHEME_COLOR_FIELDS:
            kwargs["widget"] = RGBAColorWidget
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    def has_add_permission(self, request):
        return not UITheme.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = UITheme.load()
        return HttpResponseRedirect(
            reverse("admin:library_uitheme_change", args=[obj.pk])
        )


@admin.register(LogFillConfig)
class LogFillConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Fill Strategy", {
            "fields": ["strategy", "fallback_category"],
            "description": "When a built log falls short of a full hour "
                            "(e.g. a playlist or rotation runs out early), "
                            "keep re-picking from this category — "
                            "respecting recency rules — until the hour is "
                            "filled as tightly as possible.",
        }),
    ]
    autocomplete_fields = ["fallback_category"]

    def has_add_permission(self, request):
        return not LogFillConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = LogFillConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_logfillconfig_change", args=[obj.pk])
        )


@admin.register(UploadConfig)
class UploadConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Batch Limits", {
            "fields": ["max_batch_size_mb"],
            "description": "Applies to the drag-and-drop track upload page "
                            "(/library/import/). Can't exceed nginx's own "
                            "hard ceiling (client_max_body_size) -- see "
                            "/etc/nginx/sites-available/isadoraair.",
        }),
    ]

    def has_add_permission(self, request):
        return not UploadConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = UploadConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_uploadconfig_change", args=[obj.pk])
        )
