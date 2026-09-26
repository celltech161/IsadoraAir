from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hardware", "0011_duckingconfig_ptt_auto_manual_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="audiopipeline",
            name="audio_gap_diagnostics_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Enable bounded sub-second audio-gap diagnostics for "
                    "troubleshooting the Engine → StereoTool → encoder audio "
                    "path. Enabling or disabling this setting requires a restart "
                    "of the IsadoraAir Engine and encoder services to take effect."
                ),
                verbose_name="Enable audio-gap diagnostics",
            ),
        ),
    ]
