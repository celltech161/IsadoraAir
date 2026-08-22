from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone


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
    public site's database directly -- the integrated ingest command creates one row
    here per still-open request it sees on each poll, and the engine's own
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
      scheduled    -- assigned to a specific LogItem and expected to air;
                      log_item is set. NOT terminal -- a scheduled slot
                      can still be lost (an hour-boundary rollover can
                      discard it before it plays), in which case
                      refresh_song_request_statuses reverts it back to
                      pending/no_slot_soon rather than leaving it stuck.
      fulfilled    -- the track has ACTUALLY started playing (set from
                      the engine's real air-start event, LogItem.played_at
                      -- see library.services.engine._create_deck /
                      webrequests.services.mark_song_requests_aired).
                      log_item is (usually still) set. This is
                      deliberately NOT the same moment as scheduling --
                      an earlier version of this feature conflated the
                      two, which meant the public site could be told a
                      request was "fulfilled" minutes before the song
                      actually aired, or -- if the assigned slot got
                      discarded by an hour rollover before its turn --
                      never at all.
      unavailable  -- track became ineligible before its turn came up
                      (deleted, ready2air flipped off, recategorized out
                      of music, its file went missing, etc.) -- distinct
                      from expired because the reason is different and
                      the public site should say something different to
                      the requester.
      expired      -- sat unfulfilled past WebRequestConfig.expire_after_hours
                      with no track-level problem -- just never got a
                      turn.

    dedication_message, together with requester_name and the requested
    track, is formatted into a spoken intro synthesized via Kokoro
    (voice am_fenrir) and spliced immediately ahead of the requested
    track once it airs -- see intro_track/intro_log_item below,
    webrequests.services.build_dedication_intro_text/synthesize_
    dedication_intro, and library.services.engine's dedication-splice
    machinery (_maybe_insert_dedication_intro and friends). Delivery is
    best-effort: a song can air with no intro (synthesis not ready in
    time, contention, the engine's own last-second scheduling safety
    net), but once an intro starts airing its song is guaranteed to
    follow -- never the reverse."""

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("no_slot_soon", "No slot soon"),
        ("scheduled", "Scheduled"),
        ("fulfilled", "Fulfilled"),
        ("unavailable", "Unavailable"),
        ("expired", "Expired"),
    ]

    # Three explicit groups, replacing the old NON_TERMINAL_STATUSES --
    # that single constant used to get reused for both "still eligible
    # to be assigned a slot" (must exclude scheduled, or an
    # already-scheduled request could be double-booked) and "should
    # still be reported to the public site" (must include scheduled).
    # Candidate-selection code must always use WAITING_STATUSES;
    # reporting/ETA-refresh code uses ACTIVE_STATUSES.
    WAITING_STATUSES = ("pending", "no_slot_soon")
    ACTIVE_STATUSES = WAITING_STATUSES + ("scheduled",)
    TERMINAL_STATUSES = ("fulfilled", "unavailable", "expired")

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
    scheduled_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set the moment this request was assigned a specific "
                   "LogItem (status became scheduled). Retained as "
                   "history even after fulfillment; cleared whenever the "
                   "request returns to pending/no_slot_soon/unavailable.",
    )
    fulfilled_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set the moment the track ACTUALLY started airing "
                   "(status became fulfilled) -- not when it was merely "
                   "assigned a slot. See scheduled_at for that.",
    )
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
    status_updated_at = models.DateTimeField(
        default=timezone.now,
        help_text="Version/ordering timestamp the public site uses to "
                   "reject an out-of-order or delayed status push -- "
                   "bumped explicitly by every code path that changes "
                   "status OR estimated_play_time (deliberately NOT "
                   "auto_now: that fires on QuerySet.update() not at "
                   "all, which is how nearly every transition here "
                   "writes for concurrency safety, and silently "
                   "overrides an explicitly-assigned value on .save(), "
                   "so it added a footgun with no actual benefit here).",
    )

    log_item = models.ForeignKey(
        "library.LogItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="fulfilled_song_request",
        help_text="The specific LogItem this request was assigned to, once scheduled.",
    )
    intro_track = models.ForeignKey(
        "library.Track", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dedication_intro_requests",
        help_text="The synthesized spoken-intro clip for this request, once "
                   "rendered. Never cleared by reconciliation regardless of "
                   "status -- reusable if the request reschedules, and a "
                   "clean audit trail even for a terminal request.",
    )
    intro_log_item = models.ForeignKey(
        "library.LogItem", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="dedication_intro_for_request",
        help_text="Set the moment intro_track is actually spliced into the "
                   "live queue ahead of log_item -- the reinsertion guard "
                   "(an intro is only ever spliced once per song "
                   "assignment) and the restart-recovery key. Cleared "
                   "whenever the request's assignment is abandoned "
                   "(pending/no_slot_soon/unavailable/expired); retained "
                   "on fulfilled as historical evidence the pairing aired "
                   "correctly.",
    )
    estimated_play_time = models.DateTimeField(
        null=True, blank=True,
        help_text="While pending: a best-guess air time, recomputed every "
                   "refresh_song_request_statuses cycle by checking each "
                   "upcoming open, music-kind LogItem for real eligibility "
                   "(recency included) rather than just chronological order. "
                   "Advisory only until scheduled -- the actual assignment "
                   "can land in a different slot than a given cycle's guess. "
                   "Once scheduled: the real, certain scheduled_time (or "
                   "live drift-corrected ETA) of the LogItem it landed in. "
                   "Once fulfilled: the real air timestamp, matching "
                   "fulfilled_at. Null while status is no_slot_soon, "
                   "expired, or unavailable.",
    )

    class Meta:
        ordering = ["submitted_at"]
        verbose_name = "Song Request"
        verbose_name_plural = "Song Requests"

    def __str__(self):
        track_label = str(self.track) if self.track_id else "(track removed)"
        return f"[{self.status}] {self.requester_name or 'anonymous'}: {track_label}"
