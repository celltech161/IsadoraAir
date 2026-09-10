# r0060 Phase 6 -- recurring disaster-recovery assurance.
#
# Adds the "backup" MonitorCheck kind (probe_backup, monitoring/services/
# probes.py -- reads isadoraair/backup_assurance.py's own local,
# nonsecret receipts; never SFTP, never a DB connection).
#
# Deliberately additive-only: `choices` is approved non-database field
# metadata (see updatecenter_probe.py's own field-metadata allowlist),
# so this migration classifies fully "additive" under Update Center's
# Phase B v1 automatic-migration allowlist and needs no manual review to
# apply.
#
# No RunPython here on purpose -- an earlier version of this migration
# also seeded a disabled default "Backup Recovery Assurance" check row,
# which made the whole migration classify "manual" (RunPython is
# outside the Phase B v1 automatic allowlist) purely as a side effect of
# a data-seeding step that isn't actually required at deploy time. The
# default row is instead created/configured explicitly during station
# Phase 6 activation, after real backup/round-trip receipts already
# exist -- see docs/DISASTER_RECOVERY_STATUS.md's Phase 6 section for
# the recommended settings an operator enters by hand (or via a future
# management command), and monitoring/tests/test_backup_check.py for
# proof the model fully supports kind="backup" without any seeded row.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('monitoring', '0012_alter_monitorcheck_kind'),
    ]

    operations = [
        migrations.AlterField(
            model_name='monitorcheck',
            name='kind',
            field=models.CharField(choices=[('systemd', 'Systemd Service'), ('disk', 'Disk Usage'), ('cpu', 'CPU Usage'), ('memory', 'Memory Usage'), ('temperature', 'Temperature'), ('transmitter_param', 'Transmitter Parameter'), ('transmitter_indicator', 'Transmitter Status Indicator'), ('audio_silence', 'Audio Silence (Liquidsoap)'), ('encoder_group', 'Encoder Stream Health'), ('rbds', 'RBDS Connection'), ('weather', 'Weather Health'), ('backup', 'Backup Recovery Assurance')], max_length=24),
        ),
    ]
