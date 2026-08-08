import json

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import RoadConditionsConfiguration, RoadConditionsSyncRun, RoadEvent


@admin.register(RoadConditionsConfiguration)
class RoadConditionsConfigurationAdmin(admin.ModelAdmin):
    """Singleton config admin -- same has_add/has_delete/changelist_view
    redirect pattern as WeatherConfigAdmin/AmberAlertConfigAdmin (see
    weather/admin.py)."""

    fieldsets = [
        ("Master Switch", {
            "fields": ["enabled"],
            "description": "Off by default. Nothing polls or writes RoadEvent rows until this is on.",
        }),
        ("On-Air Report Framing", {
            "fields": ["report_preamble", "report_postamble"],
            "description": "Optional station branding spoken immediately before/after the generated "
                            "road-condition report body (or the no-current-events message) -- see "
                            "generate_road_condition_audio. Either field may be left blank to omit that "
                            "piece entirely. Both support the literal token {announcer_name}, replaced "
                            "with the FULL on-air name (e.g. 'Claira Sky', not just 'Claira') of "
                            "whichever voice (day/night) is currently selected for this report. There "
                            "is no separate announcer-name setting here "
                            "on purpose -- that voice metadata (see the Weather Configuration voice "
                            "schedule) is already the single source of truth for the name.",
        }),
        ("Item Transition Sound", {
            "fields": ["transition_sound_enabled", "transition_sound_path"],
            "description": "Optional short sound effect (e.g. a chime or sting) inserted strictly "
                            "BETWEEN individual road-condition items when the report speaks more than "
                            "one -- never before the first item or after the last, and never possible "
                            "at all with 0 or 1 items. If the configured path is missing or unreadable "
                            "at generation time, the report is generated without the transition sound "
                            "rather than failing.",
        }),
        ("CARS API", {
            "fields": ["api_base_url", "request_timeout_seconds", "poll_cadence_minutes"],
        }),
        ("Coverage Area", {
            "fields": ["event_classifications", "counties", "routes", "min_priority"],
            "description": "There is no bounding-box/radius filter -- the live CARS API has none. "
                            "Counties and routes are matched against the exact strings KDOT's API returns. "
                            "Changing any of these only affects RoadEvent.in_scope on the NEXT complete sync -- "
                            "existing rows are re-scoped then, not retroactively deleted or hidden immediately.",
        }),
        ("Event Window", {
            "fields": ["max_event_age_days", "lookahead_days"],
        }),
        ("Diagnostics", {
            "fields": ["stale_data_threshold_minutes", "debug_logging", "status_display"],
        }),
    ]
    readonly_fields = ["status_display"]

    def has_add_permission(self, request):
        return not RoadConditionsConfiguration.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = RoadConditionsConfiguration.load()
        return HttpResponseRedirect(
            reverse("admin:road_conditions_roadconditionsconfiguration_change", args=[obj.pk])
        )

    @admin.display(description="Last fetch status")
    def status_display(self, obj):
        # Each line is built through format_html (which auto-escapes
        # its {} substitutions -- notably obj.last_error, which
        # reflects exception/HTTP-error text and should never be
        # trusted as literal HTML) and only the final join is marked
        # safe -- never build this string with raw f-string
        # interpolation directly into HTML.
        lines = []
        if obj.last_fetch_attempted_at:
            lines.append(format_html("Last attempt: {}", timezone.localtime(obj.last_fetch_attempted_at).strftime("%Y-%m-%d %H:%M:%S")))
        else:
            lines.append(format_html("Never attempted a fetch yet."))
        if obj.last_fetch_succeeded_at:
            lines.append(format_html("Last success: {}", timezone.localtime(obj.last_fetch_succeeded_at).strftime("%Y-%m-%d %H:%M:%S")))
        if obj.last_record_count is not None:
            lines.append(format_html("Last fetch record count: {}", obj.last_record_count))
        if obj.is_stale:
            lines.append(format_html('<strong style="color:#b45309">Data is stale (older than the configured threshold, or never succeeded).</strong>'))
        if obj.last_error:
            lines.append(format_html('<span style="color:#b91c1c">Last error: {}</span>', obj.last_error))
        return mark_safe("<br>".join(lines))


class CountyListFilter(admin.SimpleListFilter):
    title = "county"
    parameter_name = "county"

    def lookups(self, request, model_admin):
        counties = set()
        for row in RoadEvent.objects.values_list("counties", flat=True).iterator():
            counties.update(c for c in (row or []) if isinstance(c, str))
        return sorted((c, c) for c in counties)

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(counties__contains=[self.value()])
        return queryset


@admin.register(RoadEvent)
class RoadEventAdmin(admin.ModelAdmin):
    """Fully read-only audit trail -- every field here is written by
    sync_road_conditions (see services.sync_events), so nothing is
    editable, and there is no manual override action either (an
    earlier revision of this admin had a `mark_inactive` bulk action;
    removed -- there was no demonstrated operational need for it, and
    manually forcing `source_active`/`in_scope` off would just get
    silently reverted (or, worse, momentarily disagree with reality)
    the next time sync_road_conditions runs and sees the event again.
    If a real operational need shows up later -- e.g. an operator
    needs to suppress one bad/duplicate record between sync runs --
    add it back deliberately, with a clear "temporary, will be undone
    by the next sync" label in the UI and a test covering exactly
    that reversion, per this project's own review notes.).

    `is_current_display` surfaces the source_active/in_scope split
    (see RoadEvent's docstring) as one glance-able column/field rather
    than making every admin user learn to read two booleans together."""

    list_display = ["external_id", "headline_category", "headline_code",
                     "primary_route", "priority", "source_active", "in_scope", "start_time", "end_time", "last_seen_at"]
    list_filter = ["source_active", "in_scope", "headline_category", "priority", CountyListFilter]
    search_fields = ["external_id", "primary_route", "description", "headline_code", "counties"]
    ordering = ["-last_seen_at"]
    date_hierarchy = "last_seen_at"

    fieldsets = [
        ("Identity", {
            "fields": ["external_id", "is_current_display", "source_active", "in_scope", "deactivated_at"],
        }),
        ("Classification", {
            "fields": ["headline_category", "headline_code", "priority", "source_status"],
            "description": "This event's OWN taxonomy (from the source 'headline' object) -- "
                            "distinct from Road Conditions Configuration's event_classifications, "
                            "which is the separate API query-level filter.",
        }),
        ("Listener-Facing Text", {
            "fields": ["description"],
        }),
        ("Location", {
            "fields": ["primary_route", "primary_direction", "latitude", "longitude", "routes", "locations",
                       "counties", "districts", "geometry_display"],
            "description": "primary_route/primary_direction/latitude/longitude reflect only the FIRST "
                            "location on a multi-location event -- see routes/locations for the complete set.",
        }),
        ("Timing (source)", {
            "fields": ["start_time", "end_time", "source_update_time", "source_next_update_time",
                       "source_update_expiry_time", "update_number"],
        }),
        ("Local Tracking", {
            "fields": ["first_seen_at", "last_seen_at", "last_changed_at"],
        }),
        ("Raw Source Payload", {
            "fields": ["raw_payload_display", "payload_checksum"],
            "classes": ["collapse"],
            "description": "Sanitized for storage -- very long nested lists (e.g. a multi-hundred-point "
                            "turn-by-turn detour route) are truncated here. payload_checksum is computed "
                            "from the complete, unsanitized source record, so change detection is unaffected.",
        }),
    ]
    readonly_fields = [
        "external_id", "is_current_display", "source_active", "in_scope", "deactivated_at",
        "headline_category", "headline_code", "priority", "source_status", "description",
        "primary_route", "primary_direction", "latitude", "longitude", "routes", "locations",
        "counties", "districts", "geometry_display", "start_time", "end_time",
        "source_update_time", "source_next_update_time", "source_update_expiry_time", "update_number",
        "first_seen_at", "last_seen_at", "last_changed_at", "raw_payload_display", "payload_checksum",
    ]

    def has_add_permission(self, request):
        return False

    @admin.display(description="Currently relevant?", boolean=True)
    def is_current_display(self, obj):
        return obj.is_current

    @admin.display(description="Geometry")
    def geometry_display(self, obj):
        if not obj.geometry:
            return "(none)"
        return format_html("<code>{}</code>", obj.geometry.get("type", "?"))

    @admin.display(description="Raw payload")
    def raw_payload_display(self, obj):
        return format_html("<pre style='max-height:400px;overflow:auto'>{}</pre>", json.dumps(obj.raw_payload, indent=2, sort_keys=True))


@admin.register(RoadConditionsSyncRun)
class RoadConditionsSyncRunAdmin(admin.ModelAdmin):
    """Read-only run history -- see RoadConditionsConfiguration's
    status_display for the at-a-glance latest-attempt summary; this is
    the full audit trail behind it. A --dry-run never creates a row
    here at all (see services.sync_events) -- every row is a real run."""

    list_display = ["started_at", "outcome", "fetched_count", "relevant_count",
                     "created_count", "updated_count", "unchanged_count", "deactivated_count",
                     "error_count", "latency_ms"]
    list_filter = ["outcome"]
    ordering = ["-started_at"]
    date_hierarchy = "started_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
