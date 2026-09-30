"""3.1A stage 2 of 3 -- bounded data backfill (the only RunPython).

Creates the initial "Default Schedule" profile, assigns EVERY existing
ScheduleBlock (recurring and specific-date alike) to it, and points the
singleton ScheduleProfileState's active and default profile at it, so an
operator who does nothing sees unchanged schedule resolution.

Why RunPython: the assignment is a data fact (which profile the existing
rows belong to) that no schema operation can express. Update Center
classifies RunPython as manual, so this release requires migration-plan
review.

Safety:
* Before changing anything it verifies the existing rows can satisfy the
  per-profile uniqueness that 0088 enforces. If duplicate (day_of_week,
  start_time) or (specific_date, start_time) rows exist it aborts with the
  exact conflicting row ids and touches nothing -- it never deletes or merges
  schedule data. The migration is atomic, so an abort leaves the database
  exactly as it was.
* Schedule contents (times, rotations, playlists) are not read or rewritten.
* PlaylistLog rows are deliberately NOT touched: their schedule_profile
  stays NULL (legacy/unknown provenance) rather than manufacturing history.
* Idempotent: only blocks with no profile are assigned, and an existing
  state row's non-null pointers are preserved.
"""
from django.db import migrations
from django.db.models import Count

INITIAL_PROFILE_NAME = "Default Schedule"


def _conflicts(model, key_fields, kind):
    duplicated = (
        model.objects.filter(**{f"{key_fields[0]}__isnull": False})
        .values(*key_fields).annotate(n=Count("pk")).filter(n__gt=1)
    )
    found = []
    for group in duplicated:
        ids = sorted(model.objects.filter(**{k: group[k] for k in key_fields}).values_list("pk", flat=True))
        found.append(f"{kind} {', '.join(f'{k}={group[k]}' for k in key_fields)}: ScheduleBlock ids {ids}")
    return found


def backfill_default_profile(apps, schema_editor):
    ScheduleBlock = apps.get_model("library", "ScheduleBlock")
    ScheduleProfile = apps.get_model("library", "ScheduleProfile")
    ScheduleProfileState = apps.get_model("library", "ScheduleProfileState")

    conflicts = (
        _conflicts(ScheduleBlock, ("day_of_week", "start_time"), "recurring")
        + _conflicts(ScheduleBlock, ("specific_date", "start_time"), "specific-date")
    )
    if conflicts:
        raise RuntimeError(
            "Cannot create schedule profiles: existing ScheduleBlock rows are ambiguous and "
            "would violate per-profile uniqueness. Resolve these duplicates manually, then "
            "re-run the migration (nothing was changed): " + "; ".join(conflicts)
        )

    profile, _ = ScheduleProfile.objects.get_or_create(name=INITIAL_PROFILE_NAME)
    ScheduleBlock.objects.filter(profile__isnull=True).update(profile=profile)

    state, created = ScheduleProfileState.objects.get_or_create(
        pk=1, defaults={"active_profile": profile, "default_profile": profile},
    )
    if not created:
        changed = False
        if state.active_profile_id is None:
            state.active_profile = profile
            changed = True
        if state.default_profile_id is None:
            state.default_profile = profile
            changed = True
        if changed:
            state.save()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0086_scheduleprofile_foundation_schema"),
    ]

    operations = [
        # Reverse is a no-op: 0086's reverse drops the new columns/tables
        # wholesale, so there is nothing to undo separately.
        migrations.RunPython(backfill_default_profile, migrations.RunPython.noop),
    ]
