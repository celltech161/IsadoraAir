# Roadmap 2.27, r0121 -- Start Next as a third hour-refill cause type.
#
# Additive (Update Center mechanical): the policy's own default-Off
# capture_start_next switch, the cause's start_next_request_id and
# start_next_position_seconds columns, and cause_type's choices (metadata).
#
# Manual (each needs the exact reviewed r0121 companion authorization): the
# known-type CHECK is replaced by one that also admits "start_next", and a
# new CHECK requires a start_next row's engine/deck/position/request
# identity. Existing rows are request_substitution/operator_eject and satisfy
# both. No data is written or backfilled; nothing is enabled.
#
# Reverse re-adds the two-type CHECK and so fails while any start_next row
# exists: delete those evidence rows first. See
# docs/roadmap/2_27_START_NEXT_CAUSALITY_EXTENSION.md section 7.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0096_hour_refill_install_gate'),
    ]

    operations = [
        migrations.AddField(
            model_name='hourrefillpolicy',
            name='capture_start_next',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='hourrefillcause',
            name='start_next_request_id',
            field=models.CharField(blank=True, default='', max_length=64),
        ),
        migrations.AddField(
            model_name='hourrefillcause',
            name='start_next_position_seconds',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='hourrefillcause',
            name='cause_type',
            field=models.CharField(choices=[('request_substitution', 'Shorter web-request substitution'), ('operator_eject', 'Completed operator Eject'), ('start_next', 'Completed Start Next fade (r0121)')], max_length=32),
        ),
        migrations.RemoveConstraint(
            model_name='hourrefillcause',
            name='hour_refill_cause_known_type',
        ),
        migrations.AddConstraint(
            model_name='hourrefillcause',
            constraint=models.CheckConstraint(condition=models.Q(('cause_type__in', ['request_substitution', 'operator_eject', 'start_next'])), name='hour_refill_cause_known_type'),
        ),
        migrations.AddConstraint(
            model_name='hourrefillcause',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('cause_type', 'start_next'), _negated=True), models.Q(('deck_generation__isnull', False), ('original_track_id_snapshot__isnull', False), ('start_next_position_seconds__isnull', False), models.Q(('engine_instance', ''), _negated=True), models.Q(('deck_slot', ''), _negated=True), models.Q(('start_next_request_id', ''), _negated=True)), _connector='OR'), name='hour_refill_cause_start_next_fields'),
        ),
    ]
