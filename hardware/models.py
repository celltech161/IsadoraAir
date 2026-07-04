from django.db import models


class AudioOutput(models.Model):
    """Named playback sink (e.g. studio monitor). The device value is
    an ALSA path like 'plughw:2,0' chosen from what aplay -l reports at
    form-render time."""
    name = models.CharField(max_length=64, unique=True)
    device = models.CharField(
        max_length=100, blank=True,
        help_text="ALSA device path, e.g. plughw:2,0. Pick from the dropdown — options are discovered live from aplay -l.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    # Interim leveling for the studio monitor path only (StereoTool will
    # handle real transmitter processing separately). Only meaningful on
    # whichever row the engine resolves as "Studio Monitor" — see
    # engine.py's STUDIO_MONITOR_NAME / _apply_agc_config.
    agc_enabled = models.BooleanField(
        default=False,
        help_text="Off by default — nothing changes until explicitly enabled and tuned.",
    )
    agc_ratio = models.FloatField(
        default=2.0,
        help_text="Compression ratio, e.g. 2.0 = 2:1. Higher = more compression.",
    )
    agc_threshold = models.FloatField(
        default=0.3,
        help_text="Linear amplitude (0.0-1.0) above which compression kicks in.",
    )
    agc_soft_knee = models.BooleanField(
        default=True,
        help_text="Soft-knee (smooth) vs hard-knee transition into compression.",
    )
    agc_makeup_gain_db = models.FloatField(
        default=3.0,
        help_text="Gain applied after compression, in dB, to bring the leveled signal back up.",
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Audio Output"
        verbose_name_plural = "Audio Outputs"

    def __str__(self):
        return self.name


class AudioInput(models.Model):
    """Named capture source (e.g. studio mic). The device value is an
    ALSA path like 'plughw:2,0' chosen from what arecord -l reports at
    form-render time."""
    name = models.CharField(max_length=64, unique=True)
    device = models.CharField(
        max_length=100, blank=True,
        help_text="ALSA device path, e.g. plughw:2,0. Pick from the dropdown — options are discovered live from arecord -l.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Audio Input"
        verbose_name_plural = "Audio Inputs"

    def __str__(self):
        return self.name
