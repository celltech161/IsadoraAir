import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0082_logitem_playback_claim_and_playevent_occurrence"),
    ]

    operations = [
        migrations.AddField(
            model_name="playevent",
            name="duration_evidence_state",
            field=models.CharField(
                choices=[
                    ("legacy", "Legacy / pre-segment semantics"),
                    ("active", "Active, complete evidence so far"),
                    ("complete", "Terminal, complete evidence"),
                    ("interrupted", "Contains an uncertain interruption tail"),
                ],
                default="legacy",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="PlayEventSegment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "generation_id",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                (
                    "log_item_id_snapshot",
                    models.BigIntegerField(blank=True, db_index=True, null=True),
                ),
                ("deck_slot", models.CharField(blank=True, default="", max_length=1)),
                ("deck_generation", models.PositiveBigIntegerField(default=0)),
                ("start_reason", models.CharField(blank=True, default="", max_length=48)),
                ("started_at", models.DateTimeField()),
                ("last_confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("confirmed_duration_seconds", models.FloatField(default=0.0)),
                ("termination_reason", models.CharField(blank=True, default="", max_length=64)),
                (
                    "evidence_state",
                    models.CharField(
                        choices=[
                            ("active", "Active"),
                            ("complete", "Complete"),
                            ("interrupted", "Interrupted / uncertain tail"),
                        ],
                        db_index=True,
                        default="active",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "play_event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="duration_segments",
                        to="library.playevent",
                    ),
                ),
            ],
            options={
                "ordering": ["started_at", "id"],
                "indexes": [
                    models.Index(
                        fields=["play_event", "evidence_state"],
                        name="library_pla_play_ev_40b232_idx",
                    ),
                ],
            },
        ),
    ]
