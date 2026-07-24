"""Push the currently-playing track to TuneIn's AIR API when it changes.

TuneIn's rule (per their docs at https://tunein.com/broadcasters/api/)
is exactly one HTTP call per song change -- they warn against timer-
based repeated submission. This command reconciles the two by running
on a fast timer (every ~30s) but only issuing an outbound request
when the current PlayEvent's id differs from what we last pushed:

  running latest = PlayEvent.objects.order_by('-started_at').first()
  if running_latest.id == config.last_pushed_play_event_id:
      no-op
  else:
      GET http://air.radiotime.com/Playing.ashx?<credentials>&title=...&artist=...
      update config.last_pushed_play_event_id on 200

That way TuneIn sees one call per track change no matter how often the
timer fires. commercial=true is set when the PlayEvent's category_kind
is 'Spot', matching TuneIn's semantics for a non-music air block.

Config carries station_id / partner_id / partner_key + an enabled
flag. Disabled or missing config = silent no-op; a fresh install
running this timer does no harm until the operator fills the form in
at /admin/library/tuneinconfig/.
"""
import urllib.parse

from django.core.management.base import BaseCommand
from django.utils import timezone

import requests


TUNEIN_URL = "http://air.radiotime.com/Playing.ashx"


class Command(BaseCommand):
    help = "Push the current PlayEvent to TuneIn AIR when it changes (dedupe-by-id)."

    def add_arguments(self, parser):
        parser.add_argument("--timeout", type=float, default=8.0,
                             help="HTTP timeout (default 8s).")
        parser.add_argument("--force", action="store_true",
                             help="Push even if the latest PlayEvent id matches last_pushed_play_event_id.")

    def handle(self, *args, **opts):
        from library.models import PlayEvent, TuneInConfig

        cfg = TuneInConfig.load()
        if not cfg.enabled:
            return
        if not (cfg.station_id and cfg.partner_id and cfg.partner_key):
            self.stderr.write("TuneIn config incomplete -- fill in Admin > Config > TuneIn AIR.")
            return

        latest = PlayEvent.objects.order_by("-started_at").first()
        if latest is None:
            return  # nothing has aired yet

        if not opts["force"] and latest.id == cfg.last_pushed_play_event_id:
            return  # already told TuneIn about this one

        # Blank title = nothing worth reporting (unusual, but engine
        # instrumentation could produce it during an odd deck fail).
        title = (latest.track_title or "").strip()
        if not title:
            return

        artist = (latest.track_artist or "").strip()
        album = (latest.album_title or "").strip()
        is_commercial = (latest.category_kind or "").lower() == "spot"

        params = {
            "partnerId": cfg.partner_id,
            "partnerKey": cfg.partner_key,
            "id": cfg.station_id,
            "title": title,
            "artist": artist,
        }
        if album:
            params["album"] = album
        if is_commercial:
            params["commercial"] = "true"

        try:
            resp = requests.get(TUNEIN_URL, params=params, timeout=opts["timeout"])
        except requests.RequestException as exc:
            self._record_result(cfg, latest, ok=False, msg=f"HTTP error: {exc}")
            return

        ok = 200 <= resp.status_code < 300
        # TuneIn returns text/xml body; the status code is the authoritative
        # signal per their docs, so we don't parse the body -- just record
        # a truncated snippet for admin debugging.
        body_snippet = (resp.text or "").strip()[:120]
        msg = f"HTTP {resp.status_code} {body_snippet}" if body_snippet else f"HTTP {resp.status_code}"
        self._record_result(cfg, latest, ok=ok, msg=msg)

    def _record_result(self, cfg, play_event, ok, msg):
        """Persist push outcome onto TuneInConfig. Only bumps
        last_pushed_play_event_id on success -- a failed push leaves
        the dedupe pointer alone so the NEXT timer fire re-tries the
        same PlayEvent (TuneIn's rate limiter permitting)."""
        cfg.last_pushed_at = timezone.now()
        cfg.last_push_status = f"{'OK' if ok else 'FAIL'}: {msg}"
        fields = ["last_pushed_at", "last_push_status"]
        if ok:
            cfg.last_pushed_play_event_id = play_event.id
            fields.append("last_pushed_play_event_id")
        # save with update_fields so a concurrent admin edit on other
        # config fields (station_id etc) can't race with this write.
        cfg.save(update_fields=fields)
        if not ok:
            self.stderr.write(f"TuneIn push failed for PlayEvent {play_event.id}: {msg}")
