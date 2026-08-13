# ALSA / audio-device inventory — IsadoraAir 1.2 Phase 3

Read-only inspection of production's audio device layout (2026-08-12),
completing the Phase 1 discovery audit's deferred device-identity item.
Goal: let a restorer understand *why* this box's audio routes
correctly, not to redesign audio discovery. Nothing was unplugged,
rebound, or reconfigured to produce this.

## Logical alias → physical hardware map

| Logical alias | Where configured | Physical hardware | Card/device | Stable across reboot? |
|---|---|---|---|---|
| `Studio Monitor` (AudioOutput) | Django admin, `hardware.AudioOutput` row | Onboard HDA codec (Conexant CX20632), analog line-out | `plughw:2,0` (card 2 "PCH") | **No** — real hardware auto-enumerates in kernel probe order; nothing pins card 2 specifically. See below. |
| `Studio Microphone 1` (AudioInput) | Django admin, `hardware.AudioInput` row | Same onboard HDA codec, analog mic-in (full-duplex device 0 — playback and capture share one PCM device number on this codec) | `plughw:2,0` (card 2 "PCH") | Same caveat as above. |
| `Stereotool Input` (AudioOutput) | Django admin, `hardware.AudioOutput` row | First `snd-aloop` instance, playback side — engine.py writes its pre-StereoTool master mix here; StereoTool's own (externally configured, not IsadoraAir-managed) input reads from the matching capture side | `plughw:0,0` (card 0 "Loopback") | **Yes** — pinned by `/etc/modprobe.d/isadoraair-aloop.conf` (`index=0,3,4`), not auto-numbered. |
| `airtap` / `airtap_ds` | `/etc/asound.conf` (repo-managed: `deploy/asound.conf`) | Second `snd-aloop` instance, device 1 subdevice 0 — StereoTool's *processed* output; shared via `dsnoop` so encoders (Liquidsoap) and aircheck (ffmpeg) can both read the one stream | `hw:Loopback_1,1,0` = card 3, device 1, subdev 0 | **Yes** — same pinning. |
| (unused) | -- | Third `snd-aloop` instance, reserved | Card 4 "Loopback_2" | Yes (pinned), but not currently consumed by any IsadoraAir code path. |
| N/A (USB DAC present, not currently wired into any AudioInput/AudioOutput row) | -- | Topping D10s USB DAC, USB VID:PID `152a:8750` | Card 1 "D10s" | **No** — USB enumeration order-dependent, and not referenced by name/serial anywhere. |

Full current `/proc/asound/cards` for reference (2026-08-12):
```
 0 [Loopback       ]: Loopback - Loopback           (pinned, index=0)
 1 [D10s           ]: USB-Audio - D10s               (unpinned, USB enumeration order)
 2 [PCH            ]: HDA-Intel - HDA Intel PCH       (unpinned, kernel probe order)
 3 [Loopback_1     ]: Loopback - Loopback           (pinned, index=3)
 4 [Loopback_2     ]: Loopback - Loopback           (pinned, index=4)
```

## Flagged: unstable card numbering for real hardware

**`Studio Monitor` and `Studio Microphone 1` both reference `plughw:2,0`
by bare card number, with nothing pinning card 2 to the onboard HDA
codec specifically.** If a USB audio device enumerates before the
onboard codec on a future boot (e.g. added/removed hardware, USB
re-enumeration after a kernel/driver change), card 2 could become a
different physical device, silently misrouting studio monitor/mic
audio. The three `snd-aloop` instances are *not* at risk of this
(pinned by `index=` in `/etc/modprobe.d/isadoraair-aloop.conf`) — only
the two real hardware devices are.

Not fixed in this pass (Phase 3's stated goal is understanding today's
routing, not redesigning device discovery) — feeds directly into
roadmap item 1.3 (USB recovery). The durable fix, when that item is
scoped, is almost certainly ALSA's own persistent card-naming
mechanism (`udev` rules keyed on the codec's stable identity, or
`/etc/asound.conf`'s `pcm.!default`/`ctl.!default` blocks switching from
`card 2` to a name-based `hw:PCH` reference — HDA Intel PCH's
short-name `PCH` is itself derived from a stable PCI-slot/subsystem
match, not enumeration order, so this could be a small, low-risk fix
when that roadmap item is actually scoped — noted here, not applied
now).

## Configuration file inventory

| File | Role |
|---|---|
| `/etc/asound.conf` (repo: `deploy/asound.conf`, byte-identical, confirmed via `diff`) | `pcm.!default`/`ctl.!default` → card 2; `airtap`/`airtap_ds` dsnoop alias definitions with the full period/buffer-size tuning rationale in its own comments |
| `/etc/modprobe.d/isadoraair-aloop.conf` (repo: `deploy/isadoraair-aloop.conf`, **added this Phase 3 pass** — previously host-only, a real DR gap) | Pins `snd-aloop` to three instances at indices 0/3/4 |
| `/etc/modules-load.d/snd-aloop.conf` (single line `snd-aloop`, not repo-managed -- trivial enough that a one-line `README.md` command is the whole "install," no file worth versioning) | Loads the module at boot |
| `hardware.AudioOutput`/`hardware.AudioInput` DB rows (station-specific, not in any repo) | Maps the human-facing device names (`Studio Monitor`, `Studio Microphone 1`, `Stereotool Input`) to ALSA device strings; read by `library/services/engine.py`'s `_resolve_studio_monitor_device`/`_resolve_mic_device`/`_resolve_stereotool_device` |
| StereoTool's own saved profile/settings (external to IsadoraAir, not a systemd/CLI argument — `stereotool.service`'s `ExecStart` only passes `-p 8085 -w 192.168/16`, its web UI port and IP allowlist) | StereoTool's own input/output ALSA device selection lives inside its own settings, configured via its web UI, not exposed to or managed by IsadoraAir code |

## snd-aloop reconstruction

**Required kernel module**: `snd-aloop` (in-tree, ships with the
standard Ubuntu kernel — no external DKMS/out-of-tree build needed).

**modules-load.d config** (loads it at boot): `/etc/modules-load.d/snd-aloop.conf`
containing exactly the line `snd-aloop`.

**modprobe.d config** (pins the 3-instance layout):
`/etc/modprobe.d/isadoraair-aloop.conf` (repo-managed, see
`deploy/isadoraair-aloop.conf`), containing:
```
options snd-aloop enable=1,1,1 index=0,3,4
```

**Expected resulting layout** (each instance is a full loopback card —
playback side and capture side, 8 subdevices each by default):
- Index 0 ("Loopback"): engine.py's pre-StereoTool master mix out.
- Index 3 ("Loopback_1"): StereoTool's processed output back in;
  `airtap`/`airtap_ds` in `/etc/asound.conf` share device 1 subdevice 0
  of this card specifically.
- Index 4 ("Loopback_2"): reserved, not currently used.

**ALSA routing depending on it**: `Stereotool Input` AudioOutput
(`plughw:0,0`), the `airtap`/`airtap_ds` aliases (`hw:Loopback_1,1,0`),
and by extension every encoder/aircheck path that reads through
`airtap` (`encoder_manager.py`'s `DEFAULT_INPUT_DEVICE = "airtap"`).

**Verification after restore** (read-only, safe to run any time):
```bash
# Module loaded?
lsmod | grep snd_aloop

# Correct 3-instance/pinned-index layout?
cat /proc/asound/cards
# Expect three "Loopback" lines at indices 0, 3, 4 (order in the file
# follows index, so they should be the 1st, and among the last two
# entries -- exact position among real hardware varies).

# Confirm the specific subdevice airtap depends on actually exists:
aplay -L | grep -A1 "hw:CARD=Loopback_1,DEV=1"
```
A small preflight check performing the module/layout parts of this
automatically is included in this phase's preflight command (see
`docs/RUNTIME_BASELINE.md`).
