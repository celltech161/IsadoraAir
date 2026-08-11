from django.core.exceptions import ValidationError
from django.db import models

BITRATE_CHOICES = [(k, f"{k} kbps") for k in (64, 96, 128, 160, 192, 224, 256, 320)]

# Fields whose change is runtime-affecting -- feeds the generated Liquidsoap
# script directly, or changes which input_device group a row belongs to.
# Originally lived only in encoders/admin.py (Phase 2's restart-dispatch
# decision); moved here (2026-08-10, Phase 2 hardening) to be the single
# shared source of truth for TWO independently-important decisions that
# must never be allowed to drift apart: "does this change need the encoder
# manager to reconcile this row's group" (admin.py's own informational
# messaging -- Phase 3 replaced the restart dispatch itself with the
# manager discovering the change on its own) and "does this change the
# configuration fingerprint" (encoders/services/lkg.py's
# compute_fingerprint) -- both are fundamentally the same question ("does
# this affect the running script"), so one field list answers both.
# `name` and `sort_order` are
# deliberately EXCLUDED: neither is read anywhere in encoder_manager.py's
# build_liquidsoap_script/_output_block (confirmed by source inspection --
# `name` only ever appears in the manager's own log line, `sort_order`
# only affects admin/queryset ordering). `description` is ALSO excluded
# for the same reason -- not read by encoder_manager.py.
RUNTIME_AFFECTING_FIELDS = frozenset({
    "enabled", "protocol", "host", "port", "mount", "username", "password",
    "format", "bitrate_kbps", "input_device", "station_name", "genre",
    "url", "public", "provider", "mp3_rate_mode",
})
# `provider` and `mp3_rate_mode` (roadmap 3.10, 2026-08-11) are included
# even though `provider` alone never changes the literal output.icecast()/
# output.shoutcast() call syntax build_liquidsoap_script renders -- a
# provider change still changes VALIDATION semantics (encoders/services/
# validation.py's validate_provider_policy), supported-format policy, and
# which destination-health path monitoring/services/probes.py's
# evaluate_encoder_group_health takes for this row (the new generic
# Liquidsoap connection-state signal vs. the external Shoutcast DNAS
# /statistics probe -- see that function's own docstring). A provider
# edit that silently kept the OLD accepted fingerprint would let a row
# skip re-validation against its new provider's rules and skip
# re-qualification under its new health-check path entirely -- exactly
# what this set exists to prevent. `mp3_rate_mode` is a literal renderer
# input (_format_block) and obviously belongs here for the same reason
# every other rendered field does.


class Encoder(models.Model):
    """One outbound stream connection (Icecast/Shoutcast). Enabled rows
    are grouped by input_device into shared Liquidsoap subprocesses in
    the isadoraair-encoders service — one process per distinct
    input_device, each carrying every enabled row that reads from it —
    see encoders/services/encoder_manager.py.

    Adding, deleting, or changing a row's runtime-affecting fields
    (encoders/admin.py's RUNTIME_AFFECTING_FIELDS) is a topology change
    for that row's input_device group, not a live-adjustable property.
    Since Phase 3 (2026-08-10), the admin no longer restarts anything
    itself -- it just commits the change; the running EncoderManager
    process discovers the drift on its own (encoders/services/
    encoder_manager.py's _reconcile) and replaces ONLY that group's
    live child, after re-validating and re-preflighting it, leaving
    every other group's process completely untouched -- but only when
    the row is, was, or becomes `enabled`; a disabled row was never
    part of the running topology, so adding/deleting/editing one that
    stays disabled triggers no reconciliation. Fields outside
    RUNTIME_AFFECTING_FIELDS (e.g. sort_order, or the display-only
    `name`) never trigger reconciliation regardless of `enabled` — see
    encoders/admin.py."""
    PROTOCOL_CHOICES = [
        ("icecast", "Icecast"),
        ("shoutcast1", "Shoutcast 1"),
        ("shoutcast2", "Shoutcast 2"),
    ]
    FORMAT_CHOICES = [
        ("mp3", "MP3"),
        ("aac", "AAC"),
        ("vorbis", "Ogg Vorbis"),
    ]
    # Roadmap 3.10: provider PRESETS layered on top of the generic
    # protocol/format model -- deliberately NOT a parallel protocol
    # ("live365"/"radio_co" are never valid `protocol` values). Live365
    # is an Icecast source destination; Radio.co is a Shoutcast-1-style
    # source destination. `provider` only ever narrows what
    # encoders/services/validation.py's validate_provider_policy accepts
    # for an already-generic-valid row, and selects which destination-
    # health path monitoring/services/probes.py's
    # evaluate_encoder_group_health takes (see that function's own
    # docstring) -- it never changes what Liquidsoap operator
    # encoder_manager.py's _output_block renders (still exactly
    # output.icecast()/output.shoutcast() either way).
    PROVIDER_CHOICES = [
        ("generic", "Generic"),
        ("live365", "Live365"),
        ("radio_co", "Radio.co"),
    ]
    # MP3 rate-mode policy, generic (not a Radio.co/Live365-only hack --
    # any Encoder row, provider or not, can opt into an explicit CBR/ABR
    # policy instead of the bitrate-threshold-based default). See
    # encoders/services/validation.py's effective_mp3_rate_mode() for the
    # exact "auto" resolution rule, shared by both the renderer
    # (encoder_manager.py's _format_block) and provider validation.
    MP3_RATE_MODE_CHOICES = [
        ("auto", "Auto (bitrate-based, current default behavior)"),
        ("cbr", "Constant bitrate (CBR)"),
        ("abr", "Average bitrate (ABR)"),
    ]

    name = models.CharField(max_length=64, unique=True)
    enabled = models.BooleanField(default=True)
    protocol = models.CharField(max_length=12, choices=PROTOCOL_CHOICES, default="icecast")
    host = models.CharField(max_length=255)
    port = models.PositiveIntegerField(default=8000)
    mount = models.CharField(
        max_length=128, blank=True,
        help_text="Icecast: the real mount path, e.g. /stream. Shoutcast 2: the "
                   "numeric Stream ID, entered the same way, e.g. /4 for stream 4 "
                   "(parsed into Liquidsoap's icy_id). Not used for Shoutcast 1.",
    )
    username = models.CharField(
        max_length=64, default="source", blank=True,
        help_text="Icecast source username. Not used for Shoutcast 1/2.",
    )
    password = models.CharField(max_length=128)
    format = models.CharField(
        max_length=10, choices=FORMAT_CHOICES, default="mp3",
        help_text="Streamed via Liquidsoap. AAC is Icecast/Shoutcast 2 only, not "
                   "Shoutcast 1 (the legacy ICY protocol never supported anything "
                   "but MP3 in practice).",
    )
    bitrate_kbps = models.PositiveIntegerField(choices=BITRATE_CHOICES, default=128)
    provider = models.CharField(
        max_length=10, choices=PROVIDER_CHOICES, default="generic",
        help_text="Generic: manually configured Icecast/Shoutcast destination. Live365/"
                   "Radio.co: provider presets layered on top of the same generic Icecast/"
                   "Shoutcast transport -- selecting one narrows which protocol/format/"
                   "MP3 rate-mode combinations are accepted and how destination health is "
                   "verified, it does not add a new streaming protocol.",
    )
    mp3_rate_mode = models.CharField(
        max_length=4, choices=MP3_RATE_MODE_CHOICES, default="auto",
        help_text="Auto preserves the existing behavior exactly: bitrates below 192 kbps "
                   "use LAME ABR, 192 kbps and up use CBR. CBR/ABR force that choice "
                   "regardless of bitrate. Some providers (e.g. Live365, Radio.co) require "
                   "an effective CBR MP3 stream -- Auto at 192 kbps or higher already "
                   "satisfies that without changing this field.",
    )
    input_device = models.CharField(
        max_length=100, blank=True,
        help_text="ALSA capture device. Defaults to the StereoTool HD Output bridge.",
    )
    station_name = models.CharField(max_length=255, blank=True)
    genre = models.CharField(max_length=255, blank=True)
    description = models.CharField(max_length=255, blank=True)
    url = models.URLField(blank=True)
    public = models.BooleanField(default=False)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Encoder"
        verbose_name_plural = "Encoders"

    def clean(self):
        # Delegates to the centralized validation layer (Phase 2
        # hardening, 2026-08-10) rather than duplicating rules here --
        # encoders/services/validation.py is now the ONE place every
        # protocol/format/connection-field rule lives, since a direct
        # ORM write (fixture, migration, shell one-liner) bypasses this
        # method entirely and must be caught by that same layer at
        # candidate-render time regardless. Local import: encoders/
        # services/validation.py imports encoders.models at its own
        # module level (for FORMAT_CHOICES/PROTOCOL_CHOICES labels and
        # the default Encoder.objects queryset), so a top-level import
        # here would be circular.
        from encoders.services.validation import validate_single_encoder
        errors = validate_single_encoder(self)
        if errors:
            raise ValidationError(errors)

    @property
    def shoutcast_sid(self):
        """Normalized Shoutcast Stream ID, e.g. "/4" -> "4", as a STRING
        -- for MATCHING against a live Shoutcast server's /statistics
        STREAM[@id] (see monitoring/services/shoutcast.py, whose XML-
        parsed SIDs are strings), not for script generation. Lenient by
        design: falls back to "1" on a blank/malformed mount rather than
        raising, because monitoring must still be ABLE to ask "is SID 1
        up?" even for a row that centralized validation would reject --
        an invalid row is exactly the case operators most need
        monitoring to keep functioning for, not the case to make this
        property throw.

        For SCRIPT GENERATION specifically, encoders/services/
        encoder_manager.py's _output_block uses
        encoders.services.validation.normalize_shoutcast_sid() instead
        -- strict, raises on anything that isn't a clean positive
        integer, and returns an int (never a string substituted
        directly into `icy_id=`). Two different contracts for two
        different consumers: monitoring needs a best-effort string that
        never raises, script generation needs a verified integer or an
        explicit failure. Single source of truth for this normalization
        used to be duplicated inline in both call sites, a real risk
        given the two MUST agree (a drift here would make monitoring
        check the wrong SID and silently misreport a stream's real
        status).

        Shoutcast 1 is single-stream with no real SID concept -- always
        "1" (matching Liquidsoap's own default icy_id), regardless of
        whatever `mount` happens to hold. `mount`'s own help text
        already documents it as "Not used for Shoutcast 1"; a stray
        value there (an admin left something in the field, or a row
        was switched from Shoutcast 2 without clearing it) must not
        silently change which SID monitoring checks. None for Icecast,
        which addresses by mount path, not SID -- callers must not
        treat a None SID as "stream 1"."""
        if self.protocol == "shoutcast1":
            return "1"
        if self.protocol != "shoutcast2":
            return None
        return (self.mount or "").strip("/") or "1"

    def __str__(self):
        return self.name
