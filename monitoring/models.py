import socket
import traceback as _traceback
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone as _django_tz


class MonitorCheck(models.Model):
    """One health check the monitoring poller runs every cycle — a
    systemd unit's ActiveState, a disk/cpu/memory/temperature reading, a
    transmitter parameter/indicator, or a Liquidsoap silence report. See
    monitoring/services/monitor.py for the poll loop and
    monitoring/services/probes.py for the per-kind probe functions.

    Deliberately one flexible model rather than one-per-kind: every kind
    is structurally the same "poll something, compare against a
    threshold or fault value, debounce, maybe notify" shape, so a single
    queryset and a single admin list serve all of them. clean() enforces
    which fields matter per kind, the same way encoders.Encoder.clean()
    enforces which fields matter per protocol.

    Admin saves here do NOT restart any systemd service — unlike
    Encoders/AudioPipeline, the poller re-reads
    MonitorCheck.objects.filter(enabled=True) fresh every cycle, so a
    config change just takes effect on the next ~10s tick."""

    KIND_CHOICES = [
        ("systemd", "Systemd Service"),
        ("disk", "Disk Usage"),
        ("cpu", "CPU Usage"),
        ("memory", "Memory Usage"),
        ("temperature", "Temperature"),
        ("transmitter_param", "Transmitter Parameter"),
        ("transmitter_indicator", "Transmitter Status Indicator"),
        ("audio_silence", "Audio Silence (Liquidsoap)"),
        ("encoder_group", "Encoder Stream Health"),
        ("rbds", "RBDS Connection"),
    ]

    name = models.CharField(max_length=100, unique=True)
    kind = models.CharField(max_length=24, choices=KIND_CHOICES)
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    # --- kind="systemd" ---
    systemd_unit = models.CharField(
        max_length=100, blank=True,
        help_text="Unit name, e.g. isadoraair-engine.service.",
    )

    # --- kind="disk" ---
    disk_path = models.CharField(
        max_length=255, blank=True,
        help_text="Mount point to check, e.g. / or /srv/isadoraair.",
    )

    # --- kind="temperature" ---
    thermal_zone_label = models.CharField(
        max_length=64, blank=True,
        help_text="A psutil.sensors_temperatures() entry label to watch specifically "
                   "(e.g. 'Package id 0' under the 'coretemp' chip). Leave blank to "
                   "watch the highest current reading across every sensor on the box.",
    )

    # --- kind="transmitter_param" ---
    transmitter_parameter = models.CharField(
        max_length=64, blank=True,
        help_text="COBALT get-parameter, e.g. psu.fwd_power, psu.vswr, "
                   "psu.pa_temperature, psu.fan_speed_measured_fan1.",
    )

    # --- kind="transmitter_indicator" ---
    transmitter_indicator = models.CharField(
        max_length=64, blank=True,
        help_text="COBALT indicator parameter, e.g. status.indicator.rf, "
                   "status.indicator.vswr, status.indicator.temp, status.rf_interlock.",
    )
    fault_values = models.CharField(
        max_length=128, blank=True,
        help_text="Comma-separated values that mean FAULT (critical), e.g. 'R' or 'True'.",
    )
    warn_values = models.CharField(
        max_length=128, blank=True,
        help_text="Comma-separated values that mean WARNING (degraded, not fault), e.g. 'O'.",
    )

    # --- kind="audio_silence" ---
    silence_device_slug = models.CharField(
        max_length=100, blank=True,
        help_text="Matches encoders.services.encoder_manager._slug(input_device) — "
                   "used to find /run/isadoraair/liquidsoap_silence_<slug>.json.",
    )

    # --- kind="encoder_group" ---
    encoder_group_slug = models.CharField(
        max_length=100, blank=True,
        help_text="Matches encoders.services.encoder_manager._slug(input_device) — "
                   "used to find both /run/isadoraair/liquidsoap_silence_<slug>.json "
                   "(Liquidsoap's own self-report) and "
                   "/run/isadoraair/encoder_group_<slug>.json (the encoder manager's "
                   "own process-supervision state). Same slug convention as "
                   "silence_device_slug above -- a separate field rather than reusing "
                   "that one, since this is a genuinely different check kind (the "
                   "truthful AGGREGATE across manager/child/audio/Shoutcast signals, "
                   "not audio silence alone) that an operator may or may not want "
                   "configured alongside the existing Audio Silence check for the "
                   "same group. Added 2026-08-05 after a real production outage where "
                   "neither the existing systemd check nor the existing audio_silence "
                   "check caught a Liquidsoap crash-loop -- see probe_encoder_group.",
    )
    encoder_group_systemd_unit = models.CharField(
        max_length=100, blank=True,
        help_text="Systemd unit backing the encoder manager itself, e.g. "
                   "isadoraair-encoders.service — checked directly (reusing "
                   "probe_systemd's own logic) as one of several signals this kind "
                   "combines. Separate from the plain 'Systemd Service' kind's own "
                   "systemd_unit field above so this check's config is self-contained "
                   "rather than depending on another MonitorCheck row existing.",
    )

    # --- numeric thresholds, used by disk/cpu/memory/temperature/transmitter_param ---
    warning_threshold = models.FloatField(
        null=True, blank=True,
        help_text="Value at which this check becomes WARNING. Percent for cpu/mem/disk; "
                   "units vary by kind — see that kind's own help text.",
    )
    critical_threshold = models.FloatField(
        null=True, blank=True,
        help_text="Value at which this check becomes CRITICAL.",
    )
    threshold_direction = models.CharField(
        max_length=8, default="above",
        choices=[("above", "Alert when ABOVE threshold"), ("below", "Alert when BELOW threshold")],
        help_text="Most checks alert when the value climbs too high (disk/cpu/mem/temp/VSWR). "
                   "Use BELOW for things where a drop is bad, e.g. forward power or fan RPM.",
    )

    # --- debounce / alerting, applies to every kind ---
    consecutive_failures_required = models.PositiveSmallIntegerField(
        default=2,
        help_text="Consecutive bad polls needed before this check flips status and a "
                   "notification fires — avoids alerting on a single transient blip.",
    )
    notify_on_warning = models.BooleanField(default=True)
    notify_on_critical = models.BooleanField(default=True)

    show_as_card = models.BooleanField(
        default=True,
        help_text="Uncheck to keep this check running and alerting but hide its own "
                   "dashboard card -- useful when another card already displays its "
                   "status (e.g. a transmitter indicator check whose value colors a "
                   "paired parameter card, like TX RF Indicator coloring TX Forward Power).",
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Monitor Check"
        verbose_name_plural = "Monitor Checks"

    def clean(self):
        errors = {}
        if self.kind == "systemd" and not self.systemd_unit:
            errors["systemd_unit"] = "Required for Systemd Service checks."
        if self.kind == "disk" and not self.disk_path:
            errors["disk_path"] = "Required for Disk Usage checks."
        if self.kind in ("disk", "cpu", "memory", "temperature", "transmitter_param"):
            if self.warning_threshold is None and self.critical_threshold is None:
                errors["critical_threshold"] = "At least one threshold is required for this kind."
        if self.kind == "transmitter_param" and not self.transmitter_parameter:
            errors["transmitter_parameter"] = "Required for Transmitter Parameter checks."
        if self.kind == "transmitter_indicator":
            if not self.transmitter_indicator:
                errors["transmitter_indicator"] = "Required for Transmitter Status Indicator checks."
            if not self.fault_values:
                errors["fault_values"] = "Required — at least one FAULT value."
        if self.kind == "audio_silence" and not self.silence_device_slug:
            errors["silence_device_slug"] = "Required for Audio Silence checks."
        if self.kind == "encoder_group":
            if not self.encoder_group_slug:
                errors["encoder_group_slug"] = "Required for Encoder Stream Health checks."
            if not self.encoder_group_systemd_unit:
                errors["encoder_group_systemd_unit"] = "Required for Encoder Stream Health checks."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return self.name


class TransmitterConfig(models.Model):
    """Singleton — connection details for the Aquabroadcast COBALT
    transmitter's ASCII TCP control port (see
    monitoring/services/transmitter_client.py). No host known yet — until
    one is entered, transmitter_param/transmitter_indicator checks simply
    report 'unknown', not critical."""
    host = models.CharField(
        max_length=255, blank=True,
        help_text="Transmitter IP or hostname. Leave blank to disable transmitter polling.",
    )
    port = models.PositiveIntegerField(
        default=23,
        help_text="TCP port for the ASCII control protocol — confirm against the unit's own network settings.",
    )
    timeout_seconds = models.FloatField(default=3.0)
    poll_interval_seconds = models.PositiveIntegerField(
        default=30,
        help_text="How often to poll the transmitter specifically — independent of the "
                   "main monitor loop, since a TCP round-trip to RF hardware is slower "
                   "than a local systemctl/psutil call.",
    )
    full_power_watts = models.FloatField(
        default=265.0,
        help_text="Forward power reading that counts as 100% on the Forward Power meter.",
    )

    class Meta:
        verbose_name = "Transmitter Config"
        verbose_name_plural = "Transmitter Config"

    def __str__(self):
        return "Transmitter Config"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class NotificationConfig(models.Model):
    """Singleton — where alert emails go and how often."""
    enabled = models.BooleanField(default=True)
    recipients = models.TextField(
        blank=True,
        help_text="One email per line (or comma-separated). For SMS-via-carrier-gateway, "
                   "use e.g. 5551234567@vtext.com — treated as a plain email address, "
                   "no special handling needed.",
    )
    cooldown_minutes = models.PositiveIntegerField(
        default=30,
        help_text="Minimum time between repeat notifications for the same ongoing "
                   "failure. Recovery notifications always send immediately.",
    )

    class Meta:
        verbose_name = "Notification Config"
        verbose_name_plural = "Notification Config"

    def __str__(self):
        return "Notification Config"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj

    def recipient_list(self):
        raw = self.recipients.replace(",", "\n")
        return [r.strip() for r in raw.splitlines() if r.strip()]


class ListenerPeak(models.Model):
    """Singleton -- highest total-listeners-across-all-streams count seen
    since the last operator-initiated reset. Written by the monitor
    tick's Shoutcast poll (see monitoring/services/monitor.py); read by
    the dashboard listener widget. Reset via a double-click on the
    widget which POSTs to a monitoring endpoint; the endpoint sets
    peak_total to the CURRENT live total (not zero) so the next tick
    doesn't immediately raise the number back up and make the reset
    feel laggy.

    Kept here in monitoring rather than in encoders because the poll
    infrastructure (10 s tick, state-file writer) already lives here
    and this is fundamentally a monitoring artifact -- how many
    listeners are hearing the station right now."""

    peak_total = models.PositiveIntegerField(
        default=0,
        help_text="Highest sum-of-listeners-across-all-enabled-encoders "
                   "value seen since peak_since_at. Updated live by the "
                   "monitor tick.",
    )
    peak_since_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When the current peak-tracking window started -- either "
                   "engine first-boot or the operator's last reset.",
    )
    peak_reached_at = models.DateTimeField(
        null=True, blank=True,
        help_text="When peak_total was last raised. Blank if we haven't "
                   "seen anyone since the last reset.",
    )

    class Meta:
        verbose_name = "Listener Peak"
        verbose_name_plural = "Listener Peak"

    def __str__(self):
        return f"Listener Peak = {self.peak_total}"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class SystemEvent(models.Model):
    """A curated, operator-facing event log for /monitoring/.

    Populated by:

      * The monitoring poller on every check-status TRANSITION (see
        MonitorManager._run_cycle) -- ok/warning/critical/unknown edges
        for every enabled MonitorCheck. This is the free-coverage
        story: every existing and future check emits events with no
        per-check code.
      * The playback engine on exceptional paths -- skipped log items
        (deleted-track or missing-file ghost), _glib_safe catches,
        stuck-deck watchdog trips.
      * (Future) log builder, backup, analyze, RBDS.

    NOT a replacement for the systemd journal -- the journal remains
    authoritative for full detail (multi-line tracebacks, per-tick
    debug prints). This table is a short, structured summary a
    part-time operator can scan on /monitoring/ without opening a
    shell.

    Retention: pruned to ~30 days by the isadoraair-prune-systemevents
    timer (see deploy/). Nothing else depends on old rows.
    """

    LEVEL_CHOICES = [
        ("info", "Info"),
        ("warning", "Warning"),
        ("error", "Error"),
        ("critical", "Critical"),
    ]

    # Free-form category slugs -- deliberately not a choices field so a
    # new subsystem can start emitting without a migration. Common values
    # today: "monitor", "engine", "encoder", "builder", "backup",
    # "analyze", "rbds".
    category = models.CharField(max_length=32, db_index=True)
    level = models.CharField(max_length=16, choices=LEVEL_CHOICES, default="info", db_index=True)

    title = models.CharField(max_length=200)
    detail = models.JSONField(default=dict, blank=True)

    source = models.CharField(max_length=64, blank=True)

    # Simple in-DB dedup: emit_event() computes a stable key from
    # (category, level, title) unless the caller overrides it, and if
    # the LAST row with that key was written within the coalescing
    # window (default 60s), it bumps `repeat_count` and
    # `last_repeated_at` on that row instead of inserting a new one. A
    # flapping check therefore shows as one row with "x 12" rather than
    # drowning the operator in identical repeats.
    dedupe_key = models.CharField(max_length=200, blank=True, db_index=True)
    repeat_count = models.PositiveIntegerField(default=1)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    last_repeated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "System Event"
        verbose_name_plural = "System Events"
        indexes = [
            models.Index(fields=["-created_at", "level"]),
            models.Index(fields=["category", "-created_at"]),
        ]

    def __str__(self):
        return f"[{self.level}] {self.category}: {self.title}"


# Window during which a repeat of the same (category, level, title) is
# coalesced onto the previous row rather than inserted as a new one.
# Shorter than a normal debounced check flip (>= 10s each poll * 2
# consecutive bad polls to fire) so real distinct events are not
# accidentally coalesced; long enough that a genuinely flapping
# subsystem does not spam rows.
_EVENT_COALESCE_SECONDS = 60


def emit_event(category, title, level="info", detail=None, source=None, dedupe_key=None):
    """Record a SystemEvent for /monitoring/'s Recent Events feed.

    Contract:
      * Never raises. If the DB is unavailable or something else goes
        wrong, swallow the error and print a note to stderr so the
        calling subsystem's own reliability is not tied to the event
        log's.
      * Coalesces near-duplicate emissions within _EVENT_COALESCE_SECONDS.
        Pass an explicit `dedupe_key` to override the default
        (category+level+title).
      * `detail` should be a JSON-serialisable dict; other types are
        coerced.
    """
    try:
        detail = dict(detail) if isinstance(detail, dict) else ({"value": detail} if detail is not None else {})
        if "exception" in detail and isinstance(detail["exception"], BaseException):
            detail["exception"] = "".join(_traceback.format_exception_only(type(detail["exception"]), detail["exception"])).strip()
        if source is None:
            try:
                source = socket.gethostname()
            except Exception:
                source = ""
        key = dedupe_key if dedupe_key is not None else f"{category}|{level}|{title}"[:200]
        now = _django_tz.now()
        window_start = now - timedelta(seconds=_EVENT_COALESCE_SECONDS)
        # Lock-free coalesce: read the most recent match; if within the
        # window, bump; otherwise insert. Rare race landing an extra row
        # in a spam window is fine -- cheaper than SELECT FOR UPDATE on
        # the engine hot path.
        recent = (
            SystemEvent.objects
            .filter(dedupe_key=key, created_at__gte=window_start)
            .order_by("-created_at")
            .first()
        )
        if recent is not None:
            SystemEvent.objects.filter(pk=recent.pk).update(
                repeat_count=models.F("repeat_count") + 1,
                last_repeated_at=now,
                detail=detail,
            )
            return recent.pk
        return SystemEvent.objects.create(
            category=category, level=level, title=title,
            detail=detail, source=source, dedupe_key=key,
        ).pk
    except Exception as exc:  # noqa: BLE001 -- event log must not break its callers
        import sys as _sys
        print(f"  [emit_event] failed to record event ({category}/{level}: {title}): {exc}", file=_sys.stderr)
        return None

