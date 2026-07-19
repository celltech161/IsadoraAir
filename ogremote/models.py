from django.db import models


class OGRemoteConfig(models.Model):
    """Singleton -- master switch and tunables for the OGRemote PA/PSA
    pipeline (remote-studio audio uploads from oakgroveradio.com, via
    ogremote-ingest/get_available_uploads.py). Off by default -- this
    box isn't the production on-air system yet, and kogr-sc's own
    OGRemote instance is still live; enabling this here too would mean
    both processing the same uploads/recalls independently."""

    enabled = models.BooleanField(
        default=False,
        help_text="Master switch. Leave off until this box is the production "
                   "on-air system -- kogr-sc's own OGRemote instance is still "
                   "live and processing the same uploads/recalls otherwise.",
    )
    urgent_pa_replay_interval_minutes = models.PositiveIntegerField(
        default=20,
        help_text="How often the combined urgent/PA message clip re-inserts "
                   "into the live queue while at least one message is still "
                   "active (not yet recalled by its producer). Checked on a "
                   "tight fixed cadence and re-read fresh each time -- no "
                   "restart needed after changing this.",
    )
    poll_interval_minutes = models.PositiveIntegerField(
        default=2,
        help_text="How often get_available_uploads polls the OGRemote API for "
                   "new uploads / recalls. Effective floor is 1 minute (the "
                   "systemd timer's own cadence -- setting this lower makes "
                   "it fire every 1 minute). kogr-sc ran this at 5 minutes; "
                   "isadoraair ships at 2. Re-read fresh at the start of "
                   "every poll fire, so a change here takes effect on the "
                   "next tick with no restart needed.",
    )
    notify_email = models.EmailField(
        blank=True, default="",
        help_text="Address for OGRemote pipeline failure notifications. Blank disables.",
    )

    class Meta:
        verbose_name = "OGRemote Configuration"
        verbose_name_plural = "OGRemote Configuration"

    def __str__(self):
        return "OGRemote Configuration"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class OGRemoteCategory(models.Model):
    """One row per OGRemote category_key (a fixed set the ingest scripts
    know how to process -- adding a genuinely new category type still
    needs a code change for its audio-processing recipe). This table is
    the LOCAL authority for where a category's output goes and whether
    it's recallable -- deliberately not driven by the processor_script
    path the remote API used to supply per upload row, which meant
    blindly subprocess-ing a path a remote response told us to run.
    Lets a new category (e.g. 3min_ctr) get wired up here -- pick the
    target Category, flip it on -- without a code change, once its
    processing recipe exists in code."""

    OUTPUT_MODE_CHOICES = [
        ("single", "Single file (newest upload overwrites)"),
        ("accumulating", "Accumulating (each submission is its own track)"),
    ]

    category_key = models.SlugField(
        max_length=50, unique=True,
        help_text="Matches the category_key the remote API sends, e.g. 'cal_events'.",
    )
    name = models.CharField(max_length=100, help_text="Internal label only.")
    target_category = models.ForeignKey(
        "library.Category", on_delete=models.PROTECT,
        help_text="Library category delivered tracks land in.",
    )
    output_mode = models.CharField(max_length=12, choices=OUTPUT_MODE_CHOICES, default="single")
    recallable = models.BooleanField(
        default=True,
        help_text="Whether a producer-requested recall removes this category's "
                   "delivered output (file + Track row).",
    )
    enabled = models.BooleanField(default=True)
    artist_tag = models.CharField(
        max_length=100, blank=True,
        help_text="ID3 artist tag applied to delivered tracks, e.g. 'Calendar of Events'.",
    )

    class Meta:
        verbose_name = "OGRemote Category"
        verbose_name_plural = "OGRemote Categories"
        ordering = ["category_key"]

    def __str__(self):
        return f"{self.category_key} -> {self.target_category.code}"
