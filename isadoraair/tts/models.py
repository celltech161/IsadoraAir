"""Station-owned logical TTS configuration.

These models deliberately contain identities and checksums, never runtime or
asset paths.  Runtime locations remain in the Git-owned component manifest.
"""

from __future__ import annotations

import math
from pathlib import PurePath

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from isadoraair.tts.voice_catalog import KOKORO_PROVIDER_VOICE_ID_SET


SHA256_VALIDATOR = RegexValidator(
    regex=r"^[0-9a-f]{64}$",
    message="Enter a lowercase 64-character SHA-256 digest.",
)
LANGUAGE_VALIDATOR = RegexValidator(
    regex=r"^[A-Za-z][A-Za-z0-9_-]{0,31}$",
    message="Enter a language identifier such as en-us or en_US.",
)


def _plain_filename(value: str, *, suffix: str) -> bool:
    return bool(value) and PurePath(value).name == value and value.endswith(suffix)


class PiperVoiceModel(models.Model):
    """Authoritative metadata for one externally provisioned Piper model."""

    model_id = models.SlugField(
        max_length=128,
        unique=True,
        help_text="Stable model identity; not a filename or filesystem path.",
    )
    model_filename = models.CharField(
        max_length=255,
        help_text="Basename of the .onnx file under the canonical Piper asset root.",
    )
    config_filename = models.CharField(
        max_length=255,
        help_text="Basename of the paired .onnx.json file under the canonical Piper asset root.",
    )
    model_sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    config_sha256 = models.CharField(max_length=64, validators=[SHA256_VALIDATOR])
    language = models.CharField(max_length=32, validators=[LANGUAGE_VALIDATOR])
    sample_rate_hz = models.PositiveIntegerField(
        help_text="Exact native sample rate declared by this model's JSON configuration.",
    )

    class Meta:
        ordering = ["model_id"]
        verbose_name = "Piper Voice Model"
        verbose_name_plural = "Piper Voice Models"

    def __str__(self):
        return self.model_id

    def clean(self):
        super().clean()
        errors = {}
        if not _plain_filename(self.model_filename, suffix=".onnx"):
            errors["model_filename"] = "Use one .onnx basename without directory components."
        if not _plain_filename(self.config_filename, suffix=".onnx.json"):
            errors["config_filename"] = "Use one .onnx.json basename without directory components."
        if self.model_filename and self.config_filename != f"{self.model_filename}.json":
            errors["config_filename"] = "The config must be the JSON sidecar paired with the model."
        if self.sample_rate_hz and not 8000 <= self.sample_rate_hz <= 192000:
            errors["sample_rate_hz"] = "Sample rate must be between 8000 and 192000 Hz."
        if errors:
            raise ValidationError(errors)


class StationTTSVoice(models.Model):
    """One stable logical voice selected by station feature configuration."""

    class Engine(models.TextChoices):
        KOKORO = "kokoro", "Kokoro"
        PIPER = "piper", "Piper"

    name = models.SlugField(
        max_length=128,
        unique=True,
        help_text="Stable logical station voice ID exposed to callers.",
    )
    enabled = models.BooleanField(
        default=False,
        help_text="Disabled voices cannot be resolved or synthesized.",
    )
    engine = models.CharField(max_length=16, choices=Engine.choices)
    provider_voice = models.SlugField(
        max_length=128,
        blank=True,
        help_text="Kokoro's native voice ID. Blank for Piper voices.",
    )
    piper_model = models.ForeignKey(
        PiperVoiceModel,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="station_voices",
        help_text="Required only for a Piper logical voice.",
    )
    language = models.CharField(
        max_length=32,
        default="en-us",
        validators=[LANGUAGE_VALIDATOR],
        help_text="Default synthesis language. Piper must match its model-bound language.",
    )
    speed = models.FloatField(
        default=1.0,
        help_text="Default positive speed multiplier; callers may deliberately override it.",
    )

    class Meta:
        ordering = ["name"]
        verbose_name = "Station TTS Voice"
        verbose_name_plural = "Station TTS Voices"

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        errors = {}
        try:
            speed = float(self.speed)
        except (TypeError, ValueError):
            speed = 0
        if not math.isfinite(speed) or speed <= 0:
            errors["speed"] = "Speed must be a positive finite number."
        if self.engine == self.Engine.KOKORO:
            if not self.provider_voice:
                errors["provider_voice"] = "A Kokoro voice requires a provider voice ID."
            elif self.provider_voice not in KOKORO_PROVIDER_VOICE_ID_SET:
                errors["provider_voice"] = "Select a supported Kokoro provider voice."
            if self.piper_model_id is not None:
                errors["piper_model"] = "A Kokoro voice cannot select a Piper model."
        elif self.engine == self.Engine.PIPER:
            if self.piper_model_id is None:
                errors["piper_model"] = "A Piper voice requires a Piper model."
            if self.provider_voice:
                errors["provider_voice"] = "A Piper voice takes its provider identity from the model."
            if self.piper_model_id is not None and self.language:
                model_language = self.piper_model.language.replace("_", "-").lower()
                voice_language = self.language.replace("_", "-").lower()
                if model_language != voice_language:
                    errors["language"] = "Language must match the selected Piper model."
        if errors:
            raise ValidationError(errors)
