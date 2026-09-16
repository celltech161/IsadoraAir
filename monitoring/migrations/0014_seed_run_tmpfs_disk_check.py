# P2 1.13B -- adds a default "Disk: /run (runtime tmpfs)" MonitorCheck
# so /run capacity risk (the Aircheck working file, active-segment
# handoffs, legacy HE-AAC intermediates, and every other Aircheck
# recovery artifact that can transiently live there) participates in
# the SAME generic Monitoring warning/critical/notification machinery
# every other "disk" kind check already uses -- monitoring.services.
# probes' disk probe is reused as-is; nothing Aircheck-specific is
# added to it.
#
# Thresholds: kept identical to the existing generic disk defaults
# (warning 60%, critical 75%, see monitoring/migrations/0002_seed_
# default_checks.py) rather than diverging. /run's absolute consequence
# of exhaustion is more severe than a slowly-filling persistent disk
# (socket/PID-file failures across the whole system, not just Aircheck),
# but tmpfs capacity is also normally proportionally generous relative
# to Aircheck's own bounded footprint (AIRCHECK_WORKING_FILE_MAX_BYTES
# plus whatever recovery.py's evacuation/reconciliation has not yet
# cleared -- both intentionally small and bounded by this same roadmap
# item), so reaching even 60% of /run's OWN size already represents a
# genuine anomaly worth surfacing, not routine operation. An operator
# can retune this row like any other disk MonitorCheck.
#
# get_or_create keyed on `name`, matching 0002's own seeding
# convention: idempotent, safe to run on any existing installation,
# never overwrites an operator's own customized row of the same name,
# and never touches any other check. RunPython (not schema-only)
# classifies this migration "manual" under Update Center's Phase B v1
# automatic-migration allowlist, same as any other RunPython -- see
# 0013's own comment for why that distinction matters at deploy time.
from django.db import migrations


def seed_run_disk_check(apps, schema_editor):
    MonitorCheck = apps.get_model("monitoring", "MonitorCheck")
    MonitorCheck.objects.get_or_create(
        name="Disk: /run (runtime tmpfs)",
        defaults={
            "kind": "disk",
            "sort_order": 70,
            "disk_path": "/run",
            "warning_threshold": 60.0,
            "critical_threshold": 75.0,
        },
    )


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("monitoring", "0013_backup_recovery_assurance_check"),
    ]

    operations = [
        migrations.RunPython(seed_run_disk_check, reverse_noop),
    ]
