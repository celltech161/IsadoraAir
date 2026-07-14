"""CD ripping: disc identification via libdiscid, metadata lookup via
MusicBrainz, and orchestration of whipper for the actual bit-accurate
ripping. Kept in its own module (rather than views.py) so the ripping
logic stays testable without a request context, and so the /library/
import view file doesn't balloon."""

import subprocess
from pathlib import Path

DEFAULT_CD_DEVICE = "/dev/sr0"


class DiscNotFoundError(RuntimeError):
    """No disc in the tray (or the drive isn't ready). Distinct from
    MBLookupError so the frontend can differentiate 'insert a disc'
    from 'the disc is in but MusicBrainz has no record of it'."""


class MBLookupError(RuntimeError):
    """Disc was read successfully but MusicBrainz has no matching
    release for the disc ID. Operator can still rip -- the frontend
    just needs to fall back to a manual-entry tag form."""


def _configure_mb_client():
    """MusicBrainz asks that clients set a useragent so they can
    identify traffic sources -- do it here so both the detect
    endpoint and the rip job use the same string."""
    import musicbrainzngs
    musicbrainzngs.set_useragent(
        app="IsadoraAir",
        version="1.0",
        contact="kansasaerial@gmail.com",
    )


def read_disc_id(device=DEFAULT_CD_DEVICE):
    """Read the disc via libdiscid. Returns a `discid.Disc` object.
    Raises DiscNotFoundError with a human-readable message if the
    drive is empty or the read fails for any reason."""
    import discid
    try:
        return discid.read(device)
    except discid.DiscError as exc:
        raise DiscNotFoundError(f"Could not read disc from {device}: {exc}")


def lookup_mb_release(disc):
    """Look up the disc on MusicBrainz. Returns a dict:
    {mbid, album_title, album_artist, year, genre, tracks: [
        {position (1-based), title, artist, duration_seconds}, ...
    ]}. Raises MBLookupError if no release matches."""
    import musicbrainzngs
    _configure_mb_client()
    try:
        result = musicbrainzngs.get_releases_by_discid(
            disc.id,
            includes=["artists", "recordings"],
        )
    except musicbrainzngs.ResponseError as exc:
        # 404 -- disc read fine, but MB doesn't know it.
        raise MBLookupError(f"MusicBrainz has no release for disc {disc.id}: {exc}")
    except musicbrainzngs.WebServiceError as exc:
        # Network / server error -- distinct case; caller should retry
        # or fall back to manual entry, but the message differs from
        # "not found" so we surface the underlying reason.
        raise MBLookupError(f"MusicBrainz lookup failed: {exc}")

    releases = (result.get("disc", {}).get("release-list", [])
                or result.get("cdstub", {}).get("release-list", []))
    if not releases:
        raise MBLookupError(f"No MusicBrainz releases matched disc {disc.id}.")
    release = releases[0]
    mbid = release.get("id")

    album_title = release.get("title", "")
    album_artist = ""
    artist_credit = release.get("artist-credit") or []
    if artist_credit:
        # artist-credit is a list of {name, artist:{...}} plus join
        # phrases as bare strings -- join everything preserving order.
        album_artist = "".join(
            (item["name"] if isinstance(item, dict) else str(item))
            for item in artist_credit
        )

    year = None
    date = release.get("date") or ""
    if date:
        try:
            year = int(date[:4])
        except ValueError:
            year = None

    tracks = []
    # Find the medium (physical disc side) whose track list matches our
    # disc ID -- multi-disc releases return all mediums; only ours is
    # relevant to what's in the tray.
    for medium in release.get("medium-list", []):
        matched = False
        for disc_entry in medium.get("disc-list", []):
            if disc_entry.get("id") == disc.id:
                matched = True
                break
        if not matched and len(release.get("medium-list", [])) > 1:
            continue
        for track in medium.get("track-list", []):
            recording = track.get("recording", {}) or {}
            title = recording.get("title") or track.get("title", "")
            track_artist = ""
            for item in recording.get("artist-credit") or []:
                if isinstance(item, dict):
                    track_artist += item.get("name", "")
                else:
                    track_artist += str(item)
            duration_ms = int(track.get("length") or recording.get("length") or 0)
            tracks.append({
                "position": int(track.get("position") or track.get("number") or 0),
                "title": title,
                "artist": track_artist or album_artist,
                "duration_seconds": duration_ms / 1000.0 if duration_ms else None,
            })
        if matched:
            # Prefer the medium that positively matched our disc id.
            break

    return {
        "mbid": mbid,
        "album_title": album_title,
        "album_artist": album_artist,
        "year": year,
        "genre": "",
        "tracks": sorted(tracks, key=lambda t: t["position"]),
    }


def detect_disc(device=DEFAULT_CD_DEVICE):
    """Convenience wrapper: read disc + look up MB. Returns a dict
    with `disc_id` set, plus album/tracks if MB matched, or
    `mb_error` (string) if only the disc read worked. Raises
    DiscNotFoundError if the drive is empty."""
    disc = read_disc_id(device)
    payload = {
        "disc_id": disc.id,
        "n_tracks": len(disc.tracks),
        "device": device,
    }
    try:
        payload.update(lookup_mb_release(disc))
        payload["mb_matched"] = True
    except MBLookupError as exc:
        # Return the disc info anyway -- the frontend still has enough
        # to render an empty per-track entry form for manual tagging.
        payload["mb_matched"] = False
        payload["mb_error"] = str(exc)
        payload["tracks"] = [
            {"position": i + 1, "title": "", "artist": "",
             "duration_seconds": None}
            for i in range(len(disc.tracks))
        ]
    return payload


def eject(device=DEFAULT_CD_DEVICE):
    """Open the tray. Returns None on success; raises RuntimeError
    with the stderr on failure. `eject` is idempotent on an already-
    open tray so no need to check state first."""
    result = subprocess.run(
        ["eject", device],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"eject {device} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
