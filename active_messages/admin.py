"""Staff-only, READ-ONLY engineering diagnostics for the active-message domain.

Nothing here changes state: lifecycle, policy and executions change only
through active_messages.services (the D3 operator surface calls them with
capability checks). The models refuse every ordinary write regardless.
"""
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import ActiveBroadcastExecution, ActiveBroadcastMessage, ActiveBroadcastMessageEvent, ActiveBroadcastPolicy


def _media_link(media_id):
    if not media_id:
        return "-"
    url = reverse("admin:production_productionmedia_change", args=[media_id])
    return format_html('<a href="{}">{}</a>', url, media_id)


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class EventInline(admin.TabularInline):
    model = ActiveBroadcastMessageEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "event", "from_state", "to_state", "generation", "actor_kind", "actor_username", "detail")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class MessageEventInline(EventInline):
    fk_name = "message"


class ExecutionEventInline(EventInline):
    fk_name = "execution"


@admin.register(ActiveBroadcastPolicy)
class ActiveBroadcastPolicyAdmin(ReadOnlyAdmin):
    list_display = ("id", "generation", "native_airing_enabled", "default_replay_interval_seconds", "stinger_key",
                    "updated_at", "updated_by_username")


@admin.register(ActiveBroadcastMessage)
class ActiveBroadcastMessageAdmin(ReadOnlyAdmin):
    list_display = ("id", "kind", "state", "producer_username", "title", "activated_at", "last_completed_at",
                    "next_due_at", "expires_at")
    list_filter = ("state", "kind")
    search_fields = ("id", "producer_username", "producer_key", "title")
    readonly_fields = ("media",)
    inlines = [MessageEventInline]

    @admin.display(description="message media")
    def media(self, obj):
        return _media_link(obj.message_media_id)


@admin.register(ActiveBroadcastExecution)
class ActiveBroadcastExecutionAdmin(ReadOnlyAdmin):
    list_display = ("id", "reason", "state", "policy_generation", "component_count", "due_at", "started_at",
                    "ended_at", "failure_code")
    list_filter = ("state", "reason")
    search_fields = ("id", "composition_digest")
    readonly_fields = ("mix",)
    inlines = [ExecutionEventInline]

    @admin.display(description="aggregate media")
    def mix(self, obj):
        return _media_link(obj.mix_media_id)


@admin.register(ActiveBroadcastMessageEvent)
class ActiveBroadcastMessageEventAdmin(ReadOnlyAdmin):
    list_display = ("id", "created_at", "event", "message", "execution", "from_state", "to_state", "generation",
                    "actor_kind", "actor_username")
    list_filter = ("event", "actor_kind")
