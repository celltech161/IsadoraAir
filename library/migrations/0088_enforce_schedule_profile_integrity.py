"""3.1A stage 3 of 3 -- enforcement.

Runs only after 0087 has assigned every ScheduleBlock to a profile and
verified uniqueness. Makes ScheduleBlock.profile and the two singleton
pointers NOT NULL and adds the two partial unique constraints that make
schedule resolution deterministic:

* one recurring block per (profile, day_of_week, start_time);
* one specific-date block per (profile, specific_date, start_time).

If a row appeared between 0087 and this migration (for example from
still-running pre-3.1A code) that lacks a profile, the NOT NULL alteration
fails atomically and the migration can simply be re-run after 0087's
idempotent backfill is repeated. Tables involved (ScheduleBlock,
ScheduleProfile*) hold at most a few hundred rows; no large table is
altered or rewritten (PlaylistLog is untouched here).
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0087_backfill_default_schedule_profile"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scheduleblock",
            name="profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="schedule_blocks", to="library.scheduleprofile",
            ),
        ),
        migrations.AlterField(
            model_name="scheduleprofilestate",
            name="active_profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="library.scheduleprofile",
            ),
        ),
        migrations.AlterField(
            model_name="scheduleprofilestate",
            name="default_profile",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+", to="library.scheduleprofile",
            ),
        ),
        migrations.AddConstraint(
            model_name="scheduleblock",
            constraint=models.UniqueConstraint(
                condition=models.Q(("day_of_week__isnull", False)),
                fields=("profile", "day_of_week", "start_time"),
                name="scheduleblock_unique_weekly_slot_per_profile",
            ),
        ),
        migrations.AddConstraint(
            model_name="scheduleblock",
            constraint=models.UniqueConstraint(
                condition=models.Q(("specific_date__isnull", False)),
                fields=("profile", "specific_date", "start_time"),
                name="scheduleblock_unique_dated_slot_per_profile",
            ),
        ),
    ]
