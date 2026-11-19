"""Roadmap 2.25 B7 supplemental -- Remote DJ media-path lab (not a test module).

The REAL engine Remote DJ session path -- ``_remote_dj_session_start`` ->
``_remote_dj_build_session`` (webrtcbin, transceivers, monitor return mixer,
inbound decode chain, persistent DJ slot, gate) -- with the ``remote_dj_tee``
and studio-mic topology of ``_build_main_pipeline``, connected over a REAL
WebRTC session (ICE, DTLS-SRTP, Opus) to an in-process synthetic browser peer
(its own webrtcbin) that sends a 3200 Hz microphone tone. Only the websocket
boundary is a stand-in: ``_remote_dj_server.send_json_threadsafe`` relays the
engine's offer and candidates to the peer; the peer's answer and candidates
enter through the engine's own ``_remote_dj_handle_answer`` /
``_remote_dj_handle_ice``.

The test sandbox has no network (loopback only) and libnice skips loopback
addresses unless told; both agents get 127.0.0.1 as their only local address
through ``nice_agent_add_local_address`` (libnice via ctypes; there is no Nice
typelib on the host). Nothing touches host networking, TURN/STUN or devices.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading

from gi.repository import GLib, Gst, GstSdp, GstWebRTC

from library.tests._vt_b0_lab import wait_until

DJ_TONE = 3200
STUDIO_TONE = 3000

_nice = None


def _libnice():
    global _nice
    if _nice is None:
        name = ctypes.util.find_library("nice") or "libnice.so.10"
        lib = ctypes.CDLL(name)
        lib.nice_address_new.restype = ctypes.c_void_p
        lib.nice_address_set_from_string.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.nice_address_set_from_string.restype = ctypes.c_int
        lib.nice_agent_add_local_address.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.nice_agent_add_local_address.restype = ctypes.c_int
        lib.nice_address_free.argtypes = [ctypes.c_void_p]
        _nice = lib
    return _nice


def _gpointer(obj):
    get = ctypes.pythonapi.PyCapsule_GetPointer
    get.restype = ctypes.c_void_p
    get.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return get(obj.__gpointer__, None)


def loopback_only(ice):
    """Make the libnice agent behind a GstWebRTCNice use 127.0.0.1 only."""
    agent = ice.get_property("agent")
    lib = _libnice()
    addr = lib.nice_address_new()
    try:
        if not lib.nice_address_set_from_string(addr, b"127.0.0.1"):
            raise RuntimeError("nice_address_set_from_string failed")
        if not lib.nice_agent_add_local_address(_gpointer(agent), addr):
            raise RuntimeError("nice_agent_add_local_address failed")
    finally:
        lib.nice_address_free(addr)
    return agent


class SignalingRelay:
    """Stand-in for the websocket server (the only seam)."""

    def __init__(self, lab):
        self.lab = lab
        self.sent = []
        self.candidates = []
        self.retired = []
        self.calls = []

    def retire_attempt_threadsafe(self, attempt_id):
        self.retired.append(attempt_id)          # the websocket for that attempt closes

    def __getattr__(self, name):
        # Any other websocket-server call is recorded, never silently dropped.
        if name.endswith("_threadsafe"):
            return lambda *args, **kwargs: self.calls.append((name, args))
        raise AttributeError(name)

    def send_json_threadsafe(self, attempt_id, message):
        self.sent.append((attempt_id, message.get("type")))
        if message.get("type") == "ice":
            self.candidates.append(message.get("candidate"))
        GLib.idle_add(lambda: (self.lab.peer_receive(attempt_id, message), False)[1])


class BrowserPeer:
    """A synthetic Remote DJ browser: answers the engine's offer, sends a
    3200 Hz Opus microphone, decodes the program return."""

    def __init__(self, lab, attempt_id, tone=DJ_TONE):
        self.lab, self.attempt_id = lab, attempt_id
        self.pipe = Gst.Pipeline.new(f"b7s-browser-{attempt_id[:8]}")
        self.webrtc = Gst.ElementFactory.make("webrtcbin", None)
        self.webrtc.set_property("bundle-policy", GstWebRTC.WebRTCBundlePolicy.MAX_BUNDLE)
        self.pipe.add(self.webrtc)
        self.ice = self.webrtc.get_property("ice-agent")
        self.agent = loopback_only(self.ice)
        src = Gst.parse_bin_from_description(
            f"audiotestsrc is-live=true wave=sine freq={tone} volume=0.4 ! audioconvert ! audioresample ! "
            "audio/x-raw,rate=48000,channels=2 ! opusenc bitrate=96000 ! rtpopuspay name=pay pt=111 ! "
            'capsfilter name=rtpcaps caps="application/x-rtp,media=audio,encoding-name=OPUS,payload=111,clock-rate=48000"',
            True)
        self.src = src                 # added and started only once linked (a live source pushing
        self.return_buffers = 0        # into an unlinked pad stops for good)
        bus = self.pipe.get_bus()
        bus.add_signal_watch()

        def on_message(_bus, message):
            if message.type in (Gst.MessageType.ERROR, Gst.MessageType.WARNING):
                err, dbg = message.parse_error() if message.type == Gst.MessageType.ERROR else message.parse_warning()
                print(f"RDJ-PEER-BUS {message.type.first_value_nick} {message.src.get_name()}: {err.message} "
                      f"| {str(dbg)[:300]}", flush=True)
        bus.connect("message", on_message)
        self.webrtc.connect("on-ice-candidate", self._on_candidate)
        self.webrtc.connect("pad-added", self._on_pad)
        self.pipe.set_state(Gst.State.READY)
        self.keep = []
        self.remote_set = False
        self.candidates = []
        self.answer_result = None

    def _on_candidate(self, _el, mline, candidate):
        self.candidates.append(candidate)
        attempt, engine = self.attempt_id, self.lab.engine
        GLib.idle_add(lambda: (engine._remote_dj_handle_ice(attempt, mline, candidate), False)[1])

    def _on_pad(self, _el, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return
        sink = Gst.parse_bin_from_description("rtpopusdepay ! opusdec ! audioconvert ! fakesink sync=true name=ret",
                                              True)
        self.pipe.add(sink)
        sink.sync_state_with_parent()
        pad.link(sink.get_static_pad("sink"))

        def count(_p, _info):
            self.return_buffers += 1
            return Gst.PadProbeReturn.OK
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.BUFFER, count)

    @staticmethod
    def sdp_shape(text):
        keep = ("m=", "a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive", "a=mid:", "a=rtpmap:", "a=group:")
        return [line.strip() for line in text.splitlines() if line.startswith(keep)]

    def on_offer(self, sdp_text):
        print("RDJ-OFFER " + " | ".join(self.sdp_shape(sdp_text)), flush=True)
        # Send on the payload type the engine offered for the DJ's microphone
        # (its recvonly m-line), as a browser would.
        pt = mline = None
        lines = sdp_text.splitlines()
        sections = [i for i, line in enumerate(lines) if line.startswith("m=")]
        for n, start in enumerate(sections):
            end = sections[n + 1] if n + 1 < len(sections) else len(lines)
            if any(l.strip() == "a=recvonly" for l in lines[start:end]):
                pt, mline = int(lines[start].split()[3]), n
        self.mline = mline
        if pt is not None:
            self.src.get_by_name("pay").set_property("pt", pt)
            self.src.get_by_name("rtpcaps").set_property("caps", Gst.Caps.from_string(
                f"application/x-rtp,media=audio,encoding-name=OPUS,payload={pt},clock-rate=48000"))
        ok, msg = GstSdp.SDPMessage.new_from_text(sdp_text)
        offer = GstWebRTC.WebRTCSessionDescription.new(GstWebRTC.WebRTCSDPType.OFFER, msg)
        # Local transceivers in the offer's m-line order BEFORE the offer is
        # applied (webrtcbin associates unassociated local transceivers with
        # the remote m-lines in order): audio0 = the program return
        # (receive), audio1 = the DJ microphone (send).
        rtp = f"application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,payload={pt or 111}"
        if self.mline == 1:
            self.webrtc.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY,
                             Gst.Caps.from_string("application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000"))
        sinkpad = self.webrtc.request_pad(self.webrtc.get_pad_template("sink_%u"), None, Gst.Caps.from_string(rtp))
        self.pipe.add(self.src)
        result = self.src.get_static_pad("src").link(sinkpad)
        self.sent_buffers = 0

        def sent(_pad, _info):
            self.sent_buffers += 1
            return Gst.PadProbeReturn.OK
        sinkpad.add_probe(Gst.PadProbeType.BUFFER, sent)
        self.sinkpad = sinkpad
        print(f"RDJ-PEER link={result.value_nick} pad={sinkpad.get_name()} mline={self.mline} pt={pt}", flush=True)
        self.pipe.set_state(Gst.State.PLAYING)
        promise = Gst.Promise.new()
        self.webrtc.emit("set-remote-description", offer, promise)
        promise.wait()
        self.keep.append(promise)
        promise = Gst.Promise.new()
        self.webrtc.emit("create-answer", None, promise)
        promise.wait()
        reply = promise.get_reply()
        self.keep += [promise, reply]
        answer = reply.get_value("answer")
        promise = Gst.Promise.new()
        self.webrtc.emit("set-local-description", answer, promise)
        promise.wait()
        self.keep.append(promise)
        text = answer.sdp.as_text()
        print("RDJ-ANSWER " + " | ".join(self.sdp_shape(text)), flush=True)
        attempt, engine = self.attempt_id, self.lab.engine
        GLib.idle_add(lambda: (setattr(self, "answer_result", engine._remote_dj_handle_answer(attempt, text)), False)[1])
        self.remote_set = True

    def on_ice(self, mline, candidate):
        self.webrtc.emit("add-ice-candidate", mline, candidate)

    def drop(self):
        """Involuntary loss: the browser vanishes (no goodbye)."""
        pipe = self.pipe
        t = threading.Thread(target=lambda: pipe.set_state(Gst.State.NULL), daemon=True)
        t.start()
        t.join(5.0)


def build_remote_topology(engine, rate):
    """Insert the _build_main_pipeline Remote DJ topology into a transport-
    harness engine (before PLAYING): program bus -> caps -> remote_dj_tee ->
    master; a live studio-mic tone -> gate -> local_mic_tee -> master; the
    persistent DJ slot(s)."""
    pipe = engine.main_pipeline
    duck_src = engine.duck_gain.get_static_pad("src")
    master_pad = duck_src.get_peer()
    duck_src.unlink(master_pad)
    caps = Gst.ElementFactory.make("capsfilter", "remote_dj_fixate_caps")
    caps.set_property("caps", Gst.Caps.from_string(f"audio/x-raw,rate={rate},channels=2"))
    tee = Gst.ElementFactory.make("tee", "remote_dj_tee")
    for el in (caps, tee):
        pipe.add(el)
    duck_src.link(caps.get_static_pad("sink"))
    caps.link(tee)
    tee.request_pad_simple("src_%u").link(master_pad)
    engine.remote_dj_tee = tee
    mic = Gst.parse_bin_from_description(
        f"audiotestsrc is-live=true wave=sine freq={STUDIO_TONE} volume=0.4 ! audioconvert ! "
        f"audio/x-raw,rate={rate},channels=2 ! volume name=studio_gate volume=0.0", True)
    mic_tee = Gst.ElementFactory.make("tee", "local_mic_tee")
    pipe.add(mic)
    pipe.add(mic_tee)
    mic.link(mic_tee)
    mic_tee.request_pad_simple("src_%u").link(engine.master_mixer.request_pad_simple("sink_%u"))
    engine.local_mic_tee = mic_tee
    engine._b7s_studio_gate = mic.get_by_name("studio_gate")
    engine._build_dj_slots(1)
    # As _build_main_pipeline does: level messages refresh Remote Mic media
    # liveness (the transport harness connects only the error handler).
    engine.main_pipeline.get_bus().connect("message::element", engine._on_element_message)
    return engine._b7s_studio_gate


def wait_connected(lab, timeout=15.0):
    engine = lab.engine
    return wait_until(lambda: engine.remote_dj_session is not None
                      and getattr(engine.remote_dj_session, "webrtc", None) is not None
                      and engine.remote_dj_session.webrtc.get_property("ice-connection-state")
                      in (GstWebRTC.WebRTCICEConnectionState.CONNECTED, GstWebRTC.WebRTCICEConnectionState.COMPLETED),
                      timeout)


def describe_transceivers(webrtc, sinkpad=None):
    rows = []
    for idx in range(8):
        trans = webrtc.emit("get-transceiver", idx)
        if trans is None:
            break
        rows.append(f"{idx}:mid={trans.get_property('mid')} mline={trans.get_property('mlineindex')} "
                    f"dir={trans.get_property('direction').value_nick} "
                    f"cur={trans.get_property('current-direction').value_nick}")
    if sinkpad is not None:
        try:
            t = sinkpad.get_property("transceiver")
            rows.append(f"pad->mid={t.get_property('mid')} mline={t.get_property('mlineindex')}")
        except Exception as exc:
            rows.append(f"pad->? {exc!r}")
    return " ; ".join(rows)
