"""Roadmap 2.25 B7 -- re-admission (regeneration) preparation latency under
load. A MEASUREMENT, not a regression test: no ``test_`` module prefix, so
discovery never runs it; run it explicitly through the guarded runner:

    manage.py test library.tests.measure_vt_b7_readmission

Each trial uses the real B2 path against PostgreSQL: a claimed occurrence is
held on a scheduled deck, then re-admitted by regeneration
(``_vt2_reopen_held_occurrence``) with a lead of 100, 150 or 200 ms, exactly as
``_vtt_readmit`` / Start Next / urgent release do. Measured per trial, on the
pipeline clock:

  pre      call -> _create_deck entry (retire the held generation, playable check)
  claim    the idempotent PostgreSQL re-claim inside _create_deck
  create   _create_deck total (claim + bin build + state sync)
  decode   _create_deck exit -> first decoded buffer (streaming thread)
  dispatch first buffer -> GLib readiness callback (_vt2_on_scheduled_deck_ready)
  margin   admission - readiness callback (must be >= READY_LEAD_S = 50 ms)
  onset    rendered music onset - planned admission (program PCM)

A miss (``ready_after_admission`` / ``not_ready_after_open``) is followed by
the ordinary primed start of the same occurrence (the owner's fallback core:
``_create_deck(..., reopen=True)``), and its recovery latency and the
PlayEvent accounting are measured.

Load conditions run inside the test sandbox (its own CPUs: the runner is
started under ``taskset``): idle; representative CPU (one core at 75 % plus
three real-time encoders: the StereoTool + Liquidsoap station shape);
saturated CPU (every allowed core 100 %); concurrent PostgreSQL work (heavy
reads, row churn in the same log, short row locks); concurrent decode +
encode (three FLAC -> MP3/Opus/Vorbis pipelines at full speed); Remote DJ
connection churn (real webrtcbin loopback peers: ICE, DTLS, Opus; connect,
stream, tear down, every 3 s); a busy GLib main loop (15 ms of work every 50
ms on the engine's own loop); and all of them combined.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from django.db import connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from library.services import vt_transition
from library.tests._vt_b0_lab import NS, Lab, GLib, pump, wait_until
from library.tests._vt_integration_lab import MUSIC

LEADS = (0.100, 0.150, 0.200)
TRIALS = 20
MEDIA_S = 0.6
HERE = Path(__file__).resolve().parent


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(q / 100.0 * (len(ordered) - 1)))))
    return ordered[k]


def summary(values):
    values = [v for v in values if v is not None]
    if not values:
        return {"n": 0}
    return {"n": len(values), "median": round(statistics.median(values) * 1000, 2),
            "p95": round(pct(values, 95) * 1000, 2), "max": round(max(values) * 1000, 2)}


class Contention:
    def __init__(self, testcase, lab, kind):
        self.testcase, self.lab, self.kind = testcase, lab, kind
        self.procs, self.threads, self.sources = [], [], []
        self.stop = threading.Event()
        self.counters = {"db_ops": 0, "glib_busy_ms": 0.0}
        self.status = None

    def cpus(self):
        return len(os.sched_getaffinity(0))

    def spawn(self, argv):
        self.procs.append(subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL))

    def encoders_realtime(self, n=3):
        encs = (["lamemp3enc", "bitrate=128"], ["opusenc", "bitrate=96000"], ["vorbisenc"])
        for i in range(n):
            self.spawn(["gst-launch-1.0", "-q", "audiotestsrc", "is-live=true", "!", "audioconvert", "!",
                        "audioresample", "!", "audio/x-raw,rate=48000,channels=2", "!", *encs[i % len(encs)],
                        "!", "fakesink", "sync=true"])

    def decode_encode_full_speed(self, n=3):
        flac = self.lab.media("contention-src", [(30.0, 440)], fmt="flac")
        encs = (["lamemp3enc"], ["opusenc"], ["vorbisenc"])
        script = []
        for i in range(n):
            enc = " ".join(encs[i % len(encs)])
            script.append(f"while :; do gst-launch-1.0 -q filesrc location={flac} ! flacparse ! flacdec ! "
                          f"audioconvert ! audioresample ! audio/x-raw,rate=48000 ! {enc} ! fakesink sync=false; done")
        for line in script:
            self.spawn(["/bin/sh", "-c", line])

    def database(self, n=4):
        from library.models import LogItem
        log_id = self.testcase.log.pk

        def worker(idx):
            try:
                while not self.stop.is_set():
                    with connection.cursor() as cur:
                        cur.execute("SELECT count(*), sum(g) FROM generate_series(1, 400000) g")
                        cur.fetchone()
                    LogItem.objects.filter(playlist_log_id=log_id, position__gte=900000 + idx * 1000).update(
                        track_title="contention")
                    from django.db import transaction
                    with transaction.atomic():
                        list(LogItem.objects.select_for_update(of=("self",)).filter(
                            playlist_log_id=log_id, position=900000 + idx * 1000)[:1])
                        time.sleep(0.05)
                    self.counters["db_ops"] += 3
            finally:
                connections.close_all()
        for idx in range(n):
            t = threading.Thread(target=worker, args=(idx,), daemon=True, name=f"b7-db-{idx}")
            self.threads.append(t)
            t.start()

    def remote_dj(self):
        self.status = Path(os.environ.get("TMPDIR", "/tmp")) / f"b7-webrtc-{os.getpid()}.txt"
        self.spawn([sys.executable, str(HERE / "_vt_b7_webrtc_churn.py"), "3600", str(self.status)])

    def glib_busy(self):
        def busy():
            if self.stop.is_set():
                return False
            t0 = time.monotonic()
            while time.monotonic() - t0 < 0.015:
                pass
            self.counters["glib_busy_ms"] += 15.0
            return True
        self.sources.append(GLib.timeout_add(50, busy))

    def __enter__(self):
        k = self.kind
        if k in ("cpu_representative", "combined"):
            self.spawn(["stress-ng", "--quiet", "--cpu", "1", "--cpu-load", "75"])
            self.encoders_realtime()
        if k == "cpu_saturated":
            self.spawn(["stress-ng", "--quiet", "--cpu", str(self.cpus())])
        if k in ("database", "combined"):
            self.database()
        if k in ("media", "combined"):
            self.decode_encode_full_speed()
        if k in ("remote_dj", "combined"):
            self.remote_dj()
        if k in ("glib_busy", "combined"):
            self.glib_busy()
        pump(2.0)                     # warm-up under load
        return self

    def __exit__(self, *exc):
        self.stop.set()
        for source in self.sources:
            if GLib.MainContext.default().find_source_by_id(source):
                GLib.source_remove(source)
        for proc in self.procs:
            proc.terminate()
        for proc in self.procs:
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        for t in self.threads:
            t.join(5)
        subprocess.run(["pkill", "-TERM", "-f", "gst-launch-1.0 -q filesrc"], check=False)
        return False

    def describe(self):
        status = self.status.read_text().strip() if self.status and self.status.exists() else ""
        return {"kind": self.kind, "cpus": self.cpus(), "db_ops": self.counters["db_ops"],
                "glib_busy_ms": round(self.counters["glib_busy_ms"]), "webrtc": status}


class ReadmissionLatency(TransactionTestCase):
    def setUp(self):
        from library.models import Artist, Category, CategoryKind, LogItem, PlaylistLog, Track
        self.M = SimpleNamespace(LogItem=LogItem, Track=Track)
        kind = CategoryKind.objects.create(code="b7music", name="B7 Music")
        self.category = Category.objects.create(code="B7MUSIC", name="B7 Music", kind=kind)
        self.artist = Artist.objects.create(name="B7 Artist")
        self.log = PlaylistLog.objects.create(date=date(2027, 11, 1), hour=9, status="approved")
        for idx in range(4):
            LogItem.objects.create(playlist_log=self.log, position=900000 + idx * 1000, scheduled_time=timezone.now(),
                                   track_title="filler", track_artist="x", category=self.category)
        self.serial = 0
        self.track = None

    def lab(self):
        lab = Lab(self)
        engine = lab.engine
        for name in ("_claim_playback_occurrence", "_schedule_occurrence_air_start_from_probe",
                     "_persist_deck_duration"):
            engine.__dict__.pop(name, None)
        engine._write_now_playing = lambda _track: None
        engine.log_items = []
        engine._start_next_track = lambda slot=None: None       # EOS of a trial deck starts nothing
        lab.play()
        path = lab.media("b7-measure", [(MEDIA_S, MUSIC)])
        self.track = self.M.Track.objects.create(
            filepath=str(path), filename=Path(path).name, title="B7 measure", artist=self.artist,
            category=self.category, ready2air=True, duration_seconds=MEDIA_S, next_start_seconds=MEDIA_S - 0.05)
        return lab

    def item(self):
        self.serial += 1
        row = self.M.LogItem.objects.create(
            playlist_log=self.log, position=self.serial, scheduled_time=timezone.now(), track=self.track,
            track_title=self.track.title, track_artist=self.artist.name, category=self.category)
        return self.M.LogItem.objects.select_related("track", "category").get(pk=row.pk)

    def instrument(self, lab):
        engine = lab.engine
        marks = {}
        create = engine._create_deck
        claim = engine._claim_playback_occurrence
        ready = engine._vt2_on_scheduled_deck_ready

        def timed_create(*args, **kwargs):
            if kwargs.get("reopen") and kwargs.get("scheduled_admission_ns"):
                marks["create_in"] = lab.now_ns()
                out = create(*args, **kwargs)
                marks["create_out"] = lab.now_ns()
                return out
            return create(*args, **kwargs)

        def timed_claim(item):
            t0 = lab.now_ns()
            out = claim(item)
            marks.setdefault("claims", []).append(lab.now_ns() - t0)
            return out

        def timed_ready(slot, generation):
            marks.setdefault("dispatch", lab.now_ns())
            return ready(slot, generation)
        engine._create_deck = timed_create
        engine._claim_playback_occurrence = timed_claim
        engine._vt2_on_scheduled_deck_ready = timed_ready
        return marks

    def trial(self, lab, lead):
        from library.models import PlayEvent
        engine = lab.engine
        self.assertTrue(wait_until(lambda: engine.decks.get("B") is None and engine._deck_slot_available("B"), 5.0))
        item = self.item()
        engine._create_deck("B", item, scheduled_admission_ns=int((lab.now() + 0.8) * NS))
        pump(0.3)
        saved = {name: engine.__dict__.get(name) for name in
                 ("_create_deck", "_claim_playback_occurrence", "_vt2_on_scheduled_deck_ready")}
        marks = self.instrument(lab)
        events = []
        call = lab.now_ns()
        admission = call + int(lead * NS)
        outcome, deck = engine._vt2_reopen_held_occurrence("B", admission)
        if deck is not None:
            deck.on_scheduled_event = lambda d, kind, reason="": events.append((kind, reason, lab.now_ns()))
        self.assertTrue(wait_until(lambda: any(k in ("audible", "failed") for k, _r, _t in events), 2.0)
                        or outcome != "reopened")
        row = {"lead": lead, "outcome": outcome}
        ready_ns = getattr(deck, "scheduled_ready_ns", None) if deck is not None else None
        row["pre"] = (marks.get("create_in", call) - call) / NS
        claims = marks.get("claims") or []
        row["claim"] = claims[-1] / NS if claims else None
        if "create_in" in marks:
            row["create"] = (marks["create_out"] - marks["create_in"]) / NS
        if ready_ns is not None and "create_out" in marks:
            row["decode"] = (ready_ns - marks["create_out"]) / NS
        if ready_ns is not None and "dispatch" in marks:
            row["dispatch"] = (marks["dispatch"] - ready_ns) / NS
        if "dispatch" in marks:
            row["margin"] = (admission - marks["dispatch"]) / NS
        failed = [e for e in events if e[0] == "failed"]
        row["missed"] = bool(failed) or outcome != "reopened"
        row["reason"] = failed[0][1] if failed else ""
        pump(0.25)
        tr = lab.trace()
        if not row["missed"]:
            onset = tr.onset(MUSIC, after=admission / NS - 0.2, before=admission / NS + 1.0)
            row["onset"] = None if onset is None else onset - admission / NS
        else:
            # The owner's fallback core: an ordinary primed start of the SAME
            # occurrence (claim idempotent); recovery measured to real audio.
            fail_at = failed[0][2] if failed else lab.now_ns()
            self.assertTrue(wait_until(lambda: engine.decks.get("B") is None and engine._deck_slot_available("B"),
                                       2.0))
            engine._create_deck("B", item, reopen=True)
            pump(1.0)
            tr = lab.trace()
            onset = tr.onset(MUSIC, after=fail_at / NS)
            row["recovery"] = None if onset is None else onset - fail_at / NS
        self.assertTrue(wait_until(lambda: PlayEvent.objects.filter(log_item_id_snapshot=item.id).exists(), 3.0))
        pump(MEDIA_S + 0.2)
        rowdb = self.M.LogItem.objects.get(pk=item.pk)
        row["play_events"] = PlayEvent.objects.filter(log_item_id_snapshot=item.id).count()
        row["played"] = rowdb.played_at is not None
        for name, value in saved.items():
            if value is None:
                engine.__dict__.pop(name, None)
            else:
                engine.__dict__[name] = value
        return row

    def run_condition(self, kind, leads=LEADS, trials=TRIALS):
        lab = self.lab()
        rows = []
        with Contention(self, lab, kind) as load:
            for lead in leads:
                for _n in range(trials):
                    row = self.trial(lab, lead)
                    row["kind"] = kind
                    rows.append(row)
                    print("VT-SAMPLE " + json.dumps(row, sort_keys=True), flush=True)
            described = load.describe()
        print("VT-LOAD " + json.dumps(described, sort_keys=True), flush=True)
        for lead in leads:
            sub = [r for r in rows if r["lead"] == lead]
            misses = [r for r in sub if r["missed"]]
            out = {
                "kind": kind, "lead_ms": int(lead * 1000), "trials": len(sub), "misses": len(misses),
                "miss_reasons": sorted({r["reason"] or r["outcome"] for r in misses}),
                "claim": summary([r.get("claim") for r in sub]),
                "create": summary([r.get("create") for r in sub]),
                "decode": summary([r.get("decode") for r in sub]),
                "dispatch": summary([r.get("dispatch") for r in sub]),
                "ready_after_call": summary([(lead - r["margin"]) if r.get("margin") is not None else None
                                             for r in sub]),
                "min_margin_ms": None if not [r for r in sub if r.get("margin") is not None] else round(
                    min(r["margin"] for r in sub if r.get("margin") is not None) * 1000, 2),
                "onset_abs": summary([abs(r["onset"]) for r in sub if r.get("onset") is not None]),
                "recovery": summary([r.get("recovery") for r in misses]),
                "accounting_ok": all(r["play_events"] == 1 and r["played"] for r in sub),
            }
            print("VT-READMIT " + json.dumps(out, sort_keys=True), flush=True)
            self.assertTrue(out["accounting_ok"], out)
        lab.stop()

    def test_01_idle(self):
        self.run_condition("idle")

    def test_02_cpu_representative(self):
        self.run_condition("cpu_representative")

    def test_03_cpu_saturated(self):
        self.run_condition("cpu_saturated")

    def test_04_database(self):
        self.run_condition("database")

    def test_05_media(self):
        self.run_condition("media")

    def test_06_remote_dj(self):
        self.run_condition("remote_dj")

    def test_07_glib_busy(self):
        self.run_condition("glib_busy")

    def test_08_combined(self):
        self.run_condition("combined")

    def test_09_combined_extended(self):
        """The two candidate margins under the worst combined load, 60 trials each."""
        self.run_condition("combined", leads=(0.150, 0.200), trials=60)
