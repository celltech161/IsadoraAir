from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0081_remotedjconfig_reconnect_grace_seconds"),
    ]

    operations = [
        migrations.AddField(
            model_name="logitem",
            name="playback_claimed_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="playevent",
            name="log_item_id_snapshot",
            field=models.BigIntegerField(blank=True, null=True, unique=True),
        ),
    ]
