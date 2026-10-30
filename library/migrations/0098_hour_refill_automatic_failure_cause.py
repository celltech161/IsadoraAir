# Roadmap 2.27, r0121 -- automatic skip and media failure as hour-refill causes.
#
# Additive (Update Center mechanical): the policy's two default-Off switches
# (capture_automatic_skip, capture_media_failure), the cause's failure_phase,
# failure_reason and failure_position_seconds columns, and cause_type's
# choices (metadata).
#
# Manual (each needs its own exact reviewed r0121 authorization -- never
# pre-approved): the known-type CHECK is replaced by one that also admits
# "automatic_skip" and "media_failure", and two new CHECKs require each new
# type's identity fields. Existing rows satisfy all three. No data is written
# or backfilled; nothing is enabled.
#
# Reverse re-adds the three-type CHECK and is refused while any
# automatic_skip/media_failure row exists. It never deletes evidence: stay
# forward, or restore the pre-migration backup. See
# docs/roadmap/2_27_AUTOMATIC_FAILURE_CAUSALITY_EXTENSION.md section 5.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('library', '0097_hour_refill_start_next_cause'),
    ]

    operations = [
        migrations.AddField(
            model_name='hourrefillpolicy',
            name='capture_automatic_skip',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='hourrefillpolicy',
            name='capture_media_failure',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='hourrefillcause',
            name='failure_phase',
            field=models.CharField(blank=True, default='', max_length=16),
        ),
        migrations.AddField(
            model_name='hourrefillcause',
            name='failure_reason',
            field=models.CharField(blank=True, default='', max_length=32),
        ),
        migrations.AddField(
            model_name='hourrefillcause',
            name='failure_position_seconds',
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='hourrefillcause',
            name='cause_type',
            field=models.CharField(choices=[('request_substitution', 'Shorter web-request substitution'), ('operator_eject', 'Completed operator Eject'), ('start_next', 'Completed Start Next fade (r0121)'), ('automatic_skip', 'Automatic skip: scheduled media missing (r0121)'), ('media_failure', 'Media error ended a scheduled song (r0121)')], max_length=32),
        ),
        migrations.RemoveConstraint(
            model_name='hourrefillcause',
            name='hour_refill_cause_known_type',
        ),
        migrations.AddConstraint(
            model_name='hourrefillcause',
            constraint=models.CheckConstraint(condition=models.Q(('cause_type__in', ['request_substitution', 'operator_eject', 'start_next', 'automatic_skip', 'media_failure'])), name='hour_refill_cause_known_type'),
        ),
        migrations.AddConstraint(
            model_name='hourrefillcause',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('cause_type', 'automatic_skip'), _negated=True), models.Q(('failure_phase', 'pre_air'), ('original_track_id_snapshot__isnull', False), models.Q(('failure_reason', ''), _negated=True)), _connector='OR'), name='hour_refill_cause_automatic_skip_fields'),
        ),
        migrations.AddConstraint(
            model_name='hourrefillcause',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('cause_type', 'media_failure'), _negated=True), models.Q(('deck_generation__isnull', False), ('failure_phase__in', ['pre_air', 'post_air']), ('original_track_id_snapshot__isnull', False), models.Q(('engine_instance', ''), _negated=True), models.Q(('deck_slot', ''), _negated=True), models.Q(('failure_reason', ''), _negated=True), models.Q(models.Q(('failure_phase', 'post_air'), _negated=True), ('failure_position_seconds__isnull', False), _connector='OR')), _connector='OR'), name='hour_refill_cause_media_failure_fields'),
        ),
    ]
