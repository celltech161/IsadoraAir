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
