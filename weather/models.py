from django.db import models


DEFAULT_AMBER_EVENT_CODES = "BLU,CAE,MEP"

DEFAULT_AMBER_SAME_CODES = ",".join([
    "020027",  # Clay, KS
    "020029",  # Cloud, KS
    "020041",  # Dickinson, KS
    "020053",  # Ellsworth, KS
    "020105",  # Lincoln, KS
    "020123",  # Mitchell, KS
    "020139",  # Ottawa, KS
    "020169",  # Saline, KS
    "020000",  # Kansas statewide -- also match state-scoped alerts
])


class WeatherConfig(models.Model):
    """Singleton -- station location, NWS lookup parameters, and the
    day/night voice schedule for the weather announcer pipeline. These
    were hardcoded Python constants in the original kogr-sc scripts;
    admin-editable here so a station move or NWS zone change doesn't
    require a code edit + redeploy, matching the LogFillConfig/
    DuckingConfig/RBDSConfig convention used elsewhere in this project.

    Announcer email notifications reuse the project's own EMAIL_*
    settings (see monitoring.services.notify) via a management command
    the external weather-ingest scripts shell out to -- no separate
    SMTP credential file, unlike the original scripts."""

    station_lat = models.FloatField(
        default=39.13, help_text="Station latitude -- used for sunrise/sunset (day/night sky wording).",
    )
    station_lon = models.FloatField(
        default=-97.70, help_text="Station longitude.",
    )
    sun_alt_threshold_deg = models.FloatField(
        default=3.0, help_text="Solar altitude (degrees) above which it's considered daytime.",
    )

    nws_alert_zone = models.CharField(
        max_length=16, default="KSC143",
        help_text="NWS public forecast zone for active alerts, e.g. KSC143.",
    )
    nws_forecast_office = models.CharField(
        max_length=8, default="TOP",
        help_text="NWS forecast office code, e.g. TOP (Topeka).",
    )
    nws_forecast_grid_x = models.PositiveIntegerField(default=10)
    nws_forecast_grid_y = models.PositiveIntegerField(default=53)
    nws_cloud_stations = models.CharField(
        max_length=64, default="KCNK,KSLN",
        help_text="Comma-separated METAR station codes used to blend sky condition.",
    )

    voice_schedule = models.JSONField(
        default=list,
        help_text='List of [voice, start_hour, end_hour] triples, local time, 0-23, '
                   'end inclusive. A range may wrap past midnight (start > end). Must '
                   'cover every hour with no gaps. voice is "day" or "night". Example: '
                   '[["day",3,8],["night",9,14],["day",15,20],["night",21,2]]',
    )

    notify_email = models.EmailField(
        blank=True, default="",
        help_text="Address for weather-pipeline failure notifications. Blank disables.",
    )

    alert_sound_enabled = models.BooleanField(
        default=True,
        help_text="Master switch for the watch/warning alert beep. Off overrides "
                   "everything below -- no beep plays regardless of active alert "
                   "status. Separate from the WxAlert spoken-statement pipeline, "
                   "which is unaffected by this switch.",
    )
    alert_sound_cart = models.ForeignKey(
        "library.FXCart",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="weather_alert_configs",
        verbose_name="Alert FX Cart",
        help_text="The FX Cart fired while a qualifying watch/warning remains "
                   "active. Playback travels through IsadoraAir's normal FX/"
                   "program bus (same path as any other cart fire), so it's "
                   "present on air and in studio/remote-DJ monitoring -- not a "
                   "separate weather-only audio path. The cart's own filepath, "
                   "gain, and retrigger mode apply as usual; nothing here "
                   "overrides them. Blank/deleted cart disables the beep, same "
                   "as leaving alert_sound_enabled off.",
    )
    alert_sound_interval_seconds = models.PositiveIntegerField(
        default=600,
        help_text="How often the beep replays while a watch/warning remains active, "
                   "in seconds. Re-read fresh every check -- no restart needed.",
    )

    class Meta:
        verbose_name = "Weather Configuration"
        verbose_name_plural = "Weather Configuration"

    def __str__(self):
        return "Weather Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, created = cls.objects.get_or_create(pk=1)
        if created:
            obj.voice_schedule = [["day", 3, 8], ["night", 9, 14], ["day", 15, 20], ["night", 21, 2]]
            obj.save()
        return obj


class WeatherVoicePersona(models.Model):
    """Feature-owned persona and schedule-slot mapping above shared TTS."""

    slot = models.SlugField(
        max_length=64,
        unique=True,
        help_text="Stable key referenced by WeatherConfig.voice_schedule, such as day or night.",
    )
    tts_voice = models.ForeignKey(
        "tts.StationTTSVoice",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="weather_personas",
        help_text="Logical station voice. Blank is a safe unconfigured state.",
    )
    display_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        help_text="Feature-facing short persona name; never passed to a TTS provider.",
    )
    full_name = models.CharField(
        max_length=150,
        blank=True,
        default="",
        help_text="Listener-facing full persona name; never passed to a TTS provider.",
    )
    signoff = models.TextField(
        blank=True,
        default="",
        help_text="Weather-specific wording; never passed into TTS voice resolution.",
    )

    class Meta:
        ordering = ["slot"]
        verbose_name = "Weather Voice Persona"
        verbose_name_plural = "Weather Voice Personas"

    def __str__(self):
        return self.slot


class AmberAlertConfig(models.Model):
    """Singleton -- IPAWS OPEN configuration for AMBER (Child Abduction
    Emergency), Blue Alert (law-enforcement-officer down), and Missing
    and Endangered Persons alerts. FCC does NOT mandate these on a
    small-station license; radio stations carry them as a community-
    service voluntary broadcast. This config lets the operator opt in
    per event code, and filter to a specific area of interest so a
    Miami AMBER doesn't air on a Kansas station.

    Data flows through the same shape as the weather alert pipeline:

      IPAWS OPEN /rest/feed  (Atom summaries)
              -> filter by event code + statefips
              -> follow each entry's link for the full CAP 1.2 XML
              -> filter by SAME area code overlap
              -> fingerprint the surviving set
      change  -> synthesize with the on-duty Kokoro voice
              -> deliver + insert_urgent (analog to wx_alert.py)
      active  -> append text_core to the next scheduled forecast

    Signature verification is deliberately NOT implemented -- HTTPS to
    apps.fema.gov is the trust anchor, per operator decision. If that
    posture changes, wire in FEMA's IdenTrust cert bundle + XML DSig
    validation before the fingerprint check.
    """

    enabled = models.BooleanField(
        default=False,
        help_text=(
            "Master switch. When off, the pipeline never polls IPAWS "
            "and never speaks or inserts anything. Everything else in "
            "this form is inert while off. Off by default so a fresh "
            "install doesn't start airing AMBER Alerts before the "
            "operator has reviewed the terms and area filters."
        ),
    )

    ipaws_base_url = models.URLField(
        max_length=200,
        default="https://apps.fema.gov/IPAWSOPEN_EAS_SERVICE",
        help_text=(
            "IPAWS OPEN service root. The poller reads /rest/feed under "
            "this base for the Atom summary and /rest/eas/{id} for each "
            "full CAP message. Only change this if FEMA moves the "
            "endpoint or if you're pointing at a private mirror for "
            "testing."
        ),
    )

    event_codes = models.CharField(
        max_length=200,
        default=DEFAULT_AMBER_EVENT_CODES,
        help_text=(
            "Comma-separated CAP/SAME event codes to include. Defaults "
            "cover the three we care about: BLU (Blue Alert -- law "
            "enforcement officer down), CAE (Child Abduction Emergency "
            "-- AMBER), MEP (Missing and Endangered Persons)."
        ),
    )

    same_codes = models.CharField(
        max_length=500,
        default=DEFAULT_AMBER_SAME_CODES,
        help_text=(
            "Comma-separated 6-digit SAME area codes to include. First "
            "two digits are the state FIPS (Kansas = 20), last four "
            "are the county FIPS (000 for statewide). Defaults cover "
            "the eight KS counties in the KOGR-LP listening area plus "
            "020000 for state-scoped alerts."
        ),
    )

    poll_cadence_minutes = models.PositiveIntegerField(
        default=5,
        help_text=(
            "How often the poller checks the IPAWS feed. 5 minutes "
            "matches the general practice for voluntary broadcasters "
            "and stays under FEMA's rate ceiling. Lower is more "
            "responsive but more API load; higher risks delayed "
            "broadcast of a time-sensitive alert."
        ),
    )

    include_instruction_in_forecast = models.BooleanField(
        default=True,
        help_text=(
            "For AMBER-family alerts, the 'instruction' field is "
            "usually the tip-line phone number -- the whole point of "
            "the broadcast. Default ON means it's included in BOTH "
            "the urgent insert AND the forecast append (unlike weather "
            "safety instructions, which we drop from the forecast). "
            "Turn off if you want the forecast to summarize only."
        ),
    )

    class Meta:
        verbose_name = "AMBER Alert Configuration"
        verbose_name_plural = "AMBER Alert Configuration"

    def __str__(self):
        return f"AMBER Alert Configuration ({'enabled' if self.enabled else 'disabled'})"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def event_codes_set(self):
        return {c.strip().upper() for c in self.event_codes.split(",") if c.strip()}

    @property
    def same_codes_set(self):
        return {c.strip() for c in self.same_codes.split(",") if c.strip()}
