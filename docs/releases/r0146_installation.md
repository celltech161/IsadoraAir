# r0146 installation, verification and recovery

Publication does not authorize deployment. KOGR and WRJE require separate station-specific preflight, installation
authorization and operational acceptance. Do not run UpdateCenter, restart a service or change a station setting
merely because r0146 has been published.

r0146 makes the deck mixer live with a fixed S16LE deck bus (roadmap 1.21 A/A2) and corrects the r0145 release
contract for public clones (test only). A direct r0145 → r0146 update **restarts the audio engine only**; the engine
restart interrupts program audio briefly. Gunicorn, Monitoring, the encoders, RBDS and the workers keep running.

## Exact identity and trusted plan

- Previous published release: r0145 at `d6679c80f162ba573a50b0ed4c30400b77ce7907`.
- Integrated engineering tip: `9b4728fd004959da478839d0c1ca96bd67dd6340`, tree
  `ba50e0a85ac047a3bb4727022b6355db954604bd`, 7 linear cherry-picks on r0145: `ad5cab2` (r0145 contract correction)
  and `4f5deb2`, `8dfb6cc`, `101e865`, `c8b01be`, `b051026`, `9b4728f` (deck mixer continuity).
- R is the unique canonical commit introducing `deploy/releases/r0146.json`, the direct child of the engineering tip,
  and adds only the four release files.
- There is **no migration**. A direct r0145 → r0146 plan advances source to R, then restarts `isadoraair-engine`.
  Nothing else.
- There is no manual migration companion, package, requirements, collectstatic, nginx, systemd-unit, manual-bootstrap
  or protected-runtime action, and no Gunicorn, Monitoring, encoder, RBDS or worker restart for a direct r0145 update.
- If the trusted plan for a station directly on r0145 shows any other action, stop and investigate; do not install.
- A station below r0145 must retain every intervening release, migration and restart in the cumulative trusted plan.
  For example, a station on r0144 also restarts Gunicorn and Monitoring for r0145; a station on r0132 also applies
  `library.0101_uitheme_show_played_items`, `library.0102_uitheme_log_display_appearance` and
  `library.0103_remotedjconfig_auto_reconnect_enabled` (additive).

Never install an engineering branch, rebuild an equivalent release commit or replace the live checkout manually. Do not
manually run `migrate`. Preview and execute the exact canonical release only through the protected UpdateCenter after
separate station authorization.

## Maintenance window and live-talent safety

The engine restart briefly interrupts program audio and ends any in-progress Voice Track, so schedule an **approved
operator maintenance window** away from live programs, Remote DJ shows and urgent-alert activity.

- **On r0144 or later (KOGR is on r0145):** the installation starts from an Update Center with the **engine restart
  safeguard**, which refuses it while a studio microphone is live, a Remote DJ is connected (even with its microphone
  Off), reconnecting, establishing or occupying its slot, or engine status is unknown. **Do not override the
  safeguard to install.** End the live session and preview again.
- **Below r0144 (possible at WRJE):** the installed Update Center predates the safeguard and will not check. Before
  starting the job, confirm by direct observation that no studio microphone is live and no Remote DJ is connected,
  and that the operator agrees to the program-audio interruption.
- The safeguard checks admission, not the moment of the restart: keep the studio microphone off and Remote DJ
  disconnected until the job has finished.

## GStreamer runtime preflight (mandatory, read-only, off-air)

**Both gates below are mandatory on every station before r0146 is installed.** A station must not install r0146 if
either gate fails, is skipped or cannot be completed safely: stop and report it. The gates are independent (passing
Gate A never substitutes for Gate B), and each station runs them itself, with its own GStreamer runtime and its own
sample rate. **KOGR's result does not qualify WRJE.** The gates are an operator installation requirement, not an
UpdateCenter action: r0146 installs no package and does not upgrade or change the system GStreamer runtime.

- **Gate A: construct-time capability.** The installed Python/GObject/GStreamer must create the deck `audiomixer`
  exactly as the engine does (`Gst.ElementFactory.make_with_properties`, GStreamer 1.20 or later) with
  `force-live=true`, and the constructed mixer must report `force-live` as `True`. The property can be set only at
  construction. Any fallback to the inherited non-live mixer (no `make_with_properties`, a construction error, a
  missing element, `None` or `False`) is a **blocking failure**: the engine would silently start with the old
  defective mixer.
- **Gate B: actual fixed-format mixer output.** A standalone probe at the **station's configured sample rate** must
  show that the live deck mixer's **source pad** actually negotiates
  `audio/x-raw,format=S16LE,rate=<station rate>,channels=2,layout=interleaved`, read from the negotiated caps on the
  mixer source pad and its CAPS events, not from the capsfilter's configured property. It passes only if all of:
  exactly one consistent mixer-source CAPS event, which begins during zero-pad (no deck) startup; no unexpected
  output-format renegotiation while S16LE, F32LE and S32LE test decks are added and removed (the negotiated caps are
  unchanged at every checkpoint); no DISCONT buffer; no GStreamer bus error or warning; at least four zero-pad idle
  checkpoints at which the output is clock-driven (the mixer's output end stays within 0.25 s of pipeline running
  time with no deck present, and output keeps advancing); and digital silence (every sample zero) during zero-pad
  idle. The probe fails closed on an unsupported property, a missing plugin, unexpected caps, a DISCONT, a bus
  error or any probe failure.

Without Gate B's guarantee a live mixer with no deck negotiates a format from downstream and may renegotiate it to the
first deck's format while running: the condition that corrupted deck audio and correlated with a native crash in
qualification.

### Procedure (self-contained; nothing from any engineering checkout is needed)

It needs only a shell, the station's installed `gst-launch-1.0`, `timeout`, and the Python interpreter that runs the
audio engine, so it exercises the station's actual GObject/GStreamer bindings. It is **read-only and isolated**: it
changes no station configuration or data, restarts nothing, writes no file, and opens no sound device, encoder,
stream, database or engine socket (its only sink is an in-process `appsink`; the gate scripts below are read from
standard input). Run it as the station user. It takes about 20 seconds and is safe to repeat. It does not replace
the maintenance-window and live-talent checks below.

1. **Station interpreter and rate.** `PY` must be the interpreter in the `ExecStart` of `isadoraair-engine`
   (`systemctl cat isadoraair-engine`). `RATE` must be the station's configured **Audio Pipeline sample rate** (Django
   admin, Audio Pipeline, Sample rate; the `Pipeline sample rate -> N` line in the engine journal after its last
   start is the same value). KOGR is 44100 Hz. WRJE is documented as 48000 Hz but **must be read from WRJE itself**.
   Do not reuse another station's value.

```bash
PY=/opt/isadoraair/venv/bin/python
RATE=44100
case "$RATE" in ''|*[!0-9]*) echo "RATE must be the station's integer sample rate"; false;; esac
gst-launch-1.0 --version; "$PY" -I -c 'import sys; print(sys.executable, sys.version.split()[0])'
```

2. **Gate A.** It must print `GATE A PASS` and exit status 0. Anything else blocks installation.

```bash
"$PY" -I - <<'GATE_A_PROBE'
import sys
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)
print("GStreamer:", Gst.version_string())
make = getattr(Gst.ElementFactory, "make_with_properties", None)
if make is None:
    sys.exit("GATE A FAIL: this GStreamer binding has no Gst.ElementFactory.make_with_properties (force-live cannot be set at construction)")
try:
    mixer = make("audiomixer", ["name", "force-live"], ["mixer", True])
except Exception as exc:
    sys.exit(f"GATE A FAIL: constructing audiomixer with force-live=true raised {exc!r}")
if mixer is None:
    sys.exit("GATE A FAIL: the audiomixer element could not be created (plugin missing?)")
live = mixer.get_property("force-live")
print("force-live read back:", live)
if live is not True:
    sys.exit("GATE A FAIL: the constructed deck mixer is not live (the engine would fall back to the defective non-live mixer)")
print("GATE A PASS: audiomixer constructed with force-live=true and it reads back True")
GATE_A_PROBE
echo "Gate A exit status: $? (0 = PASS; anything else blocks installation)"
```

3. **Gate B.** The probe is the accepted engineering gate, byte for byte; the evaluator below it applies the pass
   criteria above. It must print `GATE B PASS` and exit status 0. A failure, a timeout or no result blocks
   installation.

```bash
OUT=$(timeout 120 "$PY" -I - "$RATE" 1 <<'GATE_B_PROBE'
"""Roadmap 1.21 Phase A2 -- Gate 1: deck-mixer output caps stability (standalone GStreamer; no Django, no devices).

Construction identical to engine.make_deck_mixer + make_deck_bus_caps: audiomixer force-live=true (construct-time) ->
capsfilter audio/x-raw,format=S16LE,rate=R,channels=2,layout=interleaved -> volume -> volume -> capsfilter(rate,
channels) -> tee -> master_mixer (live silence bed) -> audioconvert -> capsfilter F32LE -> appsink(sync).
Sequence: zero pads 2 s; deck1 S16LE 3 s; remove; zero pads 3 s; deck2 F32LE; +1.5 s deck3 S32LE (overlap 1.5 s);
remove deck2; remove deck3; zero pads 3 s; deck4 S16LE 2 s; remove.
Records on the deck mixer src pad: every CAPS / SEGMENT event, upstream LATENCY events, DISCONT buffers, the src caps
at each phase, output max |sample| during zero-pad phases, deck-mixer lag vs running time, bus errors/warnings.
usage: python -I gst_a2_caps_gate.py <rate> <with_capsfilter 0|1>"""
import array, json, sys, time
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib
RATE, WITH_CAPS = int(sys.argv[1]), sys.argv[2] == "1"
Gst.init(None)
pipe = Gst.Pipeline.new("a2")
mixer = Gst.ElementFactory.make_with_properties("audiomixer", ["name", "force-live"], ["mixer", True])
assert mixer.get_property("force-live") is True
chain = [mixer]
if WITH_CAPS:
    dbc = Gst.ElementFactory.make("capsfilter", "deck_bus_caps")
    dbc.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,format=S16LE,rate={RATE},channels=2,layout=interleaved"))
    chain.append(dbc)
chain += [Gst.ElementFactory.make("volume", "vt_duck_gain"), Gst.ElementFactory.make("volume", "duck_gain")]
fix = Gst.ElementFactory.make("capsfilter", "remote_dj_fixate_caps"); fix.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={RATE},channels=2"))
tee = Gst.ElementFactory.make("tee", "remote_dj_tee"); chain += [fix, tee]
master = Gst.ElementFactory.make("audiomixer", "master_mixer"); master.set_property("output-buffer-duration", 10 * Gst.MSECOND)
conv = Gst.ElementFactory.make("audioconvert"); pcaps = Gst.ElementFactory.make("capsfilter"); pcaps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,format=F32LE,rate={RATE},channels=2"))
sink = Gst.ElementFactory.make("appsink"); sink.set_property("sync", True); sink.set_property("emit-signals", True)
bed = Gst.ElementFactory.make("audiotestsrc"); bed.set_property("wave", "silence"); bed.set_property("is-live", True)
bedc = Gst.ElementFactory.make("capsfilter"); bedc.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={RATE},channels=2"))
for e in chain + [master, conv, pcaps, sink, bed, bedc]: pipe.add(e)
for a, b in zip(chain, chain[1:]): assert a.link(b), (a.get_name(), b.get_name())
tee.request_pad_simple("src_%u").link(master.request_pad_simple("sink_%u"))
bed.link(bedc); bedc.get_static_pad("src").link(master.request_pad_simple("sink_%u")); master.link(conv); conv.link(pcaps); pcaps.link(sink)
t0 = [None]
def now(): return (pipe.get_clock().get_time() - pipe.get_base_time()) / 1e9
events, phase = [], ["init"]
def fmt_caps(c):
    s = c.get_structure(0); return f'{s.get_value("format")},{s.get_value("rate")},{s.get_value("channels")},{s.get_value("layout")}'
src = mixer.get_static_pad("src")
def on_event(pad, info):
    ev = info.get_event()
    if ev.type == Gst.EventType.CAPS: events.append({"phase": phase[0], "t": round(now(), 3) if pipe.get_clock() else None, "event": "CAPS", "caps": fmt_caps(ev.parse_caps())})
    elif ev.type == Gst.EventType.SEGMENT:
        seg = ev.parse_segment(); events.append({"phase": phase[0], "event": "SEGMENT", "start": seg.start, "base": seg.base})
    return Gst.PadProbeReturn.OK
src.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, on_event)
def on_up(pad, info):
    ev = info.get_event()
    if ev.type == Gst.EventType.LATENCY: events.append({"phase": phase[0], "event": "LATENCY", "ms": round(ev.parse_latency() / 1e6, 3)})
    return Gst.PadProbeReturn.OK
src.add_probe(Gst.PadProbeType.EVENT_UPSTREAM, on_up)
tel = {"end": None, "n": 0, "discont": 0, "zero_phase_max": 0.0, "zero_phase_bufs": 0}
def on_buf(pad, info):
    b = info.get_buffer()
    if b.has_flags(Gst.BufferFlags.DISCONT): tel["discont"] += 1; events.append({"phase": phase[0], "event": "DISCONT_BUFFER"})
    seg = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
    end = b.pts + (b.duration if b.duration != Gst.CLOCK_TIME_NONE else 0)
    if seg: end = seg.parse_segment().to_running_time(Gst.Format.TIME, end)
    tel["end"] = end; tel["n"] += 1
    if phase[0].startswith("zero"):
        c = pad.get_current_caps(); f = c.get_structure(0).get_value("format")
        ok, m = b.map(Gst.MapFlags.READ)
        if ok:
            d = bytes(m.data); b.unmap(m)
            code = {"S16LE": "h", "S32LE": "i", "F32LE": "f"}.get(f)
            if code:
                arr = array.array(code, d[: len(d) - len(d) % array.array(code).itemsize])
                tel["zero_phase_max"] = max(tel["zero_phase_max"], max((abs(x) for x in arr), default=0)); tel["zero_phase_bufs"] += 1
    return Gst.PadProbeReturn.OK
src.add_probe(Gst.PadProbeType.BUFFER, on_buf)
bus = pipe.get_bus(); msgs = []
def pump(sec):
    end = time.monotonic() + sec; ctx = GLib.MainContext.default()
    while time.monotonic() < end:
        while ctx.pending(): ctx.iteration(False)
        m = bus.timed_pop_filtered(5 * Gst.MSECOND, Gst.MessageType.ERROR | Gst.MessageType.WARNING)
        if m: msgs.append(("ERROR " if m.type == Gst.MessageType.ERROR else "WARNING ") + (m.parse_error() if m.type == Gst.MessageType.ERROR else m.parse_warning())[0].message)
snap = []
def checkpoint(label):
    c = src.get_current_caps(); r = now()
    snap.append({"label": label, "running_s": round(r, 3), "src_caps": None if c is None else fmt_caps(c), "deck_pads": len(mixer.sinkpads),
                 "mixer_buffers": tel["n"], "mixer_lag_s": None if tel["end"] is None else round(r - tel["end"] / 1e9, 3)})
decks = {}
def add(name, freq, fmt):
    b = Gst.Bin.new(name); s = Gst.ElementFactory.make("audiotestsrc"); s.set_property("freq", float(freq)); s.set_property("volume", 0.4)
    c = Gst.ElementFactory.make("capsfilter"); c.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,format={fmt},rate={RATE},channels=2"))
    cv = Gst.ElementFactory.make("audioconvert")      # production decks end in audioconvert/audioresample/capsfilter(rate,ch)
    b.add(s); b.add(c); b.add(cv); s.link(c); c.link(cv); b.add_pad(Gst.GhostPad.new("src", cv.get_static_pad("src"))); pipe.add(b)
    pad = mixer.request_pad_simple("sink_%u"); assert b.get_static_pad("src").link(pad) == Gst.PadLinkReturn.OK
    b.get_static_pad("src").set_offset(int(now() * 1e9)); b.sync_state_with_parent(); decks[name] = (b, pad)
def remove(name):
    b, pad = decks.pop(name); b.get_static_pad("src").unlink(pad); mixer.release_request_pad(pad); b.set_state(Gst.State.NULL); pipe.remove(b)
pipe.set_state(Gst.State.PLAYING)
phase[0] = "zero_startup"; pump(2.0); checkpoint("zero_pads_startup")
phase[0] = "deck1_S16"; add("deck1", 400, "S16LE"); pump(3.0); checkpoint("deck1_S16LE")
remove("deck1"); phase[0] = "zero_after_deck1"; pump(3.0); checkpoint("zero_pads_after_removal")
phase[0] = "deck2_F32"; add("deck2", 600, "F32LE"); pump(1.5); checkpoint("deck2_F32LE")
phase[0] = "deck2+3"; add("deck3", 900, "S32LE"); pump(1.5); checkpoint("deck2_F32LE+deck3_S32LE")
remove("deck2"); pump(1.0); checkpoint("deck3_only"); remove("deck3")
phase[0] = "zero_after_deck3"; pump(3.0); checkpoint("zero_pads_again")
phase[0] = "deck4_S16"; add("deck4", 400, "S16LE"); pump(2.0); checkpoint("deck4_S16LE"); remove("deck4")
phase[0] = "zero_end"; pump(1.0); checkpoint("zero_end")
caps_seq = [e["caps"] for e in events if e["event"] == "CAPS"]
res = {"rate": RATE, "deck_bus_capsfilter": WITH_CAPS, "force_live": mixer.get_property("force-live"), "caps_events": caps_seq,
       "distinct_src_formats": sorted({c.split(",")[0] for c in caps_seq}), "segments": sum(e["event"] == "SEGMENT" for e in events),
       "latency_events": [e["ms"] for e in events if e["event"] == "LATENCY"], "discont_buffers": tel["discont"],
       "zero_phase_buffers": tel["zero_phase_bufs"], "zero_phase_max_abs": tel["zero_phase_max"], "bus_messages": msgs[:5],
       "checkpoints": snap}
print("A2CAPS " + json.dumps(res))
print("A2EVENTS " + json.dumps(events[:60]))
pipe.set_state(Gst.State.NULL)
GATE_B_PROBE
)
PROBE_RC=$?
EVALUATOR=$(cat <<'GATE_B_EVALUATOR'
import json
import os
import sys

rate = int(os.environ["RATE"])
probe_rc = int(os.environ["PROBE_RC"])
want = f"S16LE,{rate},2,interleaved"
lines = sys.stdin.read().splitlines()
caps_lines = [line for line in lines if line.startswith("A2CAPS ")]
event_lines = [line for line in lines if line.startswith("A2EVENTS ")]
problems = []
if probe_rc != 0:
    problems.append(f"the probe exited with status {probe_rc}")
result = events = None
if len(caps_lines) == 1 and len(event_lines) == 1:
    try:
        result = json.loads(caps_lines[0][len("A2CAPS "):])
        events = json.loads(event_lines[0][len("A2EVENTS "):])
    except ValueError as exc:
        problems.append(f"the probe output is not valid JSON ({exc})")
else:
    problems.append("the probe produced no complete result")
if result is not None and events is not None:
    if result.get("rate") != rate:
        problems.append(f"probe rate {result.get('rate')!r} is not the station rate {rate}")
    if result.get("deck_bus_capsfilter") is not True:
        problems.append("the probe did not run with the fixed deck-bus capsfilter")
    if result.get("force_live") is not True:
        problems.append("the probe's deck mixer was not force-live")
    if result.get("caps_events") != [want]:
        problems.append(f"mixer-source CAPS events are {result.get('caps_events')!r}; required exactly one: {want!r}")
    if result.get("distinct_src_formats") != ["S16LE"]:
        problems.append(f"mixer-source formats seen: {result.get('distinct_src_formats')!r}; required only S16LE")
    caps_events = [e for e in events if e.get("event") == "CAPS"]
    if len(caps_events) != 1 or caps_events[0].get("phase") != "zero_startup":
        problems.append("the single CAPS event must begin during zero-pad startup")
    if result.get("discont_buffers") != 0:
        problems.append(f"DISCONT buffers: {result.get('discont_buffers')!r}")
    if result.get("bus_messages") != []:
        problems.append(f"GStreamer bus errors/warnings: {result.get('bus_messages')!r}")
    if not (result.get("zero_phase_buffers", 0) >= 100 and result.get("zero_phase_max_abs") == 0):
        problems.append("zero-pad output must be 100+ buffers of digital silence "
                        f"(buffers {result.get('zero_phase_buffers')!r}, max |sample| {result.get('zero_phase_max_abs')!r})")
    checkpoints = result.get("checkpoints") or []
    zero_pad = [c for c in checkpoints if str(c.get("label", "")).startswith("zero")]
    if len(zero_pad) < 4:
        problems.append("fewer than four zero-pad checkpoints were recorded")
    for c in zero_pad:
        lag = c.get("mixer_lag_s")
        if c.get("deck_pads") != 0 or lag is None or abs(lag) > 0.25:
            problems.append(f"zero-pad output is not clock-driven at {c.get('label')!r} (decks {c.get('deck_pads')!r}, lag {lag!r} s)")
    if any(c.get("src_caps") != want for c in checkpoints):
        problems.append("the negotiated mixer-source caps changed or differ from the required format at a checkpoint")
    counts = [c.get("mixer_buffers") for c in checkpoints]
    if not counts or any(b is None for b in counts) or any(b <= a for a, b in zip(counts, counts[1:])):
        problems.append("mixer output did not advance at every checkpoint")
if problems:
    print("GATE B FAIL - DO NOT INSTALL r0146 on this station:")
    for problem in problems:
        print("  -", problem)
    sys.exit(1)
print(f"GATE B PASS: one CAPS event {want} from zero-pad startup, no renegotiation, 0 DISCONT, "
      "no bus messages, digital silence and clock-driven output while idle")
GATE_B_EVALUATOR
)
printf '%s\n' "$OUT" | RATE="$RATE" PROBE_RC="$PROBE_RC" "$PY" -I -c "$EVALUATOR"
echo "Gate B exit status: $? (0 = PASS; anything else blocks installation)"
```

4. **Record** the complete output of both gates, the GStreamer version, `PY` and `RATE` with the station's evidence.
   If either gate does not print its PASS line, or could not be run to completion (missing `timeout`, missing
   bindings, a hung probe, an unreadable rate), the station **must not** install r0146 until the cause is understood
   and both gates pass.

### Station applicability

- **KOGR** (Audio Pipeline 44100 Hz, GStreamer 1.28.2). The engineering preflight passed on 1.28.2 at 44100 Hz during
  release qualification and remains valid supporting evidence. Both gates are still run as part of KOGR's
  installation preflight, with the GStreamer runtime and bindings as installed at that time.
- **WRJE** (Audio Pipeline documented as 48000 Hz). **WRJE has not been checked: both gates are required on WRJE,
  independently of KOGR, at WRJE's own configured sample rate and with WRJE's own installed GStreamer, before any WRJE
  installation.** WRJE's GStreamer version and installed release are unverified, and its preflight is pending. That
  pending preflight blocks a WRJE installation (separately authorized); it does not by itself prevent publication.

## Common read-only preflight

Record the evidence separately for each station:

1. **Installed release and trusted identity.** Establish the installed release from the UpdateCenter installation
   record and the trusted updater identity. Do not infer release identity from `HEAD` alone. **Do not assume the
   station is on r0145.** WRJE was last independently documented on r0132.
2. **Release chain.** Confirm the protected updater verifies the complete chain from the installed release to r0146.
3. **Clean checkout and healthy schema.** Verify the installed checkout is clean and no migration is pending.
4. **UpdateCenter health.** Confirm trusted UpdateCenter and protected-runtime health, no running job and no failed job
   awaiting operator intervention.
5. **Trusted plan.** Preview the exact canonical target R and record the plan and action fingerprint immediately before
   installation. For a direct r0145 upgrade the action fingerprint must describe source advance and the engine restart
   only.
6. **Services.** Record the PID and activation time of Gunicorn, the audio engine, the encoders, Monitoring, RBDS and
   the workers.
7. **Program audio and encoders.** Record program audio and encoder connection state.
8. **Outstanding acceptance.** Confirm any open r0145 operational-acceptance observations (KOGR installed r0145 on
   2026-11-20) before treating the station as a fully accepted r0145 baseline.
9. **GStreamer gates.** Gate A and Gate B above have both passed on this station, at its own sample rate, with the
   output recorded. If either did not pass or could not be completed, stop.

Stop if identity, chain, schema, fingerprint, restart inventory or service health differs. Do not shorten or manually
reconstruct a catch-up plan.

## Installation expectations

Only after Gate A and Gate B have both passed on that station, and after separate authorization for it, inside the
approved window, use the protected UpdateCenter once.
For a station directly on r0145 it must:

1. Advance source to the exact canonical r0146 release commit R.
2. Restart `isadoraair-engine`.

No migration, apt package, Python requirement, systemd unit, static collection, nginx, manual-bootstrap or
protected-runtime operation is expected, and no Gunicorn, Monitoring, encoder, RBDS or worker restart. Do not restart
services outside the authorized update job. Do not retry a failed job automatically.

## Non-disruptive acceptance

Do **not** key a microphone on air, start a Remote DJ session, inject audio into the transmitter or streams, alter
the log or change station settings only to demonstrate acceptance.

1. Verify the UpdateCenter job succeeded.
2. Confirm the installed source equals the exact canonical r0146 R, the checkout is clean and no migration is pending.
3. Verify the engine restarted (new PID and activation time) and Gunicorn, Monitoring, the encoders, RBDS and the
   workers kept their recorded PIDs and activation times **(no change)**.
4. **Deck mixer.** In the engine journal since the restart, `Deck mixer: audiomixer force-live=True` is present.
   `force-live=False`, or a Monitoring warning "Deck mixer is not live (idle-start catch-up risk)", means the defective
   fallback is in use: record it and report it; the station does not have the correction.
5. **Program audio.** Program audio resumed after the restart, the encoders stayed connected, and the console shows
   the queue, NEXT, clocks and VU meters normally. Note the length of the interruption.
6. **Ordinary playout.** Observe at least two ordinary song-to-song segues: each is normal, with exactly one Play event
   per item, no dead air and no delayed first audio.
7. **After an idle (observe only).** If the scheduled programming naturally leaves the decks empty (an exhausted hour
   waiting for its next item), confirm the next song is heard promptly when it starts; otherwise record "not observed".
   Do not create an idle for this check.
8. **No regressions.** No new GStreamer critical, native crash, `vt2-teardown` event or engine restart in the journal or
   Monitoring; Voice Tracks, urgent announcements, dedications, the studio microphone and Remote DJ behave as before
   when they occur in normal operation.

## Post-install Monitoring observation

For the rest of the day after installation, review Monitoring for engine restarts, dead air, deck-mixer warnings,
GStreamer errors and encoder disconnects. Record any observation; do not remediate without authorization.

## KOGR acceptance

KOGR is on r0145 (installed 2026-11-20); its engineering preflight passed on GStreamer 1.28.2 at 44100 Hz and both
gates are run again as part of its installation preflight. Record KOGR evidence independently: Gate A and Gate B
output, preflight items 1–9 with KOGR's PIDs and activation times, the safeguard assessment, the job, the plan
fingerprint, and acceptance items 1–8.

## WRJE acceptance

Do not assume WRJE's installed release; it was last independently documented on r0132. The protected updater must
verify its actual release, schema and updater state and the complete chain, and **both GStreamer gates (A and B) must
pass on WRJE itself, at WRJE's own configured sample rate (documented as 48000 Hz),** before any installation
authorization. KOGR's gates do not count for WRJE. A catch-up plan from below r0144 includes the intervening migrations
and restarts and is **not** gated by the engine restart safeguard (apply the manual live-talent check above). Record
WRJE evidence independently. Do not reuse KOGR's results.

**A successful installation at one station is not authorization to install at the other.** Record KOGR and WRJE
evidence independently.

## Stop, evidence and recovery

Stop and preserve the UpdateCenter job, preview, action fingerprint and the engine journal if:

- the target commit, manifest hash, plan or fingerprint differs;
- any action beyond source advance and the engine restart appears for a direct r0145 upgrade, or a migration is
  pending;
- Gate A or Gate B did not pass or could not be completed before installation (do not install);
- the engine fails to restart, logs `force-live=False`, or another service restarts unexpectedly;
- program audio does not resume, encoder connectivity is unhealthy, or the console fails to load.

Do not use manual Git resets, edit database migration records, modify station data, run arbitrary service restarts or
attempt an automatic retry. Use only recovery explicitly supported by the trusted updater, and obtain new operator
authorization for any corrective action. Publication of r0146 is not rollback authority and does not authorize an
installation at either station.
