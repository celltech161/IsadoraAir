"""Roadmap 2.25 B7 measurement helper (not a test module): Remote DJ
connection / reconnect load. Two real webrtcbin peers in one pipeline (ICE on
the sandbox's loopback, DTLS-SRTP, Opus 48 kHz both ways) are connected,
stream for a few seconds, are torn down and rebuilt -- the CPU and thread
churn of a Remote DJ session connecting and reconnecting, in a separate
process. Run by measure_vt_b7_readmission inside the test sandbox only.

usage: python _vt_b7_webrtc_churn.py <seconds> <status-file>
"""

import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstWebRTC", "1.0")
gi.require_version("GstSdp", "1.0")
from gi.repository import GLib, Gst, GstSdp, GstWebRTC  # noqa: E402,F401

Gst.init(None)
DURATION = float(sys.argv[1])
STATUS = sys.argv[2]
SESSION_S = 3.0
loop = GLib.MainLoop()
stats = {"cycles": 0, "connected": 0, "failed": 0}
current = {}


def build():
    pipe = Gst.parse_launch(
        "audiotestsrc is-live=true wave=sine freq=440 ! audioconvert ! audioresample ! "
        "audio/x-raw,rate=48000,channels=2 ! opusenc bitrate=96000 ! rtpopuspay pt=96 ! "
        "application/x-rtp,media=audio,encoding-name=OPUS,payload=96 ! webrtcbin name=a bundle-policy=max-bundle "
        "webrtcbin name=b bundle-policy=max-bundle"
    )
    a, b = pipe.get_by_name("a"), pipe.get_by_name("b")
    keep = [a.get_property("ice-agent"), b.get_property("ice-agent")]   # see engine _remote_dj_build_session
    for ice in keep[:2]:
        # The sandbox has only loopback (no network); libnice ignores it unless told.
        if hasattr(ice, "add_local_ip_address"):
            ice.add_local_ip_address("127.0.0.1")
    b.emit("add-transceiver", GstWebRTC.WebRTCRTPTransceiverDirection.RECVONLY,
           Gst.Caps.from_string("application/x-rtp,media=audio,encoding-name=OPUS,payload=96"))

    def on_pad(_el, pad):
        if pad.direction != Gst.PadDirection.SRC:
            return
        chain = Gst.parse_bin_from_description(
            "rtpopusdepay ! opusdec ! audioconvert ! fakesink sync=true", True)
        pipe.add(chain)
        chain.sync_state_with_parent()
        pad.link(chain.get_static_pad("sink"))
        stats["connected"] += 1

    b.connect("pad-added", on_pad)
    # Candidates and negotiation are marshalled to the GLib thread: nothing is
    # emitted from webrtcbin's own pc thread (the loopback crash pattern).
    a.connect("on-ice-candidate", lambda _e, m, c: GLib.idle_add(lambda: (b.emit("add-ice-candidate", m, c), False)[1]))
    b.connect("on-ice-candidate", lambda _e, m, c: GLib.idle_add(lambda: (a.emit("add-ice-candidate", m, c), False)[1]))

    def negotiate():
        promise = Gst.Promise.new()
        a.emit("create-offer", None, promise)
        promise.wait()
        keep.append(promise)
        offer_reply = promise.get_reply()          # keep the reply alive while the SDP is used
        offer = offer_reply.get_value("offer")
        for element in (a,):
            pr = Gst.Promise.new()
            element.emit("set-local-description", offer, pr)
            pr.wait()
        pr = Gst.Promise.new()
        b.emit("set-remote-description", offer, pr)
        pr.wait()
        promise = Gst.Promise.new()
        b.emit("create-answer", None, promise)
        promise.wait()
        keep.append(promise)
        answer_reply = promise.get_reply()
        answer = answer_reply.get_value("answer")
        pr = Gst.Promise.new()
        b.emit("set-local-description", answer, pr)
        pr.wait()
        pr = Gst.Promise.new()
        a.emit("set-remote-description", answer, pr)
        pr.wait()
        return False

    a.connect("on-negotiation-needed", lambda _e: GLib.idle_add(negotiate))
    pipe.set_state(Gst.State.PLAYING)
    return pipe, keep


def teardown(pipe):
    # NULL off the GLib thread (never from webrtcbin's own pc thread).
    t = threading.Thread(target=lambda: pipe.set_state(Gst.State.NULL), daemon=True)
    t.start()
    t.join(5.0)
    if t.is_alive():
        stats["failed"] += 1


def cycle():
    old = current.pop("pipe", None)
    if old is not None:
        teardown(old[0])
    if time.monotonic() >= deadline:
        loop.quit()
        return False
    try:
        current["pipe"] = build()
        stats["cycles"] += 1
    except Exception:
        stats["failed"] += 1
    with open(STATUS, "w") as fh:
        fh.write(f"cycles={stats['cycles']} connected={stats['connected']} failed={stats['failed']}\n")
    return True


deadline = time.monotonic() + DURATION
cycle()
GLib.timeout_add(int(SESSION_S * 1000), cycle)
loop.run()
old = current.pop("pipe", None)
if old is not None:
    teardown(old[0])
with open(STATUS, "w") as fh:
    fh.write(f"cycles={stats['cycles']} connected={stats['connected']} failed={stats['failed']} done=1\n")
