"""Extend the remote_dj GroupAccess row so members can BROWSE the
whole library and read individual track detail pages, but not
mutate anything. Grants:

  /library/         -- full library index page + subpages
  /track/           -- track detail pages
  /api/tracks/      -- track list + per-track GET (writes still 403'd
                       view-side by api_track_detail's
                       user_is_library_read_only gate)
  /api/categories/  -- populates the library page's category filter

Also drops the pre-existing `/api/tracks/` EXACT-match entry -- it's
now covered by the wider prefix, so leaving both would just be
noise.

Idempotent: reads the current prefix list, appends only what's
missing. An admin who's already customized the row (added extra
prefixes, changed priority, etc.) keeps everything else."""
from django.db import migrations


NEW_PREFIXES = ("/library/", "/track/", "/api/tracks/", "/api/categories/")


def add_library_view(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")

    try:
        dj = Group.objects.get(name="remote_dj")
    except Group.DoesNotExist:
        return  # Fresh install where the seed migration hasn't run yet
    try:
        ga = GroupAccess.objects.get(group=dj)
    except GroupAccess.DoesNotExist:
        return

    existing_prefixes = [
        line.strip()
        for line in ga.allowed_prefixes.splitlines()
        if line.strip()
    ]
    added = False
    for p in NEW_PREFIXES:
        if p not in existing_prefixes:
            existing_prefixes.append(p)
            added = True
    ga.allowed_prefixes = "\n".join(existing_prefixes)

    existing_exact = [
        line.strip()
        for line in ga.allowed_exact.splitlines()
        if line.strip() and line.strip() != "/api/tracks/"
    ]
    if len(existing_exact) != len(
        [l for l in ga.allowed_exact.splitlines() if l.strip()]
    ):
        added = True
    ga.allowed_exact = "\n".join(existing_exact)

    if added:
        ga.save()


def remove_library_view(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")

    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return

    existing = [
        line.strip()
        for line in ga.allowed_prefixes.splitlines()
        if line.strip() and line.strip() not in NEW_PREFIXES
    ]
    ga.allowed_prefixes = "\n".join(existing)

    existing_exact = [
        line.strip()
        for line in ga.allowed_exact.splitlines()
        if line.strip()
    ]
    if "/api/tracks/" not in existing_exact:
        existing_exact.append("/api/tracks/")
    ga.allowed_exact = "\n".join(existing_exact)
    ga.save()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0056_seed_station_time_config"),
    ]

    operations = [
        migrations.RunPython(add_library_view, remove_library_view),
    ]
