from django.db import models


class AudioPipeline(models.Model):
    """Singleton — pipeline-wide GStreamer engine settings that don't
    belong on any single AudioInput/AudioOutput row.

    `sample_rate` is baked into the pipeline's topology (mixer output
    caps + the silence-priming burst used for fresh track starts — see
    engine.py's _build_main_pipeline/_create_deck).

    `program_gain_db` is a summed-bus attenuation applied AFTER the
    master mixer / duck / mic sum but BEFORE the pre-processor VU meter
    and the tee to StereoTool + Studio Monitor. It exists to keep enough
    headroom on the mixed program bus that a hot track + live mic + a
    remote-DJ voice can't push the summed peak past 0 dBFS at the input
    to StereoTool. Applied via a `volume` element in the engine chain.

    Both fields require an engine restart to change — the admin change
    form prompts for and triggers this on save."""
    SAMPLE_RATE_CHOICES = [
        (32000, "32 kHz"),
        (44100, "44.1 kHz"),
        (48000, "48 kHz"),
        (88200, "88.2 kHz"),
        (96000, "96 kHz"),
    ]
    sample_rate = models.PositiveIntegerField(
        choices=SAMPLE_RATE_CHOICES, default=48000,
        help_text="Not all audio hardware natively supports every rate — "
                   "if in doubt, 44.1kHz matches most of the library and "
                   "48kHz is the safest general-purpose default.",
    )
    program_gain_db = models.FloatField(
        default=-6.0,
        help_text="Attenuation applied to the summed master bus before "
                   "it feeds StereoTool and the Studio Monitor. Negative "
                   "values give StereoTool more input headroom; 0 dB is "
                   "unity (no attenuation). Default -6 dB matches typical "
                   "broadcast pre-processor practice.",
    )

    class Meta:
        verbose_name = "Audio Pipeline"
        verbose_name_plural = "Audio Pipeline"

    def __str__(self):
        return "Audio Pipeline"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class AudioOutput(models.Model):
    """Named playback sink (e.g. studio monitor). The device value is
    an ALSA path like 'plughw:2,0' chosen from what aplay -l reports at
    form-render time."""
    name = models.CharField(max_length=64, unique=True)
    device = models.CharField(
        max_length=100, blank=True,
        help_text="ALSA device path, e.g. plughw:2,0. Pick from the dropdown — options are discovered live from aplay -l.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    # Interim leveling for the studio monitor path only (StereoTool will
    # handle real transmitter processing separately). Only meaningful on
    # whichever row the engine resolves as "Studio Monitor" — see
    # engine.py's STUDIO_MONITOR_NAME / _apply_agc_config.
    agc_enabled = models.BooleanField(
        default=False,
        help_text="Off by default — nothing changes until explicitly enabled and tuned.",
    )
    agc_ratio = models.FloatField(
        default=2.0,
        help_text="Compression ratio, e.g. 2.0 = 2:1. Higher = more compression.",
    )
    agc_threshold = models.FloatField(
        default=0.3,
        help_text="Linear amplitude (0.0-1.0) above which compression kicks in.",
    )
    agc_soft_knee = models.BooleanField(
        default=True,
        help_text="Soft-knee (smooth) vs hard-knee transition into compression.",
    )
    agc_makeup_gain_db = models.FloatField(
        default=3.0,
        help_text="Gain applied after compression, in dB, to bring the leveled signal back up.",
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Audio Output"
        verbose_name_plural = "Audio Outputs"

    def __str__(self):
        return self.name


class AudioInput(models.Model):
    """Named capture source (e.g. studio mic). The device value is an
    ALSA path like 'plughw:2,0' chosen from what arecord -l reports at
    form-render time."""
    name = models.CharField(max_length=64, unique=True)
    device = models.CharField(
        max_length=100, blank=True,
        help_text="ALSA device path, e.g. plughw:2,0. Pick from the dropdown — options are discovered live from arecord -l.",
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    gain_db = models.FloatField(
        default=0.0,
        help_text="Software gain applied to this input in the on-air mix, in dB. "
                   "Independent of the hardware mixer controls below, which affect "
                   "the analog capture stage itself.",
    )
    mixer_control_values = models.JSONField(
        default=dict, blank=True,
        help_text="Last-applied value per ALSA mixer control name for this input's "
                   "card (e.g. {'Capture': 80, 'Mic Boost': True}). Populated/edited "
                   "via the admin's dynamically-rendered control fields below -- not "
                   "meant to be hand-edited as raw JSON.",
    )

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name = "Audio Input"
        verbose_name_plural = "Audio Inputs"

    def __str__(self):
        return self.name


class DuckingConfig(models.Model):
    """Singleton — ducks the combined deck output (not the mic) while
    PTT is live, ramped smoothly (~500ms, see engine.py's
    DUCK_RAMP_MS/_start_duck_ramp) rather than applied instantly, to
    avoid a click. Read fresh by the engine every time PTT is toggled —
    an edit here takes effect on the NEXT toggle, not retroactively for
    an already-live mic."""
    enabled = models.BooleanField(
        default=False, help_text="Off by default -- nothing ducks until enabled.",
    )
    duck_level_db = models.FloatField(
        default=-12.0,
        help_text="How much to attenuate deck/program audio while the mic is "
                   "live, in dB (negative = quieter, e.g. -12 = roughly quarter "
                   "volume). Ramped over ~500ms on PTT toggle, not instant.",
    )

    class Meta:
        verbose_name = "Ducking Config"
        verbose_name_plural = "Ducking Config"

    def __str__(self):
        return "Ducking Config"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class RemoteDJAudioInput(models.Model):
    """Singleton -- engine-side gain (and any future engine-side audio
    processing) for the incoming remote-DJ mic. Separate from the local
    AudioInput row that captures the physical studio mic, since the
    remote mic doesn't have a physical device on this box -- it's an
    RTP stream coming in via WebRTC. Same 'read fresh at session start'
    contract as AudioInput.gain_db (an edit here takes effect on the
    NEXT session connect, not retroactively for an already-live session).
    Grouped under Hardware in the admin as its own section so anything
    else remote-audio-input-related we add later (calibration reference,
    limiter thresholds, whatever) has a natural home."""
    gain_db = models.FloatField(
        default=6.0,
        help_text="Software gain applied to the incoming remote-DJ mic before "
                   "it joins the on-air mix, in dB. Default is +6 dB to compensate "
                   "for browser WebRTC's typical output level being noticeably "
                   "lower than a locally-captured mic through the same preamp -- "
                   "adjust after listening. Read at session start; changes take "
                   "effect on the next Remote DJ connect.",
    )

    class Meta:
        verbose_name = "Remote DJ Audio Input"
        verbose_name_plural = "Remote DJ Audio Input"

    def __str__(self):
        return "Remote DJ Audio Input"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj
