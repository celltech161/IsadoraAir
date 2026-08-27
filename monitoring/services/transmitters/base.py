"""Shared contracts and helpers for read-only transmitter drivers."""

import math

from monitoring.services.transmitter_client import TransmitterError


class TransmitterConfigurationError(TransmitterError):
    """The configured transmitter type cannot be safely constructed."""


class UnsupportedTransmitterParameter(TransmitterError):
    """A driver explicitly does not support a requested check/parameter."""


CANONICAL_METRICS = (
    "forward_power_watts",
    "reflected_power_watts",
    "vswr",
    "pa_temperature_c",
    "rf_output_state",
    "frequency_hz",
    "uptime_raw",
    "power_control_state",
    "vswr_limit_active",
    "temperature_limit_active",
    "fallback_active",
    "fallback_cause",
    "product_id",
    "software_version",
)


def compute_vswr(forward_power, reflected_power):
    """Return VSWR or ``None`` when the power readings are not physical."""
    try:
        forward = float(forward_power)
        reflected = float(reflected_power)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(forward) or not math.isfinite(reflected):
        return None
    if forward <= 0 or reflected < 0 or reflected >= forward:
        return None
    rho = math.sqrt(reflected / forward)
    if rho >= 1:
        return None
    return (1 + rho) / (1 - rho)
