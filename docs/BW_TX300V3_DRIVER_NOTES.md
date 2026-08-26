# BW TX300v3 Driver Notes

Protocol findings for BW Broadcast TX300v3-family transmitters, backing
`monitoring/services/transmitter_drivers/bw_tx300v3.py`. This module is
additive and standalone: it is not wired into `monitor.py`, has no
Django model dependency, and does not define or assume any common
transmitter-driver interface. That common interface (for switching
between "None", the existing Aquabroadcast COBALT client, this driver,
and future transmitter families) is expected to be introduced
separately during integration.

## Where this came from

An earlier, WRJE-local implementation of a BW Broadcast client
(`BWTransmitterClient`) existed in `monitoring/services/transmitter_client.py`
alongside the COBALT client, added 2026-08-09 and refined through
2026-08-17. It was never part of `origin/main` -- it, and eight other
WRJE-local-only commits (including a separate `import_songs --category`
feature, unrelated to this driver), were dropped from WRJE's live `main`
during an unrelated 2026-08-19/20 git revert-and-remerge that was
resolving a different bug (an EOS/web-request scheduling interaction).
They only survive on the station's `preserved-local-work-20260819`
branch, which was read but not modified to produce this module.

This module ports that recovered implementation's protocol logic,
re-verifies it live against a real unit, fixes one real bug found during
that re-verification (below), and drops the pieces the new B3 scope
excludes (no `TransmitterConfig` model fields, no wiring into
`monitor.py`/`probes.py`, no VSWR-alarm-threshold/debounce logic --
those were separate, later local commits on top of the original client
and belong to the integration layer, not this isolated driver).

## Live re-verification (2026-08-26, WRJE-LP's real unit)

Connected to the real TX300v3 at WRJE-LP over its ASCII control port
(TCP, same port the COBALT client uses -- 23) using a password supplied
by the station operator over chat for this one diagnostic session only.
That password is not recorded anywhere in this repo, this module, its
tests, or this document -- see "Credentials" below.

**Login is password-only** on this real unit -- it never prompts for a
username at all, unlike the recovered implementation's docstring, which
claimed some BW units ask for a username first. That username-then-password
branch is kept in the driver (nothing here disproves it exists on other
units/firmware), but it is unverified by this session -- WRJE's own unit
simply doesn't exercise it. Treat it as preserved-but-unconfirmed.

**Bug found and fixed during re-verification:** a wrong password gets a
terminal `...Incorrect password.` response with **no trailing `>`
prompt at all**. The recovered implementation's read loop watched only
for `>`, which means a rejected login would silently hang until the
socket timeout rather than surfacing the actual rejection. Confirmed
this live (first credential attempt was wrong) before finding the right
one. Fixed in this module by watching for either terminal shape.

**Second, independent bug found while writing this module's tests (not
inherited from the recovered code, introduced fresh here and then
caught before merge):** the read loop's original `while/else` structure
treated the remote closing the connection early as equivalent to "done,
marker found" rather than "never got what I was waiting for" -- a
premature close would silently return a truncated response instead of
raising. Fixed by unifying every non-marker exit path (timeout, closed
connection, deadline) to the same explicit `BWTx300V3TimeoutError`.
Caught by `test_get_times_out_when_prompt_never_arrives` before this
ever ran against the real unit.

## Protocol summary

```
(client)  (opens TCP connection, port 23)
(server)  Welcome to the TX-V3!\r\npassword:<SP>
(client)  <password>\r\n
(server)  \r\r\n\r\nTX-V3><SP>
(client)  get meters.pafwd\r\n
(server)  get meters.pafwd\r\n259\r\n\r\nTX-V3><SP>
```
(`<SP>` marks a real trailing space the device sends after its prompt --
written out this way here only to avoid a dangling-whitespace diff.)

- A genuine RFC854 telnet IAC negotiation happens on connect (same as
  the COBALT unit) -- this driver replies WONT/DONT to any WILL/DO it
  sees, same approach as the COBALT client.
- `get` responses are **bare numbers with no unit suffix** (`"259"`,
  not `"259.42 W"`) -- the opposite of the COBALT client's responses,
  which always carry a unit. One exception: `system.uptime` returns a
  free-text duration string, not a number at all.
- **No `OK` terminator.** The COBALT client reads until `OK\r\n`; this
  unit has no such marker. The response is terminated by the next
  shell-style prompt (`TX-V3> `) instead, which has to be read for and
  then is naturally absorbed as part of that same read (unlike the
  COBALT client, there's no separate "drain the trailing prompt before
  the next command" step needed here, since the prompt IS the
  terminator being waited for, not leftover noise after it).
- The command is echoed back before the value (visible in the example
  above) -- the parser drops any line containing the parameter name
  before picking the last remaining non-empty line as the value.
- **An unsupported parameter name gets a clean, distinct response**:
  `Unknown parameter` -- not a timeout, not a dropped connection, not a
  different error shape. This driver raises `BWTx300V3ProtocolError`
  for it rather than returning the literal string as if it were a
  value.
- There is **no discovery/list command**. `help` on the real unit
  returns exactly: `help`, `get`, `set`, `reboot`, `factoryReset` --
  five top-level commands, nothing that enumerates valid `get`
  parameter names. Every parameter name in `KNOWN_PARAMETERS` had to be
  guessed and confirmed individually against the real unit; there is no
  way to ask the device what else it supports.
- One connection, many commands: like the COBALT client, a single
  telnet session is opened, logged into once, and reused for the
  session's `get` calls -- not one-connection-per-command.

## Differences from the COBALT protocol

| Aspect | COBALT | TX300v3 (BW) |
|---|---|---|
| Port | 23 (confirmed) | 23 (confirmed, same field would work if shared in a config model) |
| Auth | None -- straight to `Cobalt>` prompt | Password required; this unit skips username, some units reportedly don't (unverified) |
| Value format | Number + unit suffix (`"249.42 W"`) | Bare number, no unit (`"259"`); one field (`system.uptime`) is a free-text string |
| Response terminator | `OK\r\n`, then a separate un-terminated `Cobalt>` prompt that must be actively drained | No `OK` -- terminated directly by the `TX-V3>` -style prompt |
| Unsupported parameter | (not documented in the COBALT client -- not this session's concern) | Explicit `Unknown parameter` text response |
| Telnet IAC negotiation | Yes (`IAC DO TERMINAL-TYPE` on connect) | Yes, same shape |
| Connection reuse | One session, many commands | One session, many commands |
| VSWR | Not exposed directly; computed client-side from `psu.fwd_power`/`psu.parev`-equivalent meters | Not exposed directly either; computed client-side from `meters.pafwd`/`meters.parev` using the identical formula |

## Verified telemetry / capability matrix

Only parameters actually confirmed live are listed. See
`KNOWN_PARAMETERS` in the module for the canonical, code-adjacent copy
of this list.

| Parameter | Canonical field (`read_status()`) | Example raw value | Notes |
|---|---|---|---|
| `meters.pafwd` | `forward_power_watts` | `"259"` | Watts, confirmed |
| `meters.parev` | `reflected_power_watts` | `"4"` | Watts, confirmed |
| (computed) | `vswr` | -- | Derived from the two above, same formula as the COBALT integration's client-side VSWR calc. Returns `None` both when the inputs are unusable (forward power missing/≤0) and when reflected power is at or above forward power (fault condition) -- the two cases are not distinguished by this driver; the integration layer should decide how to badge/alert them differently if needed. |
| `meters.patemp` | `pa_temperature_c` | `"41"` | Bare number, no unit given by the device. Assumed Celsius by convention (matches the COBALT client's equivalent `psu`-style temperature field and standard broadcast PA reporting) -- **not independently confirmed** against the unit's manual or a second thermometer. |
| `meters.fanspeed` | `fan_speed_raw` | `"16"` | Bare number. **Unit not confirmed** -- could plausibly be a percentage or PWM duty value rather than RPM. Exposed as a raw, unitless value rather than guessing. |
| `system.uptime` | `uptime_raw` | `"20 days, 22:04:36"` | Free-text string, not numeric -- do not run through `parse_numeric()`. |

**Tried and confirmed NOT supported** (all returned a clean `Unknown
parameter`, tried live against the real unit): `meters.pa_temp`,
`meters.temp`, `meters.fan1`, `meters.fan1rpm`, `meters.fanrpm`,
`meters.fan`, `meters.vswr`, `meters.power`, `meters.outputpower`,
`meters.rfstate`, `meters.fan1speed`, `meters.fanpwm`, `meters.psvolt`,
`meters.pacurrent`, `meters.pavoltage`, `meters.current`,
`meters.voltage`, `meters.pspower`, `meters.frequency`,
`meters.reflectedpower`, `status.rf`, `status.rfon`, `status.alarm`,
`status.fault`, `status.faults`, `status.freq`, `status.forwardpower`,
`status.reflectedpower`, `status.rfout`, `status.rfoutput`,
`status.pa`, `status.output`, `status.alarms`, `status.warnings`,
`rf.state`, `rf.freq`, `rf.output`, `rf.on`, `tx.frequency`,
`tx.state`, `tx.rf`, `tx.mute`, `tx.freq`, `pa.rf`, `pa.state`,
`output.state`, `output.rf`, `config.frequency`, `alarms.active`,
`alarms.list`, `alarms.state`, `fault.state`, `faults`, `warnings`,
`sys.uptime`, `version`, `sys.version`, `model`.

## Unknowns / open questions

- **RF on/off state** -- no parameter name tried returned anything
  other than `Unknown parameter`. Not exposed by this driver. May exist
  under a naming pattern not yet tried, or may not be exposed via `get`
  at all on this firmware.
- **Operating frequency** -- same story; not found under any name tried.
- **Alarm/fault list** -- same story. The COBALT integration has a
  `TransmitterConfig`-level "indicator" parameter for this kind of
  thing; no equivalent was found here.
- **`meters.fanspeed` unit** -- genuinely unconfirmed, see table above.
- **Username-first login path** -- present in the driver (per the
  recovered implementation's claim), never exercised against a real
  unit this session.
- **Whether `set`/`reboot`/`factoryReset` (seen in `help`'s own output)
  have any read-only equivalents worth exposing** -- not explored, since
  none of the three themselves are read-only and this session's mandate
  was strictly to avoid anything that could mutate state.
- **Whether other TX300v3 units / firmware revisions share this exact
  parameter set** -- only WRJE's own unit was tested. Treat
  `KNOWN_PARAMETERS` as verified for that one unit, not as a general BW
  TX300v3 spec.

## Credentials

No login credential for any real transmitter is stored anywhere in
this module, its tests, or this document. `BWTx300V3Client` has no
default password -- callers must always supply one explicitly. Tests
use an obviously-fake placeholder (`"test-only-placeholder"`) and a
TEST-NET-3 (RFC 5737) address (`203.0.113.10`), never a real host.

## Integration notes for the future common transmitter-driver interface

- `BWTx300V3Client` intentionally mirrors the existing COBALT client's
  call shape (`__enter__`/`__exit__`/`get`) so a common interface can
  wrap either with the same calling convention. This module does not
  attempt to define that interface itself, per scope.
- `read_status()` returns a plain dict of canonical field names rather
  than a dataclass/model instance, so the integration layer is free to
  choose its own shared shape across transmitter families without this
  module presupposing it.
- The COBALT integration layer (`TransmitterConfig`, `probes.py`) adds
  per-parameter alarm thresholds, debounce, and a "percent of max power"
  reading on top of the raw client -- none of that exists in this
  module by design (B3 scope: protocol/transport/parsing only). Those
  concerns were previously implemented against this same BW client on
  `preserved-local-work-20260819` (commits `2b7be72`, `01aca6a`) and can
  be referenced when building the shared integration layer, but are not
  reproduced here.
- `compute_vswr()`'s `None`-on-fault behavior (reflected ≥ forward) is
  the same semantic the COBALT integration currently maps to a
  `"critical"` status string -- the future common interface will need
  to decide how each driver's raw signal maps onto shared status
  vocabulary; this driver deliberately returns a bare `None` rather than
  a driver-specific status enum, to avoid presupposing that vocabulary.
