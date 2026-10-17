"""D3D: two additive indexes on ActiveBroadcastExecution for the operator
board's bounded history reads -- newest-first across all states with a
deterministic tie-break, and message membership in the frozen composition
snapshot (jsonb_path_ops GIN, serving ``composition_snapshot @> ...``).
No field, constraint or data change; nothing is rewritten."""
import django.contrib.postgres.indexes
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('active_messages', '0004_message_content_type_identity'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='activebroadcastexecution',
            index=models.Index(fields=['created_at', 'id'], name='active_execution_recent'),
        ),
        migrations.AddIndex(
            model_name='activebroadcastexecution',
            index=django.contrib.postgres.indexes.GinIndex(fields=['composition_snapshot'], name='active_execution_members', opclasses=['jsonb_path_ops']),
        ),
    ]
