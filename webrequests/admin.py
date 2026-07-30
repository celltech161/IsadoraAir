from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import SongRequest, WebRequestConfig


@admin.register(WebRequestConfig)
class WebRequestConfigAdmin(admin.ModelAdmin):
    """The availability grid itself (open_slots) is edited on the
    dedicated /web-request/ staff page, not here -- a 168-integer array
    has no reasonable plain-form widget. Everything else (master
    switch, rate/timeout knobs) is a normal admin form."""

    fieldsets = [
        ("Master Switch", {
            "fields": ["enabled"],
            "description": "Off by default. Nothing syncs, polls, or fulfills until this is on. "
                            "The availability grid itself is edited at /web-request/, not here.",
        }),
        ("Rate & Timing", {
            "fields": ["max_fulfilled_per_hour", "lookahead_warning_minutes", "expire_after_hours"],
        }),
        ("Notifications", {
            "fields": ["notify_email"],
        }),
    ]

    def has_add_permission(self, request):
        return not WebRequestConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = WebRequestConfig.load()
        return HttpResponseRedirect(
            reverse("admin:webrequests_webrequestconfig_change", args=[obj.pk])
        )


@admin.register(SongRequest)
class SongRequestAdmin(admin.ModelAdmin):
    """Mostly a read-only audit trail -- rows are created by the
    requests-poller script and mutated by the engine's own fulfillment
    logic. status is left editable for manual troubleshooting (e.g.
    force-expiring a stuck row); everything else that pins the request
    to a specific track/log_item is readonly so an admin edit can't
    desync the pairing IsadoraAir and the public site both think they
    agree on."""

    list_display = ["status", "requester_name", "track", "submitted_at", "fulfilled_at"]
    list_filter = ["status"]
    search_fields = ["requester_name", "external_request_id", "track__title", "track__artist__name"]
    ordering = ["-submitted_at"]

    readonly_fields = [
        "external_request_id", "track", "requester_name", "dedication_message",
        "submitted_at", "fetched_at", "fulfilled_at", "resolved_at",
        "estimated_play_time", "log_item",
    ]

    def has_add_permission(self, request):
        return False
