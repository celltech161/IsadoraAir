"""Aquabroadcast COBALT C300 transmitter driver."""

import math

from monitoring.services.transmitter_client import TransmitterClient, parse_numeric


TYPE_SLUG = "cobalt_c300"
DISPLAY_LABEL = "Aquabroadcast COBALT C300"


class CobaltTransmitterDriver:
    """Thin adapter around the live-proven COBALT client."""

    type_slug = TYPE_SLUG
    requires_password = False
    supported_canonical_metrics = frozenset({
        "forward_power_watts",
        "reflected_power_watts",
        "vswr",
        "pa_temperature_c",
    })
    _CANONICAL_PARAMETERS = {
        "forward_power_watts": "psu.fwd_power",
        "reflected_power_watts": "psu.rev_power",
        "vswr": "psu.vswr",
        "pa_temperature_c": "psu.pa_temperature",
    }

    def __init__(self, host, port, timeout=3.0):
        self._client = TransmitterClient(host, port, timeout)

    @classmethod
    def from_config(cls, config):
        return cls(config.host, config.port, timeout=config.timeout_seconds)

    def __enter__(self):
        self._client.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._client.__exit__(exc_type, exc, tb)

    def supports_reference(self, reference):
        # COBALT historically accepted arbitrary raw get-parameters from
        # operator-created MonitorCheck rows. Preserve that exact behavior.
        return bool(reference)

    def get(self, parameter):
        return self._client.get(parameter)

    def read_status(self, metrics=None):
        requested = set(
            self.supported_canonical_metrics if metrics is None else metrics
        )
        status = {}
        for metric in requested & self.supported_canonical_metrics:
            raw = self.get(self._CANONICAL_PARAMETERS[metric])
            if raw is None:
                continue
            value = parse_numeric(raw)
            if value is not None and math.isfinite(value):
                status[metric] = value
        return status
