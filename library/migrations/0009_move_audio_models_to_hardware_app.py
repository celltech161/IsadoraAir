from django.db import migrations


class Migration(migrations.Migration):
    """Hands AudioInput and AudioOutput off to the hardware app. State
    side removes them from library; database side renames the existing
    tables so the seeded rows from 0008 are preserved."""

    dependencies = [
        ("library", "0008_audioinput_audiooutput"),
        ("hardware", "0001_initial"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.DeleteModel(name="AudioInput"),
                migrations.DeleteModel(name="AudioOutput"),
            ],
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE library_audioinput RENAME TO hardware_audioinput;",
                    reverse_sql="ALTER TABLE hardware_audioinput RENAME TO library_audioinput;",
                ),
                migrations.RunSQL(
                    sql="ALTER TABLE library_audiooutput RENAME TO hardware_audiooutput;",
                    reverse_sql="ALTER TABLE hardware_audiooutput RENAME TO library_audiooutput;",
                ),
            ],
        ),
    ]
