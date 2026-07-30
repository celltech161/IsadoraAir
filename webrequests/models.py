from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models


class WebRequestConfig(models.Model):
    """Singleton -- master switch, availability grid, and rate/timeout
    knobs for the public-site song-request feature. Admin-editable
    (Config > Web Requests) and also surfaced on its own staff-only
    /web-request/ page since the availability grid needs a real
    week/hour clickable widget, not a form field.

    The grid here is deliberately an ALLOW-list (open_slots), not a
    block-list like Track.blocked_slots -- opposite default polarity on
    purpose. Track.blocked_slots defaults to "everything open" (empty
    list = no restrictions) because that field governs an
    already-curated library where the safe default is "don't
    surprise-restrict anything." This feature is off by default
    (enabled=False) and starts with NO hours open (empty open_slots)
    until an operator deliberately picks which hours accept requests --
    the safe default for a brand-new, publicly-reachable feature is
    "closed" not "wide open."

    Slot encoding matches Track.blocked_slots exactly: day_of_week*24 +
    hour, day_of_week 0=Monday (Python's .weekday() convention), station
    -local time per StationTimeConfig. Synced to the public website
    alongside the catalog push so the request page can show "closed
    right now" without a live round-trip per visitor."""

    enabled = models.BooleanField(
        default=False,
        help_text="Master switch. Off by default -- nothing is synced to the "
                   "public site, nothing is polled, nothing is fulfilled, "
                   "until this is on.",
    )
    open_slots = ArrayField(
        models.PositiveSmallIntegerField(), blank=True, default=list,
        help_text="Hour x day-of-week slots OPEN for requests, encoded as "
                   "day_of_week*24 + hour (0-167, Monday=0), station-local "
                   "time -- same encoding as Track.blocked_slots. Empty = "
                   "no hours open (safe default). Opposite polarity from "
                   "Track.blocked_slots -- this is an allow-list, not a "
                   "block-list.",
    )
    max_fulfilled_per_hour = models.PositiveSmallIntegerField(
        default=4,
        help_text="Ceiling on how many requests get swapped into music slots "
                   "within any rolling clock hour, so automatic rotation "
                   "isn't entirely displaced by a run of requests. Requests "
                   "beyond the cap simply stay pending and roll into the "
                   "next hour's budget rather than being rejected.",
    )
    lookahead_warning_minutes = models.PositiveSmallIntegerField(
        default=60,
        help_text="If no eligible music-kind slot is found within this many "
                   "minutes of a request arriving (checked against both the "
                   "availability grid and each candidate hour's actual "
                   "resolved Rotation), the request is reported to the "
                   "public site as 'no_slot_soon' instead of 'pending' -- "
                   "still queued and still eligible to fulfill, just "
                   "flagged so the site can set the requester's "
                   "expectations. Re-evaluated every cycle, so a request "
                   "can move between pending and no_slot_soon as the "
                   "lookahead window's contents change.",
    )
    expire_after_hours = models.PositiveSmallIntegerField(
        default=6,
        help_text="A request that's neither fulfilled nor found a near-term "
                   "slot within this many hours of submission is marked "
                   "'expired' and stops being retried.",
    )
    notify_email = models.EmailField(
        blank=True, default="",
        help_text="Address for web-requests-ingest pipeline failure notifications. Blank disables.",
    )

    class Meta:
        verbose_name = "Web Request Configuration"
        verbose_name_plural = "Web Request Configuration"
        indexes = [GinIndex(fields=["open_slots"])]

    def __str__(self):
        return f"Web Request Configuration ({'enabled' if self.enabled else 'disabled'})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class SongRequest(models.Model):
    """One listener-submitted song request, mirrored locally from the
    public website's own request record. IsadoraAir never talks to the
    public site's database directly -- a poller script creates one row
    here per still-open request it sees on each poll (see
    lib/webrequests_client.py on the ingest side), and the engine's own
    queue-advance logic is the only thing that ever fulfills one, by
    swapping the requested track into the next upcoming music-kind
    LogItem.

    external_request_id is the PUBLIC SITE's own primary key for the
    submission (their side, not ours) -- our dedup key across repeated
    polls, exactly like OGRemote's upload row ids. Kept as a string
    rather than assuming their id scheme is a plain integer forever.

    status values:
      pending      -- queued, no near-term-slot concern, still eligible
      no_slot_soon -- queued, but no eligible slot found within
                      WebRequestConfig.lookahead_warning_minutes as of
                      the last check. NOT terminal -- re-evaluated every
                      cycle, flips back to pending if a slot enters the
                      lookahead window.
      fulfilled    -- swapped into a LogItem; log_item is set.
      unavailable  -- track became ineligible before its turn came up
                      (deleted, ready2air flipped off, recategorized out
                      of music, etc.) -- distinct from expired because
                      the reason is different and the public site should
                      say something different to the requester.
      expired      -- sat unfulfilled past WebRequestConfig.expire_after_hours
                      with no track-level problem -- just never got a
                      turn.

    dedication_message is captured now (phase 1) but not acted on yet --
    phase 2 formats it with requester_name, synthesizes it via Kokoro,
    and inserts it as a one-off spoken intro ahead of the requested
    track. That mechanism is intentionally not built yet; this field is
    just making sure the data exists when it's time to build it."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("no_slot_soon", "No slot soon"),
        ("fulfilled", "Fulfilled"),
        ("unavailable", "Unavailable"),
        ("expired", "Expired"),
    ]

    NON_TERMINAL_STATUSES = ("pending", "no_slot_soon")

    external_request_id = models.CharField(max_length=64, unique=True, db_index=True)
    track = models.ForeignKey(
        "library.Track", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="song_requests",
    )
    requester_name = models.CharField(max_length=100, blank=True, default="")
    dedication_message = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True)

    # Website's own submission timestamp (UTC), NOT when our poller
    # happened to fetch it -- that's fetched_at below. Estimated-play-
    # time math and expire_after_hours both anchor off this.
    submitted_at = models.DateTimeField()
    fetched_at = models.DateTimeField(auto_now_add=True)
    fulfilled_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set the moment status moves into ANY terminal value "
                   "(fulfilled / expired / unavailable) -- distinct from "
                   "fulfilled_at, which only covers the fulfilled case. "
                   "Drives the status-push safety window: a terminal "
                   "status keeps getting reported for a while after "
                   "resolved_at, not just once, so a single dropped push "
                   "can't leave the public site showing stale status.",
    )

    log_item = models.ForeignKey(
        "library.LogItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fulfilled_song_request",
        help_text="The specific LogItem this request was swapped into, once fulfilled.",
    )
    estimated_play_time = models.DateTimeField(
        null=True, blank=True,
        help_text="While pending: a best-guess air time, recomputed every "
                   "refresh_song_request_statuses cycle by checking each "
                   "upcoming open, music-kind LogItem for real eligibility "
                   "(recency included) rather than just chronological order. "
                   "Advisory only until fulfilled -- the actual fulfillment "
                   "can land in a different slot than a given cycle's guess. "
                   "Once fulfilled: the real, certain scheduled_time of the "
                   "LogItem it landed in, not a guess. Null while status is "
                   "no_slot_soon, expired, or unavailable.",
    )

    class Meta:
        ordering = ["submitted_at"]
        verbose_name = "Song Request"
        verbose_name_plural = "Song Requests"

    def __str__(self):
        track_label = str(self.track) if self.track_id else "(track removed)"
        return f"[{self.status}] {self.requester_name or 'anonymous'}: {track_label}"
