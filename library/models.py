from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.postgres.fields import ArrayField


# ---------------------------------------------------------------
# Lookup / supporting models
# ---------------------------------------------------------------

class Artist(models.Model):
    name = models.CharField(max_length=255, unique=True)
    # Manual album-art override, checked ahead of embedded/Deezer/iTunes
    # lookup — mainly for artists whose tracks have no embedded art and no
    # good remote match (used as a fallback when a track has no Album).
    cover_art = models.ImageField(upload_to="artist_covers/", blank=True, null=True)
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
    # Manual album-art override, checked ahead of embedded/Deezer/iTunes
    # lookup — takes priority over Artist.cover_art for tracks that have
    # an Album.
    cover_art = models.ImageField(upload_to="album_covers/", blank=True, null=True)
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


class CategoryKind(models.Model):
    """What broad kind of content a Category holds (music/imaging/spot/
    talk, and eventually things like a remote-URL stream). Admin-managed
    (add/remove) rather than a hardcoded choices list, since new kinds can
    imply different playback handling down the line."""
    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=50)
    fill_color = models.CharField(
        max_length=50, default="#374151",
        help_text="Any CSS color value (hex, rgb, rgba) — background fill "
                   "for items of this kind in the dashboard's Coming Up list.",
    )
    sort_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Category Kind"
        verbose_name_plural = "Category Kinds"

    def __str__(self):
        return self.name


class Category(models.Model):
    """Rotation category - e.g. HOT_CURR, DEEP_80S. Distinct from Genre,
    which is descriptive only; Category is what actually drives rotation
    logic (RotationSlot references this, not Genre)."""
    RECENCY_MODE_CHOICES = [
        ("time", "Time-based"),
        ("proportional", "Proportional to category size"),
    ]

    code = models.CharField(max_length=20, unique=True)
    name = models.CharField(max_length=100)
    kind = models.ForeignKey(CategoryKind, on_delete=models.PROTECT, related_name="categories")
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
    code = models.CharField(
        max_length=20, primary_key=True,
        help_text="Short identifier, e.g. CHRISTMAS, JULY4TH, HALLOWEEN.",
    )
    name = models.CharField(max_length=100, help_text="Display name, e.g. 'Christmas', 'Independence Day'.")
    month = models.PositiveSmallIntegerField(help_text="Month of the holiday (1-12).")
    day = models.PositiveSmallIntegerField(help_text="Day of the month (1-31).")
    ramp_in_days = models.PositiveSmallIntegerField(
        default=0,
        help_text="How many days before the holiday to start mixing in holiday content.",
    )
    ramp_out_days = models.PositiveSmallIntegerField(
        default=0,
        help_text="How many days after the holiday to keep holiday content in rotation.",
    )
    max_share = models.DecimalField(
        max_digits=5, decimal_places=2, default=0,
        help_text="Max fraction of an hour's playlist that can be holiday tracks. 0.25 = 25%, 1.00 = 100%.",
    )
    max_weight_boost = models.PositiveSmallIntegerField(
        default=0,
        help_text="Extra rotation weight added to holiday-tagged tracks during the ramp period.",
    )

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
    additional_categories = models.ManyToManyField(
        Category, blank=True, related_name="secondary_tracks",
        help_text="Also eligible for rotation in these categories, on top of the "
                   "primary category above -- e.g. a blues-rock song filed under "
                   "Blues that should also play in the Rock rotation, without a "
                   "second physical copy of the file.",
    )
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
    file_hash = models.CharField(
        max_length=64, blank=True, db_index=True,
        help_text="SHA-256 of the audio file's bytes, computed once by find_duplicate_tracks "
                   "-- used to detect byte-identical copies filed under different categories.",
    )
    remote_url = models.URLField(blank=True)  # for syndicated/remote-hosted audio, not stored locally

    # --- Album art cache ---
    # Resolved once (embedded file art / Deezer / iTunes / none) and cached
    # here so repeat plays don't re-extract or re-query external APIs.
    # Album/Artist manual cover_art overrides are checked live, ahead of
    # this cache, and are never written into it (see album_art.py).
    ART_SOURCE_CHOICES = [
        ("embedded", "Embedded in file"),
        ("oakgrove", "Oak Grove Radio hosted art"),
        ("deezer", "Deezer"),
        ("itunes", "iTunes"),
        ("none", "None found"),
    ]
    art_url = models.CharField(max_length=1024, blank=True)
    art_source = models.CharField(max_length=20, choices=ART_SOURCE_CHOICES, blank=True)
    art_checked_at = models.DateTimeField(null=True, blank=True)

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
# DuplicateCandidate - review-only de-dup findings from find_duplicate_tracks
# ---------------------------------------------------------------

class DuplicateCandidate(models.Model):
    """A possible duplicate pair found by find_duplicate_tracks. Never
    deletes anything by itself -- resolution here is just a human decision
    recorded for apply_duplicate_resolutions to act on separately (that
    command has its own --apply flag; without it, it only reports what it
    WOULD do)."""
    CONFIDENCE_CHOICES = [
        ("exact", "Exact (identical file bytes)"),
        ("probable", "Probable (same title/artist/duration)"),
    ]
    RESOLUTION_CHOICES = [
        ("unresolved", "Unresolved"),
        ("keep_a", "Keep A, delete B"),
        ("keep_b", "Keep B, delete A"),
        ("keep_both", "Keep both (not actually a duplicate)"),
    ]

    track_a = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="duplicate_candidates_as_a")
    track_b = models.ForeignKey(Track, on_delete=models.CASCADE, related_name="duplicate_candidates_as_b")
    confidence = models.CharField(max_length=10, choices=CONFIDENCE_CHOICES)
    resolution = models.CharField(max_length=10, choices=RESOLUTION_CHOICES, default="unresolved")
    applied = models.BooleanField(default=False, help_text="Set by apply_duplicate_resolutions once it has actually acted on this resolution.")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-confidence", "-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["track_a", "track_b"], name="unique_duplicate_pair"),
        ]

    def __str__(self):
        return f"{self.track_a} <-> {self.track_b} ({self.confidence})"


# ---------------------------------------------------------------
# Rotation - ordered hour template of Category slots
# ---------------------------------------------------------------

class Rotation(models.Model):
    """Ordered list of Category slots that fills an hour. The log
    builder walks slots in position order, picking one Track per slot
    from that slot's Category (respecting recency)."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class RotationSlot(models.Model):
    """One position within a Rotation. `position` is maintained by the
    drag-to-sort admin inline — don't set it by hand.

    Exactly one of `category`/`track` is set. A `category` slot is the
    original behavior — the log builder randomly picks an eligible track
    from that category, respecting recency separation. A `track` slot is
    a direct insert (the hybrid rotation/playlist ask) — the log builder
    uses that exact track every time, completely skipping recency
    separation on the way in (even if it's a music track that would
    otherwise violate it). The resulting LogItem still gets a real
    scheduled_time, so it counts as "recently played" for any category
    slots that come after it, in this build or in future ones."""
    rotation = models.ForeignKey(Rotation, on_delete=models.CASCADE, related_name="slots")
    position = models.PositiveIntegerField(default=0, db_index=True)
    category = models.ForeignKey(Category, on_delete=models.PROTECT, related_name="rotation_slots", null=True, blank=True)
    track = models.ForeignKey(
        Track, on_delete=models.PROTECT, related_name="rotation_slot_inserts", null=True, blank=True,
        help_text="Direct track insert — bypasses category random-pick and recency checks for this slot.",
    )

    class Meta:
        # ordering[0] must be the per-parent position field, not the
        # parent FK — django-admin-sortable2 reads ordering[0] to find
        # the sort field. The parent rotation is implicit when an
        # inline filters by its parent.
        ordering = ["position"]
        unique_together = [("rotation", "position")]

    def clean(self):
        if bool(self.category_id) == bool(self.track_id):
            raise ValidationError("Set exactly one of category or track, not both or neither.")

    def __str__(self):
        target = self.category if self.category_id else f"[track] {self.track}"
        return f"{self.rotation} #{self.position} -> {target}"


# ---------------------------------------------------------------
# Playlist - hand-curated ordered list of Tracks
# ---------------------------------------------------------------

class Playlist(models.Model):
    """Curated ordered list of specific Tracks. The log builder copies
    items into LogItems in position order, no recency or pool picking."""
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class PlaylistItem(models.Model):
    """One position within a Playlist. `position` is maintained by the
    drag-to-sort admin inline — don't set it by hand."""
    playlist = models.ForeignKey(Playlist, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveIntegerField(default=0, db_index=True)
    track = models.ForeignKey(Track, on_delete=models.PROTECT, related_name="playlist_items")

    class Meta:
        # see RotationSlot.Meta.ordering — same reason
        ordering = ["position"]
        unique_together = [("playlist", "position")]

    def __str__(self):
        return f"{self.playlist} #{self.position} -> {self.track}"


# ---------------------------------------------------------------
# ScheduleBlock - maps a Rotation or Playlist onto real time
# ---------------------------------------------------------------

class ScheduleBlock(models.Model):
    """Maps either a Rotation (algorithmic, category slots) or a
    Playlist (curated tracks) onto a real time range.

    Time matching: either recurring weekly pattern (day_of_week set) or
    one-off override for a specific date (specific_date set). Exactly
    one of the two must be set — never both, never neither.

    Content: exactly one of (rotation, playlist) must be set — also
    enforced by check constraint.

    Precedence is implicit, not an explicit priority field: a
    ScheduleBlock with specific_date matching that date always wins
    over a recurring day_of_week match. Holiday specials and one-off
    remote broadcasts just override transparently."""
    DAY_CHOICES = [
        (0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"),
        (4, "Friday"), (5, "Saturday"), (6, "Sunday"),
    ]

    day_of_week = models.PositiveSmallIntegerField(choices=DAY_CHOICES, null=True, blank=True)
    specific_date = models.DateField(null=True, blank=True)
    start_time = models.TimeField()
    end_time = models.TimeField()
    rotation = models.ForeignKey(
        Rotation, on_delete=models.PROTECT,
        null=True, blank=True, related_name="schedule_blocks",
    )
    playlist = models.ForeignKey(
        Playlist, on_delete=models.PROTECT,
        null=True, blank=True, related_name="schedule_blocks",
    )

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
            models.CheckConstraint(
                condition=(
                    models.Q(rotation__isnull=False, playlist__isnull=True)
                    | models.Q(rotation__isnull=True, playlist__isnull=False)
                ),
                name="scheduleblock_exactly_one_of_rotation_or_playlist",
            ),
        ]

    @property
    def content(self):
        return self.rotation or self.playlist

    @property
    def content_kind(self):
        if self.rotation_id:
            return "rotation"
        if self.playlist_id:
            return "playlist"
        return None

    def __str__(self):
        when = self.specific_date if self.specific_date is not None else self.get_day_of_week_display()
        return f"{when} {self.start_time}-{self.end_time}: {self.content}"


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
    waveform_floor_db = models.FloatField(
        default=-50.0,
        verbose_name="Waveform display floor (dBFS)",
        help_text="Levels below this are invisible in the waveform display. Separate from detection thresholds.",
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
                "waveform_floor_db": -50.0,
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


class LogFillConfig(models.Model):
    """Singleton — how to top up a built log that falls short of a full
    hour (e.g. a playlist or rotation runs out early), so the engine never
    has to fall back to waiting/replaying at runtime."""
    STRATEGY_CHOICES = [
        ("repeat_last_category", "Repeat last category"),
        ("fixed_category", "Fixed category"),
    ]
    strategy = models.CharField(
        max_length=24, choices=STRATEGY_CHOICES, default="repeat_last_category",
        help_text="Which category to keep re-picking from when a log needs filling out.",
    )
    fallback_category = models.ForeignKey(
        "Category", null=True, blank=True, on_delete=models.SET_NULL,
        help_text="Used only when strategy is 'Fixed category'.",
    )

    class Meta:
        verbose_name = "Log Fill Configuration"
        verbose_name_plural = "Log Fill Configuration"

    def __str__(self):
        return "Log Fill Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class UITheme(models.Model):
    """Singleton — site-wide color palette and nav bar sizing, editable in admin."""
    bg_dark = models.CharField(max_length=50, default="#0f172a", help_text="Any CSS color value (hex, rgb, rgba).")
    bg_darker = models.CharField(max_length=50, default="#0b1120", help_text="Any CSS color value (hex, rgb, rgba).")
    panel_bg = models.CharField(max_length=50, default="rgba(15, 23, 42, 0.95)", help_text="Any CSS color value (hex, rgb, rgba).")
    accent = models.CharField(max_length=50, default="#22c55e", help_text="Any CSS color value (hex, rgb, rgba).")
    accent_soft = models.CharField(max_length=50, default="#4ade80", help_text="Any CSS color value (hex, rgb, rgba).")
    text_main = models.CharField(max_length=50, default="#f9fafb", help_text="Any CSS color value (hex, rgb, rgba).")
    text_muted = models.CharField(max_length=50, default="#9ca3af", help_text="Any CSS color value (hex, rgb, rgba).")
    danger = models.CharField(max_length=50, default="#f97373", help_text="Any CSS color value (hex, rgb, rgba).")
    border_subtle = models.CharField(max_length=50, default="rgba(148, 163, 184, 0.25)", help_text="Any CSS color value (hex, rgb, rgba).")

    nav_clock_font_size = models.CharField(max_length=20, default="1.25rem", help_text="Any CSS font-size value, e.g. 1.25rem.")
    nav_clock_font_weight = models.CharField(max_length=10, default="700", help_text="Any CSS font-weight value, e.g. 400, 600, 700.")
    nav_clock_color = models.CharField(max_length=50, default="rgba(249, 250, 251, 0.85)", help_text="Any CSS color value (hex, rgb, rgba).")

    logo = models.ImageField(upload_to="ui_theme/", blank=True, null=True, help_text="Nav bar logo. Leave blank to use the default IsadoraAir logo.")

    # --- Deck overlay (text sitting on top of album art) ---
    deck_text_shadow_color = models.CharField(
        max_length=50, default="rgba(0, 0, 0, 0.6)",
        help_text="Drop shadow behind deck title/artist/pills/etc. so text stays readable over lighter album art.",
    )
    deck_startsat_color = models.CharField(
        max_length=50, default="#3b82f6", help_text="Any CSS color value (hex, rgb, rgba).",
    )
    deck_pill_text_color = models.CharField(
        max_length=50, default="#9ca3af", help_text="Any CSS color value (hex, rgb, rgba).",
    )

    default_album_art = models.ImageField(
        upload_to="ui_theme/", blank=True, null=True,
        help_text="Shown on a deck when no album art is found anywhere in the lookup chain "
                   "(embedded/Oak Grove hosted art/Deezer/iTunes). Leave blank to show no art at all.",
    )

    class Meta:
        verbose_name = "UI Theme"
        verbose_name_plural = "UI Theme"

    def __str__(self):
        return "UI Theme"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
