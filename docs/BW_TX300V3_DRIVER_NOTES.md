# BW Broadcast TX300v3 monitoring driver

IsadoraAir transmitter monitoring supports three stable transmitter types:

- `none` — transmitter monitoring is disabled;
- `cobalt_c300` — Aquabroadcast COBALT C300;
- `bw_tx300v3` — BW Broadcast TX300v3.

The monitoring poller selects one read-only driver through the transmitter
driver registry. It opens one connection for a transmitter polling cycle and
reuses that connection for every supported configured check. Unsupported
checks are omitted without affecting supported transmitter readings or any
non-transmitter monitoring.

The implementations are peers under
`monitoring/services/transmitters/`: shared contracts live in `base.py`, each
vendor protocol lives in its own driver module, and `registry.py` is the sole
authority for stable type slugs, display labels, construction, and declared
configuration capabilities. Adding a transmitter should primarily mean adding
a driver module and one registry entry; MonitorManager and probes do not branch
on vendor type. `cobalt_c300` remains the database migration default only to
preserve existing installations, not because it is a canonical transmitter.

`none` opens no transmitter connection and removes transmitter results from
the live state document, which also removes the Transmitter group from the
Monitoring dashboard.

## Authentication and protocol

The TX300v3 control session uses TCP (normally port 23), a password-only login,
and the `TX-V3>` prompt. The driver does not send a username. Authentication,
command responses, and prompt waits are deadline-bounded; rejected logins and
connections that close before a complete response raise a transmitter error.
Passwords are never included in exceptions, logs, state JSON, or events.

RFC854 option negotiation is filtered by a bounded state machine that carries
an incomplete IAC command across TCP receive boundaries. The driver declines
`DO` with `WONT` and `WILL` with `DONT`; Telnet control bytes never become
password prompts or response text.

The implementation sends only allowlisted `get PARAMETER` requests. It has no
RF on/off, frequency, power, reboot, reset, RDS, alarm, or configuration-write
operation.

## Verified read-only parameters

| Canonical field | TX300v3 parameter | Interpretation |
| --- | --- | --- |
| `forward_power_watts` | `meters.pafwd` | Numeric forward RF power |
| `reflected_power_watts` | `meters.parev` | Numeric reflected RF power |
| `vswr` | derived from forward/reflected power | Guarded computation; unavailable for invalid power readings |
| `pa_temperature_c` | `meters.patemp` | PA temperature; field observations support degrees C, although the protocol response has no unit suffix |
| `rf_output_state` | `metering.rf_out_status` | Normalized state text such as `on` |
| `frequency_hz` | `transmitter.frequency` | Integer Hz |
| `uptime_raw` | `system.uptime` | Vendor free text |
| `power_control_state` | `metering.power_control` | Vendor state text |
| `vswr_limit_active` | `status.VSWRLimitActive` | Recognized on/off state |
| `temperature_limit_active` | `status.TempLimitActive` | Recognized on/off state |
| `fallback_active` | `meters.fallback` | Recognized on/off state; unfamiliar values remain unavailable rather than guessed |
| `fallback_cause` | `metering.fallback_cause` | Vendor free text |
| `product_id` | `system.product.id` | Vendor product identifier |
| `software_version` | `system.software.version` | Firmware/software version text |

`meters.fanspeed` exists and may be read as a raw vendor-specific value, but
its unit is not verified. It is deliberately not exposed as
`fan_speed_rpm`. Other plausible-looking PSU, mains, current, or temperature
counters are not exposed because their scaling and units have not been
verified.

## Existing MonitorCheck compatibility

Existing COBALT-oriented checks continue unchanged on the COBALT driver. The
TX300v3 driver translates these established identifiers where there is a
verified equivalent:

| Existing check identifier | TX300v3 source |
| --- | --- |
| `psu.fwd_power` | `meters.pafwd` |
| `psu.rev_power` | `meters.parev` |
| `psu.vswr` | guarded forward/reflected-power calculation |
| `psu.pa_temperature` | `meters.patemp` |
| `status.indicator.rf` | normalized `metering.rf_out_status` |
| `status.indicator.vswr` | normalized `status.VSWRLimitActive` |
| `status.indicator.temp` | normalized `status.TempLimitActive` |

Only recognized active/inactive values are translated to legacy indicator
colors. An unfamiliar vendor state remains unavailable, so the existing probe
reports `unknown` rather than incorrectly treating it as healthy.

The COBALT fan-RPM check and RF-interlock check are explicitly unsupported on
the TX300v3 because no equivalent semantics have been verified. They are
hidden for that driver rather than queried or displayed as permanent unknown
readings.

All examples and automated tests use documentation-only addresses and fake
credentials. No live transmitter connection is part of repository testing.
