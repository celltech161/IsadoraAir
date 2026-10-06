/* iPortal browser recorder (2.22B) -- microphone capture only.
 *
 * Graph:  mic -> GainNode (user gain, -15..+15 dB) -> AnalyserNode (meter)
 *              -> AudioWorklet "iportal-capture" (raw float PCM, lossless)
 *         or, if AudioWorklet is unavailable, -> MediaStreamDestination ->
 *            MediaRecorder (MIME negotiated from an explicit list, decoded
 *            back to PCM in the browser).
 *
 * Operator choices are honoured exactly: echo cancellation and automatic gain
 * control are ALWAYS off (AGC ramps the level down over a take); browser noise
 * suppression is on only when the operator turns it on.
 *
 * Deterministic device behaviour: the input device cannot change while a take
 * is being captured; if the active device disappears, the take stops, the
 * audio captured so far is kept, and onError("device_lost") fires. When the
 * tab is hidden during capture (mobile suspension), the take is paused.
 *
 * Nothing here can touch the station: a browser or microphone failure only
 * ever affects this page.
 */
(function (root) {
  "use strict";
  var A = root.IPortalAudio;
  var MIME_PREFERENCES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4", "audio/webm"];

  function supported() {
    var md = root.navigator && root.navigator.mediaDevices;
    var Ctx = root.AudioContext || root.webkitAudioContext;
    return {
      getUserMedia: !!(md && md.getUserMedia),
      audioContext: !!Ctx,
      audioWorklet: !!(Ctx && Ctx.prototype && "audioWorklet" in Ctx.prototype),
      mediaRecorder: typeof root.MediaRecorder !== "undefined",
      isIOS: /iPad|iPhone|iPod/.test(root.navigator ? root.navigator.userAgent : ""),
    };
  }

  function negotiateMime() {
    if (typeof root.MediaRecorder === "undefined" || !root.MediaRecorder.isTypeSupported) return "";
    for (var i = 0; i < MIME_PREFERENCES.length; i++) {
      if (root.MediaRecorder.isTypeSupported(MIME_PREFERENCES[i])) return MIME_PREFERENCES[i];
    }
    return "";
  }

  function Recorder(options) {
    options = options || {};
    this.workletUrl = options.workletUrl;
    this.maxSeconds = options.maxSeconds || 600;
    this.onLevel = options.onLevel || function () {};
    this.onState = options.onState || function () {};
    this.onError = options.onError || function () {};
    this.state = "idle";        // idle | armed | recording | paused | stopping
    this.gainDb = 0;
    this.noiseSuppression = false;
    this.deviceId = "";
    this._chunks = null;
    this._captured = 0;
    this._meterTimer = null;
    this._visibility = this._onVisibility.bind(this);
    this._deviceChange = this._onDeviceChange.bind(this);
  }

  Recorder.supported = supported;
  Recorder.negotiateMime = negotiateMime;

  Recorder.prototype._context = function () {
    if (!this.ctx) {
      var Ctx = root.AudioContext || root.webkitAudioContext;
      this.ctx = new Ctx();
    }
    return this.ctx;
  };

  Recorder.prototype.listDevices = async function () {
    var md = root.navigator.mediaDevices;
    if (!md || !md.enumerateDevices) return [];
    var devices = await md.enumerateDevices();
    // Labels are empty until permission is granted; callers re-list after arm().
    return devices.filter(function (d) { return d.kind === "audioinput"; })
      .map(function (d, i) { return { deviceId: d.deviceId, label: d.label || ("Microphone " + (i + 1)) }; });
  };

  Recorder.prototype._getStream = async function () {
    var md = root.navigator.mediaDevices;
    var constraints = { echoCancellation: false, autoGainControl: false, noiseSuppression: !!this.noiseSuppression };
    if (this.deviceId) constraints.deviceId = { exact: this.deviceId };
    try {
      return await md.getUserMedia({ audio: constraints });
    } catch (err) {
      if (err && (err.name === "NotAllowedError" || err.name === "SecurityError")) throw err;
      if (this.deviceId) {
        // The chosen device is gone: fall back to the default device, same constraints.
        delete constraints.deviceId;
        this.deviceId = "";
        return await md.getUserMedia({ audio: constraints });
      }
      throw err;
    }
  };

  Recorder.prototype.arm = async function (opts) {
    opts = opts || {};
    if (this.state === "recording" || this.state === "paused") throw new Error("busy");
    if (!supported().getUserMedia) throw Object.assign(new Error("unsupported"), { name: "NotSupportedError" });
    this.release();
    if (opts.deviceId !== undefined) this.deviceId = opts.deviceId;
    if (opts.noiseSuppression !== undefined) this.noiseSuppression = !!opts.noiseSuppression;
    var ctx = this._context();
    if (ctx.state === "suspended") await ctx.resume();
    this.stream = await this._getStream();
    var track = this.stream.getAudioTracks()[0];
    var settings = track && track.getSettings ? track.getSettings() : {};
    this.channelCount = settings.channelCount === 1 ? 1 : 2;
    this.source = ctx.createMediaStreamSource(this.stream);
    this.gain = ctx.createGain();
    this.gain.gain.value = A.dbToLinear(this.gainDb);
    this.analyser = ctx.createAnalyser();
    this.analyser.fftSize = 1024;
    this.source.connect(this.gain);
    this.gain.connect(this.analyser);
    this.mode = "mediarecorder";
    if (supported().audioWorklet && this.workletUrl) {
      try {
        if (!this._workletLoaded) { await ctx.audioWorklet.addModule(this.workletUrl); this._workletLoaded = true; }
        this.capture = new root.AudioWorkletNode(ctx, "iportal-capture", {
          numberOfInputs: 1, numberOfOutputs: 1, channelCount: this.channelCount,
          channelCountMode: "explicit", outputChannelCount: [this.channelCount],
        });
        var self = this;
        this.capture.port.onmessage = function (event) { self._onBlock(event.data); };
        this.gain.connect(this.capture);
        // Keep the worklet pulled without making any sound: a muted sink.
        this.silence = ctx.createGain();
        this.silence.gain.value = 0;
        this.capture.connect(this.silence);
        this.silence.connect(ctx.destination);
        this.mode = "worklet";
      } catch (err) {
        this.mode = "mediarecorder";
      }
    }
    if (this.mode === "mediarecorder") {
      if (!supported().mediaRecorder) throw Object.assign(new Error("unsupported"), { name: "NotSupportedError" });
      this.destination = ctx.createMediaStreamDestination();
      this.destination.channelCount = this.channelCount;   // iOS: mono mic -> mono destination
      this.gain.connect(this.destination);
      this.mimeType = negotiateMime();
    }
    if (track) track.addEventListener("ended", this._deviceChange);
    if (root.navigator.mediaDevices.addEventListener) {
      root.navigator.mediaDevices.addEventListener("devicechange", this._deviceChange);
    }
    root.document.addEventListener("visibilitychange", this._visibility);
    this._startMeter();
    this._setState("armed");
    return { mode: this.mode, channelCount: this.channelCount, mimeType: this.mimeType || "" };
  };

  Recorder.prototype.setGainDb = function (db) {
    this.gainDb = A.clampGainDb(db);
    if (this.gain) this.gain.gain.value = A.dbToLinear(this.gainDb);
    return this.gainDb;
  };

  Recorder.prototype.currentPeak = function () {
    if (!this.analyser) return 0;
    var buf = new Float32Array(this.analyser.fftSize);
    this.analyser.getFloatTimeDomainData(buf);
    var p = 0;
    for (var i = 0; i < buf.length; i++) { var a = Math.abs(buf[i]); if (a > p) p = a; }
    return p;
  };

  Recorder.prototype._startMeter = function () {
    var self = this;
    this._stopMeter();
    this._meterTimer = root.setInterval(function () { self.onLevel(self.currentPeak()); }, 50);
  };
  Recorder.prototype._stopMeter = function () {
    if (this._meterTimer) { root.clearInterval(this._meterTimer); this._meterTimer = null; }
  };

  /* Set Level: listen for SET_LEVEL_SECONDS, then move the gain so peaks land
   * on the target. Not available while capturing. */
  Recorder.prototype.setLevel = function (seconds) {
    var self = this;
    if (this.state !== "armed") return Promise.reject(new Error("not_armed"));
    var until = Date.now() + 1000 * (seconds || A.SET_LEVEL_SECONDS), observed = 0;
    return new Promise(function (resolve) {
      var timer = root.setInterval(function () {
        observed = Math.max(observed, self.currentPeak());
        if (Date.now() >= until || self.state !== "armed") {
          root.clearInterval(timer);
          var result = A.setLevelGain(self.gainDb, observed);
          if (result.ok) self.setGainDb(result.gainDb);
          resolve(result);
        }
      }, 50);
    });
  };

  Recorder.prototype._onBlock = function (blocks) {
    if (this.state !== "recording") return;
    var frames = blocks[0].length;
    if (this._captured + frames > this.maxSeconds * this.ctx.sampleRate) {
      frames = Math.max(0, this.maxSeconds * this.ctx.sampleRate - this._captured);
      blocks = blocks.map(function (b) { return b.subarray(0, frames); });
    }
    this._chunks.push(blocks);
    this._captured += frames;
    if (this._captured >= this.maxSeconds * this.ctx.sampleRate) {
      // The page is told first (it stops and keeps the take); stopping here
      // too guarantees capture ends even without a listener.
      this.onError("max_duration");
      this.stop();
    }
  };

  Recorder.prototype.elapsedSeconds = function () {
    if (this.mode === "worklet") return this.ctx ? this._captured / this.ctx.sampleRate : 0;
    return (this._elapsedBefore || 0) + (this._segmentStart ? (Date.now() - this._segmentStart) / 1000 : 0);
  };

  Recorder.prototype.start = function () {
    if (this.state !== "armed") throw new Error("not_armed");
    this._chunks = [];
    this._captured = 0;
    this._elapsedBefore = 0;
    if (this.mode === "worklet") {
      this.capture.port.postMessage("start");
    } else {
      var self = this;
      this._blobs = [];
      this.mediaRecorder = this.mimeType ? new root.MediaRecorder(this.destination.stream, { mimeType: this.mimeType })
        : new root.MediaRecorder(this.destination.stream);
      this.mediaRecorder.ondataavailable = function (e) { if (e.data && e.data.size) self._blobs.push(e.data); };
      this.mediaRecorder.start(1000);
      this._segmentStart = Date.now();
      this._maxTimer = root.setInterval(function () {
        if (self.elapsedSeconds() >= self.maxSeconds) { self.onError("max_duration"); self.stop(); }
      }, 200);
    }
    this._setState("recording");
  };

  Recorder.prototype.pause = function () {
    if (this.state !== "recording") return;
    if (this.mode === "worklet") {
      this.capture.port.postMessage("pause");
    } else if (this.mediaRecorder && this.mediaRecorder.state === "recording" && this.mediaRecorder.pause) {
      this.mediaRecorder.pause();
      this._elapsedBefore = this.elapsedSeconds();
      this._segmentStart = 0;
    } else {
      return;   // no pause support in this browser's MediaRecorder: keep recording, say so
    }
    this._setState("paused");
  };

  Recorder.prototype.resume = function () {
    if (this.state !== "paused") return;
    if (this.mode === "worklet") this.capture.port.postMessage("start");
    else { this.mediaRecorder.resume(); this._segmentStart = Date.now(); }
    this._setState("recording");
  };

  /* Resolves with the captured PCM ({sampleRate, channels}), possibly empty. */
  Recorder.prototype.stop = function () {
    var self = this;
    // A stop already in flight (e.g. the max-duration auto-stop) is shared with
    // every caller, so whoever asks receives the captured take.
    if (this._stopping) return this._stopping;
    if (this.state !== "recording" && this.state !== "paused") return Promise.resolve(null);
    this._setState("stopping");
    if (this._maxTimer) { root.clearInterval(this._maxTimer); this._maxTimer = null; }
    if (this.mode === "worklet") {
      this.capture.port.postMessage("stop");
      var rate = this.ctx.sampleRate, chunks = this._chunks || [];
      var channels = chunks.length ? chunks[0].length : this.channelCount;
      var total = chunks.reduce(function (s, c) { return s + c[0].length; }, 0);
      var out = [];
      for (var ch = 0; ch < channels; ch++) {
        var data = new Float32Array(total), off = 0;
        chunks.forEach(function (c) { var src = c[Math.min(ch, c.length - 1)]; data.set(src, off); off += src.length; });
        out.push(data);
      }
      this._chunks = null;
      this._stopping = Promise.resolve(A.collapseSilentChannel(A.make(rate, out)));
    } else {
      this._stopping = new Promise(function (resolve, reject) {
        self.mediaRecorder.onstop = async function () {
          try {
            var blob = new Blob(self._blobs, { type: self.mediaRecorder.mimeType || self.mimeType || "" });
            var buffer = await self.ctx.decodeAudioData(await blob.arrayBuffer());
            resolve(A.collapseSilentChannel(A.fromAudioBuffer(buffer)));
          } catch (err) { reject(err); }
        };
        self.mediaRecorder.stop();
      });
    }
    return this._stopping.then(function (pcm) {
      self._stopping = null;
      self._setState(self.stream ? "armed" : "idle");
      return pcm;
    }, function (err) {
      self._stopping = null;
      self._setState(self.stream ? "armed" : "idle");
      throw err;
    });
  };

  /* Release the microphone. Required before playback on iOS, where an active
   * capture track forces earpiece routing. */
  Recorder.prototype.release = function () {
    this._stopMeter();
    root.document.removeEventListener("visibilitychange", this._visibility);
    if (root.navigator.mediaDevices && root.navigator.mediaDevices.removeEventListener) {
      root.navigator.mediaDevices.removeEventListener("devicechange", this._deviceChange);
    }
    if (this.stream) this.stream.getTracks().forEach(function (t) { try { t.stop(); } catch (e) { /* ignore */ } });
    ["source", "gain", "analyser", "capture", "silence", "destination"].forEach(function (name) {
      if (this[name]) { try { this[name].disconnect(); } catch (e) { /* ignore */ } this[name] = null; }
    }, this);
    this.stream = null;
    if (this.state === "armed") this._setState("idle");
  };

  Recorder.prototype._onDeviceChange = function () {
    var track = this.stream && this.stream.getAudioTracks()[0];
    var lost = !track || track.readyState === "ended";
    if (!lost) return;
    var self = this;
    if (this.state === "recording" || this.state === "paused") {
      this.stop().then(function (pcm) { self.release(); self.onError("device_lost", pcm); });
    } else {
      this.release();
      this.onError("device_lost", null);
    }
  };

  Recorder.prototype._onVisibility = function () {
    if (root.document.hidden && this.state === "recording") {
      this.pause();
      if (this.state === "paused") this.onError("suspended");
    }
  };

  Recorder.prototype._setState = function (state) {
    this.state = state;
    this.onState(state);
  };

  root.IPortalRecorder = Recorder;
})(window);
