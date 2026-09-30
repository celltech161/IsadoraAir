"""3.1A stage 1 of 3 -- additive schema only.

Creates ScheduleProfile and ScheduleProfileState (its two profile pointers
temporarily nullable) and adds the two profile foreign keys:

* ScheduleBlock.profile is added NULLABLE here; 0087 backfills it and 0088
  makes it NOT NULL. Splitting the stages keeps each step simple and keeps
  schema alteration out of the transaction that performs the data update.
* PlaylistLog.schedule_profile is added in its FINAL form (nullable): NULL
  is the permanent legacy/unknown marker and historical logs are never
  backfilled.

No existing rows are touched and no table is rewritten: every added column
is nullable with no default, which PostgreSQL adds as a catalog-only change.
"""
import uuid

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0085_remote_dj_queue_set_next_access"),
    ]

    operations = [
        migrations.CreateModel(
            name="ScheduleProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uuid", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                ("name", models.CharField(max_length=100, unique=True)),
                ("description", models.TextField(blank=True, default="")),
                ("sort_order", models.IntegerField(default=0)),
                ("is_archived", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["sort_order", "name"]},
        ),
        migrations.CreateModel(
            name="ScheduleProfileState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("active_profile", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to="library.scheduleprofile",
                )),
                ("default_profile", models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.PROTECT,
                    related_name="+", to="library.scheduleprofile",
                )),
            ],
            options={
                "verbose_name": "Schedule Profile State",
                "verbose_name_plural": "Schedule Profile State",
            },
        ),
        migrations.AddField(
            model_name="scheduleblock",
            name="profile",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="schedule_blocks", to="library.scheduleprofile",
            ),
        ),
        migrations.AddField(
            model_name="playlistlog",
            name="schedule_profile",
            field=models.ForeignKey(
                blank=True, null=True, on_delete=django.db.models.deletion.PROTECT,
                related_name="playlist_logs", to="library.scheduleprofile",
            ),
        ),
    ]
