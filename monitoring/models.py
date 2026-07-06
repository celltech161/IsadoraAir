from django.core.exceptions import ValidationError
from django.db import models


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
