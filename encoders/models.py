from django.core.exceptions import ValidationError
from django.db import models

BITRATE_CHOICES = [(k, f"{k} kbps") for k in (64, 96, 128, 160, 192, 224, 256, 320)]


class Encoder(models.Model):
    """One outbound stream connection (Icecast/Shoutcast). Each enabled
    row gets its own independent Liquidsoap subprocess in the
    isadoraair-encoders service — see encoders/services/encoder_manager.py.
    Adding/editing/removing a row is a topology change for that process
    (not a live-adjustable property), so admin restarts the service on
    every save/delete — see encoders/admin.py."""
    PROTOCOL_CHOICES = [
        ("icecast", "Icecast"),
        ("shoutcast1", "Shoutcast 1"),
        ("shoutcast2", "Shoutcast 2"),
    ]
    FORMAT_CHOICES = [
        ("mp3", "MP3"),
        ("aac", "AAC"),
        ("vorbis", "Ogg Vorbis"),
    ]

    name = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    protocol = models.CharField(max_length=12, choices=PROTOCOL_CHOICES, default="icecast")
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=8000)
    mount = models.CharField(
        max_length=128, blank=True,
        help_text="Icecast: the real mount path, e.g. /stream. Shoutcast 2: the "
                   "numeric Stream ID, entered the same way, e.g. /4 for stream 4 "
                   "(parsed into Liquidsoap's icy_id). Not used for Shoutcast 1.",
    )
    username = models.CharField(
        max_length=64, default="source", blank=True,
        help_text="Icecast source username. Not used for Shoutcast 1/2.",
    )
    password = models.CharField(max_length=128)
    format = models.CharField(
        max_length=10, choices=FORMAT_CHOICES, default="mp3",
        help_text="Streamed via Liquidsoap. AAC is Icecast/Shoutcast 2 only, not "
                   "Shoutcast 1 (the legacy ICY protocol never supported anything "
                   "but MP3 in practice).",
    )
    bitrate_kbps = models.PositiveIntegerField(choices=BITRATE_CHOICES, default=128)
    input_device = models.CharField(
        max_length=100, blank=True,
        help_text="ALSA capture device. Defaults to the StereoTool HD Output bridge.",
    )
    station_name = models.CharField(max_length=255, blank=True)
    genre = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255, blank=True)
    url = models.URLField(blank=True)
    public = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Encoder"
        verbose_name_plural = "Encoders"

    def clean(self):
        if self.protocol in ("icecast", "shoutcast2") and not self.mount:
            raise ValidationError("Mount point is required for Icecast/Shoutcast 2.")
        if self.format == "aac" and self.protocol == "shoutcast1":
            raise ValidationError(
                "AAC isn't supported over Shoutcast 1 (ICY protocol only ever "
                "carried MP3) — use Icecast or Shoutcast 2."
            )

    def __str__(self):
        return self.name
