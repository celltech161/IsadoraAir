from django.db import migrations


def set_ps_mode_from_existing_frames(apps, schema_editor):
    """Preserves every existing installation's current on-air PS
    behavior exactly, under the implicit precedence rbds_manager.py has
    always used before this migration existed: if any enabled
    RBDSPSFrame rows exist, they rotate (this is now ps_mode="manual");
    otherwise RBDSConfig.station_ps is sent statically (now
    ps_mode="static"). Fresh installs have no RBDSConfig row yet at
    migration time (RBDSConfig.load()'s get_or_create(pk=1) only runs
    the first time the engine/admin actually touches it), so there is
    nothing here to update for them -- ps_mode's own field default
    ("static") already covers that case once the row is eventually
    created.

    Deliberately does NOT touch RBDSPSFrame rows themselves in any way
    (no delete, no disable, no rewrite) -- this migration only sets one
    field on the singleton RBDSConfig row."""
    RBDSConfig = apps.get_model("rbds", "RBDSConfig")
    RBDSPSFrame = apps.get_model("rbds", "RBDSPSFrame")

    config = RBDSConfig.objects.filter(pk=1).first()
    if config is None:
        return

    if RBDSPSFrame.objects.filter(enabled=True).exists():
        config.ps_mode = "manual"
    else:
        config.ps_mode = "static"
    config.save(update_fields=["ps_mode"])


def reverse_noop(apps, schema_editor):
    """Deliberately a no-op, not a re-derivation -- ps_mode may have
    been changed by an operator between the forward migration running
    and any later reverse-migrate, and guessing which state to restore
    would silently discard that. AddField's own reverse (RemoveField,
    handled by the prior migration) already removes the column; there
    is nothing else to safely undo here."""


class Migration(migrations.Migration):

    dependencies = [
        ("rbds", "0011_add_ps_mode_and_dynamic_ps_settings"),
    ]

    operations = [
        migrations.RunPython(set_ps_mode_from_existing_frames, reverse_noop),
    ]
