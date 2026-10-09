"""Staff-only engineering diagnostics for the spoken domain.

Content types are configurable here (validated on every save; the code is
read-only once used). Items, events, assets and editions are READ-ONLY:
lifecycle changes go through spoken.transitions, never through admin.
"""
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from .models import SpokenAsset, SpokenAssetEdition, SpokenContentType, SpokenItem, SpokenItemEvent


def _media_link(media_id):
    if not media_id:
        return "-"
    url = reverse("admin:production_productionmedia_change", args=[media_id])
    return format_html('<a href="{}">{}</a>', url, media_id)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(SpokenContentType)
class SpokenContentTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "enabled", "delivery_mode", "recipe_key", "target_category", "requires_review")
    list_filter = ("enabled", "delivery_mode", "recipe_key")
    search_fields = ("name", "code")

    def get_readonly_fields(self, request, obj=None):
        fields = ["created_at", "updated_at"]
        if obj is not None and obj.items.exists():
            fields.append("code")
        return fields

    def has_delete_permission(self, request, obj=None):
        return obj is None or not obj.items.exists()


class EventInline(admin.TabularInline):
    model = SpokenItemEvent
    extra = 0
    can_delete = False
    fields = ("created_at", "event", "from_state", "to_state", "actor_kind", "actor_username", "media", "detail")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SpokenItem)
class SpokenItemAdmin(ReadOnlyAdmin):
    list_display = ("id", "content_type", "state", "origin", "submitted_by_username", "submitted_at", "failure_code")
    list_filter = ("state", "origin", "content_type")
    search_fields = ("id", "title", "submitted_by_username", "import_external_id")
    readonly_fields = ("submission_link", "rendition_link", "rendition_recipe")
    inlines = [EventInline]

    @admin.display(description="submission media")
    def submission_link(self, obj):
        return _media_link(obj.submission_media_id)

    @admin.display(description="rendition media")
    def rendition_link(self, obj):
        return _media_link(obj.rendition_media_id)

    @admin.display(description="rendition recipe")
    def rendition_recipe(self, obj):
        media = obj.rendition_media
        if media is None:
            return "-"
        return f"{media.recipe_key} v{media.recipe_version} (params {media.recipe_params_digest[:12]}…; {media.toolchain})"


@admin.register(SpokenItemEvent)
class SpokenItemEventAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "item", "event", "from_state", "to_state", "actor_kind", "actor_username")
    list_filter = ("event", "actor_kind")


class EditionInline(admin.TabularInline):
    model = SpokenAssetEdition
    fk_name = "asset"
    extra = 0
    can_delete = False
    fields = ("generation", "state", "item", "rendition", "library_sha256", "promoted_at", "superseded_at")
    readonly_fields = fields

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SpokenAsset)
class SpokenAssetAdmin(ReadOnlyAdmin):
    list_display = ("id", "content_type", "mode", "slot_key", "state", "generation", "track", "library_relpath")
    list_filter = ("mode", "state", "content_type")
    inlines = [EditionInline]


@admin.register(SpokenAssetEdition)
class SpokenAssetEditionAdmin(ReadOnlyAdmin):
    list_display = ("id", "asset", "generation", "state", "item", "rendition", "promoted_at")
    list_filter = ("state",)
