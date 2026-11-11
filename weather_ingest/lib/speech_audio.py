"""Shared FLAC encode for generated Weather speech (loudness + peak safeguard).

Every Weather producer (current_temp.py, wx_forecast.py, wx_alert.py, amber_alert.py) hands one finished
speech WAV (or a concat list of them) to ffmpeg exactly once.  This module owns that one ffmpeg command so
all four share one encode chain:

    aresample=44100                          pipeline rate
    aformat=fltp, stereo                     ffmpeg's power-preserving mono->stereo upmix (same as ``-ac 2``)
    volume=<gain> dB                         calibrated, per product and per announcer voice
    alimiter=limit=0.84:level=false:latency=1
                                             true-peak safeguard at about -1.5 dBFS; ``level=false`` stops the
                                             limiter's automatic make-up gain; ``latency=1`` keeps the audio
                                             time-aligned (the default shifts it by 5 ms)
    adelay=1000:all=1                        the 1 s lead-in, unchanged, AFTER the limiter so silence is silence

and writes 44.1 kHz stereo 16-bit FLAC (lossless; no MP3 delay/padding, no 320 kbps ceiling).

Calibration (2026-11-11, existing station audio, no synthesis on air): the generated speech is far below the
station's -16 LUFS reference (Max Weatherly and Claira Sky: about -22 LUFS as published).  The gains below bring
their products to about -16 LUFS with a true peak of about -1.5 dBTP.  The gain is FIXED per product and voice (a
single deterministic pass, no per-clip measurement): the limiter bounds peaks, and a voice that has no
calibrated entry falls back to the DEFAULT row with a logged warning rather than failing the broadcast.

KanDrive (road_conditions) keeps its own two-pass loudnorm and is deliberately not touched by this module.
"""

import logging
import subprocess

log = logging.getLogger(__name__)

SAMPLE_RATE = 44100
LEAD_IN_MS = 1000
LIMITER_CEILING = 0.84          # linear; about -1.5 dBFS
FLAC_COMPRESSION_LEVEL = "5"
ENCODE_TIMEOUT_SECONDS = 120

PRODUCTS = ("temp", "obs", "forecast", "alert")

#: Fallback row for a voice with no calibrated entry (Max Weatherly's measured values: the quieter of the two
#: announcers known at calibration time, so an unknown voice is never boosted further than a known one).
DEFAULT_GAIN_DB = {"temp": 4.75, "obs": 6.0, "forecast": 6.0, "alert": 6.0}

#: Gain in dB by logical station voice (``voice["logical_voice"]``) and product.
VOICE_GAIN_DB = {
    "Max_Weatherly": {"temp": 4.75, "obs": 6.0, "forecast": 6.0, "alert": 6.0},
    # Claira Sky: temp (two clips) -16.0 LUFS at +4.75; obs and forecast -16.1 LUFS at +6.5. She was measured as
    # about 0.2 dB quieter than Max on the long products. ``alert`` has NO Claira sample (no alert has aired in her
    # hours since calibration began) and takes the same value as her forecast/obs: provisional until one is measured.
    "Claira_Sky": {"temp": 4.75, "obs": 6.5, "forecast": 6.5, "alert": 6.5},
}


def gain_db_for(product, voice):
    """The calibrated gain for ``product`` ("temp"/"obs"/"forecast"/"alert") spoken by ``voice``.

    ``voice`` is the dict ``voices.resolve_voice`` returns (or None).  Logs a warning, and uses the DEFAULT row,
    for an uncalibrated voice."""
    if product not in PRODUCTS:
        raise ValueError(f"unknown Weather speech product {product!r}")
    logical = (voice or {}).get("logical_voice") if isinstance(voice, dict) else None
    row = VOICE_GAIN_DB.get(logical)
    if row is None:
        log.warning("No loudness calibration for voice %r (product %s); using the default gain %.2f dB.",
                    logical, product, DEFAULT_GAIN_DB[product])
        return DEFAULT_GAIN_DB[product]
    return row[product]


def filter_chain(gain_db):
    """The -af chain (see the module docstring)."""
    gain = float(gain_db)
    if not -20.0 <= gain <= 20.0:
        raise ValueError(f"speech gain {gain} dB is outside the sane range")
    return (
        f"aresample={SAMPLE_RATE},aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"volume={gain:.3f}dB,alimiter=limit={LIMITER_CEILING}:level=false:latency=1,"
        f"adelay={LEAD_IN_MS}:all=1"
    )


def ffmpeg_args(input_args, output_path, gain_db, metadata):
    """The complete ffmpeg argv: ``input_args`` is the list of input options and the input itself
    (e.g. ``["-i", wav]`` or ``["-f", "concat", "-safe", "0", "-i", list]``)."""
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *input_args, "-af", filter_chain(gain_db),
           "-c:a", "flac", "-sample_fmt", "s16", "-compression_level", FLAC_COMPRESSION_LEVEL]
    for key, value in metadata.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd.append(str(output_path))
    return cmd


def encode_flac(input_args, output_path, gain_db, metadata):
    """Run the encode.  Raises ``subprocess.CalledProcessError`` / ``TimeoutExpired`` on failure -- callers
    already treat any exception as "conversion failed, publish nothing"."""
    subprocess.run(ffmpeg_args(input_args, output_path, gain_db, metadata), check=True,
                   timeout=ENCODE_TIMEOUT_SECONDS)
