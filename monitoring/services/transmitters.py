"""Vendor-neutral, read-only transmitter drivers and driver factory.

The monitoring poller talks only to this module.  Vendor protocol details stay
inside the drivers, while existing COBALT MonitorCheck identifiers continue to
work through each driver's compatibility mapping.
"""

import math
import socket
import time

from monitoring.services.transmitter_client import (
    TransmitterClient,
    TransmitterError,
    parse_numeric,
)


TRANSMITTER_NONE = "none"
TRANSMITTER_COBALT_C300 = "cobalt_c300"
TRANSMITTER_BW_TX300V3 = "bw_tx300v3"


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


class CobaltTransmitterDriver:
    """Thin adapter around the live-proven COBALT client."""

    type_slug = TRANSMITTER_COBALT_C300
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

    def __enter__(self):
        self._client.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        return self._client.__exit__(exc_type, exc, tb)

    def supports_reference(self, reference):
        # COBALT historically accepted arbitrary raw get-parameters from
        # operator-created MonitorCheck rows.  Preserve that exact behavior.
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


class BWTx300v3Driver:
    """Password-authenticated, read-only client for the BW TX300v3.

    Only the verified ``get`` parameters below are admitted.  No method in
    this driver sends RF, configuration, reset, or other control commands.
    """

    type_slug = TRANSMITTER_BW_TX300V3
    prompt = b"TX-V3>"
    password_prompt = b"Password:"
    max_response_bytes = 64 * 1024
    _AUTH_FAILURE_MARKERS = (
        b"access denied",
        b"authentication failed",
        b"incorrect",
        b"denied",
        b"failed",
        b"invalid password",
        b"login failed",
        b"login incorrect",
        b"rejected",
    )

    _CANONICAL_PARAMETERS = {
        "forward_power_watts": "meters.pafwd",
        "reflected_power_watts": "meters.parev",
        "pa_temperature_c": "meters.patemp",
        "rf_output_state": "metering.rf_out_status",
        "frequency_hz": "transmitter.frequency",
        "uptime_raw": "system.uptime",
        "power_control_state": "metering.power_control",
        "vswr_limit_active": "status.VSWRLimitActive",
        "temperature_limit_active": "status.TempLimitActive",
        "fallback_active": "meters.fallback",
        "fallback_cause": "metering.fallback_cause",
        "product_id": "system.product.id",
        "software_version": "system.software.version",
    }
    supported_canonical_metrics = frozenset((*_CANONICAL_PARAMETERS, "vswr"))
    safe_native_parameters = frozenset({
        *_CANONICAL_PARAMETERS.values(),
        "meters.fanspeed",
    })
    _LEGACY_NUMERIC = {
        "psu.fwd_power": "meters.pafwd",
        "psu.rev_power": "meters.parev",
        "psu.pa_temperature": "meters.patemp",
    }
    _LEGACY_INDICATORS = {
        "status.indicator.rf",
        "status.indicator.vswr",
        "status.indicator.temp",
    }
    _UNSUPPORTED_LEGACY = {
        "psu.fan_speed_measured_fan1",
        "status.rf_interlock",
    }

    def __init__(self, host, port, password, timeout=3.0):
        self.host = host
        self.port = port
        self.password = password
        self.timeout = timeout
        self._sock = None
        self._buf = b""
        self._native_cache = {}

    def __enter__(self):
        try:
            if not self.password:
                raise TransmitterConfigurationError(
                    "BW TX300v3 monitoring requires a password."
                )
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.timeout
            )
            self._sock.settimeout(self.timeout)
            self._buf = b""
            self._native_cache = {}
            self._read_until(self.password_prompt, "password prompt")
            try:
                password_bytes = self.password.encode("ascii")
            except UnicodeEncodeError:
                raise TransmitterError(
                    "Transmitter password must contain only ASCII characters."
                ) from None
            self._sock.sendall(password_bytes + b"\r\n")
            self._read_until(self.prompt, "authentication response", authenticating=True)
            return self
        except Exception:
            self._close()
            raise

    def __exit__(self, exc_type, exc, tb):
        self._close()
        return False

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""
                self._native_cache = {}

    def _strip_iac(self, data):
        """Strip RFC854 option negotiation and decline requested options."""
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == 255 and i + 2 < len(data):
                command, option = data[i + 1], data[i + 2]
                if command == 253:  # DO
                    self._sock.sendall(bytes([255, 252, option]))  # WONT
                elif command == 251:  # WILL
                    self._sock.sendall(bytes([255, 254, option]))  # DONT
                i += 3
                continue
            out.append(data[i])
            i += 1
        return bytes(out)

    def _read_until(self, marker, description, *, authenticating=False):
        deadline = time.monotonic() + self.timeout
        while marker not in self._buf:
            if authenticating and any(
                failure in self._buf.lower()
                for failure in self._AUTH_FAILURE_MARKERS
            ):
                raise TransmitterError("Transmitter authentication failed.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransmitterError(
                    f"Timed out waiting for transmitter {description}."
                )
            self._sock.settimeout(min(self.timeout, remaining))
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                if authenticating:
                    raise TransmitterError("Transmitter authentication failed.")
                raise TransmitterError(
                    f"Transmitter closed the connection before the {description}."
                )
            self._buf += self._strip_iac(chunk)
            if len(self._buf) > self.max_response_bytes:
                raise TransmitterError(
                    f"Transmitter {description} exceeded the response-size limit."
                )

        index = self._buf.index(marker)
        result = self._buf[:index]
        self._buf = self._buf[index + len(marker):]
        return result

    def supports_reference(self, reference):
        if not reference or reference in self._UNSUPPORTED_LEGACY:
            return False
        return (
            reference in self.safe_native_parameters
            or reference in self._LEGACY_NUMERIC
            or reference == "psu.vswr"
            or reference in self._LEGACY_INDICATORS
        )

    def _get_native(self, parameter):
        if parameter not in self.safe_native_parameters:
            raise UnsupportedTransmitterParameter(
                f"BW TX300v3 parameter {parameter!r} is not in the read-only allowlist."
            )
        if parameter in self._native_cache:
            return self._native_cache[parameter]
        command = f"get {parameter}"
        self._sock.sendall((command + "\r\n").encode("ascii"))
        raw = self._read_until(self.prompt, "command response")
        text = raw.decode("ascii", errors="replace")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        body = [line for line in lines if line != command]
        if any("unknown parameter" in line.lower() for line in body):
            raise UnsupportedTransmitterParameter(
                f"BW TX300v3 does not support parameter {parameter!r}."
            )
        value = body[0] if body else None
        self._native_cache[parameter] = value
        return value

    @staticmethod
    def _legacy_binary_indicator(raw, *, active_is_fault):
        normalized = raw.strip().lower()
        active = {"1", "active", "enabled", "on", "true", "yes"}
        inactive = {"0", "inactive", "disabled", "off", "false", "no"}
        if normalized in active:
            return "R" if active_is_fault else "G"
        if normalized in inactive:
            return "G" if active_is_fault else "R"
        return "0"

    def get(self, parameter):
        if not self.supports_reference(parameter):
            raise UnsupportedTransmitterParameter(
                f"BW TX300v3 does not support configured check {parameter!r}."
            )
        if parameter in self.safe_native_parameters:
            return self._get_native(parameter)
        if parameter in self._LEGACY_NUMERIC:
            return self._get_native(self._LEGACY_NUMERIC[parameter])
        if parameter == "psu.vswr":
            forward = parse_numeric(self._get_native("meters.pafwd"))
            reflected = parse_numeric(self._get_native("meters.parev"))
            value = compute_vswr(forward, reflected)
            return str(value) if value is not None else None
        if parameter == "status.indicator.rf":
            return self._legacy_binary_indicator(
                self._get_native("metering.rf_out_status"), active_is_fault=False
            )
        if parameter == "status.indicator.vswr":
            return self._legacy_binary_indicator(
                self._get_native("status.VSWRLimitActive"), active_is_fault=True
            )
        if parameter == "status.indicator.temp":
            return self._legacy_binary_indicator(
                self._get_native("status.TempLimitActive"), active_is_fault=True
            )
        raise UnsupportedTransmitterParameter(
            f"BW TX300v3 does not support configured check {parameter!r}."
        )

    @staticmethod
    def _numeric(raw):
        value = parse_numeric(raw)
        return value if value is not None and math.isfinite(value) else None

    @staticmethod
    def _on_off(raw):
        normalized = raw.strip().lower()
        if normalized in {"1", "active", "enabled", "on", "true", "yes"}:
            return True
        if normalized in {"0", "inactive", "disabled", "off", "false", "no"}:
            return False
        return None

    def read_status(self, metrics=None):
        requested = set(
            self.supported_canonical_metrics if metrics is None else metrics
        )
        requested &= self.supported_canonical_metrics
        native_cache = {}

        def native(parameter):
            if parameter not in native_cache:
                native_cache[parameter] = self._get_native(parameter)
            return native_cache[parameter]

        status = {}
        numeric_metrics = {
            "forward_power_watts",
            "reflected_power_watts",
            "pa_temperature_c",
        }
        on_off_metrics = {
            "vswr_limit_active",
            "temperature_limit_active",
            "fallback_active",
        }
        for metric in requested - {"vswr"}:
            parameter = self._CANONICAL_PARAMETERS[metric]
            try:
                raw = native(parameter)
            except UnsupportedTransmitterParameter:
                continue
            if raw is None:
                continue
            if metric in numeric_metrics:
                value = self._numeric(raw)
            elif metric == "frequency_hz":
                numeric = self._numeric(raw)
                value = int(numeric) if numeric is not None and numeric.is_integer() else None
            elif metric in on_off_metrics:
                value = self._on_off(raw)
            elif metric == "rf_output_state":
                value = raw.strip().lower()
            else:
                value = raw.strip()
            if value is not None:
                status[metric] = value

        if "vswr" in requested:
            try:
                forward = self._numeric(native("meters.pafwd"))
                reflected = self._numeric(native("meters.parev"))
            except UnsupportedTransmitterParameter:
                forward = reflected = None
            value = compute_vswr(forward, reflected)
            if value is not None:
                status["vswr"] = value
        return status


DRIVER_REGISTRY = {
    TRANSMITTER_COBALT_C300: CobaltTransmitterDriver,
    TRANSMITTER_BW_TX300V3: BWTx300v3Driver,
}


def create_transmitter_driver(config):
    """Build an unconnected driver for ``config`` or return None if disabled."""
    transmitter_type = config.transmitter_type
    if transmitter_type == TRANSMITTER_NONE:
        return None
    try:
        driver_class = DRIVER_REGISTRY[transmitter_type]
    except KeyError:
        raise TransmitterConfigurationError(
            f"Unsupported transmitter type {transmitter_type!r}."
        ) from None
    common = (config.host, config.port)
    if transmitter_type == TRANSMITTER_BW_TX300V3:
        return driver_class(
            *common, password=config.password, timeout=config.timeout_seconds
        )
    return driver_class(*common, timeout=config.timeout_seconds)
