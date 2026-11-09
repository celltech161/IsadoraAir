# Roadmap 1.21 (P0) -- DST fall-back replay provenance.
#
# Additive (schema only; no data written, nothing backfilled or inferred):
# * a new table library_dstfallbackreplaypass: the single authorized replay
#   pass of one approved PlaylistLog for one physical successor hour, unique
#   per (playlist_log, successor_hour_start);
# * two new nullable LogItem columns (replay_pass, replay_source_id_snapshot),
#   NULL on every existing row, with no database default so code from before
#   this migration keeps inserting LogItems during an upgrade;
# * the new LogItem origin choice "dst_replay" (choices only: no column change);
# * a partial unique constraint (one replay occurrence per original occurrence
#   per pass) and a check constraint: the provenance columns are set together
#   and only on an origin "dst_replay" row. Every existing row satisfies both
#   (provenance NULL). Reversal (to 0099) drops the table and the two columns;
#   any "dst_replay" row then stays an ordinary row, and re-applying this
#   migration accepts it (the check does not require a pass for that origin).
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0099_track_cue_marker_authority"),
    ]

    operations = [
        migrations.CreateModel(
            name="DstFallbackReplayPass",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("successor_hour_start", models.DateTimeField()),
                ("transition_at", models.DateTimeField()),
                ("time_zone", models.CharField(max_length=64)),
                ("authorized_at", models.DateTimeField()),
                ("reason", models.CharField(blank=True, default="", max_length=32)),
                ("source_log_item_ids", models.JSONField(default=list)),
                ("replay_count", models.PositiveIntegerField()),
                ("playlist_log", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE, related_name="dst_replay_passes",
                    to="library.playlistlog")),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(fields=("playlist_log", "successor_hour_start"),
                                            name="library_dst_replay_pass_once_per_log_hour"),
                ],
            },
        ),
        migrations.AddField(
            model_name="logitem",
            name="replay_pass",
            field=models.ForeignKey(
                blank=True, editable=False, null=True, on_delete=django.db.models.deletion.RESTRICT,
                related_name="replay_items", to="library.dstfallbackreplaypass"),
        ),
        migrations.AddField(
            model_name="logitem",
            name="replay_source_id_snapshot",
            field=models.BigIntegerField(blank=True, editable=False, null=True),
        ),
        migrations.AlterField(
            model_name="logitem",
            name="origin",
            field=models.CharField(blank=True, choices=[
                ("generated_schedule", "Generated schedule"), ("generated_fill", "Build-time fill"),
                ("live_fill", "Live fill"), ("operator_inserted", "Operator inserted"),
                ("engine_inserted", "Engine inserted (urgent, native, dedication intro)"),
                ("refill_installed", "Hour-refill install (2.27)"), ("dst_replay", "DST fall-back replay (1.21)"),
            ], editable=False, max_length=24, null=True),
        ),
        migrations.AddConstraint(
            model_name="logitem",
            constraint=models.UniqueConstraint(
                condition=models.Q(("replay_pass__isnull", False)), fields=("replay_pass", "replay_source_id_snapshot"),
                name="library_logitem_one_replay_per_source"),
        ),
        migrations.AddConstraint(
            model_name="logitem",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("replay_pass__isnull", True), ("replay_source_id_snapshot__isnull", True)),
                    models.Q(("origin", "dst_replay"), ("replay_pass__isnull", False),
                             ("replay_source_id_snapshot__isnull", False)),
                    _connector="OR",
                ),
                name="library_logitem_dst_replay_provenance"),
        ),
    ]
