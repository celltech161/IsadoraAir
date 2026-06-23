from django.conf import settings
from django.db import models
from django.contrib.postgres.fields import ArrayField


# ---------------------------------------------------------------
# Lookup / supporting models
# ---------------------------------------------------------------

class Artist(models.Model):
    name = models.CharField(max_length=255, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Album(models.Model):
    title = models.CharField(max_length=255)
    # Separate from Track.artist - covers compilations/various-artists
    # albums where the album-level credit differs from individual track
    # artists (mirrors the ID3 TPE1 vs TPE2 distinction).
    album_artist = models.CharField(max_length=255, blank=True)
    year = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]
        unique_together = [("title", "album_artist")]

    def __str__(self):
        return self.title


class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Category(models.Model):
    """Rotation category - e.g. HOT_CURR, DEEP_80S. Distinct from Genre,
    which is descriptive only; Category is what actually drives rotation
    logic (RotationSlot references this, not Genre)."""
    KIND_CHOICES = [
        ("music", "Music"),
        ("imaging", "Imaging"),
        ("spot", "Spot"),
        ("talk", "Talk"),
    ]

    RECENCY_MODE_CHOICES = [
        ("time", "Time-based"),
        ("proportional", "Proportional to category size"),
    ]

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    kind = models.CharField(max_length=10, choices=KIND_CHOICES, default="music")
    description = models.TextField(blank=True)
    color = models.CharField(max_length=20, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    artist_separation = models.FloatField(
        null=True, blank=True,
        verbose_name="Artist separation (hours)",
        help_text="Override: minimum hours before repeating the same artist in this category. Blank = use global default.",
    )
    title_separation = models.FloatField(
        null=True, blank=True,
        verbose_name="Title separation (hours)",
        help_text="Override: minimum hours before repeating the same title in this category. Blank = use global default.",
    )
    recency_mode = models.CharField(
        max_length=12, choices=RECENCY_MODE_CHOICES, default="time",
        help_text="Time-based uses fixed hour windows. Proportional scales separation to category size.",
    )

    class Meta:
        ordering = ["sort_order", "code"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.code


class Holiday(models.Model):
    """Holiday-themed rotation weighting with ramp-in/ramp-out."""
    code = models.CharField(max_length=20, primary_key=True)
    name = models.CharField(max_length=100)
    month = models.PositiveSmallIntegerField()
    day = models.PositiveSmallIntegerField()
    ramp_in_days = models.PositiveSmallIntegerField(default=0)
    ramp_out_days = models.PositiveSmallIntegerField(default=0)
    max_share = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    max_weight_boost = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["month", "day"]

    def __str__(self):
        return self.name


# ---------------------------------------------------------------
# Track - the core model
# ---------------------------------------------------------------

class Track(models.Model):
    ENERGY_CHOICES = [
        ("slow", "Slow"),
        ("midtempo", "Midtempo"),
        ("upbeat", "Upbeat"),
    ]
    VOCAL_TYPE_CHOICES = [
        ("male", "Male"),
        ("female", "Female"),
        ("group", "Group"),
    ]
    END_TYPE_CHOICES = [
        ("auto", "Auto"),
        ("nofade", "No fade"),
        ("cold", "Cold"),
        ("fade", "Fade"),
    ]

    # --- File identity ---
    filepath = models.CharField(max_length=1024, unique=True)
    filename = models.CharField(max_length=255)
    format = models.CharField(max_length=20, blank=True)

    # --- Metadata ---
    title = models.CharField(max_length=255)
    artist = models.ForeignKey(Artist, on_delete=models.PROTECT, related_name="tracks")
    album = models.ForeignKey(Album, on_delete=models.SET_NULL, null=True, blank=True, related_name="tracks")
    genre = models.ForeignKey(Genre, on_delete=models.SET_NULL, null=True, blank=True, related_name="tracks")
    year = models.PositiveIntegerField(null=True, blank=True)
    track_number = models.PositiveSmallIntegerField(null=True, blank=True)
    disc_number = models.PositiveSmallIntegerField(null=True, blank=True)
    composer = models.CharField(max_length=255, blank=True)
    publisher = models.CharField(max_length=255, blank=True)
    record_label = models.CharField(max_length=255, blank=True)
    comments = models.TextField(blank=True)

    # --- Audio technical ---
    duration_seconds = models.FloatField(null=True, blank=True)
    sample_rate = models.PositiveIntegerField(null=True, blank=True)
    channels = models.PositiveSmallIntegerField(null=True, blank=True)
    bit_depth = models.PositiveSmallIntegerField(null=True, blank=True)

    # --- Playout marks (seconds from start of file) ---
    cue_in_seconds = models.FloatField(default=0)
    cue_out_seconds = models.FloatField(null=True, blank=True)
    next_start_seconds = models.FloatField(null=True, blank=True)  # auto-mix trigger point
    intro_until_seconds = models.FloatField(null=True, blank=True)
    sweep_start_seconds = models.FloatField(null=True, blank=True)
    outro_starts_seconds = models.FloatField(null=True, blank=True)
    hook_in_seconds = models.FloatField(null=True, blank=True)
    hook_out_seconds = models.FloatField(null=True, blank=True)

    # --- Scheduling ---
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True, related_name="tracks")
    rotation_weight = models.PositiveSmallIntegerField(default=3)  # 0-5
    ready2air = models.BooleanField(default=False)  # gate: must be human-reviewed before entering rotation
    play_hours = ArrayField(models.PositiveSmallIntegerField(), blank=True, default=list)  # 0-23
    play_days = ArrayField(models.PositiveSmallIntegerField(), blank=True, default=list)    # 0-6
    energy = models.CharField(max_length=10, choices=ENERGY_CHOICES, blank=True)
    vocal_type = models.CharField(max_length=10, choices=VOCAL_TYPE_CHOICES, blank=True)
    end_type = models.CharField(max_length=10, choices=END_TYPE_CHOICES, default="auto")
    holidays = models.ManyToManyField(Holiday, blank=True, related_name="tracks")

    # --- State ---
    play_count = models.PositiveIntegerField(default=0)
    last_played_at = models.DateTimeField(null=True, blank=True)
    waveform_path = models.CharField(max_length=1024, blank=True)
    related_artists = models.CharField(max_length=500, blank=True)  # extracted feat./ft. credits
    remote_url = models.URLField(blank=True)  # for syndicated/remote-hosted audio, not stored locally

    # --- RBDS override ---
    alt_send_enabled = models.BooleanField(
        default=False,
        help_text="If checked, alt_send_text is sent to RBDS instead of the normal artist/title.",
    )
    alt_send_text = models.CharField(
        max_length=64, blank=True,
        help_text="Alternate RadioText sent to RBDS when alt_send_enabled is checked (64-char RBDS RadioText limit).",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["artist", "title"]
        indexes = [
            models.Index(fields=["category", "ready2air"]),
        ]

    def __str__(self):
        return f"{self.artist} - {self.title}"


# ---------------------------------------------------------------
# Rotation - weighted category pools
# ---------------------------------------------------------------

class Rotation(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class RotationSlot(models.Model):
    """One weighted category within a Rotation's pool. The rotation
    walker (Phase 3 build_log) picks a Category from here according to
    weight, then a Track from that Category."""
    rotation = models.ForeignKey(Rotation, on_delete=models.CASCADE, related_name="slots")
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="rotation_slots")
    weight = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["rotation", "-weight"]
        unique_together = [("rotation", "category")]

    def __str__(self):
        return f"{self.rotation} / {self.category} (w={self.weight})"


# ---------------------------------------------------------------
# Clock - hour-long programming templates
# ---------------------------------------------------------------

class Clock(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ClockSlot(models.Model):
    """One position within a Clock's hour. Each slot points to a
    Rotation - the rotation walker picks a Category (by weight) then a
    Track from that Category to fill this slot at build-log time."""
    clock = models.ForeignKey(Clock, on_delete=models.CASCADE, related_name="slots")
    position = models.PositiveSmallIntegerField()  # order within the hour
    rotation = models.ForeignKey(Rotation, on_delete=models.PROTECT, related_name="clock_slots")

    class Meta:
        ordering = ["clock", "position"]
        unique_together = [("clock", "position")]

    def __str__(self):
        return f"{self.clock} #{self.position} -> {self.rotation}"


# ---------------------------------------------------------------
# ScheduleBlock - maps Clocks onto real time
# ---------------------------------------------------------------

class ScheduleBlock(models.Model):
    """Maps a Clock template onto a real time range, either as a
    recurring weekly pattern (day_of_week set) or a one-off override for
    a specific date (specific_date set). Exactly one of the two must be
    set - never both, never neither.

    Precedence is implicit, not an explicit priority field: when
    build_log resolves what clock applies to a given date/time, a
    ScheduleBlock with specific_date matching that date always wins over
    one that only matches via day_of_week. This keeps "holiday special"
    or "remote broadcast Tuesday" overrides simple to reason about
    without a separate priority ranking to maintain."""
    DAY_CHOICES = [
        (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
        (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
    ]

    day_of_week = models.PositiveSmallIntegerField(choices=DAY_CHOICES, null=True, blank=True)
    specific_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    clock = models.ForeignKey(Clock, on_delete=models.PROTECT, related_name="schedule_blocks")

    class Meta:
        ordering = ["specific_date", "day_of_week", "start_time"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(day_of_week__isnull=False, specific_date__isnull=True)
                    | models.Q(day_of_week__isnull=True, specific_date__isnull=False)
                ),
                name="scheduleblock_exactly_one_of_day_or_date",
            ),
        ]

    def __str__(self):
        when = self.specific_date if self.specific_date is not None else self.get_day_of_week_display()
        return f"{when} {self.start_time}-{self.end_time}: {self.clock}"


# ---------------------------------------------------------------
# PlaylistLog - a generated day's log
# ---------------------------------------------------------------

class PlaylistLog(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("approved", "Approved"),
    ]

    date = models.DateField()
    hour = models.PositiveSmallIntegerField(default=0)
    generated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="draft")

    class Meta:
        ordering = ["-date", "hour"]
        unique_together = [("date", "hour")]

    def __str__(self):
        return f"Log for {self.date} {self.hour:02d}:00 ({self.status})"


class LogItem(models.Model):
    """One scheduled track within a PlaylistLog, produced by the
    rotation walker: ScheduleBlock -> Clock -> ClockSlot -> Rotation ->
    RotationSlot (by weight) -> Category -> Track."""
    playlist_log = models.ForeignKey(PlaylistLog, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveIntegerField()
    scheduled_time = models.DateTimeField()
    track = models.ForeignKey(Track, on_delete=models.PROTECT, related_name="log_items")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    played_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["playlist_log", "position"]
        unique_together = [("playlist_log", "position")]

    def __str__(self):
        return f"{self.playlist_log.date} #{self.position}: {self.track}"


# ---------------------------------------------------------------
# Site configuration (singleton models)
# ---------------------------------------------------------------

class AnalysisConfig(models.Model):
    """Singleton — audio analysis thresholds editable from admin."""
    next_start_threshold_db = models.FloatField(
        default=-26.0,
        verbose_name="Next Start threshold (dBFS)",
        help_text="Scan backward from end of track; last sample above this level sets the auto-mix trigger point.",
    )
    cue_in_threshold_db = models.FloatField(
        default=-45.0,
        verbose_name="Cue In threshold (dBFS)",
        help_text="Scan forward from start; first sample above this level sets the cue-in point.",
    )
    cue_in_min_seconds = models.FloatField(
        default=0.1,
        verbose_name="Cue In minimum (seconds)",
        help_text="Cue-in values smaller than this are treated as 0.",
    )
    analysis_sample_rate = models.PositiveIntegerField(
        default=4410,
        verbose_name="Analysis sample rate (Hz)",
        help_text="Sample rate for the ffmpeg decode pass. Lower = faster but less precise.",
    )
    analysis_window_seconds = models.FloatField(
        default=0.05,
        verbose_name="RMS window (seconds)",
        help_text="Length of each RMS envelope window.",
    )
    waveform_points = models.PositiveIntegerField(
        default=1000,
        verbose_name="Waveform resolution",
        help_text="Number of data points in the UI waveform.",
    )

    class Meta:
        verbose_name = "Analysis Configuration"
        verbose_name_plural = "Analysis Configuration"

    def __str__(self):
        return "Analysis Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "next_start_threshold_db": getattr(settings, "NEXT_START_THRESHOLD_DB", -26.0),
                "cue_in_threshold_db": getattr(settings, "CUE_IN_THRESHOLD_DB", -45.0),
                "cue_in_min_seconds": getattr(settings, "CUE_IN_MIN_SECONDS", 0.1),
                "analysis_sample_rate": getattr(settings, "ANALYSIS_SAMPLE_RATE", 4410),
                "analysis_window_seconds": getattr(settings, "ANALYSIS_WINDOW_SECONDS", 0.05),
                "waveform_points": getattr(settings, "ANALYSIS_WAVEFORM_POINTS", 1000),
            },
        )
        return obj


class RecencyConfig(models.Model):
    """Singleton — global recency avoidance defaults for the log builder."""
    artist_separation = models.FloatField(
        default=2.5,
        verbose_name="Artist separation (hours)",
        help_text="Default minimum hours before repeating the same artist. Can be overridden per category.",
    )
    title_separation = models.FloatField(
        default=8.0,
        verbose_name="Title separation (hours)",
        help_text="Default minimum hours before repeating the same title. Can be overridden per category.",
    )

    class Meta:
        verbose_name = "Recency Configuration"
        verbose_name_plural = "Recency Configuration"

    def __str__(self):
        return "Recency Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "artist_separation": 2.5,
                "title_separation": 8.0,
            },
        )
        return obj
