"""Deterministic audio fixtures and measurements for spoken/render tests.

Fixtures are synthesized in Python (``wave`` + ``math``), so they are
byte-identical on every run. Voice is a 440 Hz tone, beds are 1500 Hz, which
lets the tests follow each one separately through a rendition: the rendition
is decoded to 8 kHz mono PCM and a Goertzel filter measures the energy of
each frequency per window.
"""
from __future__ import annotations

import array
import io
import math
import re
import subprocess
import wave

VOICE_HZ = 440.0
# The voice stand-ins are mono; preparation upmixes to stereo exactly as the
# legacy processors did (``-ac 2``), which applies ffmpeg's -3 dB centre mix.
# Beds are stereo. So a mono voice sits 3 dB lower against a bed than its
# file amplitude suggests -- in legacy and native alike.
MONO_UPMIX = 10 ** (-3 / 20)
BED_HZ = 1500.0
ANALYSIS_RATE = 8000


def tone_wav(segments, *, rate=48000, channels=1) -> bytes:
    """``segments``: [(seconds, frequency or None for silence, amplitude)]."""
    frames = array.array("h")
    for seconds, frequency, amplitude in segments:
        count = int(round(seconds * rate))
        for n in range(count):
            value = 0.0 if frequency is None else amplitude * math.sin(2 * math.pi * frequency * n / rate)
            sample = int(max(-1.0, min(1.0, value)) * 32767)
            for _ in range(channels):
                frames.append(sample)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(frames.tobytes())
    return buffer.getvalue()


def voice(seconds=3.0, *, gap=None, amplitude=0.25) -> bytes:
    """A voice stand-in; ``gap`` = (start, length) of silence inside it."""
    if gap is None:
        return tone_wav([(seconds, VOICE_HZ, amplitude)])
    start, length = gap
    return tone_wav([(start, VOICE_HZ, amplitude), (length, None, 0), (seconds - start, VOICE_HZ, amplitude)])


def bed(seconds=2.0, amplitude=0.5) -> bytes:
    return tone_wav([(seconds, BED_HZ, amplitude)], channels=2)


def decode_mono(path) -> array.array:
    raw = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-ac", "1", "-ar",
                          str(ANALYSIS_RATE), "-f", "s16le", "-"], check=True, capture_output=True).stdout
    samples = array.array("h")
    samples.frombytes(raw)
    return samples


def _goertzel(samples, start, end, frequency):
    """Magnitude of ``frequency`` in samples[start:end], Hann-windowed (so a
    strong tone elsewhere does not leak in), normalized so a full-window sine
    of amplitude A (on a +-1.0 scale) reads about A/4."""
    n = max(1, end - start)
    k = 2 * math.cos(2 * math.pi * frequency / ANALYSIS_RATE)
    s1 = s2 = 0.0
    for offset in range(n):
        weight = 0.5 - 0.5 * math.cos(2 * math.pi * offset / n)
        s0 = (samples[start + offset] / 32768.0) * weight + k * s1 - s2
        s2, s1 = s1, s0
    power = s1 * s1 + s2 * s2 - k * s1 * s2
    return math.sqrt(max(power, 0.0)) / n


def energy_profile(path, frequency, window=0.25):
    """[(window start seconds, amplitude-like magnitude)] for one frequency."""
    samples = decode_mono(path)
    step = int(window * ANALYSIS_RATE)
    return [(start / ANALYSIS_RATE, _goertzel(samples, start, min(start + step, len(samples)), frequency))
            for start in range(0, len(samples) - step + 1, step)]


def present(profile, start, end, *, threshold):
    """Every window fully inside [start, end) is above ``threshold``."""
    windows = [value for t, value in profile if t >= start and t + 0.25 <= end]
    return bool(windows) and min(windows) > threshold


def absent(profile, start, end, *, threshold):
    windows = [value for t, value in profile if t >= start and t + 0.25 <= end]
    return bool(windows) and max(windows) < threshold


def integrated_loudness(path) -> float:
    result = subprocess.run(["ffmpeg", "-nostdin", "-nostats", "-i", str(path), "-af", "ebur128", "-f", "null",
                             "-"], capture_output=True, text=True, check=True)
    values = re.findall(r"I:\s+(-?\d+(?:\.\d+)?) LUFS", result.stderr)
    return float(values[-1])


def raw_insert(model, **values):
    """Insert one row WITHOUT the spoken guards -- Django's base
    QuerySet.bulk_create called directly, i.e. the raw-SQL-equivalent path
    that is outside the ORM authority boundary. Only for proving database
    constraints and for fabricating adversarial fixtures."""
    from django.db.models import QuerySet
    return QuerySet.bulk_create(model._base_manager.all(), [model(**values)])[0]
