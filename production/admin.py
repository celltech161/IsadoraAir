"""Read-only operator/developer diagnostics for ProductionMedia.

Immutable technical facts must never be editable here: no add, no change, no
delete, no bulk actions. The page exists to answer "what is this media, where
did it come from, is it valid, do its bytes still exist".
"""
import os
import stat

from django.contrib import admin

from .models import ProductionMedia
from .services import layout


@admin.register(ProductionMedia)
class ProductionMediaAdmin(admin.ModelAdmin):
    list_display = (
        "id", "kind", "owner_username", "validation_state", "validation_code", "retention_state",
        "container", "codec", "decoded_duration_seconds", "byte_size", "created_at",
    )
    list_filter = ("kind", "validation_state", "retention_state", "container", "codec")
    search_fields = ("id", "sha256", "owner_username", "original_filename", "storage_key")
    ordering = ("-created_at",)
    list_select_related = ("owner",)
    date_hierarchy = "created_at"

    fieldsets = (
        ("Identity and custody", {"fields": ("id", "kind", "owner", "owner_username", "created_at")}),
        ("Storage", {"fields": ("storage_key", "storage_status", "sha256", "byte_size")}),
        ("Technical facts", {"fields": (
            "container", "codec", "sample_rate", "channels",
            "decoded_duration_seconds", "header_duration_seconds", "probe",
        )}),
        ("Untrusted display metadata", {"fields": ("original_filename", "declared_content_type")}),
        ("Validation", {"fields": ("validation_state", "validation_code", "validated_at")}),
        ("Derivation", {"fields": (
            "derived_from", "recipe_key", "recipe_version", "recipe_params_digest", "toolchain",
        )}),
        ("Retention", {"fields": ("retention_state", "purged_at")}),
    )
    readonly_fields = tuple(
        name for name in (
            "id", "kind", "owner", "owner_username", "created_at", "storage_key", "storage_status",
            "sha256", "byte_size", "container", "codec", "sample_rate", "channels",
            "decoded_duration_seconds", "header_duration_seconds", "probe", "original_filename",
            "declared_content_type", "validation_state", "validation_code", "validated_at",
            "derived_from", "recipe_key", "recipe_version", "recipe_params_digest", "toolchain",
            "retention_state", "purged_at",
        )
    )

    @admin.display(description="Bytes on disk")
    def storage_status(self, obj):
        """stat() only -- never opens, hashes or alters anything."""
        if obj is None or not obj.pk:
            return "-"
        if not obj.is_present:
            return "purged (no bytes expected)"
        try:
            info = os.lstat(layout.resolve_storage_path(obj.storage_key))
        except FileNotFoundError:
            return "MISSING"
        except Exception:
            return "unreadable"
        if not stat.S_ISREG(info.st_mode):
            return "NOT A REGULAR FILE"
        if info.st_size != obj.byte_size:
            return f"SIZE MISMATCH ({info.st_size} on disk, {obj.byte_size} recorded)"
        return "present, size matches"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}
