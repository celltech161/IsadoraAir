"""Roadmap 2.5C -- fix a pre-existing, unrelated-to-authorization
reachability gap discovered while re-auditing the real endpoint
implementations: the "force-next" button remote_dj mode's dashboard
already renders and wires up (dashboard.html's setNext() posts to
/api/engine/queue/set-next/, per remote_dj_page's own docstring: "keeps
... force-next buttons available ... a remote DJ is trusted to queue,
reorder, and jump ahead in their own show") has never actually worked
for a remote_dj account -- migration 0054's seeded GroupAccess row never
included this prefix, so GroupBasedAccessMiddleware has always 403'd/
redirected the request before it reached the view.

This is a plain reachability fix, independent of roadmap 2.5's new
capability layer (which separately now also requires
playout.queue_manage at this endpoint -- see
authz.migrations.0006_correct_remote_host_playout_capability). Without
this GroupAccess grant, that capability grant alone would still leave
the documented feature non-functional (GroupAccess runs first, in
middleware, before any view-level authorize() call).

Idempotent (appends only what's missing), same pattern as
0065_remote_dj_fx_access.py / 0068_remote_dj_voicetrack_access.py."""
from django.db import migrations


NEW_EXACT = ("/api/engine/queue/set-next/",)


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
        ("library", "0084_alter_uitheme_logo_alter_uitheme_station_logo"),
    ]

    operations = [
        migrations.RunPython(add, remove),
    ]
