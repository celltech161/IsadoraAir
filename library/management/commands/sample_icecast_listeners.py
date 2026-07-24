"""Sample streaming-server listener counts across every enabled
outbound Encoder and write one IcecastSample row per invocation.

Despite the model name (IcecastSample) this handles both:

  Icecast          -> /status-json.xsl (public, no auth)
  Shoutcast 1/2    -> /statistics?json=1 (Shoutcast 2.6+; public)

One HTTP fetch per unique (host, port) combination, not per Encoder
row, so a server hosting multiple mounts / stream IDs is polled once.
Aggregated listener counts land in `listeners_total`; per-mount /
per-stream breakdown lands in `listeners_by_mount` as
"host:port/mount" -> N.

Fired by isadoraair-sample-icecast.timer every minute; the report-time
ATH computation (compute_ath in royalty_reports.py) integrates the
samples across the reporting period.

Silent on success (systemd journal noise otherwise); prints only when
something goes wrong. Idempotent -- a duplicate fire just adds another
sample point, which the ATH integrator handles cleanly via its dt-
between-samples calculation.

Timeout on each fetch: 5s. An unreachable server is skipped (no
listeners counted from it for this sample); other reachable servers
still contribute."""
import json

from django.core.management.base import BaseCommand

import requests


def _fetch_icecast(host, port, timeout):
    """Return list of (mount, listeners) tuples from Icecast."""
    url = f"http://{host}:{port}/status-json.xsl"
    resp = requests.get(url, timeout=timeout)
    data = resp.json()
    sources = data.get("icestats", {}).get("source", [])
    # Single-mount Icecast returns an object; multi-mount an array.
    if isinstance(sources, dict):
        sources = [sources]
    elif not isinstance(sources, list):
        sources = []
    out = []
    for src in sources:
        mount = src.get("mount") or src.get("listenurl", "").rstrip("/").split("/")[-1] or "?"
        if not mount.startswith("/"):
            mount = "/" + mount
        try:
            listeners = int(src.get("listeners", 0) or 0)
        except (TypeError, ValueError):
            listeners = 0
        out.append((mount, listeners))
    return out


def _fetch_shoutcast(host, port, timeout):
    """Return list of (stream_id_as_mount, listeners) tuples from
    Shoutcast 2. /statistics?json=1 is the modern endpoint; older
    Shoutcast 1 boxes don't have it. If SC1 support becomes real we
    can fall back to per-sid /stats?sid=N XML."""
    url = f"http://{host}:{port}/statistics?json=1"
    resp = requests.get(url, timeout=timeout)
    data = resp.json()
    out = []
    for stream in data.get("streams", []) or []:
        sid = stream.get("id")
        mount = f"/{sid}" if sid is not None else "?"
        try:
            listeners = int(stream.get("currentlisteners", 0) or 0)
        except (TypeError, ValueError):
            listeners = 0
        out.append((mount, listeners))
    return out


PROTOCOL_FETCHERS = {
    "icecast": _fetch_icecast,
    "shoutcast1": _fetch_shoutcast,
    "shoutcast2": _fetch_shoutcast,
}


class Command(BaseCommand):
    help = "Sample streaming server listener counts for the royalty-report ATH baseline."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=5.0,
                             help="HTTP timeout per host (default 5s).")

    def handle(self, *args, **opts):
        from encoders.models import Encoder
        from library.models import IcecastSample

        # Group encoders by (host, port, protocol). Same host running
        # both protocols would be rare, but keep it in the key so we
        # don't accidentally overwrite one type's totals with another's.
        endpoints = set(
            (enc.host, enc.port, enc.protocol)
            for enc in Encoder.objects.filter(enabled=True)
            if enc.protocol in PROTOCOL_FETCHERS
        )
        if not endpoints:
            IcecastSample.objects.create(listeners_total=0, listeners_by_mount={})
            return

        listeners_total = 0
        per_mount = {}

        for host, port, protocol in sorted(endpoints):
            fetch = PROTOCOL_FETCHERS[protocol]
            try:
                results = fetch(host, port, opts["timeout"])
            except (requests.RequestException, json.JSONDecodeError, ValueError) as exc:
                self.stderr.write(f"  sample failed for {host}:{port} ({protocol}): {exc}")
                continue

            for mount, listeners in results:
                key = f"{host}:{port}{mount}"
                per_mount[key] = per_mount.get(key, 0) + listeners
                listeners_total += listeners

        IcecastSample.objects.create(
            listeners_total=listeners_total,
            listeners_by_mount=per_mount,
        )
