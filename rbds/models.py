import re

from django.core.exceptions import ValidationError
from django.db import models

from rbds.services.rbds_pty import RBDS_PTY_CHOICES

PROTOCOL_CHOICES = [
    ("uecp", "UECP (binary)"),
    ("ascii", "StereoTool ASCII"),
]
TRANSPORT_CHOICES = [
    ("tcp", "TCP"),
    ("udp", "UDP"),
]
SOURCE_TYPE_CHOICES = [
    ("static", "Static text"),
    ("file", "Local file"),
    ("url", "URL"),
]


class RBDSConfig(models.Model):
    """Singleton -- network connection to StereoTool's RDS encoder plus
    static (rarely-changed) RDS/RBDS fields. Editing network/protocol
    fields here is a topology change for the rbds engine's live socket
    (can't hot-swap TCP<->UDP or reconnect mid-stream cleanly), so admin
    restarts isadoraair-rbds on every save -- see rbds/admin.py. PS
    rotation/message content live in RBDSPSFrame/RBDSMessage instead,
    which the engine re-polls on its own normal cadence with no restart
    needed (mirrors MonitorCheck's "poller re-reads fresh" convention)."""

    # --- Network ---
    host = models.CharField(
        max_length=255, default="127.0.0.1",
        help_text="StereoTool's RDS server address. Defaults to localhost since "
                   "StereoTool normally runs on this same box.",
    )
    port = models.PositiveIntegerField(
        default=4001,
        help_text="StereoTool's UECP/ASCII server port. 4001 is StereoTool's own "
                   "generic default, and what this box's own StereoTool instance "
                   "actually listens on.",
    )
    transport = models.CharField(max_length=3, choices=TRANSPORT_CHOICES, default="tcp")
    protocol = models.CharField(
        max_length=5, choices=PROTOCOL_CHOICES, default="uecp",
        help_text="UECP is the real binary protocol StereoTool's encoder expects in "
                   "normal use. ASCII is StereoTool's own simplified text alternative -- "
                   "note TA is not sent at all in ASCII mode (StereoTool's ASCII dialect "
                   "has no TA command).",
    )

    # --- UECP addressing (ignored in ASCII mode) ---
    uecp_site_address = models.PositiveSmallIntegerField(
        default=1, help_text="UECP 10-bit site address (0-1023). Only used in UECP mode.",
    )
    uecp_encoder_address = models.PositiveSmallIntegerField(
        default=0, help_text="UECP 6-bit encoder address (0-63). Only used in UECP mode.",
    )

    # --- Static RDS identity ---
    pi_code = models.CharField(
        max_length=4, blank=True, default="",
        help_text="1-4 hex digit Program Identification code, e.g. 1000. Raw hex entry "
                   "only -- no callsign-to-PI calculator in this version.",
    )
    station_ps = models.CharField(
        max_length=8, blank=True,
        help_text="Static 8-character PS (Program Service name) shown when no dynamic "
                   "PS frame is configured below, e.g. 'KOGR-LP '. Padded/truncated to "
                   "8 chars automatically at send time.",
    )
    pty = models.PositiveSmallIntegerField(
        choices=RBDS_PTY_CHOICES, default=0,
        help_text="RBDS Program Type -- this is the US RBDS table, NOT the European "
                   "RDS PTY table (different numbering).",
    )
    tp = models.BooleanField("Traffic Program", default=False)
    ta = models.BooleanField(
        "Traffic Announcement", default=False,
        help_text="Not sent at all when Protocol is ASCII mode (StereoTool's ASCII "
                   "dialect has no TA command) -- the value is kept regardless so "
                   "switching back to UECP doesn't lose it.",
    )
    ms = models.BooleanField(
        "Music/Speech", default=True, help_text="Checked = Music, unchecked = Speech.",
    )
    di_dynamic_pty = models.BooleanField("DI: Dynamic PTY", default=False)
    di_compressed = models.BooleanField("DI: Compressed", default=False)
    di_artificial_head = models.BooleanField("DI: Artificial Head", default=False)
    di_stereo = models.BooleanField("DI: Stereo", default=True)

    # --- AF list ---
    af_frequencies_mhz = models.CharField(
        max_length=255, blank=True,
        help_text="Comma-separated alternate frequencies in MHz, e.g. '89.5, 91.3'. "
                   "Valid range 87.6-107.9. Only used in UECP mode (ASCII has no AF command).",
    )

    # --- Clock time (CT) ---
    send_ct = models.BooleanField(
        "Send Clock Time (CT)", default=False,
        help_text="Sends the encoder's real-time clock (UECP MEC 0x0D) so receivers "
                   "can auto-set their displayed clock. UECP mode only (no ASCII "
                   "equivalent exists). Per spec the underlying fields are always UTC "
                   "plus a separate local-offset field the receiver applies itself -- "
                   "this is computed automatically from the server's configured "
                   "timezone, not something to set here.",
    )

    # --- now-playing / RT behavior ---
    now_playing_format = models.CharField(
        max_length=255, default="{artist} - {title}",
        help_text="Python str.format() template applied to now_playing.json's artist/"
                   "title fields. If artist is empty (e.g. an alt_send_text override), "
                   "just {title} is sent verbatim. Truncated to 64 chars (RadioText limit).",
    )
    use_rt_plus = models.BooleanField(
        "Send RT+ tagging", default=True,
        help_text="Tags the artist/title inside the RadioText string so RT+-capable "
                   "receivers can display Artist and Title as separate fields (car head "
                   "units, TEF6686-class radios, etc.) instead of as one scrolling line. "
                   "Works on both protocols: over ASCII it rides in the RT+= command; "
                   "over binary UECP it uses the same proprietary MEC 0x24 sequence "
                   "RDS Magic 4 sends to StereoTool (reverse-engineered from a live "
                   "capture, byte-for-byte match against RDS Magic 4's output). RT "
                   "sources that don't have a clean artist/title split (weather, "
                   "promos, station IDs) get a single \"cover the whole RT\" tag "
                   "instead, so receivers won't apply the previous song's offsets on "
                   "top of the new text. Turn off to send only plain RadioText.",
    )
    nowplaying_min_seconds = models.PositiveIntegerField(
        default=20,
        help_text="Now-playing is the default/continuous RT state -- RT Rotation "
                   "Messages periodically interrupt it for their own Display Seconds, "
                   "then control returns to now-playing. This is the floor on how long "
                   "now-playing must show again before the next message is allowed to "
                   "interrupt it. Not itself a rotation slot -- see the RT Rotation "
                   "Messages page for the promo side of the rotation.",
    )

    class Meta:
        verbose_name = "RBDS Config"
        verbose_name_plural = "RBDS Config"

    def clean(self):
        errors = {}
        if self.pi_code and not re.fullmatch(r"[0-9A-Fa-f]{1,4}", self.pi_code):
            errors["pi_code"] = "Enter 1-4 hex digits, e.g. 1000."
        if self.af_frequencies_mhz:
            for raw in self.af_frequencies_mhz.split(","):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    freq = float(raw)
                except ValueError:
                    errors["af_frequencies_mhz"] = f"'{raw}' isn't a number."
                    break
                if not (87.6 <= freq <= 107.9):
                    errors["af_frequencies_mhz"] = f"{freq} MHz is out of range (87.6-107.9)."
                    break
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return "RBDS Config"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class RBDSPSFrame(models.Model):
    """One 8-char PS frame in the dynamic-PS rotation. If zero enabled
    rows exist, RBDSConfig.station_ps is sent as a static PS instead.
    Purely content -- the engine re-polls RBDSPSFrame.objects.filter(enabled=True)
    on its own normal cadence, no restart needed on save (see
    rbds/admin.py -- only RBDSConfig triggers a restart)."""
    text = models.CharField(
        max_length=8,
        help_text="Exactly what's transmitted as PS (padded/truncated to 8 chars).",
    )
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    hold_seconds = models.PositiveSmallIntegerField(
        default=8, help_text="How long this frame stays on-air before rotating to the next.",
    )

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "PS Rotation Frame"
        verbose_name_plural = "PS Rotation Frames"

    def __str__(self):
        return self.text


class RBDSMessage(models.Model):
    """One RT/RT+ rotation slot -- rotates in alongside now-playing (see
    rbds/services/rbds_manager.py's rotation algorithm). Simple rotation
    + optional date range only -- not a full hour x day-of-week schedule
    (explicitly out of scope for this version)."""
    name = models.CharField(
        max_length=100, help_text="Internal label only, not transmitted.",
    )
    source_type = models.CharField(
        max_length=6, choices=SOURCE_TYPE_CHOICES, default="static",
        help_text="Where this message's text comes from.",
    )
    text = models.CharField(
        max_length=64, blank=True,
        help_text="RadioText sent verbatim (64-char limit). Used when Source Type is "
                   "'Static text'.",
    )
    file_path = models.CharField(
        max_length=500, blank=True,
        help_text="Local file path read fresh every Poll Interval below. Used when "
                   "Source Type is 'Local file'. Content is trimmed and truncated to "
                   "64 chars.",
    )
    source_url = models.URLField(
        blank=True,
        help_text="URL fetched (HTTP GET) fresh every Poll Interval below. Used when "
                   "Source Type is 'URL'. Response body is trimmed and truncated to "
                   "64 chars.",
    )
    poll_interval_seconds = models.PositiveIntegerField(
        default=30,
        help_text="How often to re-read the file / re-fetch the URL. Ignored (no "
                   "effect) for Static text.",
    )
    rt_plus_delimiter = models.CharField(
        max_length=4, blank=True,
        help_text="Controls how this message's RT+ tags are shaped. If set "
                   "(e.g. '|'), the text is split on its first occurrence into "
                   "artist and title, and two RT+ tags (item.artist + item.title) are "
                   "emitted at the corresponding character offsets -- same shape as a "
                   "now-playing song. Leave blank to emit a single tag covering the "
                   "whole text -- appropriate for weather, promos, station IDs, and "
                   "any message that isn't artist/title-shaped. That single-tag path "
                   "is what prevents receivers from applying the previous song's "
                   "artist/title offsets on top of this message's text (which they "
                   "will happily do if no tags are sent at all).",
    )
    enabled = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)
    display_seconds = models.PositiveSmallIntegerField(
        default=10,
        help_text="How long this message stays on-air each time it's shown, before "
                   "reverting to now-playing RT.",
    )
    start_date = models.DateField(
        null=True, blank=True,
        help_text="Leave blank for no start restriction -- active immediately.",
    )
    end_date = models.DateField(
        null=True, blank=True,
        help_text="Leave blank for no end restriction -- active indefinitely.",
    )

    class Meta:
        ordering = ["sort_order", "id"]
        verbose_name = "RT Rotation Message"
        verbose_name_plural = "RT Rotation Messages"

    def clean(self):
        errors = {}
        if self.start_date and self.end_date and self.start_date > self.end_date:
            errors["end_date"] = "End date must be on/after start date."
        if self.source_type == "file" and not self.file_path:
            errors["file_path"] = "Required when Source Type is 'Local file'."
        if self.source_type == "url" and not self.source_url:
            errors["source_url"] = "Required when Source Type is 'URL'."
        if errors:
            raise ValidationError(errors)

    def is_active_today(self, today):
        if self.start_date and today < self.start_date:
            return False
        if self.end_date and today > self.end_date:
            return False
        return True

    def __str__(self):
        return self.name
