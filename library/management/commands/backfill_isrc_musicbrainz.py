"""Backfill Track.isrc from MusicBrainz for tracks lacking a tag-based
ISRC (i.e. what backfill_isrc couldn't find in file tags).

Two-step per track:

  1. search_recordings(artist + title [+ album]) -> candidate list.
     Uses MB's Lucene syntax; artist and title are always in the
     query, album is added when Track.album is set. Limit 5 results.

  2. filter candidates by duration tolerance (default 5s against
     Track.duration_seconds). Recordings whose length differs by more
     than that are dropped. Recordings with no length data are kept
     as best-effort candidates.

  3. If exactly one candidate survives -> fetch the full recording
     with includes=['isrcs'] -> take the first ISRC in the response.
     Validate against the ISO 3901 shape (same regex as import_songs
     / backfill_isrc) and write to Track.isrc.

  4. If zero survive -> "no_match" (may be MB doesn't know it, or
     the title/artist metadata differs).

  5. If multiple survive -> "ambiguous". Skipped by default; --allow-
     ambiguous writes the first candidate's ISRC anyway (opt-in
     because a wrong ISRC would misdirect royalties).

Idempotent -- successful writes persist per-track. Mid-run crash /
SIGINT / rate-limit kill can be recovered by re-running; only tracks
with empty ISRC are picked up on the next pass.

Rate-limited: musicbrainzngs.set_rate_limit(True) defaults to ~1 req/s
which is what MB publicly asks for. Fresh library backfill is roughly
(unmapped tracks) x 1-2 calls x 1s -- typically an overnight run.

Requires MUSICBRAINZ_CONTACT set in .env (see settings.py); the CD
ripping flow uses the same value. Anonymous MB access works but is
rate-limited harder and MB reserves the right to block anonymous
clients."""
import re
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from library.models import Track


ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")


def _normalize_isrc(value):
    """Same canonicalization as import_songs / backfill_isrc."""
    if not value:
        return ""
    normalized = re.sub(r"[\s\-]+", "", str(value)).upper()
    return normalized if ISRC_RE.match(normalized) else ""


def _configure_mb():
    """Set useragent + enable the built-in rate limiter. Shares the
    MUSICBRAINZ_CONTACT env var with the CD-ripping flow."""
    import musicbrainzngs
    contact = getattr(settings, "MUSICBRAINZ_CONTACT", "") or "unset@example.invalid"
    musicbrainzngs.set_useragent(
        app="IsadoraAir", version="1.0", contact=contact,
    )
    musicbrainzngs.set_rate_limit(True)  # ~1 req/s
    return musicbrainzngs


class Command(BaseCommand):
    help = "Populate Track.isrc from MusicBrainz for tracks lacking a tag-based ISRC."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0,
                             help="Cap tracks processed (0 = no cap, run to end).")
        parser.add_argument("--dry-run", action="store_true",
                             help="Report what would change without writing.")
        parser.add_argument("--duration-tolerance", type=float, default=5.0,
                             help="Max duration mismatch in seconds (default 5).")
        parser.add_argument("--allow-ambiguous", action="store_true",
                             help="Write first candidate's ISRC even when multiple "
                                  "survive duration filtering. Opt-in because a wrong "
                                  "ISRC would misdirect royalties.")
        parser.add_argument("--start-id", type=int, default=0,
                             help="Resume from a specific Track.id onward.")

    def handle(self, *args, **opts):
        mb = _configure_mb()

        qs = Track.objects.filter(Q(isrc="") | Q(isrc__isnull=True))
        qs = qs.exclude(title="").exclude(artist__isnull=True)
        if opts["start_id"] > 0:
            qs = qs.filter(id__gte=opts["start_id"])
        qs = qs.order_by("id").select_related("artist", "album")
        if opts["limit"] > 0:
            qs = qs[: opts["limit"]]

        total = qs.count()
        self.stdout.write(
            f"Backfilling ISRC via MusicBrainz for {total} tracks "
            f"({'DRY RUN' if opts['dry_run'] else 'live'}, "
            f"duration_tol={opts['duration_tolerance']}s, "
            f"allow_ambiguous={opts['allow_ambiguous']})"
        )
        self.stdout.write(
            "At ~1 req/s and ~2 calls per track, rough ETA: "
            f"~{(total * 2) // 3600}h{((total * 2) % 3600) // 60}m"
        )

        found = 0
        no_match = 0
        ambiguous = 0
        no_isrc = 0
        errors = 0
        skipped = 0
        started = time.time()

        for i, track in enumerate(qs.iterator(), start=1):
            artist_name = track.artist.name if track.artist else ""
            album_title = track.album.title if track.album else ""
            title = track.title or ""

            if not artist_name or not title:
                skipped += 1
                continue

            try:
                # Escape Lucene special chars in each field. musicbrainzngs
                # doesn't do this for us; a title with a colon or slash
                # will break the query otherwise.
                q_artist = _escape_lucene(artist_name)
                q_title = _escape_lucene(title)
                q_parts = [f'artist:"{q_artist}"', f'recording:"{q_title}"']
                if album_title:
                    q_parts.append(f'release:"{_escape_lucene(album_title)}"')
                query = " AND ".join(q_parts)

                result = mb.search_recordings(query=query, limit=5)
                candidates = result.get("recording-list", []) or []
            except Exception as exc:
                errors += 1
                self.stderr.write(f"  [{track.id}] search error: {exc}")
                continue

            # Filter by duration tolerance. MB stores length in ms.
            if track.duration_seconds:
                target_ms = track.duration_seconds * 1000
                tol_ms = opts["duration_tolerance"] * 1000
                filtered = []
                for c in candidates:
                    length = c.get("length")
                    if length is None:
                        filtered.append(c)  # unknown length -> keep as best-effort
                        continue
                    try:
                        if abs(int(length) - target_ms) <= tol_ms:
                            filtered.append(c)
                    except (TypeError, ValueError):
                        filtered.append(c)
                candidates = filtered

            if not candidates:
                no_match += 1
                continue
            if len(candidates) > 1 and not opts["allow_ambiguous"]:
                ambiguous += 1
                continue

            mbid = candidates[0].get("id")
            if not mbid:
                skipped += 1
                continue

            try:
                detail = mb.get_recording_by_id(mbid, includes=["isrcs"])
                recording = detail.get("recording", {})
                isrcs = recording.get("isrc-list", []) or []
            except Exception as exc:
                errors += 1
                self.stderr.write(f"  [{track.id}] lookup error mbid={mbid}: {exc}")
                continue

            isrc = ""
            for candidate_isrc in isrcs:
                normalized = _normalize_isrc(candidate_isrc)
                if normalized:
                    isrc = normalized
                    break

            if not isrc:
                no_isrc += 1
                continue

            found += 1
            if opts["dry_run"]:
                self.stdout.write(
                    f"  [dry-run] {track.id} {artist_name!r} - {title!r} -> {isrc}"
                )
            else:
                Track.objects.filter(id=track.id).update(isrc=isrc)

            if i % 50 == 0:
                elapsed = time.time() - started
                rate = i / elapsed if elapsed > 0 else 0
                remaining = (total - i) / rate if rate > 0 else 0
                self.stdout.write(
                    f"  ...{i}/{total} found={found} no_match={no_match} "
                    f"ambiguous={ambiguous} errors={errors} "
                    f"(~{remaining/60:.0f}m remaining)"
                )

        self.stdout.write(self.style.SUCCESS(
            f"Done. found={found} no_match={no_match} ambiguous={ambiguous} "
            f"no_isrc={no_isrc} errors={errors} skipped={skipped} scanned={total}"
        ))


# Lucene-syntax chars that need escaping in a MB search query.
# From MB API docs -- these mean special things and must be backslashed.
_LUCENE_SPECIAL = r'+-&|!(){}[]^"~*?:\/'


def _escape_lucene(value):
    """Escape Lucene special chars so a title / artist / album with a
    colon, slash, or bracket doesn't break the search query. Also
    strips control chars that MB rejects."""
    result = []
    for ch in str(value):
        if ch in _LUCENE_SPECIAL:
            result.append("\\" + ch)
        elif ord(ch) < 32:
            result.append(" ")
        else:
            result.append(ch)
    return "".join(result)
