/* iPortal workstation controller (2.22B).
 *
 * Domain-neutral: everything domain-specific arrives in the RecordingContext
 * JSON the server rendered (title, display rows, allowed operations, limits,
 * revision, current on-air audio). The page never names a path or a storage
 * key; it talks to the adapter's own recorder endpoints with Django's session
 * cookie and CSRF token.
 *
 * Three states are kept visibly distinct: the PREVIEW (browser-local edit,
 * never on air), a saved take (immutable ProductionMedia, not yet on air),
 * and ON AIR (the domain's committed binding).
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
    pcm: null, dirty: false, ops: [],
    sourceKind: "none",            // none | recording | import | edit-media | edit-legacy
    parentMediaId: null, importFile: null, importUnedited: false,
    selection: null, view: null, playhead: 0,
    undo: new A.UndoStack(), recorder: null, playing: null,
    pendingTake: null, upload: null,
  };
  root.IPortalWorkstation = S;      // exposed for tests / debugging only

  function allowed(op) { return S.ctx.allowed_operations.indexOf(op) >= 0; }
  function subjectKey() { return S.ctx.adapter + "|" + JSON.stringify(S.ctx.subject); }

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
      cur.textContent = "Nothing on air yet.";
    }
    $("ipLoadCurrent").hidden = !(c.current && allowed("edit"));
    $("ipRemove").hidden = !allowed("remove");
    refreshControls();
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
      onLevel: function (p) { $("ipMeter").style.width = Math.min(100, Math.round(p * 100)) + "%"; },
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

  function record() {
    var rec = ensureRecorder();
    S.punch = $("ipPunch").checked && S.pcm ? { at: S.playhead, mode: $("ipPunchReplace").checked ? "replace" : "insert" } : null;
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
    if (S.punch && S.pcm) {
      pushUndo("punch-in");
      S.pcm = A.punchIn(S.pcm, take, S.punch.at, S.punch.mode);
      S.ops.push(S.punch.mode === "replace" ? "punch-replace" : "punch-insert");
      if (S.sourceKind === "import") S.importUnedited = false;
    } else {
      if (S.pcm) pushUndo("re-record");
      S.pcm = take;
      S.sourceKind = "recording";
      S.parentMediaId = null;
      S.importFile = null;
      S.ops = [];
    }
    S.punch = null;
    S.selection = null;
    S.view = { start: 0, end: A.duration(S.pcm) };
    markDirty();
  }

  // -- editor ------------------------------------------------------------------
  function pushUndo(label) { if (S.pcm) S.undo.push(S.pcm, label); }

  function markDirty() {
    S.dirty = !!S.pcm;
    if (S.sourceKind === "import" && S.ops.length) S.importUnedited = false;
    draw();
    refreshControls();
    scheduleDraft();
  }

  function edit(label, fn) {
    if (!S.pcm) return;
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
    if (!S.pcm || !A.length(S.pcm)) { $("ipSelection").textContent = "No audio yet."; return; }
    var v = S.view || { start: 0, end: A.duration(S.pcm) }, span = Math.max(1e-6, v.end - v.start);
    if (S.selection) {
      var x0 = (S.selection.start - v.start) / span * w, x1 = (S.selection.end - v.start) / span * w;
      ctx2d.fillStyle = "rgba(59,130,246,0.25)"; ctx2d.fillRect(Math.min(x0, x1), 0, Math.abs(x1 - x0), h);
    }
    var peaks = A.peaksForRange(S.pcm, v.start, v.end, w);
    ctx2d.strokeStyle = "#22c55e"; ctx2d.beginPath();
    peaks.forEach(function (p, x) { ctx2d.moveTo(x + 0.5, h / 2 - p[1] * h / 2); ctx2d.lineTo(x + 0.5, h / 2 - p[0] * h / 2); });
    ctx2d.stroke();
    var px = (S.playhead - v.start) / span * w;
    ctx2d.strokeStyle = "#f59e0b"; ctx2d.beginPath(); ctx2d.moveTo(px, 0); ctx2d.lineTo(px, h); ctx2d.stroke();
    var sel = S.selection ? " · selection " + fmt(S.selection.start) + "–" + fmt(S.selection.end) : "";
    $("ipSelection").textContent = "Length " + fmt(A.duration(S.pcm)) + " · playhead " + fmt(S.playhead) + sel;
  }

  function canvasTime(event) {
    var canvas = $("ipWave"), rect = canvas.getBoundingClientRect();
    var v = S.view || { start: 0, end: A.duration(S.pcm) };
    var x = Math.max(0, Math.min(rect.width, event.clientX - rect.left));
    return v.start + (x / rect.width) * (v.end - v.start);
  }

  function zoom(factor) {
    if (!S.pcm) return;
    var total = A.duration(S.pcm), v = S.view || { start: 0, end: total };
    var span = Math.max(0.05, Math.min(total, (v.end - v.start) * factor));
    var start = Math.max(0, Math.min(total - span, S.playhead - span / 2));
    S.view = { start: start, end: start + span };
    draw();
  }

  async function decodeToPcm(arrayBuffer) {
    var rec = ensureRecorder();
    var buffer = await rec._context().decodeAudioData(arrayBuffer.slice(0));
    return A.collapseSilentChannel(A.fromAudioBuffer(buffer));
  }

  async function importFile(file) {
    if (!file) return;
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
      S.parentMediaId = null; S.ops = []; S.selection = null; S.view = { start: 0, end: A.duration(pcm) };
      markDirty();
      message("Imported " + file.name + ". Edit it here, or save it as is.", "ok");
    } catch (err) {
      message("That file could not be read as audio in this browser.", "error");
    }
  }

  async function loadCurrent() {
    try {
      var resp = await fetch(S.api.source + "?" + query(S.ctx.subject), { credentials: "same-origin" });
      if (!resp.ok) throw new Error("http " + resp.status);
      var pcm = await decodeToPcm(await resp.arrayBuffer());
      S.pcm = pcm;
      S.sourceKind = S.ctx.current && S.ctx.current.origin === "production_media" ? "edit-media" : "edit-legacy";
      S.parentMediaId = S.sourceKind === "edit-media" ? S.ctx.current.media_id : null;
      S.importFile = null; S.ops = []; S.undo.clear(); S.selection = null;
      S.view = { start: 0, end: A.duration(pcm) };
      S.dirty = false;
      draw(); refreshControls();
      message("Loaded the on-air take into the editor. Your edits become a NEW take when you save.", "info");
    } catch (err) {
      message("The on-air take could not be loaded for editing.", "error");
    }
  }

  // -- preview playback (browser-local; never the station output) --------------
  function stopPlayback() {
    if (S.playing) { try { S.playing.stop(); } catch (e) { /* ignore */ } S.playing = null; }
    refreshControls();
  }
  function play() {
    if (!S.pcm) return;
    stopPlayback();
    var rec = ensureRecorder();
    rec.release();                                   // iOS: free the mic so playback uses the speaker
    var ac = rec._context();
    if (ac.state === "suspended") ac.resume();
    var src = ac.createBufferSource();
    src.buffer = A.toAudioBuffer(ac, S.pcm);
    src.connect(ac.destination);
    var from = S.selection ? S.selection.start : S.playhead;
    var dur = S.selection ? S.selection.end - S.selection.start : undefined;
    src.onended = function () { if (S.playing === src) { S.playing = null; refreshControls(); } };
    src.start(0, from, dur);
    S.playing = src;
    refreshControls();
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
      savedAt: Date.now(), revision: S.ctx.revision, sourceKind: S.sourceKind, parentMediaId: S.parentMediaId,
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
        if (!cursor.value || Date.now() - cursor.value.savedAt > DRAFT_MAX_AGE_MS) cursor.delete();
        cursor.continue();
      };
    }).catch(function () {});
  }
  function checkDraft() {
    return draftTx("readonly", function (store) { return store.get(subjectKey()); }).then(function (draft) {
      if (!draft || !draft.channels) return;
      S.draft = draft;
      var changed = draft.revision !== S.ctx.revision ? " The on-air audio has changed since then." : "";
      $("ipDraftText").textContent = "You have an unsaved draft from " + new Date(draft.savedAt).toLocaleString() + "." + changed;
      $("ipDraftBanner").hidden = false;
    }).catch(function () {});
  }
  function restoreDraft() {
    var d = S.draft;
    if (!d) return;
    S.pcm = A.make(d.sampleRate, d.channels);
    S.sourceKind = d.sourceKind === "import" ? "recording" : d.sourceKind;   // the original import bytes are not kept
    S.parentMediaId = d.parentMediaId; S.ops = d.ops || []; S.importFile = null; S.importUnedited = false;
    S.view = { start: 0, end: A.duration(S.pcm) }; S.selection = null;
    $("ipDraftBanner").hidden = true;
    markDirty();
    message("Draft restored. It is not on air until you save.", "info");
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
    var data = await api(S.api.commit, { subject: S.ctx.subject, media_id: media.media_id, revision: S.ctx.revision });
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
    S.pendingTake = null;
    S.dirty = false;
    S.undo.clear();
    await deleteDraft();
    renderContext();
    message("Saved. This take is now " + S.ctx.air_label.toLowerCase() + ".", "ok");
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
    $("ipConflict").hidden = true;
  }

  async function removeFromAir() {
    if (!root.confirm("Remove this from air? The audio itself is kept by the station and can be recovered by an administrator.")) return;
    var data = await api(S.api.remove, { subject: S.ctx.subject, revision: S.ctx.revision });
    if (data._status === 409) { $("ipConflict").hidden = false; return; }
    if (!data.ok) { message(rejection(data), "error"); return; }
    S.ctx = data.context;
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
    $("ipPlayStop").disabled = !S.playing;
    $("ipKeep").disabled = $("ipDelete").disabled = !has || !S.selection || capturing;
    $("ipUndo").disabled = !S.undo.canUndo() || capturing;
    $("ipExport").disabled = !has || !allowed("export");
    $("ipSave").disabled = !has || !S.dirty || capturing || busy || !canWrite || !allowed("save");
    $("ipDiscard").disabled = !has || capturing || busy;
    $("ipRetry").hidden = !(S.pendingTake && S.pendingTake.validation_state === "unvalidated");
    $("ipCancelUpload").hidden = !busy;
    $("ipImportBtn").disabled = !allowed("import") || capturing || busy;
    if (S.dirty) $("ipSaveState").textContent = $("ipSaveState").textContent.indexOf("Upload") === 0
      ? $("ipSaveState").textContent : "Unsaved changes — not on air.";
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
    $("ipPlay").addEventListener("click", play);
    $("ipPlayStop").addEventListener("click", stopPlayback);
    $("ipZoomIn").addEventListener("click", function () { zoom(0.5); });
    $("ipZoomOut").addEventListener("click", function () { zoom(2); });
    $("ipZoomFit").addEventListener("click", function () { if (S.pcm) { S.view = { start: 0, end: A.duration(S.pcm) }; draw(); } });
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
      S.pcm = item.pcm; S.ops.pop(); S.selection = null; S.view = { start: 0, end: A.duration(S.pcm) };
      markDirty();
    });
    $("ipExport").addEventListener("click", exportWav);
    $("ipSave").addEventListener("click", save);
    $("ipRetry").addEventListener("click", retryValidation);
    $("ipCancelUpload").addEventListener("click", function () { if (S.upload) S.upload.abort(); });
    $("ipDiscard").addEventListener("click", function () {
      if (S.dirty && !root.confirm("Discard your unsaved edit?")) return;
      S.pcm = null; S.dirty = false; S.ops = []; S.undo.clear(); S.sourceKind = "none";
      deleteDraft(); draw(); refreshControls();
    });
    $("ipRemove").addEventListener("click", removeFromAir);
    $("ipConflictReload").addEventListener("click", function () {
      S.pcm = null; S.dirty = false; S.undo.clear(); deleteDraft(); draw(); reloadContext();
    });
    $("ipDraftRestore").addEventListener("click", restoreDraft);
    $("ipDraftDiscard").addEventListener("click", function () { deleteDraft(); $("ipDraftBanner").hidden = true; });

    var canvas = $("ipWave"), dragFrom = null;
    canvas.addEventListener("mousedown", function (e) { if (!S.pcm) return; dragFrom = canvasTime(e); });
    canvas.addEventListener("mousemove", function (e) {
      if (dragFrom === null) return;
      var t = canvasTime(e);
      S.selection = Math.abs(t - dragFrom) > 0.01 ? { start: Math.min(t, dragFrom), end: Math.max(t, dragFrom) } : null;
      draw(); refreshControls();
    });
    root.addEventListener("mouseup", function (e) {
      if (dragFrom === null) return;
      var t = canvasTime(e);
      if (Math.abs(t - dragFrom) <= 0.01) { S.playhead = t; S.selection = null; }
      dragFrom = null; draw(); refreshControls();
    });
    root.addEventListener("beforeunload", function (e) {
      if (S.dirty || (S.recorder && (S.recorder.state === "recording" || S.recorder.state === "paused"))) {
        e.preventDefault(); e.returnValue = "";
      }
    });
  }

  capabilityCheck();
  wire();
  renderContext();
  draw();
  pruneDrafts().then(checkDraft);
})(window);
