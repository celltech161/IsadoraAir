from pathlib import Path

from adminsortable2.admin import SortableAdminBase, SortableAdminMixin, SortableTabularInline
from django import forms
from django.contrib import admin, messages
from library.auth_forms import InviteCapablePasswordResetForm
from django.contrib.auth.forms import UserChangeForm as DjangoUserChangeForm
from django.contrib.auth.models import User
from django.db.models import Count
from django.http import HttpResponseRedirect
from django.template.loader import render_to_string
from django.urls import path, reverse
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
    EmailLog,
    GroupAccess,
    Holiday,
    LogFillConfig,
    LogItem,
    NavMenuItem,
    PlayEvent,
    Playlist,
    PlaylistItem,
    PlaylistLog,
    RemoteDJConfig,
    Rotation,
    RotationSlot,
    RoyaltyReport,
    ScheduleBlock,
    Genre,
    FXBusConfig,
    FXCart,
    StationInfo,
    VoiceTrack,
    VoiceTrackConfig,
    StationTimeConfig,
    TuneInConfig,
    Track,
    UITheme,
    CDRipConfig,
    UploadConfig,
)


# The `library` app has accumulated models that aren't really "song library"
# related (scheduling/traffic, site-wide config). Rather than a real Django
# app split (would mean renaming app_labels and migrating tables), this
# just regroups the admin index/sidebar display into clearer sections.
# Every model keeps living in `library` underneath — only the admin UI
# grouping changes.
_TRAFFIC_MODELS = {"playlist", "rotation", "scheduleblock", "playlistlog"}
_CONFIG_MODELS = {"analysisconfig", "recencyconfig", "uitheme", "logfillconfig", "uploadconfig", "navmenuitem", "remotedjconfig", "stationtimeconfig", "stationinfo", "tuneinconfig", "fxbusconfig", "fxcart", "voicetrackconfig"}
_LOG_MODELS = {"emaillog", "playevent", "royaltyreport"}


class SectionedAdminSite(admin.AdminSite):
    def get_app_list(self, request, app_label=None):
        app_dict = self._build_app_dict(request, app_label)
        library_app = app_dict.pop("library", None)
        if library_app:
            traffic_app = {**library_app, "name": "Traffic", "models": []}
            config_app = {**library_app, "name": "Config", "models": []}
            logs_app = {**library_app, "name": "Logs", "models": []}
            plain_models = []
            for model in library_app["models"]:
                key = model["object_name"].lower()
                if key in _TRAFFIC_MODELS:
                    traffic_app["models"].append(model)
                elif key in _CONFIG_MODELS:
                    config_app["models"].append(model)
                elif key in _LOG_MODELS:
                    logs_app["models"].append(model)
                else:
                    plain_models.append(model)
            library_app["models"] = plain_models

            app_dict["library"] = library_app
            if traffic_app["models"]:
                app_dict["library_traffic"] = traffic_app
            if config_app["models"]:
                app_dict["library_config"] = config_app
            if logs_app["models"]:
                app_dict["library_logs"] = logs_app

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
        ("Cue-point Analysis Overrides", {
            "fields": ["next_start_threshold_db_override", "cue_in_threshold_db_override"],
            "description": (
                "Per-category dBFS thresholds for the automatic cue-point "
                "analyzer (analyze_tracks). Leave blank to use the global "
                "defaults from Analysis Configuration. Use MORE-NEGATIVE "
                "values (quieter, e.g. -35 dBFS instead of the default -26) "
                "for genres that sit below normal broadcast level -- "
                "classical, ambient, spoken word -- otherwise the "
                "next-track trigger fires over the natural tail. Category "
                "detail page in the frontend has a \"Re-analyze Tracks\" "
                "button that re-runs the analysis on every track in the "
                "category with these overrides applied."
            ),
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
    list_display = ["title", "artist", "album", "category", "duration_seconds", "isrc", "ready2air"]
    list_filter = ["ready2air", "category", "additional_categories", "energy", "end_type", "format"]
    search_fields = ["title", "artist__name", "album__title", "isrc"]
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
                "waveform_left_color",
                "waveform_right_color",
            ],
            "description": "Track detail pages show a stereo L/R split waveform "
                            "(zero-amplitude line centered, left channel drawn "
                            "upward, right channel drawn downward).",
        }),
    ]

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        if db_field.name in ("waveform_left_color", "waveform_right_color"):
            kwargs["widget"] = forms.TextInput(attrs={"type": "color"})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

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


@admin.register(StationTimeConfig)
class StationTimeConfigAdmin(admin.ModelAdmin):
    """Singleton: the station's operating timezone. Picks from every
    IANA zone the running Python knows about (~400+). Applied to
    every UI clock, the /schedule/ current-hour highlight, Coming Up
    ETAs, and event timestamps. Takes effect on the very next
    request -- no code edit, no gunicorn restart."""
    fieldsets = [
        (None, {
            "fields": ["timezone"],
            "description": (
                "Displayed times across the site are pinned to this "
                "timezone regardless of a viewer's device timezone. "
                "Stored data in the database stays UTC; only display "
                "shifts."
            ),
        }),
    ]

    def has_add_permission(self, request):
        return not StationTimeConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        obj = StationTimeConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_stationtimeconfig_change", args=[obj.pk])
        )


@admin.register(TuneInConfig)
class TuneInConfigAdmin(admin.ModelAdmin):
    """Singleton: TuneIn AIR now-playing API credentials + push state.
    See TuneInConfig docstring; obtained by emailing TuneIn per
    https://tunein.com/broadcasters/api/. Pushes are driven by the
    isadoraair-tunein-push.timer (every 30s, no-op when nothing has
    changed since the last successful push)."""
    fieldsets = [
        ("Credentials", {
            "fields": ["station_id", "partner_id", "partner_key", "enabled"],
            "description": (
                "Values from TuneIn's registration email. station_id must "
                "start with 's' (e.g. 's339896'). Uncheck `enabled` to pause "
                "pushes without deleting the credentials -- useful during "
                "diagnosis or a syndicated block."
            ),
        }),
        ("Push state (read-only)", {
            "fields": ["last_pushed_play_event_id", "last_pushed_at", "last_push_status"],
            "description": (
                "Updated by the timer. last_pushed_play_event_id bumps only "
                "on a successful HTTP 2xx from TuneIn -- a transient failure "
                "leaves this alone so the next timer fire retries the same "
                "PlayEvent."
            ),
        }),
    ]
    readonly_fields = ["last_pushed_play_event_id", "last_pushed_at", "last_push_status"]

    def has_add_permission(self, request):
        return not TuneInConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        obj = TuneInConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_tuneinconfig_change", args=[obj.pk])
        )


@admin.register(StationInfo)
class StationInfoAdmin(admin.ModelAdmin):
    """Singleton: station-identifying strings used in SoundExchange NCE
    reports. Empty is allowed (the NCE header row will just carry
    blanks), but you'll want to fill it in before submitting a real
    report. Editable without a restart -- next report generation
    picks up the new values."""
    fieldsets = [
        (None, {
            "fields": ["legal_name", "call_letters", "stream_name"],
            "description": (
                "These strings appear in the header row of the SoundExchange "
                "NCE Report of Use (Reports page -> Generate). Not read by the "
                "playback engine or streaming encoders -- purely licensor-"
                "facing paperwork."
            ),
        }),
    ]

    def has_add_permission(self, request):
        return not StationInfo.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        obj = StationInfo.load()
        return HttpResponseRedirect(
            reverse("admin:library_stationinfo_change", args=[obj.pk])
        )


@admin.register(CDRipConfig)
class CDRipConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Drive", {
            "fields": ["device", "drive_read_offset"],
            "description": "The optical drive on the box, and its "
                           "AccurateRip read offset. If you replace the "
                           "drive, look up the new model's offset at "
                           "accuraterip.com/driveoffsets.htm and update "
                           "the value here -- no redeploy needed. "
                           "This install ships with hp PLDS DVDRW "
                           "DU8AESH which uses offset +6.",
        }),
        ("Rip pipeline", {
            "fields": ["staging_root", "require_accurate_rip"],
        }),
    ]

    def has_add_permission(self, request):
        return not CDRipConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = CDRipConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_cdripconfig_change", args=[obj.pk])
        )


@admin.register(RemoteDJConfig)
class RemoteDJConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        (None, {
            "fields": ["enabled"],
            "description": "Master switch for Remote DJ over WebRTC. While "
                            "off, engine.py's pipeline is completely "
                            "unchanged and the signaling server doesn't "
                            "start -- flipping this on requires an engine "
                            "restart to take effect.",
        }),
        ("WebRTC/ICE", {
            "fields": ["stun_server", "ice_udp_min_port", "ice_udp_max_port"],
        }),
    ]

    def has_add_permission(self, request):
        return not RemoteDJConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = RemoteDJConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_remotedjconfig_change", args=[obj.pk])
        )


class NavMenuChildInline(SortableTabularInline):
    # Self-referential inline: children of the parent NavMenuItem being
    # edited. fk_name is required since Django can't infer which FK to
    # NavMenuItem to use for the inline relation on a self-referential
    # model (there's only one here, but the ORM still needs it spelled
    # out explicitly).
    model = NavMenuItem
    fk_name = "parent"
    extra = 1
    fields = ["label", "url_name", "custom_url", "extra_active_view_names", "enabled", "open_in_new_tab"]


@admin.register(GroupAccess)
class GroupAccessAdmin(admin.ModelAdmin):
    """Standalone Group Access admin. ALSO surfaced as an inline on
    the auth.Group admin below (GroupAccessInline + GroupAdmin) so a
    fresh deployment can wire up permissions in one obvious place --
    edit the Group, configure access on the same page."""
    list_display = ("group", "priority", "landing_url")
    list_editable = ("priority", "landing_url")
    ordering = ("priority", "group__name")
    fieldsets = (
        (None, {
            "fields": ("group", "priority", "landing_url"),
            "description": (
                "Which auth.Group this row configures, where its members "
                "land after login (raw URL path, e.g. /library/), and "
                "how it competes with other groups for the landing choice "
                "(lower priority number wins)."
            ),
        }),
        ("Allowed request paths", {
            "fields": ("allowed_prefixes", "allowed_exact", "allowed_regex"),
            "description": (
                "One entry per line in each of the three lists. "
                "A member of this group can reach any request path in "
                "the UNION of the three lists. Middleware caches the "
                "computed set and invalidates on save."
            ),
        }),
    )


class GroupAccessInline(admin.StackedInline):
    """Inline GroupAccess on the auth.Group admin page. Optional
    -- a Group without a GroupAccess row is treated as 'not a
    recognized group' by the middleware (its members fall to the
    welcome page). Adding this inline row is how you grant a Group
    any privileges at all."""
    model = GroupAccess
    can_delete = True
    max_num = 1
    fields = ("priority", "landing_url", "allowed_prefixes", "allowed_exact", "allowed_regex")


# Re-register auth.Group's admin with our inline attached. Django ships
# a default GroupAdmin; we unregister and register a subclass so the
# access rows show up on the standard Groups page in admin. Uses import-
# time try/except so a fresh install that hasn't loaded auth yet doesn't
# blow up (auth is core to Django though; in practice this always runs).
try:
    from django.contrib.auth.admin import GroupAdmin
    from django.contrib.auth.models import Group as AuthGroup

    class GroupAdminWithAccess(GroupAdmin):
        inlines = [GroupAccessInline]

    admin.site.unregister(AuthGroup)
    admin.site.register(AuthGroup, GroupAdminWithAccess)
except Exception:
    # Django admin not fully wired yet -- next request will import
    # this module again after apps are ready. No blocking failure.
    pass


@admin.register(NavMenuItem)
class NavMenuItemAdmin(SortableAdminMixin, admin.ModelAdmin):
    # Top-level items only -- children are managed via the sortable inline
    # below, on their own parent's change form (same shape as
    # RotationSlotInline/PlaylistItemInline above), not mixed into this
    # changelist's own drag-reorder sequence.
    list_display = ["label", "resolved_url", "enabled", "open_in_new_tab", "child_count"]
    list_filter = ["enabled"]
    search_fields = ["label"]
    fields = ["label", "url_name", "custom_url", "extra_active_view_names", "enabled", "open_in_new_tab"]
    inlines = [NavMenuChildInline]

    def get_queryset(self, request):
        return super().get_queryset(request).filter(parent__isnull=True).annotate(_child_count=Count("children"))

    def get_extra_model_filters(self, request):
        return {"parent__isnull": True}

    @admin.display(description="Children", ordering="_child_count")
    def child_count(self, obj):
        return obj._child_count


# --- Password-setup invite for the built-in User admin ---
#
# Onboarding a new DJ (studio or remote) means creating a User with no
# password they know -- the admin User-add form still requires SOMETHING
# be typed into the password fields, but that value is never handed to
# the DJ. Instead of a manual "note the temp password down, tell them
# over the phone" step, this adds a button on the User change form that
# fires the exact same PasswordResetForm flow /password-reset/ uses --
# same email templates, same signed token, same expiry -- so the DJ's
# first-ever login is through the normal "set your password" page.
# Safe to click on ANY user (not just brand new ones): it's the same
# "forgot password" flow, so it just gives them a fresh way in.
admin.site.unregister(User)


class UserAddNoPasswordForm(forms.ModelForm):
    """Add-form for User with the password fields removed entirely --
    the account is created with set_unusable_password() so the ONLY way
    in is the invite email (or a future admin-triggered reset), never a
    password someone typed into this form and then had to relay to the
    DJ out of band."""
    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_unusable_password()
        if commit:
            user.save()
        return user


@admin.register(User)
class InviteCapableUserAdmin(admin.ModelAdmin):
    # Mirrors django.contrib.auth.admin.UserAdmin's shape closely enough
    # for this project's needs (no groups/permissions editor customization
    # otherwise) while inserting the invite button into "Personal info"
    # right after email, and the invite status into the changelist.
    list_display = ["username", "email", "first_name", "last_name", "is_staff", "password_status"]
    list_filter = ["is_staff", "is_superuser", "is_active", "groups"]
    search_fields = ["username", "first_name", "last_name", "email"]
    ordering = ["username"]
    filter_horizontal = ["groups", "user_permissions"]
    form = DjangoUserChangeForm  # the real one -- read-only hash display + "change password" link
    fieldsets = (
        (None, {"fields": ("username", "password")}),
        ("Personal info", {"fields": ("first_name", "last_name", "email", "invite_button")}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Important dates", {"fields": ("last_login", "date_joined")}),
    )
    # No password1/password2 -- add_form creates the account with
    # set_unusable_password() instead. Plain ModelAdmin doesn't know to
    # actually USE add_form/add_fieldsets on its own the way Django's
    # real UserAdmin does -- get_form()/get_fieldsets() below wire that
    # up explicitly (mirroring contrib.auth.admin.UserAdmin's own
    # pattern). Without this override, get_fieldsets() always returns
    # `fieldsets` regardless of add-vs-change, which made the ADD page
    # try to render "password" and "invite_button" through fields that
    # add_form never declared -- ModelAdmin's fieldset-driven form
    # builder then silently synthesized a PLAIN TEXT password field
    # from the raw model column instead of erroring, which is exactly
    # the footgun this whole feature exists to avoid. Caught via a real
    # Playwright add-user run before this could ship.
    add_form = UserAddNoPasswordForm
    add_fieldsets = (
        (None, {"fields": ("username", "first_name", "last_name", "email")}),
    )
    readonly_fields = ["invite_button"]

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = self.add_form
        return super().get_form(request, obj, **kwargs)

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return self.add_fieldsets
        return super().get_fieldsets(request, obj)

    def get_urls(self):
        return [
            path(
                "<int:user_id>/send-invite/",
                self.admin_site.admin_view(self.send_invite_view),
                name="auth_user_send_invite",
            ),
            *super().get_urls(),
        ]

    def send_invite_view(self, request, user_id):
        user = self.get_object(request, user_id)
        if user is None:
            self.message_user(request, "User not found.", level=messages.ERROR)
            return HttpResponseRedirect(reverse("admin:auth_user_changelist"))
        if not user.email:
            self.message_user(
                request, f"{user.username} has no email address on file -- add one first.",
                level=messages.ERROR,
            )
        else:
            form = InviteCapablePasswordResetForm({"email": user.email})
            if form.is_valid():
                form.save(
                    request=request,
                    use_https=request.is_secure(),
                    email_template_name="registration/password_reset_email.html",
                    subject_template_name="registration/password_reset_subject.txt",
                )
                self.message_user(request, f"Password setup email sent to {user.email}.")
            else:
                # Realistically only fires if the address is malformed --
                # PasswordResetForm.save() is a deliberate no-op (not an
                # error) for a well-formed address with no matching user,
                # to avoid leaking account existence.
                self.message_user(
                    request, f"Couldn't send: {user.email} doesn't look like a valid address.",
                    level=messages.ERROR,
                )
        return HttpResponseRedirect(reverse("admin:auth_user_change", args=[user_id]))

    @admin.display(description="Password setup")
    def invite_button(self, obj):
        if obj is None or obj.pk is None:
            return "(save the user first)"
        url = reverse("admin:auth_user_send_invite", args=[obj.pk])
        label = "Resend password setup email" if obj.has_usable_password() else "Send password setup email"
        return format_html(
            '<a class="button" href="{}">{}</a> '
            '<span style="color:#888;font-size:0.85em;">'
            "Sends the same link as “Forgot password?” on the login page."
            "</span>",
            url, label,
        )

    @admin.display(description="Password", boolean=True)
    def password_status(self, obj):
        return obj.has_usable_password()


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    # Read-only audit trail -- rows are written only by
    # library.email_backend.LoggingSMTPBackend, never by hand.
    list_display = ["sent_at", "to", "subject", "success"]
    list_filter = ["success"]
    search_fields = ["to", "subject", "body"]
    readonly_fields = ["to", "from_email", "subject", "body", "success", "error", "sent_at"]
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(PlayEvent)
class PlayEventAdmin(admin.ModelAdmin):
    """Read-only view of the airplay ledger. Rows are written by the
    engine at deck creation/removal; the admin must never mutate them
    (per PlayEvent's docstring -- historical integrity for statutory
    reporting)."""

    list_display = ("started_at", "category_kind", "track_artist",
                    "track_title", "isrc", "duration_played_seconds", "source")
    list_filter = ("category_kind", "source")
    search_fields = ("track_title", "track_artist", "isrc", "album_title")
    date_hierarchy = "started_at"
    ordering = ("-started_at",)

    readonly_fields = (
        "track", "track_title", "track_artist", "album_title", "record_label",
        "isrc", "category_kind", "source",
        "started_at", "ended_at", "duration_played_seconds",
    )
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RoyaltyReport)
class RoyaltyReportAdmin(admin.ModelAdmin):
    """Read-only listing of generated royalty reports. Reports are
    generated via /reports/ (or the CLI command); this admin exists so
    a staff user can also see them from the sidebar without visiting
    the frontend page. Downloads still go through the /reports/
    endpoint so the auth check is enforced on the file bytes."""

    list_display = ("period_start", "format", "total_plays", "unique_tracks",
                    "isrc_percent", "generated_at", "generated_by")
    list_filter = ("format",)
    ordering = ("-period_start", "-generated_at")
    readonly_fields = (
        "period_start", "period_end", "format",
        "generated_at", "generated_by",
        "total_plays", "unique_tracks", "unique_artists", "plays_with_isrc",
        "file",
    )
    fields = readonly_fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Reports can be regenerated from PlayEvent -- allowing delete
        # is safe (no info lost), and useful for tidying test rows.
        return True


@admin.register(FXCart)
class FXCartAdmin(admin.ModelAdmin):
    """One-shot cart definitions. Each row becomes a button on both the
    main dashboard and the remote-DJ console, ordered by sort_order.
    File-existence badge in the list view catches "file was deleted
    but the row is still here" mistakes at a glance.

    Change form is customized (admin/library/fxcart/change_form.html) to
    surface a drag-drop upload widget above the standard fields --
    dropped files land under /srv/isadoraair/carts/ via
    /api/fx/cart-upload/ and the JS fills in the filepath input. Hard-
    coded paths (typed into filepath directly) still work; the two
    input methods coexist and neither is destructive to the other."""

    change_form_template = "admin/library/fxcart/change_form.html"

    list_display = ["_badge", "name", "sort_order", "keyboard_shortcut",
                    "retrigger_mode", "gain_db", "enabled"]
    list_editable = ["sort_order", "enabled"]
    list_filter = ["enabled", "retrigger_mode"]
    search_fields = ["name", "filepath"]
    ordering = ["sort_order", "name"]
    fieldsets = [
        (None, {
            "fields": ["name", "filepath", "sort_order", "enabled"],
        }),
        ("Behavior", {
            "fields": ["retrigger_mode", "keyboard_shortcut", "gain_db"],
            "description": (
                "retrigger_mode picks what happens when the button is pressed while "
                "it's already playing (restart is the radio-convention default). "
                "keyboard_shortcut is optional; blank = click-only."
            ),
        }),
        ("Appearance", {
            "fields": ["idle_color", "playing_color"],
            "description": (
                "Button color when idle vs. the fill color that sweeps left-to-right "
                "across the button as the cart plays. RGBA -- pick any color, tune "
                "opacity for softer looks."
            ),
        }),
    ]

    @admin.display(description="")
    def _badge(self, obj):
        if not obj.filepath:
            return format_html('<span title="No file path" style="color:#e67;">&#9888;</span>')
        if not obj.file_exists:
            return format_html(
                '<span title="File not found at {}" style="color:#e67;">&#9888;</span>',
                obj.filepath,
            )
        return format_html('<span title="File OK" style="color:#6c6;">&#9679;</span>')

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # RGBAColorWidget on both color fields, same as UITheme/CategoryKind.
        if db_field.name in ("idle_color", "playing_color"):
            kwargs["widget"] = RGBAColorWidget
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(FXBusConfig)
class FXBusConfigAdmin(admin.ModelAdmin):
    """Singleton config for the FX sub-mixer. Volume + polyphony cap."""

    fieldsets = [
        (None, {
            "fields": ["volume_db", "polyphony_cap"],
            "description": (
                "FX bus output gain into the master mixer plus a cap on simultaneous "
                "fires. Volume takes effect on the next engine command tick (no "
                "restart). Polyphony cap requires an engine restart to re-provision "
                "the sub-mixer's slot count."
            ),
        }),
    ]

    def has_add_permission(self, request):
        return not FXBusConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        obj = FXBusConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_fxbusconfig_change", args=[obj.pk])
        )


@admin.register(VoiceTrack)
class VoiceTrackAdmin(admin.ModelAdmin):
    """VT rows -- read-mostly. Primary create/edit path is the browser
    recording + editor UI on /track/<pk>/ (and /voicetracks/ index).
    Admin visibility here is for one-off inspection and delete;
    admin-side add is allowed but the recorder UI is where the flow
    naturally lives."""

    list_display = ["_badge", "track", "position", "duration_seconds",
                    "recorded_by", "recorded_at", "source"]
    list_filter = ["position", "source"]
    search_fields = ["track__title", "track__artist__name", "filepath"]
    raw_id_fields = ["track"]
    readonly_fields = ["duration_seconds", "recorded_at", "edited_at"]
    ordering = ["-recorded_at"]

    @admin.display(description="")
    def _badge(self, obj):
        if not obj.filepath:
            return format_html('<span title="No file path" style="color:#e67;">&#9888;</span>')
        if not obj.file_exists:
            return format_html(
                '<span title="File not found at {}" style="color:#e67;">&#9888;</span>',
                obj.filepath,
            )
        return format_html('<span title="File OK" style="color:#6c6;">&#9679;</span>')


@admin.register(VoiceTrackConfig)
class VoiceTrackConfigAdmin(admin.ModelAdmin):
    """Singleton (Config > Voice Track Config). Applies to every VT
    airing; per-VT overrides can be added later if a specific song
    needs a different duck depth."""

    fieldsets = [
        (None, {
            "fields": ["program_duck_db", "duck_ramp_ms", "min_gap_ms"],
            "description": (
                "Duck depth is the dB attenuation applied to the deck-mixer "
                "(music) bus while a VT is playing. -6 dB is a comfortable "
                "default for hearing the VT over most master-limited mixes; "
                "increase (more negative) for a starker duck, 0 for no duck."
            ),
        }),
    ]

    def has_add_permission(self, request):
        return not VoiceTrackConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.http import HttpResponseRedirect
        from django.urls import reverse
        obj = VoiceTrackConfig.load()
        return HttpResponseRedirect(
            reverse("admin:library_voicetrackconfig_change", args=[obj.pk])
        )
