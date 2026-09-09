from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from .dedication_text import (
    DedicationTemplateError,
    MAX_DEDICATION_MESSAGE_SPOKEN_LIMIT,
    MIN_DEDICATION_MESSAGE_SPOKEN_LIMIT,
)
from .dedication_text import validate_template as _validate_dedication_template_source


def validate_dedication_template_field(value):
    """Django-facing adapter around dedication_text.validate_template --
    runs at Admin/ModelForm full_clean() time (field validators run
    there automatically) so an invalid station template is rejected
    before save, not just at runtime rendering. Kept as a plain
    module-level function (not a lambda/closure) so Django's migration
    serializer can reference it by dotted path."""
    try:
        _validate_dedication_template_source(value)
    except DedicationTemplateError as exc:
        raise ValidationError(str(exc)) from exc


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
    dedication_tts_voice = models.ForeignKey(
        "tts.StationTTSVoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="dedication_configurations",
        help_text=(
            "Logical station voice used for spoken dedication intros. "
            "Blank disables intro synthesis while the requested song still airs."
        ),
    )
    dedication_tts_timeout_seconds = models.PositiveIntegerField(
        default=30,
        help_text="Shared-TTS timeout for each short dedication intro.",
    )

    # Four explicit, station-editable spoken templates -- one per
    # named/anonymous x dedication-message/request-only combination.
    # Deliberately four separate fields rather than one field with
    # conditional syntax: see webrequests/dedication_text.py for the
    # trusted {title}/{artist}/{requester_name}/{dedication_message}
    # substitution allowlist both Admin (via validate_dedication_
    # template_field below) and runtime rendering enforce. Defaults
    # reproduce the exact pre-r0056 hardcoded wording byte-for-byte --
    # see webrequests/tests/test_dedication_intros.py's
    # DedicationTextTemplateTests.
    dedication_named_message_template = models.CharField(
        max_length=512,
        default="Now here's {title} by {artist}, {dedication_message} Thanks {requester_name} for your dedication.",
        validators=[validate_dedication_template_field],
        help_text="Used when a requester name AND a dedication message are both "
                   "present. Allowed placeholders: {title} {artist} "
                   "{requester_name} {dedication_message}.",
    )
    dedication_named_request_template = models.CharField(
        max_length=512,
        default="Now here's {title} by {artist}. Thanks {requester_name} for your request.",
        validators=[validate_dedication_template_field],
        help_text="Used when a requester name is present but no dedication "
                   "message was given. Allowed placeholders: {title} {artist} "
                   "{requester_name} {dedication_message}.",
    )
    dedication_anonymous_message_template = models.CharField(
        max_length=512,
        default="Now here's {title} by {artist}, {dedication_message}",
        validators=[validate_dedication_template_field],
        help_text="Used when a dedication message is present but no requester "
                   "name was given. Allowed placeholders: {title} {artist} "
                   "{requester_name} {dedication_message}.",
    )
    dedication_anonymous_request_template = models.CharField(
        max_length=512,
        default="Now here's {title} by {artist}.",
        validators=[validate_dedication_template_field],
        help_text="Used when neither a requester name nor a dedication message "
                   "was given. Allowed placeholders: {title} {artist} "
                   "{requester_name} {dedication_message}.",
    )
    dedication_message_spoken_limit = models.PositiveSmallIntegerField(
        default=300,
        validators=[
            MinValueValidator(MIN_DEDICATION_MESSAGE_SPOKEN_LIMIT),
            MaxValueValidator(MAX_DEDICATION_MESSAGE_SPOKEN_LIMIT),
        ],
        help_text="Local, authoritative on-air length cap for a normalized "
                   "dedication message -- independent of (and much smaller "
                   "than) the public site's own 2,000-character transport "
                   "limit. A message that exceeds this is never truncated: "
                   "the intro is simply not generated and the requested song "
                   "still airs plainly.",
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
    track, is formatted into a spoken intro synthesized through the
    shared TTS service using WebRequestConfig.dedication_tts_voice (an
    operator-selected logical station voice; dedication intros are
    disabled -- no intro synthesized, song still airs plainly -- until
    one is selected) and spliced immediately ahead of the requested
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

    # -------------------------------------------------------------
    # Read-only dedication evidence -- computed only, never persisted.
    # Formalizes the distinct facts already represented by existing
    # fields (see the class docstring's field-by-field notes above and
    # docs/WEB_REQUESTS_INTEGRATION.md's "Dedication evidence" section)
    # without adding any new timestamp or model. Surfaced in
    # SongRequestAdmin's read-only "Dedication Evidence" fieldset.
    # -------------------------------------------------------------
    @property
    def intro_artifact_status(self):
        """intro_track alone means: a generated spoken artifact exists
        and is associated with this request. It does NOT mean the
        intro was ever queued or aired -- see intro_queue_status and
        intro_play_status for that."""
        return "Generated" if self.intro_track_id else "Not generated"

    @property
    def intro_queue_status(self):
        """intro_log_item means: the generated intro was actually
        spliced into a specific playlist occurrence ahead of its
        requested-song assignment. It is pairing/restart-recovery
        evidence -- it does not, by itself, prove audible playback."""
        return "Spliced" if self.intro_log_item_id else "Not spliced"

    @property
    def intro_play_status(self):
        """intro_log_item.played_at, when non-null, is the existing
        engine occurrence evidence that the dedication intro's LogItem
        actually began playback under the same engine clock used by
        every other LogItem -- the strongest currently-existing
        dedication-air evidence. This is NOT a claim about "first
        audible PCM"; it is whatever the existing LogItem playback-
        start write means, station-wide, until roadmap item 1.6
        settles that question."""
        if not self.intro_log_item_id:
            return "Not spliced"
        played_at = self.intro_log_item.played_at
        if played_at is None:
            return "Spliced / not yet played"
        return f"Aired at {played_at:%Y-%m-%d %H:%M:%S %Z}"

    @property
    def requested_song_status(self):
        """The requested song's own occurrence evidence -- fulfilled_at,
        set only after the requested song's own LogItem.played_at
        write succeeds (see webrequests.services.mark_song_requests_
        aired). Deliberately independent of the dedication intro
        evidence above: a dedication can air without its song showing
        fulfilled yet in a narrow timing window, and a song can be
        fulfilled with no intro ever having aired -- or existed -- at
        all."""
        if self.fulfilled_at is not None:
            return f"Fulfilled at {self.fulfilled_at:%Y-%m-%d %H:%M:%S %Z}"
        if self.log_item_id:
            return "Scheduled / not yet fulfilled"
        return "Not scheduled"
