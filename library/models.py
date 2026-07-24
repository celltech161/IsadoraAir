from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex


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
    next_start_threshold_db_override = models.FloatField(
        null=True, blank=True,
        verbose_name="Next Start Threshold (dBFS)",
        help_text="Override: dBFS threshold analyze_tracks uses to pick the "
                   "next-start trigger point on tracks in this category. Blank = "
                   "use the global AnalysisConfig default. Set MORE NEGATIVE "
                   "(quieter, e.g. -35 dBFS vs the default -26) for programme "
                   "material that runs quiet -- classical, ambient -- so we "
                   "don't fire the next track over the natural tail.",
    )
    cue_in_threshold_db_override = models.FloatField(
        null=True, blank=True,
        verbose_name="Cue In Threshold (dBFS)",
        help_text="Override: dBFS threshold for the cue-in point (first "
                   "audible content) on tracks in this category. Blank = use "
                   "global. Same reasoning as Next Start Threshold above -- "
                   "quiet material needs a quieter threshold, otherwise the "
                   "cue-in lands too far into the intro.",
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
    isrc = models.CharField(
        max_length=15, blank=True, default="",
        help_text="International Standard Recording Code (12 chars, format "
                    "CC-XXX-YY-NNNNN). Required by SoundExchange for royalty "
                    "reporting; falls back to album + record label when missing. "
                    "Auto-populated from ID3 TSRC / Vorbis ISRC at import if "
                    "present in the file's tags; can be filled in later via "
                    "the backfill_isrc management command or edited by hand.",
    )
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
    rotation_weight = models.PositiveSmallIntegerField(
        default=3,
        help_text="0-5. Relative likelihood of being picked within its category "
                   "(a 5 is ~6x as likely as a 0, not a guarantee) -- doesn't "
                   "override recency separation. Use ready2air to exclude a "
                   "track from rotation entirely.",
    )
    ready2air = models.BooleanField(default=False)  # gate: must be human-reviewed before entering rotation
    blocked_slots = ArrayField(
        models.PositiveSmallIntegerField(), blank=True, default=list,
        help_text="Hour x day-of-week slots blocked from auto-rotation, "
                   "encoded as day_of_week*24 + hour (day_of_week: Monday=0, "
                   "matching Python's .weekday() and ScheduleBlock's own "
                   "convention). Empty = always available.",
    )
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

    # Populated by api_library_upload when a user in the Contributor
    # group (or any other future upload-capable role) submits a track.
    # NULL for historical tracks and for tracks imported via
    # management commands / CD ripping / syndicated ingest -- i.e.
    # anything that predates the Contributor feature. Displayed on
    # /track/<pk>/ so the reviewing operator can see who to follow up
    # with, and used by the middleware/view gating to enforce
    # "Contributors can delete their own not-yet-approved uploads."
    # SET_NULL on user delete so removing a former Contributor leaves
    # their contributed tracks in the library instead of cascading
    # them out.
    uploaded_by = models.ForeignKey(
        "auth.User", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="uploaded_tracks",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["artist", "title"]
        indexes = [
            models.Index(fields=["category", "ready2air"]),
            GinIndex(fields=["blocked_slots"]),
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
    RotationSlot (by weight) -> Category -> Track.

    track_title/track_artist are a snapshot of the track's identity at
    the moment this LogItem was created -- kept independent of the live
    Track row so a historical log entry stays readable (what actually
    aired) even after the underlying Track/file is deleted (e.g. a
    library reorganization). track itself is SET_NULL rather than
    PROTECT for the same reason: old play history shouldn't block
    deleting a track that's no longer wanted."""
    playlist_log = models.ForeignKey(PlaylistLog, on_delete=models.CASCADE, related_name="items")
    position = models.PositiveIntegerField()
    scheduled_time = models.DateTimeField()
    track = models.ForeignKey(Track, on_delete=models.SET_NULL, null=True, blank=True, related_name="log_items")
    track_title = models.CharField(max_length=255, blank=True, default="")
    track_artist = models.CharField(max_length=255, blank=True, default="")
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, blank=True)
    played_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["playlist_log", "position"]
        unique_together = [("playlist_log", "position")]

    def __str__(self):
        return f"{self.playlist_log.date} #{self.position}: {self.track_title or self.track}"


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
    waveform_left_color = models.CharField(
        max_length=7,
        default="#22c55e",
        verbose_name="Left channel color",
        help_text="Waveform color for the left channel (drawn above the zero line).",
    )
    waveform_right_color = models.CharField(
        max_length=7,
        default="#38bdf8",
        verbose_name="Right channel color",
        help_text="Waveform color for the right channel (drawn below the zero line).",
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


class UploadConfig(models.Model):
    """Singleton — limits for the drag-and-drop track upload page
    (library/import/). Default (100) matches the real-world limit that
    was already in effect before this field existed (nginx's
    client_max_body_size), so nothing silently changes for existing
    installs until someone deliberately raises it."""
    max_batch_size_mb = models.PositiveIntegerField(
        default=100,
        verbose_name="Max batch size (MB)",
        help_text="Maximum combined size of all files in one upload action "
                   "(all files dragged/selected together count as one "
                   "batch, not per individual track). Enforced by the app "
                   "itself on top of nginx's own hard ceiling "
                   "(client_max_body_size, set in "
                   "/etc/nginx/sites-available/isadoraair) -- raising this "
                   "above that nginx value has no effect, since nginx "
                   "rejects the request before Django ever sees it.",
    )

    class Meta:
        verbose_name = "Upload Configuration"
        verbose_name_plural = "Upload Configuration"

    def __str__(self):
        return "Upload Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1, defaults={"max_batch_size_mb": 100})
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
    station_logo = models.ImageField(upload_to="ui_theme/", blank=True, null=True, help_text="Station logo shown just below the IsadoraAir logo on the login screen. Leave blank to hide.")

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


def _timezone_choices():
    """All IANA timezones the running Python knows about, alphabetized.
    Called lazily (not at import time) so a system with a corrupted
    tzdata install still lets the migration path run to the point of
    a diagnostic error instead of failing at model definition.
    Wrapped defensively -- `available_timezones()` requires a working
    IANA database. On the vanishingly unlikely event that fails, we
    fall back to a small canonical shortlist that at least covers the
    US market this project targets by default."""
    try:
        from zoneinfo import available_timezones
        return sorted((tz, tz) for tz in available_timezones())
    except Exception:
        return [
            ("America/Chicago", "America/Chicago"),
            ("America/New_York", "America/New_York"),
            ("America/Denver", "America/Denver"),
            ("America/Los_Angeles", "America/Los_Angeles"),
            ("America/Anchorage", "America/Anchorage"),
            ("Pacific/Honolulu", "Pacific/Honolulu"),
            ("UTC", "UTC"),
        ]


class StationTimeConfig(models.Model):
    """Singleton -- the station's operating timezone, displayed in every
    UI surface regardless of the viewing device's local timezone. Live-
    caught 2026 on a Mountain Time vacation: the operator's phone-side
    time rendering (schedule current-hour highlight, dashboard clocks)
    disagreed with what was actually playing back at the studio.

    The commit that first fixed this read Django's settings.TIME_ZONE
    directly (compile-time; requires a code edit + gunicorn restart).
    Moving the source of truth to this admin-editable singleton means
    a fresh deploy in any timezone just picks it from the dropdown --
    no code touch, no restart.

    Implementation note: Django's settings.TIME_ZONE is baked at process
    startup and can't be swapped at runtime. What CAN be swapped is the
    ACTIVE timezone (django.utils.timezone.activate), the officially-
    supported per-request timezone control mechanism Django exposes.
    library.middleware.StationTimeActivateMiddleware activates this
    singleton's value at the start of every request; all
    django.utils.timezone calls, template date/time filters, and
    admin datetime renders respect that automatically. Stored
    timestamps stay UTC (USE_TZ=True default); only display shifts.

    If this row doesn't exist yet on a very fresh install (before the
    seed migration runs), load() falls back to settings.TIME_ZONE
    itself -- never a broken state."""
    timezone = models.CharField(
        max_length=64,
        choices=_timezone_choices,
        default="America/Chicago",
        verbose_name="Station timezone",
        help_text="The station's operating timezone. Every UI clock, "
                   "the /schedule/ current-hour highlight, Coming Up "
                   "queue ETAs, and event timestamps render in this "
                   "zone regardless of the viewing device's local "
                   "timezone. Applied at request time via "
                   "django.utils.timezone.activate; stored DB timestamps "
                   "stay UTC and are unchanged by edits here.",
    )

    class Meta:
        verbose_name = "Station Time"
        verbose_name_plural = "Station Time"

    def __str__(self):
        return f"Station Time ({self.timezone})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        from django.conf import settings
        obj, _created = cls.objects.get_or_create(
            pk=1, defaults={"timezone": getattr(settings, "TIME_ZONE", "UTC")},
        )
        return obj


class GroupAccess(models.Model):
    """Per-group access rules for the site's URL surface. Middleware
    (library.middleware.GroupBasedAccessMiddleware) reads this table
    and unions the entries for every recognized group a user belongs
    to; any request path in the union is permitted. Staff/superuser
    bypass this table entirely.

    Designed to be admin-editable (inline on the standard auth.Group
    admin page) so a fresh deployment can wire up its own group
    scheme without touching code -- names, allowed paths, landing
    pages are all data. Default configurations for `remote_dj` and
    `Contributor` ship as a data migration.

    Three parallel path-list fields (one URL per line):
      allowed_prefixes -- request.path.startswith(one_of_these)
      allowed_exact    -- request.path == one_of_these literally
      allowed_regex    -- re.match(one_of_these, request.path)

    A group's total allowed set is the union across all three lists.
    Leave any field blank if not needed.
    """
    group = models.OneToOneField(
        "auth.Group", on_delete=models.CASCADE, related_name="access",
        help_text="The Django auth Group this access set applies to.",
    )
    allowed_prefixes = models.TextField(
        blank=True,
        help_text="One path prefix per line. A request whose path startswith any listed prefix is allowed. Example lines: /library/  /api/tracks/",
    )
    allowed_exact = models.TextField(
        blank=True,
        help_text="One exact path per line. The request path must equal the listed value literally (no startswith). Example lines: /api/tracks/  /api/engine/queue/insert/",
    )
    allowed_regex = models.TextField(
        blank=True,
        help_text="One Python regex per line, matched via re.match against the request path. For parameterized URLs a plain prefix or exact match can't cover. Example: ^/api/playlists/\\d+/play-now/$",
    )
    landing_url = models.CharField(
        max_length=200, blank=True,
        help_text="Where members of this group are redirected on '/' or on a denied page (page-shaped requests only; APIs still 403). Raw URL path, e.g. '/library/'. Blank = redirect to /welcome/.",
    )
    priority = models.IntegerField(
        default=100,
        help_text="Lower number wins when a user in multiple groups needs a landing page. Ties broken by group name (alphabetical).",
    )

    class Meta:
        verbose_name = "Group Access"
        verbose_name_plural = "Group Access"
        ordering = ["priority", "group__name"]

    def __str__(self):
        return f"Access for {self.group.name}"

    @staticmethod
    def _lines(text):
        return tuple(ln.strip() for ln in (text or "").splitlines() if ln.strip())

    def prefix_list(self):
        return self._lines(self.allowed_prefixes)

    def exact_set(self):
        return frozenset(self._lines(self.allowed_exact))

    def regex_list(self):
        import re
        return tuple(re.compile(p) for p in self._lines(self.allowed_regex))


class NavMenuItem(models.Model):
    """A row in the site nav bar (templates/base.html), admin-editable --
    not a singleton itself (there are many rows), but grouped into the
    admin's Config section same as the singletons above. Supports exactly
    one level of nesting (parent -> children, i.e. a single dropdown
    level) -- deeper nesting isn't worth the added template/CSS complexity
    for a site nav this size.

    Exactly one of url_name/custom_url should be set for a clickable item;
    both blank is valid too (a parent with children but no link of its
    own, rendered as a non-clickable dropdown label)."""
    label = models.CharField(max_length=100)
    parent = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.CASCADE, related_name="children",
        limit_choices_to={"parent__isnull": True},
        help_text="Leave blank for a top-level item. Only one level of nesting is supported.",
    )
    url_name = models.CharField(
        max_length=100, blank=True,
        help_text="A Django URL name, e.g. 'library:schedule' or 'monitoring:dashboard' -- "
                   "resolved fresh on every render, so it stays correct even if the "
                   "underlying URL path changes. Leave blank if using Custom URL instead.",
    )
    custom_url = models.CharField(
        max_length=500, blank=True,
        help_text="A raw URL (internal path or external site) for links not backed by a "
                   "Django URL name. Leave blank if using URL Name instead.",
    )
    extra_active_view_names = models.CharField(
        max_length=500, blank=True,
        help_text="Comma-separated Django view names (e.g. "
                   "'library:library-import,library:track-detail') that should ALSO "
                   "highlight this item as active, beyond its own URL Name -- for a nav "
                   "item that's the entry point to a whole section with sub-pages of "
                   "their own.",
    )
    sort_order = models.PositiveIntegerField(default=0)
    enabled = models.BooleanField(default=True, help_text="Uncheck to hide without deleting.")
    open_in_new_tab = models.BooleanField(default=False)

    class Meta:
        ordering = ["sort_order"]
        verbose_name = "Nav Menu Item"
        verbose_name_plural = "Nav Menu"

    def __str__(self):
        return self.label

    def clean(self):
        if self.url_name and self.custom_url:
            raise ValidationError("Set either URL Name or Custom URL, not both.")
        if self.parent_id and self.parent.parent_id:
            raise ValidationError("Nav items support only one level of nesting -- "
                                   "this item's chosen parent is itself a child.")
        if self.url_name:
            from django.urls import NoReverseMatch, reverse
            try:
                reverse(self.url_name)
            except NoReverseMatch as exc:
                raise ValidationError({"url_name": f"Doesn't resolve: {exc}"})

    @property
    def resolved_url(self):
        if self.url_name:
            from django.urls import reverse
            return reverse(self.url_name)
        return self.custom_url or None


class RemoteDJConfig(models.Model):
    """Singleton -- master switch and WebRTC tunables for Remote DJ over
    WebRTC. Off by
    default -- "land the code, don't flip it live," same pattern as
    OGRemoteConfig.enabled. Landing this while disabled means engine.py's
    pipeline topology is byte-for-byte unchanged from today (no new tee,
    no new pads) until this is deliberately turned on."""
    enabled = models.BooleanField(
        default=False,
        help_text="Master switch. While off, the engine's pipeline is "
                   "completely unchanged (no remote-DJ tee or pads exist "
                   "at all) and the signaling server isn't started.",
    )
    stun_server = models.CharField(
        max_length=200, default="stun://stun.l.google.com:19302",
        help_text="Public STUN server for ICE. Sufficient unless both ends "
                   "are behind symmetric NAT -- a TURN relay (coturn) is "
                   "deliberately not set up ahead of a real need for one.",
    )
    ice_udp_min_port = models.PositiveIntegerField(
        default=40000,
        help_text="Pinned to a small explicit range (rather than the wide "
                   "ephemeral default) so router UDP forwarding has a known, "
                   "small target.",
    )
    ice_udp_max_port = models.PositiveIntegerField(default=40010)

    class Meta:
        verbose_name = "Remote DJ Configuration"
        verbose_name_plural = "Remote DJ Configuration"

    def __str__(self):
        return "Remote DJ Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class EmailLog(models.Model):
    """One row per outgoing email, written by
    library.email_backend.LoggingSMTPBackend regardless of call site
    (password resets, monitoring alerts, anything using Django's normal
    mail API) -- there was previously no way to see what an email
    actually contained after it was sent, which directly caused a real
    incident: an admin-triggered password-reset invite went out with a
    broken link (built from the wrong Host header) and nobody could
    tell without re-deriving it by hand. Now every send is visible,
    read-only, in the admin."""
    to = models.CharField(max_length=500, help_text="Comma-separated if more than one recipient.")
    from_email = models.CharField(max_length=255, blank=True)
    subject = models.CharField(max_length=500, blank=True)
    body = models.TextField(blank=True)
    success = models.BooleanField(default=True)
    error = models.TextField(blank=True)
    # Indexed because both the default ordering below AND the retention
    # prune query filter on it -- as this table grows, the index keeps
    # both cheap without needing to revisit anything.
    sent_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-sent_at"]
        verbose_name = "Email Log"
        verbose_name_plural = "Email Log"

    def __str__(self):
        return f"{self.sent_at:%Y-%m-%d %H:%M} -> {self.to}: {self.subject}"


class CDRipJob(models.Model):
    """One CD-rip attempt. Only one is ever active at a time (there's
    one drive), but we keep completed rows around for at least a bit
    so the operator can see what came off recent discs. Progress is
    written by the detached `cd_rip_run` management command; the
    /api/cd/rip-status/ endpoint reads directly from this row."""
    STATE_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("done", "Done"),
        ("error", "Error"),
        ("cancelled", "Cancelled"),
    ]
    state = models.CharField(max_length=12, choices=STATE_CHOICES, default="pending")
    disc_id = models.CharField(max_length=64, blank=True)
    mb_release_id = models.CharField(max_length=64, blank=True)
    device = models.CharField(max_length=64, default="/dev/sr0")
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT, related_name="+",
        help_text="Category all ripped tracks land under.",
    )
    # Operator-edited album/track metadata frozen at rip-start; the
    # detached child reads this to tag each output FLAC and to build
    # per-track Artist/Album/etc rows. Shape matches the detect
    # endpoint's response.
    album_meta = models.JSONField(default=dict, blank=True)
    staging_dir = models.CharField(max_length=512, blank=True)
    whipper_pid = models.IntegerField(null=True, blank=True)
    progress_current_track = models.PositiveIntegerField(default=0)
    progress_total_tracks = models.PositiveIntegerField(default=0)
    status_message = models.CharField(max_length=500, blank=True)
    error_message = models.TextField(blank=True)
    # Track ids the rip landed successfully -- lets the UI link
    # straight to /track/<pk>/ pages for review.
    created_track_ids = models.JSONField(default=list, blank=True)
    accurate_rip_matches = models.JSONField(
        default=list, blank=True,
        help_text="Per-track AccurateRip check outcome, in track order: "
                  "'match', 'nomatch', 'notfound', or '' for tracks that "
                  "hadn't completed yet.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "CD Rip Job"
        verbose_name_plural = "CD Rip Jobs"

    def __str__(self):
        return f"CDRipJob #{self.id} ({self.state}) {self.disc_id[:12]}"


class CDRipConfig(models.Model):
    """Singleton -- CD drive characteristics and rip pipeline knobs.
    Runtime-editable in admin, so operators can dial in a new drive's
    offset without redeploying. Migration seeds it with the default
    for the current install (hp PLDS DVDRW DU8AESH, offset +6)."""
    device = models.CharField(
        max_length=64, default="/dev/sr0",
        help_text="Path to the CD drive device node (usually /dev/sr0).",
    )
    drive_read_offset = models.IntegerField(
        default=6,
        help_text="AccurateRip drive read offset in samples for THIS "
                  "drive model. Look up your drive at "
                  "accuraterip.com/driveoffsets.htm. Wrong offset means "
                  "the audio bytes are shifted vs community reference "
                  "rips, so every track gets 'AR nomatch' -- the audio "
                  "still plays fine, but the byte-identical verification "
                  "won't pass. Default (+6) is correct for the hp PLDS "
                  "DVDRW DU8AESH shipped with this box.",
    )
    require_accurate_rip = models.BooleanField(
        default=False,
        help_text="If checked, tracks that don't verify against "
                  "AccurateRip are rejected outright. If unchecked (the "
                  "default), non-verifying tracks are still imported "
                  "with a warning badge -- appropriate since many CD-Rs "
                  "and older pressings aren't in the AccurateRip "
                  "database and their rips are still perfectly usable.",
    )
    staging_root = models.CharField(
        max_length=512, default="/srv/isadoraair/rip_staging",
        help_text="Scratch directory for in-progress rips before the "
                  "post-processor moves them into the library. Must be "
                  "writable by the gunicorn service user.",
    )

    class Meta:
        verbose_name = "CD Rip Configuration"
        verbose_name_plural = "CD Rip Configuration"

    def __str__(self):
        return "CD Rip Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class PlayEvent(models.Model):
    """Append-only ledger of every track that actually hit the mixer.
    Written by the playback engine at _create_deck (started_at + all
    snapshot fields set) and updated at _remove_deck (ended_at +
    duration_played_seconds set). Distinct from LogItem.played_at
    because retention differs -- programming logs (PlaylistLog /
    LogItem) may be pruned to save space, but play evidence must be
    retained for statutory-license reporting audits (SoundExchange
    NCE etc), typically for the greater of the statute of
    limitations and the licensor's own retention requirement.

    Snapshot fields (track_title, track_artist, album_title,
    record_label, isrc, category_kind) are captured at write time and
    never mutated by application code -- so a later admin rename, a
    Track deletion, or a CategoryKind edit cannot corrupt historical
    rows. `track` FK is SET_NULL for audit-lookup convenience only;
    the snapshot fields are the load-bearing reporting data.

    Not admin-editable (see PlayEventAdmin has_add/change/delete_perm
    overrides) -- read-only from the outside.

    The 30-second SoundExchange threshold is applied at REPORT time
    (query filter on duration_played_seconds), not at write time --
    that way short-cut plays still exist as evidence for the
    operator's own bookkeeping and only get excluded from the PRO
    export."""

    SOURCE_CHOICES = [
        ("scheduled", "Scheduled"),
        ("insert", "Manual insert / remote-DJ"),
        ("playlist_play_now", "Playlist Play Now"),
    ]

    track = models.ForeignKey(
        Track, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="play_events",
    )

    # Snapshot fields -- immutable once written. Kept independent of
    # live Track / Artist / Album / Category so history stays intact
    # through any downstream rename or delete.
    track_title = models.CharField(max_length=255, blank=True, default="")
    track_artist = models.CharField(max_length=255, blank=True, default="")
    album_title = models.CharField(max_length=255, blank=True, default="")
    record_label = models.CharField(max_length=255, blank=True, default="")
    isrc = models.CharField(max_length=15, blank=True, default="")
    category_kind = models.CharField(max_length=64, blank=True, default="")

    source = models.CharField(
        max_length=32, choices=SOURCE_CHOICES, default="scheduled",
    )

    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_played_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["started_at", "category_kind"]),
        ]
        ordering = ["-started_at"]

    def __str__(self):
        return (
            f"{self.started_at:%Y-%m-%d %H:%M:%S} "
            f"[{self.category_kind or 'none'}] "
            f"{self.track_artist} -- {self.track_title}"
        )


class IcecastSample(models.Model):
    """One point-in-time sample of Icecast listener counts, for
    Aggregate Tuning Hours computation on royalty reports.

    Written every minute by the sample_icecast_listeners management
    command via isadoraair-sample-icecast.timer. One row per sample
    (not per-mount) -- `listeners_by_mount` is a JSONField so a whole
    server's mount table lands in a single row rather than N rows
    every minute; per-mount rollups happen at report time.

    ATH computation (library/services/royalty_reports.py compute_ath)
    integrates listeners * dt across the reporting period, treating
    each sample as instantaneous and its `listeners_total` as
    representative until the next sample. Handles irregular sampling
    gracefully by using the actual dt between consecutive samples,
    capped at 1h to prevent a single sample from dominating after a
    sampler outage. Missing samples for the full period returns 0.

    Not admin-editable (would corrupt the ATH baseline).
    Auto-pruned after 18 months by isadoraair-prune-icecast.timer --
    same rationale as the EmailLog retention window; SoundExchange
    audit lookback is typically 3 years but we snapshot the derived
    ATH into each RoyaltyReport row at generation time, so the
    underlying samples become disposable once a report is generated
    for the period. Keep the timer disabled until we're comfortable
    with the derived report accuracy."""

    sampled_at = models.DateTimeField(auto_now_add=True, db_index=True)
    listeners_total = models.PositiveIntegerField(default=0)
    listeners_by_mount = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-sampled_at"]
        verbose_name = "Icecast Listener Sample"
        verbose_name_plural = "Icecast Listener Samples"

    def __str__(self):
        return f"{self.sampled_at:%Y-%m-%d %H:%M:%S} total={self.listeners_total}"


class RoyaltyReport(models.Model):
    """Metadata + persisted output file for one generated royalty
    report. Written both by the management command (when invoked with
    --persist) and the /reports/ web UI. Kept out of the PlayEvent
    ledger deliberately -- PlayEvents are the raw evidence, this is a
    derivative snapshot from that evidence at a specific moment. If
    PlayEvents change (they shouldn't, but the schema doesn't forbid
    an admin from bulk-deleting old rows) the report file stays
    accurate to what was submitted to the licensor.

    Files land in MEDIA_ROOT/royalty_reports/. Filename is
    <period_start>-<format>.<ext>, e.g. 2026-07-01-soundexchange_nce.csv.
    Access-gated (view side only allows staff/superuser to see or
    generate); the underlying file is served through the view, not
    directly by nginx, so an accidentally-leaked URL doesn't bypass
    the auth check."""

    FORMAT_CHOICES = [
        ("soundexchange_nce", "SoundExchange NCE (Report of Use)"),
        ("summary", "Summary (human-readable stats)"),
        ("raw_csv", "Raw CSV (every PlayEvent, all snapshot fields)"),
    ]

    period_start = models.DateField(
        help_text="First day of the reporting period (typically the 1st of the month).",
    )
    period_end = models.DateField(
        help_text="Last day of the reporting period (last day of the month).",
    )
    format = models.CharField(max_length=32, choices=FORMAT_CHOICES)

    generated_at = models.DateTimeField(auto_now_add=True)
    generated_by = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="royalty_reports_generated",
    )

    # Snapshotted stats -- lets the /reports/ list page render without
    # a full PlayEvent re-scan per row. Same rationale as the snapshot
    # fields on PlayEvent itself.
    total_plays = models.PositiveIntegerField(default=0)
    unique_tracks = models.PositiveIntegerField(default=0)
    unique_artists = models.PositiveIntegerField(default=0)
    plays_with_isrc = models.PositiveIntegerField(default=0)

    # Aggregate Tuning Hours embedded in the NCE report header. Either
    # computed from IcecastSample rows in the period (ath_computed),
    # or manually overridden by the operator on the /reports/ form
    # (ath_override). If both present, override wins and ath_used
    # reflects that. Kept as separate fields (not just one "ath")
    # so an operator later checking a submitted report can see WHAT
    # value went out and WHY -- was it computed or overridden?
    ath_computed = models.FloatField(null=True, blank=True)
    ath_override = models.FloatField(null=True, blank=True)
    ath_used = models.FloatField(null=True, blank=True)

    file = models.FileField(upload_to="royalty_reports/", blank=True, null=True)

    class Meta:
        ordering = ["-period_start", "-generated_at"]
        verbose_name = "Royalty Report"
        verbose_name_plural = "Royalty Reports"

    def __str__(self):
        return (
            f"{self.period_start:%Y-%m} {self.get_format_display()} "
            f"({self.total_plays} plays, generated {self.generated_at:%Y-%m-%d})"
        )

    @property
    def isrc_percent(self):
        if not self.total_plays:
            return 0.0
        return 100.0 * self.plays_with_isrc / self.total_plays
