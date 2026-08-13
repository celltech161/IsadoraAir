# GStreamer element inventory — IsadoraAir 1.2 Phase 3

Derived by grepping the codebase for every `Gst.ElementFactory.make(...)`
call and cross-checking each element against the production host's
actual `gst-inspect-1.0` output and `dpkg -S` package ownership
(2026-08-12, Ubuntu 26.04, GStreamer 1.28.2) — not copied from the
existing README package list. See "README package list audit" at the
bottom for the one real discrepancy this found.

`library/services/engine.py` is the **only** production module that
touches GStreamer directly (confirmed: `grep -rl "gi.require_version.*Gst\|from gi.repository import Gst"`
across the whole repo returns only `engine.py` and one test file that
mirrors the same elements for a mixer-timeline test). No other app,
management command, or companion project uses GStreamer.

## Elements referenced in code

| Element | Used by (engine.py) | Plugin family | Apt package | Present on production? |
|---|---|---|---|---|
| `alsasrc` | studio mic input (`_start_mic_pipeline`) | plugins-base (built against system ALSA) | `gstreamer1.0-alsa` | ✅ |
| `alsasink` | master program output, StereoTool loop-out sink | plugins-base (built against system ALSA) | `gstreamer1.0-alsa` | ✅ |
| `audioconvert` | every format-normalization point: mic, deck playback, FX/VT fire paths, Remote DJ, monitoring mixdown | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `audioresample` | same points as `audioconvert`, paired 1:1 throughout | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `audiomixer` | master mixer, program+FX submix, Remote DJ monitor mixer | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `audiotestsrc` | silence-filler sources (Remote DJ slot idle, FX bus idle, deck gap-fill) | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `capsfilter` | fixed-format enforcement throughout (mic, deck, output, Remote DJ, FX, VT, monitoring) | core | `libgstreamer1.0-0` (core, always present) | ✅ |
| `volume` | every gain-stage: mic gain/PTT/gate, remote-DJ gain, program gain, AGC makeup, duck gain (2×), VT duck, FX bus gain, FX/VT per-fire gain | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `level` | output metering (`output_level`), Remote DJ per-session level | plugins-good | `gstreamer1.0-plugins-good` | ✅ |
| `audiodynamic` | AGC compressor (`agc_dynamic`) | plugins-good | `gstreamer1.0-plugins-good` | ✅ |
| `rglimiter` | AGC brickwall limiter (`agc_limiter`, ReplayGain limiter reused as a true-peak limiter) | plugins-good | `gstreamer1.0-plugins-good` | ✅ |
| `input-selector` | Remote DJ per-slot source select (local mic vs. remote WebRTC) | core | `libgstreamer1.0-0` | ✅ |
| `tee` | StereoTool loop-out split, Remote DJ signaling split, local-mic split | core | `libgstreamer1.0-0` | ✅ |
| `queue` | thread-boundary buffering at mic, monitoring, Remote DJ session paths | core | `libgstreamer1.0-0` | ✅ |
| `concat` | gapless deck-boundary splicing | core | `libgstreamer1.0-0` | ✅ |
| `filesrc` | deck/FX/VT file playback source | core | `libgstreamer1.0-0` | ✅ |
| `decodebin` | deck/FX/VT file decode (format-agnostic; see "decodebin's actual decode path" below) | plugins-base (`libgstplayback.so`) | `gstreamer1.0-plugins-base` | ✅ |
| `fakesink` | probe-only pipelines (duration/analysis passes, test scaffolding) | core | `libgstreamer1.0-0` | ✅ |
| `webrtcbin` | Remote DJ WebRTC session (SDP/ICE/DTLS-SRTP) | plugins-bad | `gstreamer1.0-plugins-bad` | ✅ |
| `opusenc` | monitoring mixdown → Opus (fed to `rtpopuspay`) | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `opusdec` | Remote DJ inbound Opus decode | plugins-base | `gstreamer1.0-plugins-base` | ✅ |
| `rtpopuspay` | monitoring mixdown RTP payload | plugins-good | `gstreamer1.0-plugins-good` | ✅ |
| `rtpopusdepay` | Remote DJ inbound RTP depayload | plugins-good | `gstreamer1.0-plugins-good` | ✅ |

**Nice/WebRTC note**: `webrtcbin` itself only needs `gstreamer1.0-plugins-bad`
(confirmed above — it's `libgstwebrtc.so`, not a separate nice-specific
element). `gstreamer1.0-nice` (ICE/STUN/TURN, package `gstreamer1.0-nice`,
0.1.23-2 on production) is a **runtime dependency `webrtcbin` loads
internally** for ICE candidate gathering, not a distinct element
IsadoraAir's own code names directly — still required for Remote DJ to
actually establish a session, just not visible to a code grep.

## `decodebin`'s actual decode path

`decodebin` is a meta-element; what it autoplugs depends on the actual
file formats fed to it. The production library
(`/srv/isadoraair/music`, 717+ GB) is, by extension count: FLAC (27,070
files, dominant), M4A/AAC (2,381), MP3 (583), AIFF (14), a handful of
WAV/MP2. Verified which plugin provides each container/codec:

| Format | Demux/decode element | Plugin family | Apt package |
|---|---|---|---|
| FLAC | `flacparse` + `flacdec` | plugins-good | `gstreamer1.0-plugins-good` |
| M4A/AAC | `qtdemux` (container) + `avdec_aac` (codec) | plugins-good + **libav** | `gstreamer1.0-plugins-good` + `gstreamer1.0-libav` |
| MP3 | `mpegaudioparse` + `id3demux` (tags) + `avdec_mp3` (codec) | plugins-good + **libav** | `gstreamer1.0-plugins-good` + `gstreamer1.0-libav` |
| AIFF | `aiffparse` | **plugins-bad** | `gstreamer1.0-plugins-bad` |
| WAV | `wavparse` | plugins-good | `gstreamer1.0-plugins-good` |

Notable: classic `mad` (the traditional gst-plugins-ugly MP3 decoder) is
**not present** in this Ubuntu release's `gstreamer1.0-plugins-ugly`
package at all (confirmed via `gst-inspect-1.0 mad` → "No such element"
and `dpkg -L gstreamer1.0-plugins-ugly` → no `libgstmad.so`). MP3 decode
is fully served by `gst-libav`'s `avdec_mp3` instead — meaning
**`gstreamer1.0-libav` is load-bearing for two of the five library
formats** (AAC and MP3), not merely a nice-to-have.

## README package-list audit

Current `README.md` (project root, install section) documents:

```
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-alsa \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-libav
```

Cross-checked against the table above:

- `python3-gi`, `gir1.2-gstreamer-1.0` — required (PyGObject bindings; production also has `gir1.2-gst-plugins-base-1.0`/`-bad-1.0`/`-extra-1.0` installed, none of which engine.py's own imports require directly, but harmless/typically pulled in transitively).
- `gstreamer1.0-alsa` — **required** (`alsasrc`/`alsasink`).
- `gstreamer1.0-plugins-base` — **required** (majority of the element list, including `decodebin` itself).
- `gstreamer1.0-plugins-good` — **required** (`level`, `audiodynamic`, `rglimiter`, `rtpopus{pay,depay}`, plus FLAC/MP3-tag/WAV demux/decode).
- `gstreamer1.0-plugins-bad` — **required** (`webrtcbin`, AIFF demux).
- `gstreamer1.0-libav` — **required** (AAC and MP3 codec decode, per above — not optional).
- **`gstreamer1.0-plugins-ugly` — installed on production, documented as required, but nothing in this inventory (neither engine.py's own elements nor the five library formats' actual decode path) uses anything it provides** (its plugin set is a52dec/asf/cdio/dvdlpcmdec/dvdread/dvdsub/mpeg2dec/realmedia/sid/x264 — none of which are library audio formats or codecs used here). Flagging this as a documentation discrepancy for a future, separate cleanup pass — **not changed in this Phase 3 pass** (out of the stated safety boundary: no README/behavior changes beyond what's needed for the dependency baseline itself). It's possible this package earned its place historically for a format no longer in the library, or was copied from a generic GStreamer tutorial's package list; worth a deliberate decision rather than silently dropping it.

`gstreamer1.0-nice` is **not currently in the documented install list at
all**, despite being a real runtime dependency of `webrtcbin`/Remote DJ
(confirmed installed on production as `gstreamer1.0-nice` 0.1.23-2). This
is a genuine gap, not a discrepancy in the other direction — Remote DJ
would very likely fail to establish ICE connectivity without it on a
from-scratch install that only followed the currently-documented list.
