# Roadmap 1.21 (P0) -- DST fall-back replay authorization marker.
#
# Additive, schema only (no data written, nothing backfilled or inferred):
# * PlaylistLog.dst_replay_hour_start: one nullable column, NULL on every
#   existing row, no database default (code from before this migration keeps
#   inserting PlaylistLogs during an upgrade). The engine sets it once, under
#   the approved log's row lock and in the same transaction as the replay
#   occurrences; it is the sole authority that the log's single replay happened.
# * the LogItem origin choice "dst_replay" (choices only: no column change).
#
# Forward-only, like every Update Center migration. Reversing it by hand
# (migrate library 0099) drops the marker: any "dst_replay" rows stay ordinary
# rows, but the record that a replay was authorized is lost, so a re-applied
# marker column is NULL again. See docs/DST_FALLBACK_CONTINUITY.md.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0099_track_cue_marker_authority"),
    ]

    operations = [
        migrations.AddField(
            model_name="playlistlog",
            name="dst_replay_hour_start",
            field=models.DateTimeField(blank=True, editable=False, null=True),
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
    ]
