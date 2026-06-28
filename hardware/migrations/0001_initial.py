from django.db import migrations, models


class Migration(migrations.Migration):
    """Adopts AudioInput and AudioOutput that previously lived in the
    library app. Uses SeparateDatabaseAndState so no SQL runs here;
    library/0009_remove_audio_models does the ALTER TABLE RENAME that
    moves the existing tables from library_* to hardware_*."""

    initial = True

    dependencies = []

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="AudioInput",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("name", models.CharField(max_length=64, unique=True)),
                        ("device", models.CharField(blank=True, help_text="ALSA device path, e.g. plughw:2,0. Pick from the dropdown — options are discovered live from arecord -l.", max_length=100)),
                        ("sort_order", models.PositiveSmallIntegerField(default=0)),
                    ],
                    options={
                        "verbose_name": "Audio Input",
                        "verbose_name_plural": "Audio Inputs",
                        "ordering": ["sort_order", "name"],
                    },
                ),
                migrations.CreateModel(
                    name="AudioOutput",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("name", models.CharField(max_length=64, unique=True)),
                        ("device", models.CharField(blank=True, help_text="ALSA device path, e.g. plughw:2,0. Pick from the dropdown — options are discovered live from aplay -l.", max_length=100)),
                        ("sort_order", models.PositiveSmallIntegerField(default=0)),
                    ],
                    options={
                        "verbose_name": "Audio Output",
                        "verbose_name_plural": "Audio Outputs",
                        "ordering": ["sort_order", "name"],
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
