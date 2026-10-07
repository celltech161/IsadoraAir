"""Read-only operator/developer diagnostics for ProductionMedia.

Immutable technical facts must never be editable here: no add, no change, no
delete, no bulk actions. The page exists to answer "what is this media, where
did it come from, is it valid, do its bytes still exist".

One operational sub-page hangs off it: Validation limits -- the validation
service's two admission limits, kept in .env through the shared managed-
settings mechanism (isadoraair/env_config.py, isadoraair/env_admin.py), like
the other .env-backed admin settings. Nothing about a ProductionMedia row.
"""
import os
import stat

from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from isadoraair import env_admin

from .models import ProductionMedia
from .services import admission, layout

VALIDATION_LIMIT_KEYS = ["PRODUCTION_VALIDATION_MAX_ACTIVE", "PRODUCTION_VALIDATION_MAX_PENDING"]


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

    # -- Validation limits (.env, via the shared managed-settings sub-page) --------
    change_list_template = "admin/production/productionmedia/change_list.html"

    def get_urls(self):
        return [
            path("validation-limits/", self.admin_site.admin_view(self.validation_limits_view),
                 name="production_productionmedia_validation_limits"),
            *super().get_urls(),
        ]

    def validation_limits_view(self, request):
        """GET: anyone who may view ProductionMedia. POST: superusers only --
        these limits protect the on-air host; ProductionMedia itself is never
        changeable here, so its change permission cannot be the gate."""
        if not self.has_view_permission(request):
            raise PermissionDenied
        here = reverse("admin:production_productionmedia_validation_limits")
        if request.method == "POST":
            if not request.user.is_superuser:
                raise PermissionDenied
            # Not stripped: the canonical parser accepts plain digits only.
            values = {key: request.POST.get(key.lower(), "") for key in VALIDATION_LIMIT_KEYS}
            env_admin.handle_env_subform_post(
                request, self.message_user, values,
                audit_title="Validation limits updated", audit_category="production",
                dedupe_key="production|validation-limits", restart_check_keys=VALIDATION_LIMIT_KEYS,
            )
            return HttpResponseRedirect(here)
        notices = [{
            "level": "info",
            "text": (
                f"These limits protect the on-air host from validation resource exhaustion. "
                f"Concurrent validations: default {admission.ACTIVE_DEFAULT}, allowed "
                f"{admission.ACTIVE_MIN}-{admission.ACTIVE_MAX}. Pending validation requests: default "
                f"{admission.PENDING_DEFAULT}, allowed {admission.PENDING_MIN}-{admission.PENDING_MAX}. "
                "Further uploads are answered 'busy' and can be validated again later. A saved change "
                "takes effect when isadoraair-validation is restarted; 'Running' below is what that "
                "service reports it is enforcing now. The per-run memory, task, CPU and time limits "
                "and the sandbox are not adjustable here."
            ),
        }]
        context = env_admin.env_subform_context(
            request, VALIDATION_LIMIT_KEYS, title="Validation limits",
            change_url=reverse("admin:production_productionmedia_changelist"),
            admin_site=self.admin_site, model=self.model, extra={"notices": notices},
        )
        return TemplateResponse(request, "admin/env_subform.html", context)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}
