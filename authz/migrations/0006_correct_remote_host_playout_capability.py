"""Roadmap 2.5C -- correct a mismapping discovered during the "audit the
actual current endpoint implementations again before editing" pass
(workorder instruction 6/7).

`remote_dj_page`'s own docstring is explicit about the intended split:
remote_dj mode "hides the operator-only controls (Studio Mic PTT,
per-track edit links, deck eject/pause, waveform click-seek)... but
keeps search-to-add, Play Now, drag-to-reorder queue, and force-next
buttons available." I.e. Remote Host is intended to manage the UPCOMING
QUEUE (insert a track, force a track to play next) but never deck
transport control (pause/resume/eject) or seek -- those remain
operator/Station-Administrator-only, matching today's real GroupAccess
grant (remote_dj's seeded row never included /api/engine/seek/ or
/api/engine/deck/).

The single `playout.control` capability seeded in 2.5A ("Control
playout (queue/seek/deck commands)") bundled BOTH of these together and
was granted wholesale to the Remote Host Role -- which would have been
a real permission EXPANSION relative to today's intended/actual
behavior once 2.5C wires playout.control into api_engine_seek and
api_engine_deck_command. Per the operator's own instruction ("If the
earlier capability mapping accidentally grants something broader than
current behavior, fix the mapping rather than treating backward
compatibility as permission expansion"), this migration:

  1. Adds a new capability `playout.queue_manage` ("Manage the upcoming
     queue: insert a track, force a track to play next") for exactly
     the Remote-Host-appropriate subset (api_engine_insert_track,
     api_engine_set_next).
  2. Removes the Remote Host -> playout.control binding.
  3. Grants Remote Host -> playout.queue_manage instead.
  4. Leaves playout.control (now exclusively seek + deck commands) on
     Station Administrator only, unchanged.

Idempotent (get_or_create / filter().delete()), safe on a database that
has already run 2.5A's seed migration."""
from django.db import migrations


def forwards(apps, schema_editor):
    Capability = apps.get_model("authz", "Capability")
    Role = apps.get_model("authz", "Role")
    RoleCapability = apps.get_model("authz", "RoleCapability")

    queue_manage, _ = Capability.objects.update_or_create(
        slug="playout.queue_manage",
        defaults={
            "label": "Manage the upcoming queue (insert, force-next)",
            "requires_schedule": True,
        },
    )

    try:
        remote_host = Role.objects.get(name="Remote Host")
    except Role.DoesNotExist:
        return  # Fresh install where 2.5A's seed hasn't run yet -- nothing to correct.

    try:
        playout_control = Capability.objects.get(slug="playout.control")
    except Capability.DoesNotExist:
        playout_control = None
    if playout_control is not None:
        RoleCapability.objects.filter(role=remote_host, capability=playout_control).delete()

    RoleCapability.objects.get_or_create(role=remote_host, capability=queue_manage)


def backwards(apps, schema_editor):
    Capability = apps.get_model("authz", "Capability")
    Role = apps.get_model("authz", "Role")
    RoleCapability = apps.get_model("authz", "RoleCapability")

    try:
        remote_host = Role.objects.get(name="Remote Host")
        queue_manage = Capability.objects.get(slug="playout.queue_manage")
    except (Role.DoesNotExist, Capability.DoesNotExist):
        pass
    else:
        RoleCapability.objects.filter(role=remote_host, capability=queue_manage).delete()
        try:
            playout_control = Capability.objects.get(slug="playout.control")
        except Capability.DoesNotExist:
            pass
        else:
            RoleCapability.objects.get_or_create(role=remote_host, capability=playout_control)

    Capability.objects.filter(slug="playout.queue_manage").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("authz", "0005_scheduleaccessconfig_scheduled_enforcement_enabled"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
