/* iPortal workstation controller (2.22B).
 *
 * Domain-neutral: everything domain-specific arrives in the RecordingContext
 * JSON the server rendered (title, display rows, allowed operations, limits,
 * revision, the current committed audio). The page never names a path or a storage
 * key; it talks to the adapter's own recorder endpoints with Django's session
 * cookie and CSRF token.
 *
 * Three states are kept visibly distinct: the PREVIEW (browser-local edit,
 * never on air), a saved take (immutable ProductionMedia, not yet committed),
 * and the domain's committed binding (by default worded ON AIR; a domain
 * supplies its own wording through RecordingContext.terms).
 */
(function (root) {
  "use strict";
  var A = root.IPortalAudio;
  var doc = root.document;
  var DRAFT_DB = "iportal-drafts", DRAFT_STORE = "drafts";
  var DRAFT_MAX_AGE_MS = 7 * 24 * 3600 * 1000, DRAFT_MAX_BYTES = 300 * 1024 * 1024;

  function $(id) { return doc.getElementById(id); }
  function cookie(name) {
    var match = doc.cookie.split(";").map(function (c) { return c.trim(); })
      .filter(function (c) { return c.indexOf(name + "=") === 0; })[0];
    return match ? decodeURIComponent(match.slice(name.length + 1)) : "";
  }
  function fmt(seconds) {
    if (!isFinite(seconds) || seconds < 0) seconds = 0;
    var m = Math.floor(seconds / 60), s = seconds - m * 60;
    return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
  }
  function query(params) {
    return Object.keys(params).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]); }).join("&");
  }

  var S = {
    ctx: JSON.parse($("iportal-context").textContent),
    api: JSON.parse($("iportal-api").textContent),
    page: JSON.parse($("iportal-page").textContent),
    baseRevision: null,            // the revision the editor content is based on (see commit)
    pcm: null, dirty: false, ops: [],
    sourceKind: "none",            // none | recording | import | edit-media | edit-legacy
    parentMediaId: null, importFile: null, importUnedited: false,
    selection: null, view: null, playhead: 0,
    undo: new A.UndoStack(), recorder: null, playing: null,
    previewTiming: null, previewFrame: null,
    pendingTake: null, upload: null,
  };
  root.IPortalWorkstation = S;      // exposed for tests / debugging only

  S.baseRevision = S.ctx.revision;

  function allowed(op) { return S.ctx.allowed_operations.indexOf(op) >= 0; }
  // The domain's wording for its committed state (RecordingContext.terms);
  // the fallbacks are the original wording.
  var DEFAULT_TERMS = {
    badge: "ON AIR", save: "Save & put on air", current: "on-air take",
    current_exists: "A take is already on air here", none: "Nothing on air yet.",
    changed: "The on-air audio has changed since then.", saved: "",
    editor_hint: "browser only; nothing here is on air until you save",
    restored: "Draft restored. It is not on air until you save.",
  };
  function term(name) { return (S.ctx.terms && S.ctx.terms[name]) || DEFAULT_TERMS[name]; }
  // Drafts are keyed by the SERVER-provided per-user namespace + workspace +
  // subject: another person using this browser profile never sees them.
  var DRAFT_KEY_PREFIX = "u:" + S.page.draft_namespace + "|";
  function subjectKey() { return DRAFT_KEY_PREFIX + S.ctx.adapter + "|" + JSON.stringify(S.ctx.subject); }
  // Content that no longer derives from an older editing session (a fresh
  // recording, an import, the committed take just loaded) is based on what the
  // page currently knows is committed.
  function rebase() { S.baseRevision = S.ctx.revision; }

  function message(text, kind) {
    var box = $("ipMessages");
    box.innerHTML = "";
    if (!text) return;
    var div = doc.createElement("div");
    div.className = "ip-banner " + (kind || "info");
    div.textContent = text;
    box.appendChild(div);
  }

  // -- context rendering -------------------------------------------------------
  function renderContext() {
    var c = S.ctx;
    $("ipTitle").textContent = c.title;
    $("ipPurpose").textContent = c.purpose;
    $("ipAirBadge").textContent = term("badge");
    $("ipSave").textContent = term("save");
    $("ipLoadCurrent").textContent = $("ipLoadCurrentEmpty").textContent = "Edit the " + term("current");
    $("ipEditorEmptyText").textContent = term("current_exists");
    $("ipEditorHint").textContent = term("editor_hint");
    $("ipMax").textContent = fmt(c.max_duration_seconds);
    var blocked = $("ipBlocked");
    blocked.hidden = !c.blocked_reason;
    blocked.textContent = c.blocked_reason || "";
    var table = $("ipDisplay");
    table.innerHTML = "";
    c.display.forEach(function (row) {
      var tr = doc.createElement("tr"), th = doc.createElement("th"), td = doc.createElement("td");
      th.textContent = row[0]; td.textContent = row[1];
      tr.appendChild(th); tr.appendChild(td); table.appendChild(tr);
    });
    var cur = $("ipAirCurrent");
    cur.innerHTML = "";
    if (c.current) {
      var label = doc.createElement("div");
      label.className = "ip-small";
      label.textContent = c.current.label + (c.current.duration_seconds ? " · " + fmt(c.current.duration_seconds) : "");
      var audio = doc.createElement("audio");
      audio.controls = true; audio.preload = "none"; audio.src = c.current.preview_url; audio.style.width = "100%";
      audio.addEventListener("play", function () { if (S.recorder) S.recorder.release(); });
      cur.appendChild(label); cur.appendChild(audio);
    } else {
      cur.textContent = term("none");
    }
    $("ipLoadCurrent").hidden = !(c.current && allowed("edit"));
    $("ipRemove").hidden = !allowed("remove");
    draw();                                          // the empty editor's text depends on what is committed
    refreshControls();
  }

  // -- input meter (r0114): the console's calibrated dBFS scale ----------------
  // The bar is the RMS level; the thin marker is the peak, held and falling
  // like the console's (audio_core.PeakHold). Display only: nothing here reads
  // or changes captured samples, the gain or Set Level.
  var VU_MIN_DB = A.vuMinDb(S.page.vu_min_db);
  var meterHold = new A.PeakHold();
  function initMeter() {
    var bar = $("ipMeter"), marks = A.vuThresholds(VU_MIN_DB);
    bar.style.setProperty("--ip-vu-yellow", marks.yellow + "%");
    bar.style.setProperty("--ip-vu-red", marks.red + "%");
    bar.setAttribute("aria-valuemin", String(VU_MIN_DB));
    var scale = $("ipMeterScale");
    scale.innerHTML = "";
    [{ db: VU_MIN_DB, cls: "first" }, { db: A.VU_YELLOW_DBFS, cls: "emph" }, { db: A.VU_RED_DBFS, cls: "" },
     { db: 0, cls: "last" }].forEach(function (tick) {
      if (tick.db < VU_MIN_DB) return;
      var span = doc.createElement("span");
      span.className = "ip-tick " + tick.cls;
      span.style.left = A.vuPercent(tick.db, VU_MIN_DB) + "%";
      span.textContent = String(Math.round(tick.db));
      scale.appendChild(span);
    });
    renderLevels(null);
  }
  function renderLevels(levels) {
    var bar = $("ipMeter"), mask = $("ipMeterMask"), mark = $("ipMeterPeak");
    if (!levels) {                                   // no live input: settle, never freeze
      meterHold.reset();
      mask.style.width = "100%";
      mark.hidden = true;
      bar.dataset.rmsPct = "0"; bar.dataset.peakPct = "0";
      bar.setAttribute("aria-valuenow", String(VU_MIN_DB));
      bar.setAttribute("aria-valuetext", "no input");
      return;
    }
    var rmsDb = A.levelDbfs(levels.rms), held = meterHold.update(A.levelDbfs(levels.peak), root.performance.now());
    var fill = A.vuPercent(rmsDb, VU_MIN_DB), peakPct = A.vuPercent(held, VU_MIN_DB);
    mask.style.width = (100 - fill) + "%";
    mark.hidden = !(peakPct > 0);
    mark.style.left = peakPct + "%";
    bar.dataset.rmsPct = fill.toFixed(2); bar.dataset.peakPct = peakPct.toFixed(2);
    var shown = isFinite(rmsDb) ? Math.max(VU_MIN_DB, Math.min(0, rmsDb)) : VU_MIN_DB;
    bar.setAttribute("aria-valuenow", shown.toFixed(1));
    bar.setAttribute("aria-valuetext", isFinite(rmsDb) ? rmsDb.toFixed(1) + " dBFS RMS" : "silence");
  }

  // -- recorder ------------------------------------------------------------------
  function capabilityCheck() {
    var caps = root.IPortalRecorder.supported();
    var problem = "";
    if (!caps.getUserMedia || !caps.audioContext) problem = "Recording isn't available in this browser. You can still import an audio file.";
    else if (!caps.audioWorklet && !caps.mediaRecorder) problem = "This browser can't capture audio. You can still import an audio file.";
    $("ipUnsupported").hidden = !problem;
    $("ipUnsupported").textContent = problem;
    S.canRecord = !problem && allowed("record");
    return caps;
  }

  function ensureRecorder() {
    if (S.recorder) return S.recorder;
    S.recorder = new root.IPortalRecorder({
      workletUrl: root.IPORTAL_WORKLET_URL,
      maxSeconds: S.ctx.max_duration_seconds,
      onLevel: renderLevels,
      onState: function () { refreshControls(); },
      onError: function (code, pcm) {
        if (code === "max_duration") {
          message("Maximum length reached — recording stopped.", "warn");
          stop();                                    // receives the take from the recorder's stop
        }
        else if (code === "device_lost") {
          message("The microphone was disconnected. Recording stopped; what was captured is kept.", "warn");
          if (pcm) acceptTake(pcm);
        } else if (code === "suspended") message("Recording paused because the page was hidden.", "warn");
      },
    });
    var saved = 0;
    try { saved = Number(root.localStorage.getItem("iportal_gain_db") || 0); } catch (e) { saved = 0; }
    S.recorder.setGainDb(saved);
    $("ipGain").value = String(S.recorder.gainDb);
    $("ipGainLabel").textContent = S.recorder.gainDb.toFixed(1) + " dB";
    try { $("ipNoise").checked = root.localStorage.getItem("iportal_ns") === "1"; } catch (e) { /* ignore */ }
    return S.recorder;
  }

  async function populateDevices() {
    var rec = ensureRecorder(), select = $("ipDevice"), current = select.value;
    var devices = await rec.listDevices();
    select.innerHTML = '<option value="">Default microphone</option>';
    devices.forEach(function (d) {
      var o = doc.createElement("option"); o.value = d.deviceId; o.textContent = d.label; select.appendChild(o);
    });
    select.value = current;
  }

  async function arm() {
    var rec = ensureRecorder();
    try {
      var info = await rec.arm({ deviceId: $("ipDevice").value, noiseSuppression: $("ipNoise").checked });
      await populateDevices();   // labels only appear after permission
      message("Microphone ready (" + (info.mode === "worklet" ? "lossless capture" : "browser recorder") + ").", "ok");
    } catch (err) {
      var name = err && err.name;
      if (name === "NotAllowedError" || name === "SecurityError") {
        message("Microphone permission was denied. Allow microphone access for this site and try again.", "error");
      } else if (name === "NotFoundError") {
        message("No microphone was found.", "error");
      } else {
        message("The microphone could not be started (" + (name || err) + ").", "error");
      }
    }
    refreshControls();
  }

  var timerHandle = null;
  function startTimer() {
    stopTimer();
    timerHandle = root.setInterval(function () {
      $("ipTimer").textContent = fmt(S.recorder ? S.recorder.elapsedSeconds() : 0);
    }, 100);
  }
  function stopTimer() { if (timerHandle) { root.clearInterval(timerHandle); timerHandle = null; } }

  // Punch-in always records over the take from the playhead; the two modes
  // differ only in what follows the new audio (audio_core.punchIn): "insert"
  // keeps the original tail, "replace" ends the take with the new recording.
  // (The identifiers are the editor's and its saved provenance; the labels
  // on the page say what they do.)
  function punchMode() { return $("ipPunchReplace").checked ? "replace" : "insert"; }

  function record() {
    haltPreview();
    var rec = ensureRecorder();
    S.punch = $("ipPunch").checked && S.pcm ? { at: S.playhead, mode: punchMode() } : null;
    rec.start();
    $("ipTimer").classList.add("recording");
    startTimer();
  }

  async function stop() {
    var pcm = await S.recorder.stop();
    stopTimer();
    $("ipTimer").classList.remove("recording");
    if (pcm && A.length(pcm) > 0) acceptTake(pcm);
    refreshControls();
  }

  function acceptTake(take) {
    haltPreview();
    if (S.punch && S.pcm) {
      pushUndo("punch-in");
      S.pcm = A.punchIn(S.pcm, take, S.punch.at, S.punch.mode);
      S.ops.push(S.punch.mode === "replace" ? "punch-replace" : "punch-insert");
      if (S.sourceKind === "import") S.importUnedited = false;
    } else {
      if (S.pcm) pushUndo("re-record");
      S.pcm = take;
      rebase();
      S.sourceKind = "recording";
      S.parentMediaId = null;
      S.importFile = null;
      S.ops = [];
    }
    S.punch = null;
    S.selection = null;
    S.view = { start: 0, end: A.duration(S.pcm) };
    S.playhead = Math.max(0, Math.min(S.playhead, A.duration(S.pcm)));
    markDirty();
  }

  // -- editor ------------------------------------------------------------------
  function pushUndo(label) { if (S.pcm) S.undo.push(S.pcm, label, { baseRevision: S.baseRevision }); }

  function markDirty() {
    S.dirty = !!S.pcm;
    if (S.sourceKind === "import" && S.ops.length) S.importUnedited = false;
    draw();
    refreshControls();
    scheduleDraft();
  }

  function edit(label, fn) {
    if (!S.pcm) return;
    haltPreview();
    pushUndo(label);
    S.pcm = fn(S.pcm);
    S.ops.push(label);
    S.importUnedited = false;
    S.view = { start: 0, end: A.duration(S.pcm) };
    S.selection = null;
    S.playhead = Math.min(S.playhead, A.duration(S.pcm));
    markDirty();
  }

  function draw() {
    var canvas = $("ipWave"), ctx2d = canvas.getContext("2d");
    var w = canvas.width = canvas.clientWidth || canvas.width, h = canvas.height;
    ctx2d.fillStyle = "#0b0d10"; ctx2d.fillRect(0, 0, w, h);
    if (!S.pcm || !A.length(S.pcm)) {
      // An existing committed take is never implied away: say it is not loaded.
      var onAir = !!S.ctx.current;
      $("ipSelection").textContent = onAir ? "Editor empty — the " + term("current") + " is not loaded." : "No audio yet.";
      if (onAir) {
        ctx2d.fillStyle = "#9ca3af"; ctx2d.font = "14px sans-serif"; ctx2d.textAlign = "center";
        ctx2d.fillText("The " + term("current") + " is not loaded — use “Edit the " + term("current") + "” to edit it.", w / 2, h / 2);
      }
      return;
    }
    var v = S.view || { start: 0, end: A.duration(S.pcm) }, span = Math.max(1e-6, v.end - v.start);
    if (S.selection) {
      var x0 = (S.selection.start - v.start) / span * w, x1 = (S.selection.end - v.start) / span * w;
      ctx2d.fillStyle = "rgba(59,130,246,0.25)"; ctx2d.fillRect(Math.min(x0, x1), 0, Math.abs(x1 - x0), h);
    }
    var peaks = A.peaksForRange(S.pcm, v.start, v.end, w);
    ctx2d.strokeStyle = "#22c55e"; ctx2d.beginPath();
    peaks.forEach(function (p, x) { ctx2d.moveTo(x + 0.5, h / 2 - p[1] * h / 2); ctx2d.lineTo(x + 0.5, h / 2 - p[0] * h / 2); });
    ctx2d.stroke();
    // Draw the playhead last, matching Track Detail's white line and
    // triangle-tipped silhouette. A zoomed-out cursor is not projected onto
    // an edge: it simply is not part of this view.
    if (S.playhead >= v.start && S.playhead <= v.end) {
      var px = (S.playhead - v.start) / span * w;
      ctx2d.strokeStyle = "#ffffff"; ctx2d.lineWidth = 2;
      ctx2d.beginPath(); ctx2d.moveTo(px, 0); ctx2d.lineTo(px, h); ctx2d.stroke();
      ctx2d.fillStyle = "#ffffff";
      ctx2d.beginPath();
      ctx2d.moveTo(px - 4, 0); ctx2d.lineTo(px + 4, 0); ctx2d.lineTo(px, 5); ctx2d.closePath(); ctx2d.fill();
      ctx2d.beginPath();
      ctx2d.moveTo(px - 4, h); ctx2d.lineTo(px + 4, h); ctx2d.lineTo(px, h - 5); ctx2d.closePath(); ctx2d.fill();
    }
    var sel = S.selection ? " · selection " + fmt(S.selection.start) + "–" + fmt(S.selection.end) : "";
    $("ipSelection").textContent = "Length " + fmt(A.duration(S.pcm)) + " · playhead " + fmt(S.playhead) + sel;
  }

  // The Zoom +/- buttons centre on the playhead; the wheel and pinch anchor on
  // the pointer (audio_core.zoomView). Views never leave the clip; zooming and
  // panning change only S.view -- never the PCM, the playhead or the selection.
  function currentView() {
    var total = S.pcm ? A.duration(S.pcm) : 0, v = S.view || { start: 0, end: total };
    return A.clampView(v.start, v.end - v.start, total);
  }
  function zoom(factor) {
    if (!S.pcm) return;
    S.view = A.zoomView(currentView(), A.duration(S.pcm), factor, S.playhead, 0.5);
    draw();
  }
  function fitView() {
    if (!S.pcm) return;
    S.view = A.clampView(0, A.duration(S.pcm), A.duration(S.pcm));
    draw();
  }

  async function decodeToPcm(arrayBuffer) {
    var rec = ensureRecorder();
    var buffer = await rec._context().decodeAudioData(arrayBuffer.slice(0));
    return A.collapseSilentChannel(A.fromAudioBuffer(buffer));
  }

  async function importFile(file) {
    if (!file) return;
    haltPreview();
    if (file.size > S.ctx.max_bytes) { message("That file is larger than the limit for this workspace.", "error"); return; }
    try {
      var bytes = await file.arrayBuffer();
      var pcm = await decodeToPcm(bytes);
      if (A.duration(pcm) > S.ctx.max_duration_seconds) {
        message("That file is longer than the " + fmt(S.ctx.max_duration_seconds) + " limit.", "error");
        return;
      }
      if (S.pcm) pushUndo("import");
      S.pcm = pcm; S.sourceKind = "import"; S.importFile = file; S.importUnedited = true;
      rebase();
      S.parentMediaId = null; S.ops = []; S.selection = null; S.view = { start: 0, end: A.duration(pcm) };
      S.playhead = Math.max(0, Math.min(S.playhead, A.duration(pcm)));
      markDirty();
      message("Imported " + file.name + ". Edit it here, or save it as is.", "ok");
    } catch (err) {
      message("That file could not be read as audio in this browser.", "error");
    }
  }

  async function loadCurrent() {
    haltPreview();
    try {
      var resp = await fetch(S.api.source + "?" + query(S.ctx.subject), { credentials: "same-origin" });
      if (!resp.ok) throw new Error("http " + resp.status);
      var pcm = await decodeToPcm(await resp.arrayBuffer());
      S.pcm = pcm;
      rebase();
      S.sourceKind = S.ctx.current && S.ctx.current.origin === "production_media" ? "edit-media" : "edit-legacy";
      S.parentMediaId = S.sourceKind === "edit-media" ? S.ctx.current.media_id : null;
      S.importFile = null; S.ops = []; S.undo.clear(); S.selection = null;
      S.view = { start: 0, end: A.duration(pcm) };
      S.playhead = Math.max(0, Math.min(S.playhead, A.duration(pcm)));
      S.dirty = false;
      draw(); refreshControls();
      message("Loaded the " + term("current") + " into the editor. Your edits become a NEW take when you save.", "info");
    } catch (err) {
      message("The " + term("current") + " could not be loaded for editing.", "error");
    }
  }

  // -- preview playback (browser-local; never the station output) --------------
  // A conventional transport (r0114): S.transport is "stopped" | "playing" |
  // "paused". Preview ends in exactly three different ways, never conflated:
  //  * pausePreview() -- the playhead stays where it paused; Play resumes there;
  //  * stopPreview()  -- the operator's Stop, and a natural end (whole clip or
  //                      selection): the playhead returns to exactly 0;
  //  * haltPreview()  -- internal, before recording, punch-in, an edit, an
  //                      import, a load or a discard: the playhead is KEPT
  //                      (punch-in and editing use it) and a pause is forgotten.
  // Every started source carries the generation it was started in; its onended
  // is detached when it is superseded, and ignored if it still arrives -- a
  // stale source can never stop or reset newer playback. Only one source is
  // ever audible: each start first ends the previous one.
  S.transport = "stopped"; S.previewRange = null; S.previewGen = 0;

  function clampPlayhead(seconds) {
    var total = S.pcm ? A.duration(S.pcm) : 0;
    seconds = Number(seconds);
    if (!isFinite(seconds)) seconds = 0;
    return Math.max(0, Math.min(total, seconds));
  }
  function cancelPreviewFrame() {
    if (S.previewFrame !== null) root.cancelAnimationFrame(S.previewFrame);
    S.previewFrame = null;
  }
  function updatePreviewPlayhead() {
    var timing = S.previewTiming;
    if (!timing) return;
    var elapsed = Math.max(0, timing.context.currentTime - timing.startedAt);
    S.playhead = clampPlayhead(Math.min(timing.end, timing.from + elapsed));
  }
  function previewTick() {
    S.previewFrame = null;
    if (!S.playing || !S.previewTiming) return;
    updatePreviewPlayhead();
    draw();
    if (S.playing && S.previewTiming) S.previewFrame = root.requestAnimationFrame(previewTick);
  }
  function startPreviewLoop() {
    cancelPreviewFrame();
    S.previewFrame = root.requestAnimationFrame(previewTick);
  }
  /* Silence the current source and freeze the playhead where it is; the
   * caller decides what the transport and the playhead become. */
  function detachSource() {
    var source = S.playing;
    if (S.previewTiming) updatePreviewPlayhead();
    S.previewGen += 1;
    S.playing = null;
    S.previewTiming = null;
    cancelPreviewFrame();
    if (source) {
      source.onended = null;
      try { source.stop(); } catch (e) { /* ignore */ }
    }
  }
  function settled() { draw(); refreshControls(); }
  function haltPreview() {
    detachSource();
    S.transport = "stopped";
    S.previewRange = null;
    settled();
  }
  function stopPreview() {
    detachSource();
    S.transport = "stopped";
    S.previewRange = null;
    S.playhead = 0;
    settled();
  }
  function pausePreview() {
    if (S.transport !== "playing") return;
    detachSource();
    S.transport = "paused";
    settled();
  }
  function sameSelection(a, b) {
    return (!a && !b) || (!!a && !!b && a.start === b.start && a.end === b.end);
  }
  /* Start one source playing [from, end) of the current PCM. */
  function startSource(from, end) {
    detachSource();
    var rec = ensureRecorder();
    rec.release();                                   // iOS: free the mic so playback uses the speaker
    var ac = rec._context(), gen = S.previewGen;
    var src;
    try {
      src = ac.createBufferSource();
      src.buffer = A.toAudioBuffer(ac, S.pcm);
      src.connect(ac.destination);
      src.onended = function () {
        if (gen !== S.previewGen || S.playing !== src) return;          // superseded: never ours to end
        stopPreview();                                                   // a natural end
      };
      S.playing = src;
      S.previewTiming = { context: ac, startedAt: ac.currentTime, from: from, end: end };
      S.transport = "playing";
      src.start(0, from, end - from);
    } catch (err) {
      haltPreview();
      message("Preview could not start in this browser.", "warn");
      return;
    }
    if (ac.state === "suspended") {
      var resumed = ac.resume();
      if (resumed && resumed.catch) resumed.catch(function () {
        if (gen !== S.previewGen) return;
        haltPreview();
        message("The browser did not allow audio to play. Press Preview again.", "warn");
      });
    }
    startPreviewLoop();
    refreshControls();
  }
  /* Play from the playhead (or the selection), to the clip (or selection) end. */
  function startPreview() {
    if (!S.pcm || !A.length(S.pcm)) return;
    var total = A.duration(S.pcm), sel = S.selection;
    var from = clampPlayhead(sel ? sel.start : S.playhead);
    var end = clampPlayhead(sel ? sel.end : total);
    if (!sel && from >= end) from = 0;                // parked at the end: play from the top
    if (end <= from) { haltPreview(); return; }
    S.previewRange = { from: from, end: end, selection: sel ? { start: sel.start, end: sel.end } : null };
    startSource(from, end);
  }
  /* The Play / Pause / Resume button. */
  function togglePreview() {
    if (S.transport === "playing") { pausePreview(); return; }
    if (S.transport === "paused" && S.previewRange && sameSelection(S.previewRange.selection, S.selection)) {
      var from = clampPlayhead(S.playhead), end = clampPlayhead(S.previewRange.end);
      if (from < end) { startSource(from, end); return; }
    }
    // stopped, or the paused selection was changed: a fresh start
    detachSource();
    S.transport = "stopped";
    S.previewRange = null;
    startPreview();
  }
  /* A click or tap on the waveform moves the playhead; during playback it seeks. */
  function seekTo(seconds) {
    var t = clampPlayhead(seconds), wasPlaying = S.transport === "playing";
    if (wasPlaying) detachSource();
    S.playhead = t;
    S.selection = null;
    if (wasPlaying) { S.transport = "stopped"; S.previewRange = null; startPreview(); return; }
    settled();
  }

  // -- drafts (IndexedDB, browser-local, bounded) -----------------------------------
  function openDrafts() {
    return new Promise(function (resolve, reject) {
      if (!root.indexedDB) { reject(new Error("no_indexeddb")); return; }
      var req = root.indexedDB.open(DRAFT_DB, 1);
      req.onupgradeneeded = function () { req.result.createObjectStore(DRAFT_STORE); };
      req.onsuccess = function () { resolve(req.result); };
      req.onerror = function () { reject(req.error); };
    });
  }
  function draftTx(mode, fn) {
    return openDrafts().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx = db.transaction(DRAFT_STORE, mode), store = tx.objectStore(DRAFT_STORE), out = fn(store);
        tx.oncomplete = function () { resolve(out && out.result !== undefined ? out.result : out); db.close(); };
        tx.onerror = function () { reject(tx.error); db.close(); };
      });
    });
  }
  var draftTimer = null;
  function scheduleDraft() {
    if (draftTimer) root.clearTimeout(draftTimer);
    draftTimer = root.setTimeout(saveDraft, 1200);
  }
  function saveDraft() {
    if (!S.pcm || !S.dirty || A.byteSize(S.pcm) > DRAFT_MAX_BYTES) return Promise.resolve();
    var draft = {
      savedAt: Date.now(), revision: S.baseRevision, sourceKind: S.sourceKind, parentMediaId: S.parentMediaId,
      ops: S.ops.slice(), sampleRate: S.pcm.sampleRate, channels: S.pcm.channels,
    };
    return draftTx("readwrite", function (store) { return store.put(draft, subjectKey()); }).catch(function () {});
  }
  function deleteDraft() {
    return draftTx("readwrite", function (store) { return store.delete(subjectKey()); }).catch(function () {});
  }
  function pruneDrafts() {
    return draftTx("readwrite", function (store) {
      var req = store.openCursor();
      req.onsuccess = function () {
        var cursor = req.result;
        if (!cursor) return;
        // Expired drafts of anyone, and drafts stored before keys were
        // per-user (no "u:" prefix) -- those could belong to anybody.
        if (!cursor.value || Date.now() - cursor.value.savedAt > DRAFT_MAX_AGE_MS
            || String(cursor.key).indexOf("u:") !== 0) cursor.delete();
        cursor.continue();
      };
    }).catch(function () {});
  }
  function checkDraft() {
    return draftTx("readonly", function (store) { return store.get(subjectKey()); }).then(function (draft) {
      if (!draft || !draft.channels) return;
      S.draft = draft;
      var changed = draft.revision !== S.ctx.revision ? " " + term("changed") : "";
      $("ipDraftText").textContent = "You have an unsaved draft from " + new Date(draft.savedAt).toLocaleString() + "." + changed;
      $("ipDraftBanner").hidden = false;
    }).catch(function () {});
  }
  function restoreDraft() {
    var d = S.draft;
    if (!d) return;
    haltPreview();
    S.pcm = A.make(d.sampleRate, d.channels);
    // The draft was made against d.revision: saving it must be checked against
    // THAT, so a newer save by someone else is a conflict, never overwritten.
    S.baseRevision = d.revision;
    S.sourceKind = d.sourceKind === "import" ? "recording" : d.sourceKind;   // the original import bytes are not kept
    S.parentMediaId = d.parentMediaId; S.ops = d.ops || []; S.importFile = null; S.importUnedited = false;
    S.view = { start: 0, end: A.duration(S.pcm) }; S.selection = null;
    S.playhead = Math.max(0, Math.min(S.playhead, A.duration(S.pcm)));
    $("ipDraftBanner").hidden = true;
    markDirty();
    message(term("restored"), "info");
  }

  // -- save / commit -------------------------------------------------------------
  function api(url, body) {
    return fetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": cookie("csrftoken") },
      body: JSON.stringify(body),
    }).then(function (resp) { return resp.json().then(function (data) { data._status = resp.status; return data; }); });
  }

  function takeRequest() {
    if (S.sourceKind === "import" && S.importUnedited && S.importFile) {
      return { mode: "import", body: S.importFile, type: S.importFile.type || "application/octet-stream", name: S.importFile.name };
    }
    var wav = new Blob([A.encodeWav(S.pcm)], { type: "audio/wav" });
    if (S.sourceKind === "edit-media" && S.parentMediaId) {
      return { mode: "edit", body: wav, type: "audio/wav", name: "edit.wav",
               extra: { derived_from: S.parentMediaId, operations: JSON.stringify(S.ops.slice(0, 64)) } };
    }
    if (S.sourceKind === "import" || S.sourceKind === "edit-legacy") {
      return { mode: "import", body: wav, type: "audio/wav", name: S.sourceKind === "edit-legacy" ? "legacy-edit.wav" : "import-edit.wav" };
    }
    return { mode: "record", body: wav, type: "audio/wav", name: "recording.wav" };
  }

  async function save() {
    if (!S.pcm) return;
    if (A.duration(S.pcm) > S.ctx.max_duration_seconds) { message("The audio is longer than the limit.", "error"); return; }
    var req = takeRequest();
    if (req.body.size > S.ctx.max_bytes) { message("The audio is larger than the upload limit.", "error"); return; }
    await saveDraft();                                           // survives an interrupted upload
    var params = Object.assign({ mode: req.mode }, S.ctx.subject, req.extra || {});
    var controller = new AbortController();
    S.upload = controller;
    $("ipSaveState").textContent = "Uploading and validating…";
    refreshControls();
    var resp, data;
    try {
      resp = await fetch(S.api.take + "?" + query(params), {
        method: "POST", credentials: "same-origin", body: req.body, signal: controller.signal,
        headers: { "Content-Type": req.type, "X-CSRFToken": cookie("csrftoken"), "X-Recorder-Filename": req.name },
      });
      data = await resp.json();
    } catch (err) {
      S.upload = null;
      message(err && err.name === "AbortError" ? "Upload cancelled. Your draft is kept in this browser."
        : "The upload was interrupted. Your draft is kept in this browser — try again.", "warn");
      $("ipSaveState").textContent = "Not saved.";
      refreshControls();
      return;
    }
    S.upload = null;
    if (resp.status === 202 && data.media) {
      S.pendingTake = data.media;
      message("The station could not finish checking this take (" + data.media.validation_code + "). It is saved but not on air — retry validation.", "warn");
      $("ipSaveState").textContent = "Saved take, waiting for validation.";
      refreshControls();
      return;
    }
    if (!data.ok) {
      message(rejection(data), "error");
      $("ipSaveState").textContent = "Not saved.";
      refreshControls();
      return;
    }
    await commit(data.media);
  }

  function rejection(data) {
    var reasons = {
      too_large: "The audio is larger than the limit.", too_long: "The audio is longer than the limit.",
      too_short: "The audio is too short.", unsupported_type: "That file type isn't accepted.",
      empty: "The audio is empty.", decode_error: "The audio could not be decoded.",
      unreadable_container: "That file isn't a readable audio file.", unsupported_container: "That audio format isn't supported.",
      unsupported_codec: "That audio codec isn't supported.", no_audio_stream: "That file has no audio.",
      empty_audio: "That file contains no audio.", truncated_or_inconsistent: "That file appears to be damaged or truncated.",
      engine_decode_failed: "The playout engine can't play that file.",
    };
    return reasons[data.error] || ("Not saved: " + (data.message || data.error || "error"));
  }

  async function commit(media) {
    var data = await api(S.api.commit, { subject: S.ctx.subject, media_id: media.media_id, revision: S.baseRevision });
    if (data._status === 409) {
      S.pendingTake = media;
      $("ipConflict").hidden = false;
      $("ipSaveState").textContent = "Saved take (not on air) — someone else saved first.";
      refreshControls();
      return;
    }
    if (!data.ok) {
      message(rejection(data), "error");
      $("ipSaveState").textContent = "Saved take, not on air.";
      S.pendingTake = media;
      refreshControls();
      return;
    }
    S.ctx = data.context;
    rebase();
    S.pendingTake = null;
    S.dirty = false;
    S.undo.clear();
    await deleteDraft();
    renderContext();
    message(term("saved") || ("Saved. This take is now " + S.ctx.air_label.toLowerCase() + "."), "ok");
    $("ipSaveState").textContent = "No unsaved changes.";
    refreshControls();
  }

  async function retryValidation() {
    if (!S.pendingTake) return;
    var data = await api(S.api.revalidate, { subject: S.ctx.subject, media_id: S.pendingTake.media_id });
    if (!data.ok) { message(rejection(data), "error"); return; }
    if (data.media.validation_state === "valid") { await commit(data.media); return; }
    if (data.media.validation_state === "invalid") { message(rejection({ error: data.media.validation_code }), "error"); S.pendingTake = null; refreshControls(); return; }
    message("Still could not check this take (" + data.media.validation_code + "). Try again shortly.", "warn");
  }

  async function reloadContext() {
    var resp = await fetch(S.api.context + "?" + query(S.ctx.subject), { credentials: "same-origin" });
    var data = await resp.json();
    if (data.ok) { S.ctx = data.context; renderContext(); }
    if (!S.pcm) rebase();
    $("ipConflict").hidden = true;
  }

  async function removeFromAir() {
    if (!root.confirm("Remove this from air? The audio itself is kept by the station and can be recovered by an administrator.")) return;
    var before = S.ctx.revision;
    var data = await api(S.api.remove, { subject: S.ctx.subject, revision: before });
    if (data._status === 409) { $("ipConflict").hidden = false; return; }
    if (!data.ok) { message(rejection(data), "error"); return; }
    S.ctx = data.context;
    if (!S.pcm || S.baseRevision === before) rebase();          // only this user's own removal intervened
    renderContext();
    message("Removed from air.", "ok");
  }

  function exportWav() {
    if (!S.pcm) return;
    var url = URL.createObjectURL(new Blob([A.encodeWav(S.pcm)], { type: "audio/wav" }));
    var a = doc.createElement("a");
    a.href = url; a.download = "iportal-edit.wav"; doc.body.appendChild(a); a.click(); a.remove();
    root.setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
  }

  // -- controls ----------------------------------------------------------------------
  function refreshControls() {
    var rec = S.recorder, st = rec ? rec.state : "idle", busy = !!S.upload;
    var capturing = st === "recording" || st === "paused";
    var canWrite = !S.ctx.blocked_reason;
    $("ipArm").disabled = !S.canRecord || capturing || busy;
    $("ipDevice").disabled = capturing;                        // deterministic: no device change mid-take
    $("ipNoise").disabled = capturing;
    $("ipSetLevel").disabled = st !== "armed";
    $("ipRecord").disabled = !(S.canRecord && st === "armed") || busy;
    $("ipPause").disabled = !capturing;
    $("ipPause").textContent = st === "paused" ? "Resume" : "Pause";
    $("ipStop").disabled = !capturing;
    var has = !!S.pcm && A.length(S.pcm) > 0;
    ["ipPlay", "ipZoomIn", "ipZoomOut", "ipZoomFit", "ipNormalize", "ipApplyGain"].forEach(function (id) { $(id).disabled = !has || capturing; });
    // The preview transport: one Play/Pause/Resume button and a Stop that is
    // live only while something is playing or paused.
    var label = S.transport === "playing" ? "\u23F8 Pause" : (S.transport === "paused" ? "\u25B6 Resume" : "\u25B6 Preview");
    $("ipPlay").textContent = label;
    $("ipPlay").setAttribute("aria-label", S.transport === "playing" ? "Pause preview"
      : (S.transport === "paused" ? "Resume preview" : "Play preview"));
    $("ipPlay").dataset.transport = S.transport;
    $("ipPlayStop").disabled = S.transport === "stopped";
    $("ipKeep").disabled = $("ipDelete").disabled = !has || !S.selection || capturing;
    $("ipUndo").disabled = !S.undo.canUndo() || capturing;
    $("ipExport").disabled = !has || !allowed("export");
    $("ipSave").disabled = !has || !S.dirty || capturing || busy || !canWrite || !allowed("save");
    $("ipDiscard").disabled = !has || capturing || busy;
    $("ipRetry").hidden = !(S.pendingTake && S.pendingTake.validation_state === "unvalidated");
    $("ipCancelUpload").hidden = !busy;
    $("ipImportBtn").disabled = !allowed("import") || capturing || busy;
    $("ipPunchKeepTail").disabled = $("ipPunchReplace").disabled = !$("ipPunch").checked || capturing;
    // Reopened with a take already committed and nothing in the editor: show
    // that state and offer the take -- never load or create anything by itself.
    var emptyWithTake = !has && !!S.ctx.current && allowed("edit");
    $("ipEditorEmpty").hidden = !emptyWithTake;
    if (emptyWithTake) {
      $("ipEditorEmptyLength").textContent = S.ctx.current.duration_seconds ? fmt(S.ctx.current.duration_seconds) : S.ctx.current.label;
      $("ipLoadCurrentEmpty").disabled = capturing || busy;
    }
    if (S.dirty) $("ipSaveState").textContent = $("ipSaveState").textContent.indexOf("Upload") === 0
      ? $("ipSaveState").textContent : "Unsaved changes — not on air.";
  }

  // -- waveform navigation (r0114) ---------------------------------------------
  // Pointer Events serve mouse, pen and touch alike:
  //  * click / tap: move the playhead (seek, while previewing);
  //  * drag with the mouse or ONE finger: select (as before);
  //  * TWO fingers: pinch to zoom around their midpoint, move to pan -- a
  //    second finger cancels a selection the first one started, so navigating
  //    never leaves a stray selection; lifting fingers ends the gesture;
  //  * wheel: zoom around the cursor; horizontal wheel / trackpad swipe, or
  //    Shift + wheel: pan; Ctrl + wheel (trackpad pinch): zoom.
  // The canvas allows native vertical page scrolling (touch-action: pan-y).
  // Navigation changes S.view only; selection, playhead and PCM stay in
  // absolute clip seconds and are untouched.
  var TAP_SLOP_PX = 4, WHEEL_ZOOM_PER_PX = 0.002, LINE_PX = 16;
  function timeAtX(canvas, clientX) {
    var rect = canvas.getBoundingClientRect(), v = currentView();
    var frac = rect.width > 0 ? Math.max(0, Math.min(1, (clientX - rect.left) / rect.width)) : 0;
    return { time: v.start + frac * (v.end - v.start), frac: frac };
  }
  function wireWaveform(canvas) {
    var pointers = new Map();          // pointerId -> {x, y}
    var drag = null;                   // one pointer: {id, fromTime, startX, selectionBefore, moved}
    var gesture = null;                // two fingers: {view, anchorTime, distance}
    function midpoint() {
      var pts = Array.from(pointers.values());
      return { x: (pts[0].x + pts[1].x) / 2, distance: Math.max(1, Math.hypot(pts[0].x - pts[1].x, pts[0].y - pts[1].y)) };
    }
    function startGesture() {
      if (drag) { S.selection = drag.selectionBefore; drag = null; }       // navigation, not selection
      var m = midpoint(), at = timeAtX(canvas, m.x);
      gesture = { view: currentView(), anchorTime: at.time, distance: m.distance };
      draw(); refreshControls();
    }
    function moveGesture() {
      // Zoom by the change in finger distance, keeping the time first under
      // the midpoint under the midpoint as it moves -- which also pans.
      var m = midpoint(), at = timeAtX(canvas, m.x);
      S.view = A.zoomView(gesture.view, A.duration(S.pcm), gesture.distance / m.distance, gesture.anchorTime,
                          at.frac);
      draw();
    }
    function finish(event, cancelled) {
      pointers.delete(event.pointerId);
      if (gesture) {
        if (pointers.size >= 2) startGesture();                    // a finger left: continue from here
        else gesture = null;                                       // the remaining finger starts nothing
        return;
      }
      if (!drag || drag.id !== event.pointerId) return;
      var d = drag;
      drag = null;
      if (cancelled) { S.selection = d.selectionBefore; draw(); refreshControls(); return; }
      if (!d.moved) { seekTo(d.fromTime); return; }
      draw(); refreshControls();
    }
    canvas.addEventListener("pointerdown", function (event) {
      if (!S.pcm) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      try { canvas.setPointerCapture(event.pointerId); } catch (e) { /* ignore */ }
      if (pointers.size >= 2 && event.pointerType === "touch") { startGesture(); return; }   // (re)baseline
      if (pointers.size > 1 || gesture) return;
      drag = { id: event.pointerId, fromTime: timeAtX(canvas, event.clientX).time, startX: event.clientX,
               selectionBefore: S.selection, moved: false };
    });
    canvas.addEventListener("pointermove", function (event) {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (gesture && pointers.size >= 2) { moveGesture(); return; }
      if (!drag || drag.id !== event.pointerId || !S.pcm) return;
      if (!drag.moved && Math.abs(event.clientX - drag.startX) < TAP_SLOP_PX) return;
      drag.moved = true;
      var t = timeAtX(canvas, event.clientX).time;
      S.selection = Math.abs(t - drag.fromTime) > 0.01 ? { start: Math.min(t, drag.fromTime), end: Math.max(t, drag.fromTime) } : null;
      draw(); refreshControls();
    });
    canvas.addEventListener("pointerup", function (event) { finish(event, false); });
    canvas.addEventListener("pointercancel", function (event) { finish(event, true); });
    canvas.addEventListener("lostpointercapture", function (event) {
      if (pointers.has(event.pointerId)) finish(event, true);
    });
    canvas.addEventListener("wheel", function (event) {
      if (!S.pcm || !A.length(S.pcm)) return;                       // nothing to navigate: let the page scroll
      var unit = event.deltaMode === 1 ? LINE_PX : (event.deltaMode === 2 ? canvas.clientWidth || 1 : 1);
      var dx = event.deltaX * unit, dy = event.deltaY * unit;
      if (event.shiftKey && !dx) { dx = dy; dy = 0; }               // Shift + wheel: pan with a plain mouse
      var total = A.duration(S.pcm), v = currentView(), span = v.end - v.start, next;
      if (!event.ctrlKey && Math.abs(dx) > Math.abs(dy)) {
        next = A.panView(v, total, dx / Math.max(1, canvas.clientWidth) * span);
      } else {
        var at = timeAtX(canvas, event.clientX);
        var factor = Math.exp(Math.max(-0.7, Math.min(0.7, dy * WHEEL_ZOOM_PER_PX)));
        next = A.zoomView(v, total, factor, at.time, at.frac);
      }
      if (next.start === v.start && next.end === v.end) return;  // at a limit: leave the page its scroll
      event.preventDefault();
      S.view = next;
      draw();
    }, { passive: false });
  }

  function wire() {
    $("ipArm").addEventListener("click", arm);
    $("ipDevice").addEventListener("change", function () { if (S.recorder && S.recorder.state === "armed") arm(); });
    $("ipNoise").addEventListener("change", function () {
      try { root.localStorage.setItem("iportal_ns", this.checked ? "1" : "0"); } catch (e) { /* ignore */ }
      if (S.recorder && S.recorder.state === "armed") arm();
    });
    $("ipGain").addEventListener("input", function () {
      var db = ensureRecorder().setGainDb(this.value);
      $("ipGainLabel").textContent = db.toFixed(1) + " dB";
      try { root.localStorage.setItem("iportal_gain_db", String(db)); } catch (e) { /* ignore */ }
    });
    $("ipSetLevel").addEventListener("click", async function () {
      message("Speak at your normal level for " + A.SET_LEVEL_SECONDS + " seconds…", "info");
      $("ipSetLevel").disabled = true;
      var result = await S.recorder.setLevel();
      if (result.ok) {
        $("ipGain").value = String(result.gainDb);
        $("ipGainLabel").textContent = result.gainDb.toFixed(1) + " dB";
        try { root.localStorage.setItem("iportal_gain_db", String(result.gainDb)); } catch (e) { /* ignore */ }
        message("Level set to " + (result.gainDb > 0 ? "+" : "") + result.gainDb.toFixed(1) + " dB.", "ok");
      } else {
        message("No audio detected — level unchanged.", "warn");
      }
      refreshControls();
    });
    $("ipRecord").addEventListener("click", record);
    $("ipPause").addEventListener("click", function () {
      if (S.recorder.state === "paused") S.recorder.resume(); else S.recorder.pause();
    });
    $("ipStop").addEventListener("click", stop);
    $("ipImportBtn").addEventListener("click", function () { $("ipImport").click(); });
    $("ipImport").addEventListener("change", function () { importFile(this.files[0]); this.value = ""; });
    $("ipLoadCurrent").addEventListener("click", loadCurrent);
    $("ipLoadCurrentEmpty").addEventListener("click", loadCurrent);
    $("ipPunch").addEventListener("change", refreshControls);
    $("ipPlay").addEventListener("click", togglePreview);
    $("ipPlayStop").addEventListener("click", stopPreview);
    $("ipZoomIn").addEventListener("click", function () { zoom(0.5); });
    $("ipZoomOut").addEventListener("click", function () { zoom(2); });
    $("ipZoomFit").addEventListener("click", fitView);
    $("ipKeep").addEventListener("click", function () {
      var sel = S.selection; edit("trim-keep", function (p) { return A.trim(p, sel.start, sel.end, "keep"); });
    });
    $("ipDelete").addEventListener("click", function () {
      var sel = S.selection; edit("trim-delete", function (p) { return A.trim(p, sel.start, sel.end, "delete"); });
    });
    $("ipNormalize").addEventListener("click", function () { edit("normalize", function (p) { return A.normalize(p).pcm; }); });
    $("ipApplyGain").addEventListener("click", function () {
      var db = A.clampGainDb($("ipEditGain").value);
      $("ipEditGain").value = String(db);
      edit("gain:" + db.toFixed(1), function (p) { return A.applyGain(p, db); });
    });
    $("ipUndo").addEventListener("click", function () {
      var item = S.undo.pop();
      if (!item) return;
      haltPreview();
      S.pcm = item.pcm; S.ops.pop(); S.selection = null; S.view = { start: 0, end: A.duration(S.pcm) };
      S.playhead = Math.max(0, Math.min(S.playhead, A.duration(S.pcm)));
      if (item.meta) S.baseRevision = item.meta.baseRevision;     // the restored audio's own base
      markDirty();
    });
    $("ipExport").addEventListener("click", exportWav);
    $("ipSave").addEventListener("click", save);
    $("ipRetry").addEventListener("click", retryValidation);
    $("ipCancelUpload").addEventListener("click", function () { if (S.upload) S.upload.abort(); });
    $("ipDiscard").addEventListener("click", function () {
      if (S.dirty && !root.confirm("Discard your unsaved edit?")) return;
      haltPreview();
      S.pcm = null; S.dirty = false; S.ops = []; S.undo.clear(); S.sourceKind = "none";
      S.selection = null; S.view = null; S.playhead = 0;
      rebase(); deleteDraft(); draw(); refreshControls();
    });
    $("ipRemove").addEventListener("click", removeFromAir);
    $("ipConflictReload").addEventListener("click", function () {
      haltPreview();
      S.pcm = null; S.dirty = false; S.undo.clear(); deleteDraft(); draw(); reloadContext();
    });
    $("ipDraftRestore").addEventListener("click", restoreDraft);
    $("ipDraftDiscard").addEventListener("click", function () { deleteDraft(); $("ipDraftBanner").hidden = true; });

    wireWaveform($("ipWave"));
    root.addEventListener("resize", draw);
    root.addEventListener("beforeunload", function (e) {
      if (S.dirty || (S.recorder && (S.recorder.state === "recording" || S.recorder.state === "paused"))) {
        e.preventDefault(); e.returnValue = "";
      }
    });
  }

  capabilityCheck();
  initMeter();
  wire();
  renderContext();
  draw();
  pruneDrafts().then(checkDraft);
})(window);
