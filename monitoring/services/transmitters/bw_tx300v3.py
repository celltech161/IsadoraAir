"""BW Broadcast TX300v3 transmitter driver."""

import math
import socket
import time

from monitoring.services.transmitter_client import TransmitterError, parse_numeric

from .base import (
    TransmitterConfigurationError,
    UnsupportedTransmitterParameter,
    compute_vswr,
)


TYPE_SLUG = "bw_tx300v3"
DISPLAY_LABEL = "BW Broadcast TX300v3"


class BWTx300v3Driver:
    """Password-authenticated, read-only client for the BW TX300v3.

    Only the verified ``get`` parameters below are admitted. No method in
    this driver sends RF, configuration, reset, or other control commands.
    """

    type_slug = TYPE_SLUG
    requires_password = True
    prompt = b"TX-V3>"
    password_prompt = b"Password:"
    max_response_bytes = 64 * 1024
    _IAC = 255
    _WILL = 251
    _WONT = 252
    _DO = 253
    _DONT = 254
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
        self._iac_state = "data"
        self._iac_command = None

    @classmethod
    def from_config(cls, config):
        return cls(
            config.host,
            config.port,
            password=config.password,
            timeout=config.timeout_seconds,
        )

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
            self._reset_iac_state()
            self._read_until(
                self.password_prompt, "password prompt", case_insensitive=True
            )
            try:
                password_bytes = self.password.encode("ascii")
            except UnicodeEncodeError:
                raise TransmitterError(
                    "Transmitter password must contain only ASCII characters."
                ) from None
            self._sock.sendall(password_bytes + b"\r\n")
            self._read_until(
                self.prompt, "authentication response", authenticating=True
            )
            return self
        except Exception:
            self._close()
            raise

    def __exit__(self, exc_type, exc, tb):
        self._close()
        return False

    def _reset_iac_state(self):
        self._iac_state = "data"
        self._iac_command = None

    def _close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""
                self._native_cache = {}
                self._reset_iac_state()

    def _strip_iac(self, data):
        """Filter RFC854 negotiation with bounded state across recv calls.

        The state machine retains at most a command byte while waiting for an
        option byte. The BW monitoring client declines every option it is
        asked to perform or accept and never exposes Telnet control bytes as
        transmitter response text.
        """
        out = bytearray()
        for byte in data:
            if self._iac_state == "data":
                if byte == self._IAC:
                    self._iac_state = "command"
                else:
                    out.append(byte)
                continue

            if self._iac_state == "command":
                if byte in {self._DO, self._DONT, self._WILL, self._WONT}:
                    self._iac_command = byte
                    self._iac_state = "option"
                else:
                    # Other two-byte Telnet commands (and escaped IAC) are
                    # control traffic, not ASCII application response data.
                    self._reset_iac_state()
                continue

            command = self._iac_command
            if command == self._DO:
                self._sock.sendall(bytes([self._IAC, self._WONT, byte]))
            elif command == self._WILL:
                self._sock.sendall(bytes([self._IAC, self._DONT, byte]))
            self._reset_iac_state()
        return bytes(out)

    def _read_until(
        self, marker, description, *, authenticating=False, case_insensitive=False
    ):
        # bytes.lower() only folds ASCII A-Z and leaves every other byte
        # (including Telnet IAC/option bytes already stripped by
        # _strip_iac) untouched, so this is exactly ASCII case-insensitive
        # matching -- never a general/locale-aware casefold. Only the
        # marker search uses the folded view; self._buf itself, and the
        # bytes returned/retained from it, keep their original case.
        search_marker = marker.lower() if case_insensitive else marker

        def _find(buf):
            haystack = buf.lower() if case_insensitive else buf
            return haystack.find(search_marker)

        deadline = time.monotonic() + self.timeout
        while _find(self._buf) == -1:
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

        index = _find(self._buf)
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
        if raw is None:
            return None
        normalized = raw.strip().lower()
        active = {"1", "active", "enabled", "on", "true", "yes"}
        inactive = {"0", "inactive", "disabled", "off", "false", "no"}
        if normalized in active:
            return "R" if active_is_fault else "G"
        if normalized in inactive:
            return "G" if active_is_fault else "R"
        return None

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
                value = (
                    int(numeric)
                    if numeric is not None and numeric.is_integer()
                    else None
                )
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
