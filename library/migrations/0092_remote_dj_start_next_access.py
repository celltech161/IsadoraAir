"""Roadmap 2.23 (D3) -- make the Start Next endpoint reachable for the
``remote_dj`` group.

GroupBasedAccessMiddleware runs before any view-level authorize() call, so
the new ``playout.start_next`` capability (authz.0009) alone would leave the
Remote Host's Start Next control returning the middleware's 403. This grants
the one exact path and nothing else: the deck (Eject/Pause/Resume), seek and
Studio Mic endpoints stay outside ``remote_dj``'s allowlist.

Idempotent (appends only what's missing), same pattern as
0085_remote_dj_queue_set_next_access.py."""
from django.db import migrations


NEW_EXACT = ("/api/engine/start-next/",)


def add(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return
    existing = [l.strip() for l in ga.allowed_exact.splitlines() if l.strip()]
    changed = False
    for p in NEW_EXACT:
        if p not in existing:
            existing.append(p)
            changed = True
    if changed:
        ga.allowed_exact = "\n".join(existing)
        ga.save()


def remove(apps, schema_editor):
    GroupAccess = apps.get_model("library", "GroupAccess")
    Group = apps.get_model("auth", "Group")
    try:
        dj = Group.objects.get(name="remote_dj")
        ga = GroupAccess.objects.get(group=dj)
    except (Group.DoesNotExist, GroupAccess.DoesNotExist):
        return
    existing = [
        l.strip() for l in ga.allowed_exact.splitlines()
        if l.strip() and l.strip() not in NEW_EXACT
    ]
    ga.allowed_exact = "\n".join(existing)
    ga.save()


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0091_playouttransportconfig"),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
