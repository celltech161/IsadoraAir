from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("updatecenter", "0003_updatejob_migration_plan_review_and_more")]

    operations = [
        migrations.AddField(
            model_name="updatejob",
            name="migration_recovery",
            field=models.JSONField(blank=True, default=None, null=True),
        ),
    ]
