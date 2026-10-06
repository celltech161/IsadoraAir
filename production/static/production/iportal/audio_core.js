/* iPortal audio core (2.22B) -- pure, DOM-free audio operations.
 *
 * Audio is held as plain PCM: { sampleRate: Number, channels: [Float32Array, ...] }
 * so every operation is deterministic, unit-testable, and storable in IndexedDB
 * (drafts). Nothing here touches the network, the station or the program
 * output: this is browser-local editing only.
 *
 * Behaviour ported from the proven OGRemote workstation (gain range, Set Level,
 * peak normalize, insert/replace punch-in, silent-channel collapse, WAV).
 */
(function (root) {
  "use strict";

  var GAIN_MIN_DB = -15;
  var GAIN_MAX_DB = 15;
  var GAIN_STEP_DB = 0.5;
  var SET_LEVEL_SECONDS = 6;
  var SET_LEVEL_TARGET_DBFS = -6;       // aim peaks here
  var SET_LEVEL_MIN_SIGNAL_DBFS = -40;  // below this it is "no signal"
  var NORMALIZE_PEAK = 0.95;            // peak normalize target (existing VT editor rule)
  var SILENCE_PEAK = 1e-3;              // ~ -60 dBFS: a "silent" channel
  var UNDO_MAX_STEPS = 5;
  var UNDO_MAX_BYTES = 400 * 1024 * 1024;

  function dbToLinear(db) { return Math.pow(10, db / 20); }
  function linearToDb(x) { return x > 0 ? 20 * Math.log10(x) : -Infinity; }

  function clampGainDb(db) {
    var value = Number(db);
    if (!isFinite(value)) value = 0;
    value = Math.max(GAIN_MIN_DB, Math.min(GAIN_MAX_DB, value));
    return Math.round(value / GAIN_STEP_DB) * GAIN_STEP_DB;
  }

  /* Set Level: the measurement was taken at currentGainDb, so the observed
   * peak already includes it: new = current + (target - observed). */
  function setLevelGain(currentGainDb, observedPeak) {
    var observedDbfs = linearToDb(observedPeak);
    if (!(observedDbfs >= SET_LEVEL_MIN_SIGNAL_DBFS)) {
      return { ok: false, reason: "no_signal", observedDbfs: observedDbfs };
    }
    var gain = clampGainDb((Number(currentGainDb) || 0) + (SET_LEVEL_TARGET_DBFS - observedDbfs));
    return { ok: true, gainDb: gain, observedDbfs: observedDbfs };
  }

  function make(sampleRate, channels) { return { sampleRate: sampleRate, channels: channels }; }
  function length(pcm) { return pcm.channels.length ? pcm.channels[0].length : 0; }
  function duration(pcm) { return pcm.sampleRate ? length(pcm) / pcm.sampleRate : 0; }
  function byteSize(pcm) { return length(pcm) * pcm.channels.length * 4; }
  function clone(pcm) { return make(pcm.sampleRate, pcm.channels.map(function (c) { return new Float32Array(c); })); }

  function peak(pcm) {
    var max = 0;
    pcm.channels.forEach(function (data) {
      for (var i = 0; i < data.length; i++) {
        var a = data[i] < 0 ? -data[i] : data[i];
        if (a > max) max = a;
      }
    });
    return max;
  }

  function toSample(pcm, seconds) {
    var n = Math.round(Number(seconds) * pcm.sampleRate);
    return Math.max(0, Math.min(length(pcm), isFinite(n) ? n : 0));
  }

  /* keep: only [start, end); delete: everything except [start, end). */
  function trim(pcm, startSeconds, endSeconds, mode) {
    var a = toSample(pcm, startSeconds), b = toSample(pcm, endSeconds);
    if (b < a) { var t = a; a = b; b = t; }
    return make(pcm.sampleRate, pcm.channels.map(function (data) {
      if (mode === "delete") {
        var out = new Float32Array(data.length - (b - a));
        out.set(data.subarray(0, a), 0);
        out.set(data.subarray(b), a);
        return out;
      }
      return new Float32Array(data.subarray(a, b));
    }));
  }

  function applyGain(pcm, db) {
    var factor = dbToLinear(clampGainDb(db));
    return make(pcm.sampleRate, pcm.channels.map(function (data) {
      var out = new Float32Array(data.length);
      for (var i = 0; i < data.length; i++) out[i] = data[i] * factor;
      return out;
    }));
  }

  /* An explicit user operation: scale so the peak lands on NORMALIZE_PEAK. */
  function normalize(pcm, target) {
    var goal = target || NORMALIZE_PEAK;
    var p = peak(pcm);
    if (p <= SILENCE_PEAK) return { pcm: clone(pcm), gain: 1, applied: false };
    var gain = goal / p;
    return {
      pcm: make(pcm.sampleRate, pcm.channels.map(function (data) {
        var out = new Float32Array(data.length);
        for (var i = 0; i < data.length; i++) out[i] = data[i] * gain;
        return out;
      })), gain: gain, applied: true,
    };
  }

  /* Punch-in at atSeconds (OGRemote semantics):
   *   insert  -- keep the original tail after the overdub (length grows only
   *              if the overdub runs past the original end);
   *   replace -- the clip ends where the overdub ends. */
  function punchIn(original, overdub, atSeconds, mode) {
    if (overdub.sampleRate !== original.sampleRate) throw new Error("sample_rate_mismatch");
    var total = length(original), start = toSample(original, atSeconds), n = length(overdub);
    var outLen = mode === "replace" ? start + n : Math.max(start + n, total);
    return make(original.sampleRate, original.channels.map(function (orig, ch) {
      var out = new Float32Array(Math.max(1, outLen));
      out.set(orig.subarray(0, Math.min(start, total)), 0);
      var src = overdub.channels[Math.min(ch, overdub.channels.length - 1)];
      out.set(src.subarray(0, Math.max(0, Math.min(n, outLen - start))), start);
      if (mode !== "replace" && start + n < total) out.set(orig.subarray(start + n), start + n);
      return out;
    }));
  }

  function concat(a, b) {
    if (!a) return b;
    if (a.sampleRate !== b.sampleRate) throw new Error("sample_rate_mismatch");
    return make(a.sampleRate, a.channels.map(function (data, ch) {
      var other = b.channels[Math.min(ch, b.channels.length - 1)];
      var out = new Float32Array(data.length + other.length);
      out.set(data, 0);
      out.set(other, data.length);
      return out;
    }));
  }

  /* iOS Safari / some Bluetooth profiles record a mono mic into one side of a
   * stereo stream: collapse to mono when exactly one channel is silent. */
  function collapseSilentChannel(pcm) {
    if (pcm.channels.length < 2) return pcm;
    var peaks = pcm.channels.slice(0, 2).map(function (c) { return peak(make(pcm.sampleRate, [c])); });
    var live = peaks.map(function (p) { return p > SILENCE_PEAK; });
    if (live[0] && !live[1]) return make(pcm.sampleRate, [new Float32Array(pcm.channels[0])]);
    if (live[1] && !live[0]) return make(pcm.sampleRate, [new Float32Array(pcm.channels[1])]);
    return pcm;
  }

  /* 16-bit PCM WAV (little-endian), the lossless interchange the recorder
   * saves: no extra lossy generation is ever added in the browser. */
  function encodeWav(pcm) {
    var channels = pcm.channels.length, frames = length(pcm), bytesPerSample = 2;
    var dataBytes = frames * channels * bytesPerSample;
    var buffer = new ArrayBuffer(44 + dataBytes), view = new DataView(buffer);
    function str(offset, text) { for (var i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i)); }
    str(0, "RIFF"); view.setUint32(4, 36 + dataBytes, true); str(8, "WAVE");
    str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, channels, true);
    view.setUint32(24, pcm.sampleRate, true); view.setUint32(28, pcm.sampleRate * channels * bytesPerSample, true);
    view.setUint16(32, channels * bytesPerSample, true); view.setUint16(34, 16, true);
    str(36, "data"); view.setUint32(40, dataBytes, true);
    var offset = 44;
    for (var i = 0; i < frames; i++) {
      for (var ch = 0; ch < channels; ch++) {
        var s = Math.max(-1, Math.min(1, pcm.channels[ch][i]));
        view.setInt16(offset, s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7fff), true);
        offset += 2;
      }
    }
    return new Uint8Array(buffer);
  }

  /* min/max per bucket for the waveform view of [startSeconds, endSeconds). */
  function peaksForRange(pcm, startSeconds, endSeconds, buckets) {
    var a = toSample(pcm, startSeconds), b = Math.max(a + 1, toSample(pcm, endSeconds));
    var per = Math.max(1, Math.floor((b - a) / buckets)), out = [];
    var mono = pcm.channels;
    for (var k = 0; k < buckets; k++) {
      var lo = 0, hi = 0, from = a + k * per, to = Math.min(b, from + per);
      for (var i = from; i < to; i++) {
        for (var ch = 0; ch < mono.length; ch++) {
          var v = mono[ch][i];
          if (v < lo) lo = v;
          if (v > hi) hi = v;
        }
      }
      out.push([lo, hi]);
    }
    return out;
  }

  function fromAudioBuffer(audioBuffer) {
    var channels = [];
    for (var ch = 0; ch < audioBuffer.numberOfChannels; ch++) channels.push(new Float32Array(audioBuffer.getChannelData(ch)));
    return make(audioBuffer.sampleRate, channels);
  }

  function toAudioBuffer(ctx, pcm) {
    var buffer = ctx.createBuffer(pcm.channels.length, Math.max(1, length(pcm)), pcm.sampleRate);
    pcm.channels.forEach(function (data, ch) { buffer.getChannelData(ch).set(data); });
    return buffer;
  }

  /* Bounded undo: at most UNDO_MAX_STEPS snapshots and UNDO_MAX_BYTES. */
  function UndoStack(maxSteps, maxBytes) {
    this.maxSteps = maxSteps || UNDO_MAX_STEPS;
    this.maxBytes = maxBytes || UNDO_MAX_BYTES;
    this.items = [];
  }
  UndoStack.prototype.push = function (pcm, label, meta) {
    this.items.push({ pcm: pcm, label: label || "", meta: meta || null });
    while (this.items.length > this.maxSteps || (this.items.length > 1 && this.bytes() > this.maxBytes)) {
      this.items.shift();
    }
  };
  UndoStack.prototype.pop = function () { return this.items.length ? this.items.pop() : null; };
  UndoStack.prototype.clear = function () { this.items = []; };
  UndoStack.prototype.canUndo = function () { return this.items.length > 0; };
  UndoStack.prototype.bytes = function () {
    return this.items.reduce(function (sum, item) { return sum + byteSize(item.pcm); }, 0);
  };

  root.IPortalAudio = {
    GAIN_MIN_DB: GAIN_MIN_DB, GAIN_MAX_DB: GAIN_MAX_DB, GAIN_STEP_DB: GAIN_STEP_DB,
    SET_LEVEL_SECONDS: SET_LEVEL_SECONDS, SET_LEVEL_TARGET_DBFS: SET_LEVEL_TARGET_DBFS,
    SET_LEVEL_MIN_SIGNAL_DBFS: SET_LEVEL_MIN_SIGNAL_DBFS, NORMALIZE_PEAK: NORMALIZE_PEAK, SILENCE_PEAK: SILENCE_PEAK,
    dbToLinear: dbToLinear, linearToDb: linearToDb, clampGainDb: clampGainDb, setLevelGain: setLevelGain,
    make: make, length: length, duration: duration, byteSize: byteSize, clone: clone, peak: peak,
    trim: trim, applyGain: applyGain, normalize: normalize, punchIn: punchIn, concat: concat,
    collapseSilentChannel: collapseSilentChannel, encodeWav: encodeWav, peaksForRange: peaksForRange,
    fromAudioBuffer: fromAudioBuffer, toAudioBuffer: toAudioBuffer, UndoStack: UndoStack,
  };
})(typeof window !== "undefined" ? window : this);
