"""Album-art lookup for deck displays.

Fallback chain, in order:
  1. Manual override — `Album.cover_art` then `Artist.cover_art` — always
     checked live against the DB (never cached), so an admin upload takes
     effect immediately with no cache to invalidate.
  2. Cached result on `Track` (`art_url`/`art_source`/`art_checked_at`),
     if a previous lookup already resolved this track — including a cached
     "none" (a track that had no match anywhere doesn't get re-queried
     against external services on every play). A cached "none" still gets
     the current UI Theme default art substituted in live (see below).
  3. Embedded art extracted directly from the audio file (mutagen) —
     instant and offline-safe, and the best match available for an
     already-curated local library.
  4. Oak Grove Radio's in-house hosted album art for mixshows/segments —
     `https://artwork.oakgroveradio.com/{artist-slug}.png`, artist
     name lowercased with spaces converted to hyphens. Checked for every
     track regardless of category (unlike Deezer/iTunes below), since
     this is specifically curated for exactly the syndicated non-music
     content (mixshows, segments) that a public music catalog wouldn't
     have anyway.
  5. Deezer, via a same-origin server-side call (no browser CORS issues,
     one shot per track instead of a client-side multi-step chain).
  6. iTunes, as a last external fallback.
  7. None found — cached as such; the UI Theme default album art (if an
     admin has uploaded one) is substituted in live at request time
     rather than baked into the per-track cache, so changing the default
     applies instantly to every track that has no real match.

Ported/adapted from the Oak Grove Radio stream player's album-art lookup
(deezer_proxy.php + client-side sanitize/fallback logic), with the
Shoutcast-string-parsing piece dropped since IsadoraAir already has
structured artist/title/album fields per track and direct filesystem
access to the audio itself — but the artist-slug hosted-art step is kept
and pointed at Oak Grove's real server, since that's genuine in-house
content for this station's own mixshows/segments, not a stand-in for
something IsadoraAir already has a better source for.
"""
import re
import unicodedata
from pathlib import Path

import requests
from django.conf import settings
from django.utils import timezone
from mutagen import File as MutagenFile

ART_CACHE_DIR = Path(settings.MEDIA_ROOT) / "album_art_cache"
OAKGROVE_ALBUMART_BASE = "https://artwork.oakgroveradio.com/"

_PRIMARY_TAG_MARKERS = ["rip", "remaster", "bit", "edit", "clean"]
_FEAT_PATTERNS = [
    re.compile(r"\s+\(feat\.?[^)]*\)", re.IGNORECASE),
    re.compile(r"\s+\[feat\.?[^\]]*\]", re.IGNORECASE),
    re.compile(r"\s+feat\.?.*$", re.IGNORECASE),
    re.compile(r"\s+ft\.?.*$", re.IGNORECASE),
]


def _sanitize_primary_tags(title):
    if not title:
        return title
    out = title
    for token in _PRIMARY_TAG_MARKERS:
        out = re.sub(rf"\([^)]*{token}[^)]*\)", "", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip()


def _strip_featured(text):
    if not text:
        return text
    out = text
    for pattern in _FEAT_PATTERNS:
        out = pattern.sub("", out)
    return re.sub(r"\s{2,}", " ", out).strip()


def _strip_all_parens(text):
    if not text:
        return text
    return re.sub(r"\s{2,}", " ", re.sub(r"\([^)]*\)", "", text)).strip()


def _slugify_artist(name):
    """Matches Oak Grove Radio's own `toSlug()` exactly, since this has to
    line up with real filenames already hosted on their server — e.g.
    'Big Picture Science' -> 'big-picture-science'."""
    if not name:
        return ""
    normalized = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in normalized if not unicodedata.combining(c))
    lowered = stripped.lower()
    cleaned = re.sub(r"[^a-z0-9\s-]", "", lowered).strip()
    hyphenated = re.sub(r"\s+", "-", cleaned)
    return re.sub(r"-+", "-", hyphenated)


def _oakgrove_lookup(artist_name):
    slug = _slugify_artist(artist_name)
    if not slug:
        return None
    url = f"{OAKGROVE_ALBUMART_BASE}{slug}.png"
    try:
        r = requests.head(url, timeout=5, allow_redirects=True)
        return url if r.status_code == 200 else None
    except Exception:
        return None


def _extract_embedded_art_bytes(filepath):
    """Returns (bytes, mime) for the first embedded cover picture found, or
    (None, None). Covers FLAC/Vorbis (.pictures), MP4/M4A ('covr' atom),
    and ID3-tagged formats (MP3/AIFF, via APIC frames)."""
    try:
        audio = MutagenFile(filepath)
    except Exception:
        return None, None
    if audio is None:
        return None, None

    pictures = getattr(audio, "pictures", None)
    if pictures:
        pic = pictures[0]
        return pic.data, pic.mime or "image/jpeg"

    tags = getattr(audio, "tags", None)
    if tags is None:
        return None, None

    try:
        covr = tags.get("covr")
    except Exception:
        covr = None
    if covr:
        from mutagen.mp4 import MP4Cover
        cover = covr[0]
        mime = "image/png" if cover.imageformat == MP4Cover.FORMAT_PNG else "image/jpeg"
        return bytes(cover), mime

    if hasattr(tags, "getall"):
        apics = tags.getall("APIC")
        if apics:
            return apics[0].data, apics[0].mime or "image/jpeg"

    return None, None


def _cache_embedded_art(track):
    data, mime = _extract_embedded_art_bytes(track.filepath)
    if not data:
        return None
    ext = "png" if mime == "image/png" else "jpg"
    ART_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = ART_CACHE_DIR / f"{track.id}.{ext}"
    out_path.write_bytes(data)
    return f"{settings.MEDIA_URL}album_art_cache/{track.id}.{ext}"


def _deezer_search(artist, title):
    try:
        structured = f'artist:"{artist}" track:"{title}"' if artist else title
        r = requests.get(
            "https://api.deezer.com/search",
            params={"q": structured, "limit": 1},
            timeout=5,
        )
        data = r.json() if r.ok else None
        hits = data.get("data") if data else None
        if not hits:
            loose = f"{title} {artist}".strip()
            r = requests.get(
                "https://api.deezer.com/search",
                params={"q": loose, "limit": 1},
                timeout=5,
            )
            data = r.json() if r.ok else None
            hits = data.get("data") if data else None
        if not hits:
            return None, None
        album = hits[0].get("album") or {}
        art = album.get("cover_xl") or album.get("cover_big") or album.get("cover_medium") or album.get("cover")
        link = hits[0].get("link")
        return (art, link) if art else (None, None)
    except Exception:
        return None, None


def _itunes_search(artist, title):
    try:
        q = f"{title} {artist}".strip()
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": q, "entity": "song", "limit": 1},
            timeout=5,
        )
        if not r.ok:
            return None, None
        results = r.json().get("results") or []
        if not results:
            return None, None
        hit = results[0]
        art = (hit.get("artworkUrl100") or "").replace("100x100", "600x600")
        link = hit.get("trackViewUrl")
        return (art, link) if art else (None, None)
    except Exception:
        return None, None


def _default_art_result():
    from library.models import UITheme
    default = UITheme.load().default_album_art
    art = default.url if default else None
    return {"art": art, "link": None, "source": "default" if art else "none"}


def resolve_album_art(track):
    """Returns {"art": url_or_None, "link": url_or_None, "source": str}."""
    result = _resolve_album_art_inner(track)
    # Daily cache-buster for `oakgrove` art -- Oak Grove Radio's hosted
    # album-art URLs are stable file paths (e.g. birdnote.png), and the
    # daily push replaces the PNG BYTES at that URL without changing
    # the URL. The server-side Track.art_source cache is correct
    # (URL is still the right one), but the browser HTTP cache keys
    # by URL and will keep serving yesterday's image bytes until its
    # cache TTL expires -- exactly the "stuck on last week's art"
    # symptom the user reported for BirdNote Daily.
    #
    # Daily granularity is intentional: the OGR image server replaces
    # its files at push time (which is daily for BirdNote and the
    # other self-hosted-episode-art shows), so a date-tag guarantees
    # a fresh fetch per calendar day without invalidating browser
    # cache mid-day for tracks whose art didn't change. Non-oakgrove
    # sources (Deezer, iTunes, embedded, default) point at
    # per-image hash URLs or Django media that never mutate in place,
    # so they don't need busting.
    if result.get("source") == "oakgrove" and result.get("art"):
        day = timezone.localdate().isoformat()
        sep = "&" if "?" in result["art"] else "?"
        result["art"] = f"{result['art']}{sep}v={day}"
    return result


def _resolve_album_art_inner(track):
    if track.album_id and track.album.cover_art:
        return {"art": track.album.cover_art.url, "link": None, "source": "Album Override"}
    if track.artist_id and track.artist.cover_art:
        return {"art": track.artist.cover_art.url, "link": None, "source": "Artist Override"}

    if track.art_source:
        if track.art_source == "none":
            return _default_art_result()
        return {"art": track.art_url or None, "link": None, "source": track.art_source}

    artist_name = track.artist.name if track.artist_id else ""
    title = track.title or ""

    art_url = _cache_embedded_art(track)
    if art_url:
        track.art_url, track.art_source, track.art_checked_at = art_url, "embedded", timezone.now()
        track.save(update_fields=["art_url", "art_source", "art_checked_at"])
        return {"art": art_url, "link": None, "source": "embedded"}

    # Oak Grove Radio's in-house hosted art (for mixshows/segments) is
    # checked for every track regardless of category — it's curated
    # specifically for this station's own syndicated content, so it
    # doesn't carry the false-match risk a public catalog search does.
    oakgrove_art = _oakgrove_lookup(artist_name)
    if oakgrove_art:
        track.art_url, track.art_source, track.art_checked_at = oakgrove_art, "oakgrove", timezone.now()
        track.save(update_fields=["art_url", "art_source", "art_checked_at"])
        return {"art": oakgrove_art, "link": None, "source": "oakgrove"}

    # Deezer/iTunes are scoped to music-category tracks only — spots,
    # legal IDs, imaging, and liners aren't "albums," and matching their
    # title text against a public catalog risks a confident-looking but
    # wrong cover (seen in testing: an imaging drop's title coincidentally
    # matched an unrelated song). Embedded-art and Oak Grove's hosted art
    # above are exempt from this since neither can produce a false match.
    is_music = bool(track.category_id and track.category.kind_id and track.category.kind.code == "music")
    if not is_music:
        track.art_url, track.art_source, track.art_checked_at = "", "none", timezone.now()
        track.save(update_fields=["art_url", "art_source", "art_checked_at"])
        return _default_art_result()

    title_primary = _sanitize_primary_tags(title)
    art, link = _deezer_search(artist_name, title_primary)
    if not art:
        title_feat = _strip_featured(title_primary)
        art, link = _deezer_search(artist_name, title_feat)
    if not art:
        title_no_parens = _strip_all_parens(title_primary)
        art, link = _deezer_search(artist_name, title_no_parens)
    if art:
        track.art_url, track.art_source, track.art_checked_at = art, "deezer", timezone.now()
        track.save(update_fields=["art_url", "art_source", "art_checked_at"])
        return {"art": art, "link": link, "source": "deezer"}

    title_sanitized = _strip_all_parens(_strip_featured(_sanitize_primary_tags(title)))
    art, link = _itunes_search(artist_name, title_sanitized)
    if art:
        track.art_url, track.art_source, track.art_checked_at = art, "itunes", timezone.now()
        track.save(update_fields=["art_url", "art_source", "art_checked_at"])
        return {"art": art, "link": link, "source": "itunes"}

    track.art_url, track.art_source, track.art_checked_at = "", "none", timezone.now()
    track.save(update_fields=["art_url", "art_source", "art_checked_at"])
    return _default_art_result()
