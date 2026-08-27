"""Vendor-neutral read-only transmitter driver package.

Application code selects a stable type through the registry/factory. Vendor
protocols, capability mappings, and construction details remain in peer driver
modules. The exports here preserve the original public service import surface.
"""

from .base import (
    CANONICAL_METRICS,
    TransmitterConfigurationError,
    TransmitterError,
    UnsupportedTransmitterParameter,
    compute_vswr,
)
from .bw_tx300v3 import BWTx300v3Driver
from .cobalt import CobaltTransmitterDriver
from .registry import (
    DRIVER_REGISTRY,
    TRANSMITTER_BW_TX300V3,
    TRANSMITTER_COBALT_C300,
    TRANSMITTER_NONE,
    TransmitterDriverRegistration,
    create_transmitter_driver,
    get_transmitter_registration,
    transmitter_type_choices,
    transmitter_type_requires_password,
)


__all__ = [
    "BWTx300v3Driver",
    "CANONICAL_METRICS",
    "CobaltTransmitterDriver",
    "DRIVER_REGISTRY",
    "TRANSMITTER_BW_TX300V3",
    "TRANSMITTER_COBALT_C300",
    "TRANSMITTER_NONE",
    "TransmitterConfigurationError",
    "TransmitterDriverRegistration",
    "TransmitterError",
    "UnsupportedTransmitterParameter",
    "compute_vswr",
    "create_transmitter_driver",
    "get_transmitter_registration",
    "transmitter_type_choices",
    "transmitter_type_requires_password",
]
