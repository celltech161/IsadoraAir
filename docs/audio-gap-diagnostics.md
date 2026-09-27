# Audio-artifact diagnostics

## Purpose and scope

IsadoraAir includes optional, bounded diagnostics for rare sub-second program-
audio artifacts. The reported symptom may be a short blemish, stutter, or
repeated fragment lasting roughly 5-20 ms; it is not necessarily silence.

The implementation primarily measures **transport continuity**. Healthy
transport is useful negative evidence, but it is not proof that every PCM
sample was correct. StereoTool or another component can repeat, mute,
time-correct, or corrupt a short block while timestamps and ALSA pointers
continue normally.

## Signal path and live ALSA landmarks

```text
Engine
  |
  A -- Engine ALSA playback
  |    /proc/asound/Loopback/pcm0p/sub0
  |    FLOAT_LE, stereo, 44.1 kHz
  |    period 882 frames / 20.000 ms; buffer 8,820 frames / 200 ms
  v
snd_aloop (pre-StereoTool)
  |
  B -- StereoTool capture
  |    /proc/asound/Loopback/pcm1c/sub0
  |    FLOAT_LE, stereo, 44.1 kHz
  |    period 384 frames / 8.707 ms; buffer 1,920 frames / 43.537 ms
  v
StereoTool
  |
  C -- StereoTool playback
  |    /proc/asound/Loopback_1/pcm0p/sub0
  |    FLOAT_LE, stereo, 44.1 kHz
  |    period 384 frames / 8.707 ms; buffer 1,920 frames / 43.537 ms
  v
snd_aloop (post-StereoTool)
  |
  D -- Liquidsoap capture through the `airtap` dsnoop alias
  |    /proc/asound/Loopback_1/pcm1c/sub0
  |    44.1 kHz
  |    period 16,384 frames / 371.52 ms; buffer 131,072 frames / 2.97 s
  v
Liquidsoap -> encoders / Aircheck / distribution
```

The diagnostics never open another PCM device or add another ALSA consumer.
A, B, C, and D are read through procfs only.

## What the retained signals mean

- **PTS**: GStreamer presentation timestamp. A large unexpected delta suggests
  missing or duplicated transport time.
- **DISCONT**: GStreamer marks a buffer discontinuity. It is strong transport-
  level evidence when present.
- **Buffer progression**: count of buffers reaching the pre-StereoTool probe.
  A zero-delta run means that observation point stopped advancing.
- **Render progression**: buffers rendered by the Engine sink. It separates
  upstream delivery from sink-side progress.
- **ALSA `delay`**: queued frames. At A it is useful playback runway; a collapse
  toward the 1,764-frame diagnostic threshold is potential starvation evidence.
- **ALSA `avail`**: frames available for writing or reading, depending on stream
  direction. Interpret it with the endpoint direction and `delay`.
- **`hw_ptr`**: kernel/hardware-side PCM position. Stalls or resets can localize
  a transport interruption.
- **`appl_ptr`**: application-side PCM position. At A/B/C it provides useful
  progression context.
- **Pointer stall**: the largest unchanged-pointer interval observed inside one
  20 Hz JSON window. B/C are sampled at approximately 200 Hz and reduced in
  memory so a recovered transient remains visible in the next record.
- **Pointer reset**: pointer moved backward. This is classified as boundary
  evidence rather than ordinary timing variation.
- **State other than `RUNNING`**: ALSA endpoint left its expected running state;
  this is boundary evidence.
- **Arrival jitter**: wall-clock callback or sampler lateness. It is scheduler
  context only and is not, by itself, evidence of an audible artifact.
- **Liquidsoap `source.time()`**: progression of the source's assigned media
  clock, sampled independently of normal `source.on_frame` batching. It is
  useful clock context, not a PCM checksum.

### D / dsnoop limitation

The D kernel capture PCM is owned through dsnoop. Its kernel `appl_ptr` remains
zero, and `delay`/`avail` become large cumulative values. The diagnostics mark
D's application pointer unavailable rather than deriving a false stall. D's
`state` and `hw_ptr` show post-loopback kernel progression, but do not prove
that Liquidsoap consumed every sample without duplication or corruption.

## What these diagnostics cannot prove

Healthy ALSA pointers do not prove PCM sample integrity. A short audible
stutter or repeated PCM fragment can occur with healthy pointer progression,
no PTS gap, no DISCONT, no XRUN, and no silence. A roughly 5 ms content defect
may be completely invisible to transport telemetry.

In particular, the diagnostics cannot rule out StereoTool repeating samples,
duplicating a processed block, briefly muting or corrupting samples, or making
a very small resampling/time correction while transport continues.

Lossy Aircheck recordings are weak evidence for sub-20-ms waveform defects.
The normal 64 kb/s HE-AACv2 Aircheck has approximately 46.4 ms codec frames and
can transform or conceal a much shorter underlying artifact.

## Django-admin master switch

Open **Administration -> Hardware -> Audio Pipeline**, then use:

```text
Enable audio-gap diagnostics
unchecked = disabled
checked   = enabled
```

The saved checkbox is desired startup state. It never rewires a running audio
graph. After changing it, perform a controlled restart of:

```bash
sudo systemctl restart isadoraair-engine
sudo systemctl restart isadoraair-encoders
```

StereoTool does not require a restart.

When disabled at startup, the Engine creates no diagnostic daemon thread, no
200 Hz procfs sampler, no 20 Hz JSON writer, and no diagnostic pad probe.
Generated Liquidsoap omits the diagnostic callback, `source.time()` sampling,
and recurrent diagnostic writer. The renderer state is included in the LKG
fingerprint. Thus disabled mode retains only passive imported code and has
effectively zero active diagnostic CPU, I/O, polling, timer, or thread cost.

## Operator workflow after a heard artifact

1. Note the local timestamp as accurately as practical and describe what was
   heard: silence, stutter, repeated fragment, distortion, or another blemish.
2. SSH to IsadoraAir promptly. The schema-4 two-file ring normally retains
   approximately 4.5-4.8 minutes.
3. Run the analyzer before restarting any service:

   ```bash
   cd /opt/isadoraair
   venv/bin/python manage.py analyze_audio_gap "YYYY-MM-DD HH:MM:SS"
   ```

4. For an approximate timestamp, widen the search:

   ```bash
   venv/bin/python manage.py analyze_audio_gap "YYYY-MM-DD HH:MM:SS" --window 30
   ```

5. If the incident matters, immediately copy both current and rotated files
   from `/run/isadoraair/audio_gap_diagnostics.jsonl*`, plus
   `/run/isadoraair/post_stereotool_audio.json`, to durable storage.
6. Do not restart Engine, encoders, ALSA, snd_aloop, Liquidsoap, or StereoTool
   until the ring is preserved.

## Interpreting analyzer output

- **Pre-StereoTool evidence** covers PTS continuity, DISCONT, Engine buffer
  progress, sink rendering, queue runway, and A.
- **A/B/C/D telemetry** reports state, pointer deltas and resets, delay/avail
  extrema, fast-sample read errors, and the largest pointer stall retained
  inside each JSON interval.
- **ALSA runway evidence** applies most directly to A's playback queue.
- **B discontinuity with healthy A** points toward pre-ST snd_aloop capture or
  StereoTool input.
- **C discontinuity with healthy A/B** points toward StereoTool processing or
  output.
- **D discontinuity with healthy A/B/C** points toward the post-ST loopback or
  Liquidsoap input boundary, subject to the dsnoop limitation above.
- **Arrival jitter and sampler lateness** are advisory scheduling context.
  Arrival jitter alone is not an audible-artifact diagnosis.
- **Notable samples and journal correlation** identify exact timestamps worth
  comparing with service jobs, errors, or operator reports. Temporal proximity
  alone does not establish causation.

`No transport-level discontinuity evidence detected` means exactly that. It
does not mean `no audible artifact occurred` and does not establish PCM-content
integrity.

## Resource cost and retention

The r0087 baseline wrote approximately 1.5 KB at 20 Hz (about 30 KB/s) to two
4 MiB tmpfs files and retained about 4.5 minutes. A conservative four-endpoint
20 Hz procfs benchmark used 0.64% of one CPU core and caused no physical block-
device I/O; r0087 directly sampled only A through procfs.

The schema-4 extension samples B/C/D at approximately 200 Hz on the existing
diagnostic daemon thread and still writes only at 20 Hz. The measured standalone
candidate cost was 6.21% of one CPU core, approximately 0.52% of this 12-core
host. Maximum measured sample interval was about 5.20 ms. Estimated tmpfs JSON
traffic is about 59 KB/s. Two 8 MiB files impose a 16 MiB hard bound and retain
roughly 4.5-4.8 minutes. Procfs reads and `/run` writes produced zero physical
block-device I/O in the benchmark.

Disabled-path tests prove that the sampler, ring thread, pad probe, Liquidsoap
callback, diagnostic recurrent timer, and diagnostic file output are all
omitted. Production OFF/ON acceptance should additionally confirm no file
growth and no diagnostic thread after each controlled restart.

## 2026-09-26 11:15 CDT incident

The first naturally heard incident after r0087 activation occurred at
approximately 11:15 CDT. Retained evidence showed continuous Engine/GStreamer
PTS, no DISCONT, advancing Engine and sink buffers, ALSA A in `RUNNING`, and a
minimum A delay of 7,728 frames (about 175.24 ms). There was no A starvation,
pointer reset, XRUN, Engine recovery, or track boundary. This materially
disfavors Engine producer starvation and Engine-side playback underrun.

It does not exclude B/C/D behavior, StereoTool processing, PCM-content defects,
FM-only output behavior, or downstream distribution. The post-ST Aircheck
showed no gross collapse but was lossy HE-AACv2 and cannot exclude a 5-20 ms
underlying artifact.

The preserved evidence archive is stored durably at:

```text
/var/lib/isadoraair/reports/isadoraair-audio-gap-20260926-111500-CDT-evidence-final.tar.gz
SHA-256 ca6777e0bf82ef4b8074fd7df14523158e650a481c6c6af6a95610f1bab44e59
```

## Open hypotheses

The evidence still permits:

- snd_aloop capture-side timing;
- StereoTool internal processing or resampling/time correction;
- post-ST ALSA transport behavior;
- CPU P-state/C-state interaction with a timing-sensitive boundary;
- snd_aloop timer-source behavior;
- an FM-only D10s/USB output-path artifact; and
- downstream encoder, distribution, or monitoring behavior.

A track transition, weather/road generation, metadata update, database task, or
other short workload may be a stimulus without being the root cause. The 11:15
incident occurred mid-song, so track transitions cannot explain every event.
