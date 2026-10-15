"""Station configuration of produced-content types and producer assignments.

Ordinary model admin: the form runs ``ProducedContentType.clean()`` (field
combinations per delivery mode, the durable key, the frozen delivery mode of
a type active messages, a spoken backend or test takes reference); the
database CHECK constraints refuse the same combinations for any other writer.

D3C: a regular type is the configuration authority of its managed spoken
backend -- saving it synchronizes the backend (produced_content.backend); the
backend link and its health are shown read-only.
"""
from django.contrib import admin, messages
from django.urls import reverse
from django.utils.html import format_html

from . import backend
from .models import ProducedContentTestTake, ProducedContentType, ProducedContentTypeAssignment


class AssignmentInline(admin.TabularInline):
    model = ProducedContentTypeAssignment
    extra = 0
    autocomplete_fields = ["user"]
    readonly_fields = ["created_at"]


@admin.register(ProducedContentType)
class ProducedContentTypeAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "delivery_mode", "enabled", "target_category", "output_mode", "recipe_key",
                    "backend_health", "producer_self_publish", "recallable", "replay_interval_seconds",
                    "assignment_count", "sort_order"]
    list_filter = ["delivery_mode", "enabled"]
    search_fields = ["key", "name", "description"]
    ordering = ["sort_order", "name", "key"]
    autocomplete_fields = ["target_category"]
    inlines = [AssignmentInline]
    readonly_fields = ["created_at", "updated_at", "spoken_backend", "backend_health"]
    fieldsets = [
        (None, {"fields": ["key", "name", "description", "enabled", "sort_order", "delivery_mode"]}),
        ("Regular library content", {
            "fields": ["target_category", "output_mode", "recipe_key", "artist_tag", "spoken_backend",
                       "backend_health"],
            "description": "Only for the regular delivery mode. A producer's submission is processed and delivered "
                           "automatically by the station's spoken-content pipeline into the target category "
                           "(single: the new delivery replaces the current edition; accumulating: each delivery "
                           "adds an item). Recipe: one of " + ", ".join(backend.D3C_RECIPES) + " (required to "
                           "produce). The linked spoken backend is managed from here; never edit it directly.",
        }),
        ("Public address / urgent", {
            "fields": ["producer_self_publish", "recallable", "replay_interval_seconds"],
            "description": "Only for public-address and urgent types. Leave blank for the defaults "
                           "(self-publish: yes; producer recall: yes; replay: the active-message policy default).",
        }),
        ("History", {"fields": ["created_at", "updated_at"]}),
    ]

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        if obj is not None:
            fields.append("key")                      # the durable identity
        return fields

    @admin.display(description="Producers")
    def assignment_count(self, obj):
        return obj.assignments.count()

    @admin.display(description="Spoken backend")
    def spoken_backend(self, obj):
        backing = obj.spoken_content_type if obj is not None and obj.pk else None
        if backing is None:
            return "-"
        url = reverse("admin:spoken_spokencontenttype_change", args=[backing.pk])
        return format_html('<a href="{}">{}</a>', url, backing.code)

    @admin.display(description="Backend")
    def backend_health(self, obj):
        if obj is None or obj.pk is None:
            return "-"
        verdict = backend.health(obj)
        return verdict.text if verdict.ok else format_html("<strong>{}</strong>", verdict.text)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        verdict = backend.synchronize(obj)
        if not verdict.ok:
            messages.warning(request, f"{obj.name}: {verdict.text}. Producers cannot submit until this is fixed.")

    def has_delete_permission(self, request, obj=None):
        # Types are history (active messages and their airings reference them): disable instead.
        return False


@admin.register(ProducedContentTypeAssignment)
class ProducedContentTypeAssignmentAdmin(admin.ModelAdmin):
    list_display = ["user", "content_type", "delivery_mode", "created_at"]
    list_filter = ["content_type__delivery_mode", "content_type"]
    search_fields = ["user__username", "content_type__key", "content_type__name"]
    autocomplete_fields = ["user", "content_type"]
    readonly_fields = ["created_at"]

    @admin.display(description="Delivery mode", ordering="content_type__delivery_mode")
    def delivery_mode(self, obj):
        return obj.content_type.get_delivery_mode_display()


@admin.register(ProducedContentTestTake)
class ProducedContentTestTakeAdmin(admin.ModelAdmin):
    """Read-only: temporary TEST / NO-AIR bindings (never airable)."""
    list_display = ["content_type", "user", "revision", "updated_at", "expires_at"]
    list_filter = ["content_type"]
    readonly_fields = ["user", "content_type", "media", "revision", "created_at", "updated_at", "expires_at"]

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
