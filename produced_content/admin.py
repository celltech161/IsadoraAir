"""Station configuration of produced-content types and producer assignments.

Ordinary model admin: the form runs ``ProducedContentType.clean()`` (field
combinations per delivery mode, the durable key, the frozen delivery mode of
a type active messages reference); the database CHECK constraints refuse the
same combinations for any other writer.
"""
from django.contrib import admin

from .models import ProducedContentType, ProducedContentTypeAssignment


class AssignmentInline(admin.TabularInline):
    model = ProducedContentTypeAssignment
    extra = 0
    autocomplete_fields = ["user"]
    readonly_fields = ["created_at"]


@admin.register(ProducedContentType)
class ProducedContentTypeAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "delivery_mode", "enabled", "target_category", "output_mode",
                    "producer_self_publish", "recallable", "replay_interval_seconds", "assignment_count", "sort_order"]
    list_filter = ["delivery_mode", "enabled"]
    search_fields = ["key", "name", "description"]
    ordering = ["sort_order", "name", "key"]
    autocomplete_fields = ["target_category"]
    inlines = [AssignmentInline]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = [
        (None, {"fields": ["key", "name", "description", "enabled", "sort_order", "delivery_mode"]}),
        ("Regular library content", {
            "fields": ["target_category", "output_mode", "recipe_key", "artist_tag"],
            "description": "Only for the regular delivery mode. Configuration only; processing arrives later.",
        }),
        ("Public address / urgent", {
            "fields": ["producer_self_publish", "recallable", "replay_interval_seconds"],
            "description": "Only for public-address and urgent types. Leave blank for the defaults "
                           "(self-publish: yes; recall: no; replay: the active-message policy default).",
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
