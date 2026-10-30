# Roadmap 1.21, r0123 Package A -- operator-protected cue markers.
#
# Additive (schema only, no data written): each Track gains its per-marker
# protection/provenance state, a monotonic marker revision, and a separate
# analysis-request timestamp. Existing marker values are untouched; a marker
# with a value and no state entry is historical (unknown provenance) and
# protected by library.services.cue_markers. The two NOT NULL columns carry
# database defaults so code that predates them can still insert Tracks.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0098_hour_refill_automatic_failure_cause"),
    ]

    operations = [
        migrations.AddField(
            model_name="track",
            name="cue_marker_state",
            field=models.JSONField(blank=True, db_default={}, default=dict),
        ),
        migrations.AddField(
            model_name="track",
            name="cue_marker_revision",
            field=models.PositiveBigIntegerField(db_default=0, default=0),
        ),
        migrations.AddField(
            model_name="track",
            name="analysis_requested_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
    ]
