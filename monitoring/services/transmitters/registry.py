"""Stable transmitter type registry and vendor-neutral driver factory."""

from dataclasses import dataclass

from .base import TransmitterConfigurationError
from .bw_tx300v3 import (
    DISPLAY_LABEL as BW_TX300V3_LABEL,
    TYPE_SLUG as TRANSMITTER_BW_TX300V3,
    BWTx300v3Driver,
)
from .cobalt import (
    DISPLAY_LABEL as COBALT_C300_LABEL,
    TYPE_SLUG as TRANSMITTER_COBALT_C300,
    CobaltTransmitterDriver,
)


TRANSMITTER_NONE = "none"


@dataclass(frozen=True)
class TransmitterDriverRegistration:
    """Stable type metadata and construction contract for one driver."""

    type_slug: str
    display_label: str
    driver_class: type | None

    @property
    def requires_password(self):
        return bool(
            self.driver_class
            and getattr(self.driver_class, "requires_password", False)
        )

    def build(self, config):
        if self.driver_class is None:
            return None
        return self.driver_class.from_config(config)


DRIVER_REGISTRY = {
    TRANSMITTER_NONE: TransmitterDriverRegistration(
        TRANSMITTER_NONE, "None / disabled", None
    ),
    TRANSMITTER_COBALT_C300: TransmitterDriverRegistration(
        TRANSMITTER_COBALT_C300, COBALT_C300_LABEL, CobaltTransmitterDriver
    ),
    TRANSMITTER_BW_TX300V3: TransmitterDriverRegistration(
        TRANSMITTER_BW_TX300V3, BW_TX300V3_LABEL, BWTx300v3Driver
    ),
}


def transmitter_type_choices():
    """Return stable model/admin choices from the driver registry."""
    return [
        (registration.type_slug, registration.display_label)
        for registration in DRIVER_REGISTRY.values()
    ]


def get_transmitter_registration(transmitter_type):
    try:
        return DRIVER_REGISTRY[transmitter_type]
    except KeyError:
        raise TransmitterConfigurationError(
            f"Unsupported transmitter type {transmitter_type!r}."
        ) from None


def transmitter_type_requires_password(transmitter_type):
    """Expose an authentication capability without application vendor checks."""
    return get_transmitter_registration(transmitter_type).requires_password


def create_transmitter_driver(config):
    """Build an unconnected registered driver, or ``None`` when disabled."""
    return get_transmitter_registration(config.transmitter_type).build(config)
