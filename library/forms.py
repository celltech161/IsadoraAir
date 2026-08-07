from django import forms

from library.services.hidden_track_detection import (
    DEFAULT_MIN_POSITION_SECONDS,
    DEFAULT_MIN_RESUMED_AUDIO_SECONDS,
    DEFAULT_MIN_SILENCE_SECONDS,
    DEFAULT_REQUIRED_ACTIVE_RATIO_PERCENT,
    DEFAULT_RESUMED_AUDIO_THRESHOLD_DB,
    DEFAULT_SILENCE_THRESHOLD_DB,
    MAX_DB_BOUND,
    MAX_MIN_POSITION_SECONDS,
    MAX_MIN_RESUMED_AUDIO_SECONDS,
    MAX_MIN_SILENCE_SECONDS,
    MIN_DB_BOUND,
)


class HiddenTrackDetectionForm(forms.Form):
    """Validates every operator-editable Hidden Track Detection setting
    server-side -- see library/services/hidden_track_detection.py's
    module docstring for what each field means to the detector.
    Deliberately a plain forms.Form (not a ModelForm): none of this is
    persisted anywhere (see that module's "No persistence" contract),
    it only ever configures one synchronous scan.

    Malformed input must never reach the detector or produce an
    unhandled exception -- every field below has an explicit numeric
    bound, and clean_() cross-checks the two dB thresholds against each
    other. is_valid() returning False is the only failure path; the
    view redisplays the form with field-specific errors from here."""

    silence_threshold_db = forms.FloatField(
        label="Silence threshold (dBFS)",
        initial=DEFAULT_SILENCE_THRESHOLD_DB,
        min_value=MIN_DB_BOUND,
        max_value=MAX_DB_BOUND,
        help_text=(
            "Audio at or below this level is treated as silence. "
            "−50 dBFS is a conservative starting point for digitally sourced CD rips."
        ),
    )
    min_silence_seconds = forms.FloatField(
        label="Minimum silent gap (seconds)",
        initial=DEFAULT_MIN_SILENCE_SECONDS,
        min_value=0.1,
        max_value=MAX_MIN_SILENCE_SECONDS,
        help_text=(
            "Hidden tracks commonly follow 20 seconds or more of silence. "
            "Lower values may identify deliberate pauses within ordinary songs."
        ),
    )
    resumed_audio_threshold_db = forms.FloatField(
        label="Resumed-audio threshold (dBFS)",
        initial=DEFAULT_RESUMED_AUDIO_THRESHOLD_DB,
        min_value=MIN_DB_BOUND,
        max_value=MAX_DB_BOUND,
        help_text=(
            "Audio must rise above this level after the quiet gap. Keeping this above "
            "the silence threshold helps reject low-level noise and dither."
        ),
    )
    min_resumed_audio_seconds = forms.FloatField(
        label="Minimum resumed-audio duration (seconds)",
        initial=DEFAULT_MIN_RESUMED_AUDIO_SECONDS,
        min_value=0.1,
        max_value=MAX_MIN_RESUMED_AUDIO_SECONDS,
        help_text=(
            "The returned audio must remain credible for at least this long. "
            "Increase this value to reject short noises or isolated artifacts."
        ),
    )
    required_active_ratio_percent = forms.FloatField(
        label="Required active-audio ratio (%)",
        initial=DEFAULT_REQUIRED_ACTIVE_RATIO_PERCENT,
        min_value=0.0,
        max_value=100.0,
        help_text=(
            "The percentage of post-gap analysis windows that must contain valid audio. "
            "Higher values are stricter."
        ),
    )
    min_position_seconds = forms.FloatField(
        label="Minimum position in track (seconds)",
        initial=DEFAULT_MIN_POSITION_SECONDS,
        min_value=0.0,
        max_value=MAX_MIN_POSITION_SECONDS,
        help_text=(
            "Ignore quiet gaps before this point in the file. "
            "This reduces false positives from unusual intros or pre-roll."
        ),
    )

    # --- Optional library filters ---
    ready2air = forms.ChoiceField(
        label="Ready to air",
        choices=[("yes", "Yes"), ("no", "No"), ("all", "All")],
        initial="yes",
        required=False,
    )
    category = forms.CharField(label="Category", required=False)
    track_id = forms.IntegerField(label="Track ID", required=False, min_value=1)

    def clean(self):
        cleaned = super().clean()
        silence_db = cleaned.get("silence_threshold_db")
        resumed_db = cleaned.get("resumed_audio_threshold_db")
        # Both individual fields already validated as finite numbers
        # within [MIN_DB_BOUND, MAX_DB_BOUND] by FloatField itself --
        # this is the cross-field relationship the task calls out
        # explicitly. A non-fatal ordering issue (still a real
        # validation error, not silently allowed through): the whole
        # design relies on resumed_audio_threshold_db sitting ABOVE
        # silence_threshold_db as two distinct hysteresis levels.
        if silence_db is not None and resumed_db is not None and resumed_db <= silence_db:
            self.add_error(
                "resumed_audio_threshold_db",
                "The resumed-audio threshold should be greater than the silence threshold "
                "(they act as two separate hysteresis levels).",
            )
        return cleaned

    def settings_kwargs(self):
        """(silence_threshold_db, min_silence_seconds, resumed_audio_
        threshold_db, min_resumed_audio_seconds,
        required_active_ratio_percent, min_position_seconds) as a dict
        ready for hidden_track_detection.DetectionSettings(**kwargs).
        Only call after is_valid() returns True."""
        return {
            "silence_threshold_db": self.cleaned_data["silence_threshold_db"],
            "min_silence_seconds": self.cleaned_data["min_silence_seconds"],
            "resumed_audio_threshold_db": self.cleaned_data["resumed_audio_threshold_db"],
            "min_resumed_audio_seconds": self.cleaned_data["min_resumed_audio_seconds"],
            "required_active_ratio_percent": self.cleaned_data["required_active_ratio_percent"],
            "min_position_seconds": self.cleaned_data["min_position_seconds"],
        }
