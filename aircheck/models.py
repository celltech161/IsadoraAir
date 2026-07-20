from django.core.exceptions import ValidationError
from django.db import models


FORMAT_CHOICES = [
    ("he_aac", "HE-AAC (fdkaac)"),
    ("mp3", "MP3 (libmp3lame)"),
    ("flac", "FLAC (lossless)"),
    ("wav", "WAV (uncompressed PCM)"),
]

# Default ffmpeg bitrate per format. HE-AAC is intended for compliance-
# archive quality (~30 MB/hr), MP3 320 is for high-quality airchecks
# an operator might want to review or reuse (~140 MB/hr).
DEFAULT_BITRATE_BY_FORMAT = {
    "he_aac": "64k",
    "mp3": "320k",
    # FLAC/WAV don't take a bitrate arg -- the field is ignored for
    # those but stays visible in admin so switching format doesn't
    # lose the last-remembered value.
    "flac": "",
    "wav": "",
}


class AircheckConfig(models.Model):
    """Singleton -- one row edited via Django admin. Everything the
    aircheck recorder needs to spawn ffmpeg (format, bitrate, output
    directory) plus the source device it should capture from. The
    manual on/off button on /monitoring/ reads this config each time
    a new session starts, so an admin change takes effect on the
    next Start Recording press without any restart."""

    audio_format = models.CharField(
        max_length=16, choices=FORMAT_CHOICES, default="he_aac",
        help_text="ffmpeg encoder. HE-AAC (fdkaac) is the small, "
                   "compliance-archive default (~30 MB/hr at 64k). MP3 "
                   "320 is the high-quality option (~140 MB/hr) for "
                   "airchecks you might want to review or reuse. "
                   "FLAC/WAV for lossless (~1.4 GB/hr for WAV).",
    )
    bitrate = models.CharField(
        max_length=8, default="64k", blank=True,
        help_text="ffmpeg -b:a value (e.g. '64k', '128k', '320k'). "
                   "Ignored for FLAC/WAV. Blank falls back to the "
                   "format's default (64k for HE-AAC, 320k for MP3).",
    )
    source_device = models.CharField(
        max_length=64, default="plughw:3,1",
        help_text="ALSA device the ffmpeg -i argument reads from. "
                   "Default 'plughw:3,1' = post-StereoTool loopback "
                   "(same source the encoders read). Change to a "
                   "dsnoop alias if simultaneous encoder+aircheck "
                   "reads collide.",
    )
    output_directory = models.CharField(
        max_length=255, default="/srv/isadoraair/aircheck",
        help_text="Directory where aircheck files land. Created on "
                   "first record if it doesn't exist. Should live on "
                   "a partition with plenty of headroom -- a full "
                   "day of 320 mp3 is ~3.4 GB.",
    )
    filename_template = models.CharField(
        max_length=100, default="aircheck-%Y%m%d-%H%M%S",
        help_text="Python strftime template (extension is added "
                   "automatically per audio_format). Default "
                   "'aircheck-%%Y%%m%%d-%%H%%M%%S' -> "
                   "'aircheck-20261225-143012.mp3'. Sorts "
                   "lexically = chronologically.",
    )

    class Meta:
        verbose_name = "Aircheck Configuration"
        verbose_name_plural = "Aircheck Configuration"

    def __str__(self):
        return "Aircheck Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def clean(self):
        if self.audio_format in ("he_aac", "mp3") and not self.bitrate:
            # Auto-fill rather than reject -- form-side friendlier.
            self.bitrate = DEFAULT_BITRATE_BY_FORMAT.get(self.audio_format, "")

    def effective_bitrate(self):
        return self.bitrate or DEFAULT_BITRATE_BY_FORMAT.get(self.audio_format, "")

    def file_extension(self):
        return {
            "he_aac": "m4a",
            "mp3": "mp3",
            "flac": "flac",
            "wav": "wav",
        }.get(self.audio_format, "bin")


class AircheckSession(models.Model):
    """One row per record-start -> record-stop cycle. Populated by
    the API endpoints (start_time on creation, end_time + size_bytes
    on stop). Rows aren't strictly needed for playback -- the files
    on disk are the authoritative record -- but the table is useful
    for a Recent Aircheck list on /monitoring/ and for cleanup
    (identifying orphaned files whose row is missing, or rows whose
    file was manually deleted).

    still_running defaults to True on create; the stop endpoint flips
    it and sets end_time. A row still marked running whose ffmpeg PID
    is dead is recovered on the next start attempt.
    """
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    filename = models.CharField(max_length=512)  # absolute path
    audio_format = models.CharField(max_length=16, choices=FORMAT_CHOICES)
    bitrate = models.CharField(max_length=8, blank=True)
    source_device = models.CharField(max_length=64)
    ffmpeg_pid = models.PositiveIntegerField(null=True, blank=True)
    still_running = models.BooleanField(default=True)
    size_bytes = models.BigIntegerField(null=True, blank=True)
    exit_note = models.CharField(
        max_length=200, blank=True,
        help_text="Populated on stop with a short note if ffmpeg "
                   "exited non-zero, was killed abnormally, or hit "
                   "an edge case. Blank on a clean session.",
    )

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Aircheck Session"
        verbose_name_plural = "Aircheck Sessions"

    def __str__(self):
        return f"Aircheck {self.started_at.isoformat()}"
