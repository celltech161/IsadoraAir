# Studio Monitor boundary signal verification

This harness verifies the proposed containment architecture; it does not claim
to reproduce or fix the historical production digital-zero incident.

Safe synthetic run:

```bash
venv/bin/python scratchpad/audio_output_recovery/round9_boundary_signal_verification/harness.py
```

The default run uses no ALSA devices and performs no Django database query or
write. It records P1 queue input, P2 queue output, P3 valve output, P4 limiter
output, StereoTool PCM continuity, queue levels/signals, valve state,
generation/ownership epoch, sink rendered count, and parent/sibling states.

P5 is intentionally unavailable in safe mode. An independent physical run
requires explicit output, independent capture, and ALSA status paths for both
targets:

```bash
venv/bin/python scratchpad/audio_output_recovery/round9_boundary_signal_verification/harness.py \
  --physical \
  --sink-a DEVICE --capture-a DEVICE --hw-status-a /proc/asound/.../status \
  --sink-b DEVICE --capture-b DEVICE --hw-status-b /proc/asound/.../status
```

The operator must provide real physical loops from each output to its capture
input. Merely selecting an input on the same card is not independent output
verification. `--capture-a` and `--capture-b` are independently optional, so
one cable can cover one identity per run; every open phase for the supplied
capture is P5-checked, while the other identity still receives P1-P4 and
sink/ALSA telemetry coverage. Both sink and status pairs remain required
because the full retarget sequence exercises both physical outputs.

The probe callbacks inspect each whole mapped buffer for an exact all-zero
verdict, sample at most 256 F32LE values for RMS/mean/peak, do not log, and
retain fixed-size aggregate statistics. A separate 20 ms sampler records
queue/valve/generation/sink/ALSA state.

Because an analog input normally contains nonzero noise, physical P5 phases
also require phase-local RMS of at least `0.005`; use `--p5-min-rms` to set a
different documented floor for a calibrated setup. Exact nonzero alone is not
accepted as proof that program signal crossed the physical boundary.

Production Studio generations use `sync=False`, as required by the two-UCA
P4/P5 result. `--physical-sink-sync-true` is a negative-control option that
restores the historical setting and must fail the P5 energy assertion on the
reproducing setup.

Passing means:

* when the Studio valve is open and input is nonzero, nonzero PCM reaches P4
  after each settled generation replacement, and physical P5 exceeds its
  configured signal-energy floor when enabled;
* failed targets close only the Studio valve while P1/P2 and StereoTool remain
  nonzero;
* the parent pipeline and StereoTool never leave PLAYING;
* StereoTool's maximum callback gap remains below 150 ms.

It does not establish the root cause of a production anomaly that is not
reproducible on demand.
