"""The fixed render-step library.

A recipe is a composition of these steps -- nothing else. Each step holds only
typed numbers, re-checked against the engine's own absolute bounds (defense in
depth beneath the recipe's parameter schema), and turns them into ffmpeg
filter fragments with fixed formats. No step accepts, emits or passes through
text from configuration: no filter strings, no expressions, no paths.

Audio contract: the voice is prepared at 48 kHz stereo 16-bit (as the proven
legacy processors did); mixing is ``amix ... normalize=0``; loudness is
two-pass EBU R128 ``loudnorm`` with ``linear=true``; the rendition is FLAC,
48 kHz stereo, 16-bit.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

SAMPLE_RATE = 48000
CHANNELS = 2
_FORMAT = f"aresample={SAMPLE_RATE},aformat=sample_fmts=s16:channel_layouts=stereo"


class StepError(ValueError):
    """A step was composed with a value outside the engine's absolute bounds."""


def _number(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise StepError(f"{name} must be a finite number")
    if not (low <= value <= high):
        raise StepError(f"{name} must be within [{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class Highpass:
    hz: float

    def __post_init__(self):
        _number("highpass frequency", self.hz, 10, 1000)

    def filter(self) -> str:
        return f"highpass=f={self.hz:.3f}"


@dataclass(frozen=True)
class TrimSilence:
    """Remove pauses longer than ``stop_duration`` below ``threshold_db``
    anywhere in the voice (legacy ``silenceremove`` with stop_periods=-1)."""

    threshold_db: float
    stop_duration: float

    def __post_init__(self):
        _number("silence threshold", self.threshold_db, -90, -10)
        _number("silence stop duration", self.stop_duration, 0.05, 10)

    def filter(self) -> str:
        return ("silenceremove=start_periods=0:stop_periods=-1:"
                f"stop_duration={self.stop_duration:.3f}:stop_threshold={self.threshold_db:.2f}dB")


@dataclass(frozen=True)
class DelayVoice:
    """Start the voice ``ms`` milliseconds into the mix."""

    ms: int

    def __post_init__(self):
        if isinstance(self.ms, bool) or not isinstance(self.ms, int):
            raise StepError("voice delay must be whole milliseconds")
        _number("voice delay", self.ms, 0, 60000)

    def filter(self) -> str:
        return f"adelay={self.ms}|{self.ms}"


@dataclass(frozen=True)
class Fade:
    """A fade-out of ``seconds`` -- applied to the bed after the voice ends."""

    seconds: float

    def __post_init__(self):
        _number("fade length", self.seconds, 0.1, 30)


class BedMode(str, Enum):
    # Loop the bed under the whole voice, fade it out where the voice ends,
    # end with the fade (cal_events).
    LOOP_FADE = "loop_fade"
    # Do not loop: if the bed outlasts the voice, fade it where the voice ends
    # and end there; otherwise let it end on its own under the voice (usd239min).
    FADE_IF_LONGER = "fade_if_longer"
    # Do not loop or fade: both play to their natural ends (3min_ctr, news intro).
    NATURAL_END = "natural_end"


@dataclass(frozen=True)
class BedUnderlay:
    """Mix station reference audio (named by a recipe parameter that holds a
    ReferenceAudio key) under the voice at ``gain_db``."""

    reference_param: str
    gain_db: float
    mode: BedMode
    fade: Fade | None = None

    def __post_init__(self):
        _number("bed gain", self.gain_db, -40, 20)
        if not isinstance(self.mode, BedMode):
            raise StepError("unknown bed mode")
        if self.mode is BedMode.NATURAL_END and self.fade is not None:
            raise StepError("a natural-end bed has no fade")
        if self.mode is not BedMode.NATURAL_END and self.fade is None:
            raise StepError("this bed mode needs a fade")

    @property
    def loops(self) -> bool:
        return self.mode is BedMode.LOOP_FADE

    def filter_complex(self, *, voice_end: float, bed_duration: float, delay: DelayVoice | None) -> str:
        """``[0:a]`` is the bed, ``[1:a]`` the prepared voice; output ``[mix]``.
        ``voice_end`` is when the voice ends on the mix timeline (its delay
        included); ``bed_duration`` is the bed's validated decoded length."""
        voice_end = _number("voice end", voice_end, 0, 4 * 3600)
        gain = 10 ** (self.gain_db / 20.0)
        bed = f"{_FORMAT},volume={gain:.6f}"
        duration = "longest"
        if self.mode is BedMode.LOOP_FADE:
            end = voice_end + self.fade.seconds
            bed += f",afade=t=out:st={voice_end:.3f}:d={self.fade.seconds:.3f},atrim=0:{end:.3f}"
            duration = "first"
        elif self.mode is BedMode.FADE_IF_LONGER and bed_duration > voice_end:
            end = voice_end + self.fade.seconds
            bed += (f",apad=pad_dur={self.fade.seconds:.3f},"
                    f"afade=t=out:st={voice_end:.3f}:d={self.fade.seconds:.3f},atrim=0:{end:.3f}")
        voice = _FORMAT + (f",{delay.filter()}" if delay is not None else "")
        return f"[0:a]{bed}[bed];[1:a]{voice}[voice];[bed][voice]amix=inputs=2:duration={duration}:normalize=0[mix]"


@dataclass(frozen=True)
class Loudnorm:
    integrated: float = -16.0
    true_peak: float = -1.0
    lra: float = 5.0
    two_pass: bool = True

    def __post_init__(self):
        _number("integrated loudness", self.integrated, -40, -5)
        _number("true peak", self.true_peak, -9, 0)
        _number("loudness range", self.lra, 1, 20)
        if self.two_pass is not True:
            raise StepError("loudness normalization is always two-pass")

    def targets(self) -> str:
        return f"I={self.integrated:.1f}:TP={self.true_peak:.1f}:LRA={self.lra:.1f}"

    def analysis_filter(self) -> str:
        return f"loudnorm={self.targets()}:print_format=json"

    def apply_filter(self, measured: dict) -> str:
        """``measured`` is the structurally parsed first-pass result (floats)."""
        return (f"loudnorm={self.targets()}:measured_I={measured['input_i']:.2f}:"
                f"measured_LRA={measured['input_lra']:.2f}:measured_TP={measured['input_tp']:.2f}:"
                f"measured_thresh={measured['input_thresh']:.2f}:offset={measured['target_offset']:.2f}:"
                "linear=true:print_format=summary")


@dataclass(frozen=True)
class EncodeFlac:
    sample_rate: int = SAMPLE_RATE
    channels: int = CHANNELS
    compression_level: int = 8

    def __post_init__(self):
        if self.sample_rate != SAMPLE_RATE or self.channels != CHANNELS:
            raise StepError("renditions are 48 kHz stereo")
        if not (isinstance(self.compression_level, int) and 0 <= self.compression_level <= 12):
            raise StepError("FLAC compression level must be 0..12")

    def output_args(self) -> list[str]:
        return ["-ar", str(self.sample_rate), "-ac", str(self.channels), "-c:a", "flac", "-sample_fmt", "s16",
                "-compression_level", str(self.compression_level), "-map_metadata", "-1",
                "-fflags", "+bitexact", "-flags:a", "+bitexact"]


@dataclass(frozen=True)
class RenderPlan:
    """One recipe's fixed composition, with its resolved numbers."""

    voice: tuple = ()                      # Highpass / TrimSilence, applied in order
    bed: BedUnderlay | None = None
    delay: DelayVoice | None = None
    loudnorm: Loudnorm = Loudnorm()
    encode: EncodeFlac = EncodeFlac()

    def __post_init__(self):
        for step in self.voice:
            if not isinstance(step, (Highpass, TrimSilence)):
                raise StepError("voice steps are Highpass or TrimSilence")
        if self.delay is not None and self.bed is None:
            raise StepError("a voice delay is relative to a bed")

    def voice_filter(self) -> str:
        return ",".join(step.filter() for step in self.voice)
