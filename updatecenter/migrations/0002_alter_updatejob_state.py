# Generated for the Phase C distributed-submission state.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("updatecenter", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="updatejob",
            name="state",
            field=models.CharField(
                choices=[
                    ("queued", "Queued"),
                    ("planned", "Planned"),
                    ("running", "Running"),
                    ("submission_uncertain", "Submission uncertain"),
                    ("succeeded", "Succeeded"),
                    ("failed", "Failed"),
                    ("manual_intervention_required", "Manual intervention required"),
                    ("interrupted", "Interrupted"),
                    ("cancelled", "Cancelled"),
                ],
                default="queued",
                max_length=32,
            ),
        ),
    ]
