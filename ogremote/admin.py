from django.contrib import admin
from django.http import HttpResponseRedirect
from django.urls import reverse

from .models import OGRemoteCategory, OGRemoteConfig


@admin.register(OGRemoteConfig)
class OGRemoteConfigAdmin(admin.ModelAdmin):
    fieldsets = [
        ("Master Switch", {"fields": ["enabled"]}),
        ("Urgent PA / Public Address", {"fields": ["urgent_pa_replay_interval_minutes"]}),
        ("Notifications", {"fields": ["notify_email"]}),
    ]

    def has_add_permission(self, request):
        return not OGRemoteConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = OGRemoteConfig.load()
        return HttpResponseRedirect(
            reverse("admin:ogremote_ogremoteconfig_change", args=[obj.pk])
        )


@admin.register(OGRemoteCategory)
class OGRemoteCategoryAdmin(admin.ModelAdmin):
    list_display = ["category_key", "name", "target_category", "output_mode", "recallable", "enabled"]
    list_filter = ["output_mode", "recallable", "enabled"]
    autocomplete_fields = ["target_category"]
    fieldsets = [
        (None, {"fields": ["category_key", "name", "enabled"]}),
        ("Delivery", {"fields": ["target_category", "output_mode", "artist_tag"]}),
        ("Recall", {"fields": ["recallable"]}),
    ]
