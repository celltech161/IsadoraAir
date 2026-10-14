"""Staff-only engineering diagnostics for the active-message domain.

Messages, executions and events are READ-ONLY here: they change only through
active_messages.services. The policy's composition limits and ReferenceAudio
stinger key are editable, but the admin never writes the model: a valid form
is handed to ``services.set_limits`` / ``services.set_stinger_key``
(capability checks, one generation step, stale-airing suppression). The
models refuse every ordinary write regardless.
"""
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils.html import format_html

from production.models import ReferenceAudio

from . import _store, services
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


LIMIT_FIELDS = ("max_active_messages", "max_message_seconds", "max_aggregate_seconds")


class ActiveBroadcastPolicyAdminForm(forms.ModelForm):
    stinger_key = forms.ChoiceField(
        required=False,
        choices=(),
        help_text=("Optional ReferenceAudio registration played before urgent messages. "
                   "Blank means no stinger; filesystem paths are never accepted."),
    )

    class Meta:
        model = ActiveBroadcastPolicy
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        keys = list(ReferenceAudio.objects.filter(retired_at__isnull=True).order_by("key")
                    .values_list("key", flat=True).distinct())
        current = getattr(self.instance, "stinger_key", "")
        choices = [("", "No stinger")]
        choices.extend((key, key) for key in keys)
        # A registration can disappear after configuration. Keep that value
        # visible so it can be cleared, without presenting it as healthy.
        if current and current not in keys:
            choices.append((current, f"{current} (currently unavailable)"))
        self.fields["stinger_key"].choices = choices

    def clean_stinger_key(self):
        key = self.cleaned_data["stinger_key"]
        try:
            services.validate_stinger_key(key)
        except services.InvalidRequest as exc:
            raise forms.ValidationError(str(exc), code=exc.code) from None
        return key


@admin.register(ActiveBroadcastPolicy)
class ActiveBroadcastPolicyAdmin(admin.ModelAdmin):
    """Limits and stinger are editable through their domain services;
    everything else on the policy is read-only diagnostics."""

    actions = None
    form = ActiveBroadcastPolicyAdminForm
    list_display = ("id", "generation", "native_airing_enabled", "max_active_messages", "max_message_seconds",
                    "max_aggregate_seconds", "default_replay_interval_seconds", "stinger_key", "updated_at",
                    "updated_by_username")
    fields = LIMIT_FIELDS + ("generation", "native_airing_enabled", "default_replay_interval_seconds", "stinger_key",
                             "updated_at", "updated_by_username")
    readonly_fields = ("generation", "native_airing_enabled", "default_replay_interval_seconds",
                       "updated_at", "updated_by_username")

    def changelist_view(self, request, extra_context=None):
        _store.ensure_policy()                    # the singleton exists from first sight, with its defaults
        return super().changelist_view(request, extra_context)

    def has_add_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and services.can(request.user, services.CONTROL)

    def save_model(self, request, obj, form, change):
        changes = {name: form.cleaned_data[name] for name in LIMIT_FIELDS if name in form.changed_data}
        stinger_changed = "stinger_key" in form.changed_data
        if not changes and not stinger_changed:
            return
        try:
            # The admin never saves ``obj``. Each changed value reaches its
            # sole domain service, and a combined submission is atomic.
            with transaction.atomic():
                if changes:
                    services.set_limits(actor=request.user, **changes)
                if stinger_changed:
                    services.set_stinger_key(form.cleaned_data["stinger_key"], actor=request.user)
        except services.NotPermitted as exc:
            raise PermissionDenied(str(exc)) from None
        except services.InvalidRequest as exc:
            # The form already validated ceilings and the cross-field rule (the
            # model's validators and constraints); only a concurrent change gets
            # here. Nothing was written.
            self.message_user(request, f"Policy not changed: {exc}", level=messages.ERROR)


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
