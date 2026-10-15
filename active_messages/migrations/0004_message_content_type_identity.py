"""iPortal Phase D3A: every active message now has its exact content type
(backfilled by 0003), and the producer slot is (producer_key, content_type)
instead of the broad (producer_key, kind)."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("active_messages", "0003_message_content_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="activebroadcastmessage",
            name="content_type",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, related_name="active_broadcast_messages",
                to="produced_content.producedcontenttype"),
        ),
        migrations.RemoveConstraint(
            model_name="activebroadcastmessage",
            name="active_message_one_active_per_producer_kind",
        ),
        migrations.AddConstraint(
            model_name="activebroadcastmessage",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "active")), fields=("producer_key", "content_type"),
                name="active_message_one_active_per_producer_type"),
        ),
    ]
