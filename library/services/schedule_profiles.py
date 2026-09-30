"""Schedule profile lifecycle policy for roadmap 3.1B.

Views translate these operations into HTTP.  Keeping the state transitions
here makes the active/default/archive/delete invariants usable from any future
operator surface without duplicating policy.
"""
from __future__ import annotations

import uuid as uuid_module

from django.db import IntegrityError, transaction
from django.db.models import Max

from library.models import (
    PlaylistLog,
    ScheduleBlock,
    ScheduleProfile,
    ScheduleProfileState,
)
from monitoring.models import emit_event


class ProfileLifecycleError(Exception):
    status = 400

    def __init__(self, message, *, blockers=None, current_active_uuid=None):
        super().__init__(message)
        self.message = message
        self.blockers = blockers or []
        self.current_active_uuid = current_active_uuid


class ProfileConflict(ProfileLifecycleError):
    status = 409


class StaleActivation(ProfileConflict):
    pass


def _actor_name(actor):
    if actor is None:
        return "unknown"
    return getattr(actor, "username", None) or str(actor)


def _audit(action, actor, detail):
    """Emit one privacy-safe lifecycle event per successful mutation."""
    emit_event(
        category="schedule",
        title=f"Schedule profile {action}",
        detail={"actor": _actor_name(actor), **detail},
        dedupe_key=f"schedule-profile|{action}|{uuid_module.uuid4()}",
    )


def _clean_name(name):
    value = str(name or "").strip()
    if not value:
        raise ProfileLifecycleError("name is required")
    if len(value) > ScheduleProfile._meta.get_field("name").max_length:
        raise ProfileLifecycleError("name is too long")
    return value


def _next_sort_order():
    current = ScheduleProfile.objects.aggregate(value=Max("sort_order"))["value"]
    return (current or 0) + 10


def create_profile(*, name, description="", sort_order=None, actor=None):
    values = {
        "name": _clean_name(name),
        "description": str(description or ""),
        "sort_order": _next_sort_order() if sort_order is None else sort_order,
    }
    try:
        with transaction.atomic():
            # Validate/recover the authoritative singleton before adding a
            # second profile.  Otherwise a missing state row with one profile
            # could be turned into an unrecoverably ambiguous state by Create.
            ScheduleProfileState.load()
            profile = ScheduleProfile.objects.create(**values)
            _audit("created", actor, {"profile_uuid": str(profile.uuid), "name": profile.name})
    except IntegrityError as exc:
        if ScheduleProfile.objects.filter(name=values["name"]).exists():
            raise ProfileLifecycleError("A schedule profile with that name already exists.") from exc
        raise
    return profile


@transaction.atomic
def edit_profile(profile, *, changes, actor=None):
    profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
    if profile.is_archived:
        raise ProfileConflict("Archived profiles are read-only; restore this profile before editing it.")
    if "uuid" in changes:
        raise ProfileLifecycleError("uuid is immutable")
    allowed = {"name", "description", "sort_order"}
    unknown = set(changes) - allowed
    if unknown:
        raise ProfileLifecycleError(f"Unsupported fields: {', '.join(sorted(unknown))}")
    before = {field: getattr(profile, field) for field in allowed}
    if "name" in changes:
        profile.name = _clean_name(changes["name"])
    if "description" in changes:
        profile.description = str(changes["description"] or "")
    if "sort_order" in changes:
        try:
            profile.sort_order = int(changes["sort_order"])
        except (TypeError, ValueError) as exc:
            raise ProfileLifecycleError("sort_order must be an integer") from exc
    changed = {field: {"old": before[field], "new": getattr(profile, field)} for field in allowed if before[field] != getattr(profile, field)}
    if not changed:
        return profile
    try:
        with transaction.atomic():
            profile.save(update_fields=[*changed, "updated_at"])
            _audit("edited", actor, {"profile_uuid": str(profile.uuid), "changes": changed})
    except IntegrityError as exc:
        if ScheduleProfile.objects.exclude(pk=profile.pk).filter(name=profile.name).exists():
            raise ProfileLifecycleError("A schedule profile with that name already exists.") from exc
        raise
    return profile


def clone_profile(source, *, name, description=None, sort_order=None, include_date_overrides=False, actor=None):
    clone_name = _clean_name(name)
    try:
        with transaction.atomic():
            clone = ScheduleProfile.objects.create(
                name=clone_name,
                description=source.description if description is None else str(description or ""),
                sort_order=_next_sort_order() if sort_order is None else int(sort_order),
            )
            source_rows = ScheduleBlock.objects.filter(profile=source)
            if not include_date_overrides:
                source_rows = source_rows.filter(specific_date__isnull=True)
            rows = list(source_rows.order_by("pk"))
            ScheduleBlock.objects.bulk_create([
                ScheduleBlock(
                    profile=clone,
                    day_of_week=row.day_of_week,
                    specific_date=row.specific_date,
                    start_time=row.start_time,
                    end_time=row.end_time,
                    rotation_id=row.rotation_id,
                    playlist_id=row.playlist_id,
                )
                for row in rows
            ])
            recurring_count = sum(row.day_of_week is not None for row in rows)
            date_count = len(rows) - recurring_count
            _audit("cloned", actor, {
                "source_profile_uuid": str(source.uuid),
                "new_profile_uuid": str(clone.uuid),
                "include_date_overrides": bool(include_date_overrides),
                "recurring_blocks_copied": recurring_count,
                "date_blocks_copied": date_count,
            })
    except IntegrityError as exc:
        if ScheduleProfile.objects.filter(name=clone_name).exists():
            raise ProfileLifecycleError("A schedule profile with that name already exists.") from exc
        raise
    return clone


def _locked_state():
    # Preserve ScheduleProfileState.load()'s hardened recovery/fail-closed
    # behavior, then lock the authoritative singleton in this transaction.
    ScheduleProfileState.load()
    return ScheduleProfileState.objects.select_for_update().select_related(
        "active_profile", "default_profile"
    ).get(pk=1)


@transaction.atomic
def activate_profile(profile, *, expected_active_profile_uuid, actor=None):
    if not expected_active_profile_uuid:
        raise ProfileLifecycleError("expected_active_profile_uuid is required")
    try:
        expected_active_profile_uuid = str(uuid_module.UUID(str(expected_active_profile_uuid)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ProfileLifecycleError("expected_active_profile_uuid must be a valid UUID") from exc
    state = _locked_state()
    profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
    if profile.is_archived:
        raise ProfileConflict("Archived profiles cannot be activated.")
    if str(state.active_profile.uuid) != expected_active_profile_uuid:
        raise StaleActivation(
            "The active profile changed after this page was loaded.",
            current_active_uuid=str(state.active_profile.uuid),
        )
    previous = state.active_profile
    if previous.pk == profile.pk:
        return state, False
    state.active_profile = profile
    state.save(update_fields=["active_profile", "updated_at"])
    _audit("activated", actor, {
        "previous_active_uuid": str(previous.uuid), "previous_active_name": previous.name,
        "new_active_uuid": str(profile.uuid), "new_active_name": profile.name,
    })
    return state, True


@transaction.atomic
def set_default_profile(profile, *, actor=None):
    state = _locked_state()
    profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
    if profile.is_archived:
        raise ProfileConflict("Archived profiles cannot be set as default.")
    previous = state.default_profile
    if previous.pk == profile.pk:
        return state, False
    state.default_profile = profile
    state.save(update_fields=["default_profile", "updated_at"])
    _audit("set as default", actor, {
        "previous_default_uuid": str(previous.uuid), "previous_default_name": previous.name,
        "new_default_uuid": str(profile.uuid), "new_default_name": profile.name,
    })
    return state, True


@transaction.atomic
def archive_profile(profile, *, actor=None):
    state = _locked_state()
    profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
    blockers = []
    if state.active_profile_id == profile.pk:
        blockers.append("profile is active")
    if state.default_profile_id == profile.pk:
        blockers.append("profile is default")
    if blockers:
        raise ProfileConflict("This profile cannot be archived.", blockers=blockers)
    if profile.is_archived:
        return profile, False
    profile.is_archived = True
    profile.save(update_fields=["is_archived", "updated_at"])
    _audit("archived", actor, {"profile_uuid": str(profile.uuid), "name": profile.name})
    return profile, True


@transaction.atomic
def restore_profile(profile, *, actor=None):
    profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
    if not profile.is_archived:
        return profile, False
    profile.is_archived = False
    profile.save(update_fields=["is_archived", "updated_at"])
    _audit("restored", actor, {"profile_uuid": str(profile.uuid), "name": profile.name})
    return profile, True


@transaction.atomic
def delete_profile(profile, *, actor=None):
    state = _locked_state()
    profile = ScheduleProfile.objects.select_for_update().get(pk=profile.pk)
    blockers = []
    if state.active_profile_id == profile.pk:
        blockers.append("profile is active")
    if state.default_profile_id == profile.pk:
        blockers.append("profile is default")
    block_count = ScheduleBlock.objects.filter(profile=profile).count()
    log_count = PlaylistLog.objects.filter(schedule_profile=profile).count()
    if block_count:
        blockers.append(f"profile has {block_count} schedule block(s)")
    if log_count:
        blockers.append(f"profile is referenced by {log_count} playlist log(s)")
    if blockers:
        raise ProfileConflict("This profile cannot be deleted.", blockers=blockers)
    identity = {"profile_uuid": str(profile.uuid), "name": profile.name}
    _audit("deleted", actor, identity)
    profile.delete()
    return identity
