"""Test support for the combined Weather/AMBER announcement (r0137).

Writes the canonical ``LIBRARY_ROOT/WxAlert/wx_alert.flac`` (synthetic bytes or a caller-supplied file) the way
``publish_weather_asset`` does -- a same-directory copy then an atomic rename, mtime preserved -- and the
coordinator's ``wx_alert_state.json`` describing it.  Never touches a real station path: callers pass temporary
directories that are also installed as ``LIBRARY_ROOT`` / ``WEATHER_DATA_DIR``.
"""
from __future__ import annotations

import os
import shutil
from datetime import timedelta
from pathlib import Path

from django.utils import timezone

from isadoraair import wx_alert_state


def publish_file(library_root, payload: bytes | None = None, *, source: Path | None = None, mtime_ns=None):
    """Atomically replace the canonical file (new inode), like publish_weather_asset.  Returns its path."""
    path = wx_alert_state.audio_path(Path(library_root).resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / f".{path.name}.candidate-{os.getpid()}"
    if source is not None:
        shutil.copy2(source, staging)
    else:
        staging.write_bytes(payload if payload is not None else b"announcement")
    if mtime_ns is not None:
        os.utime(staging, ns=(mtime_ns, mtime_ns))
    staging.replace(path)
    return path


def write_state(data_dir, library_root, *, generation=1, active=True, valid_for=timedelta(hours=1),
                published_at=None, audio="current", **extra):
    """The coordinator's record for the file currently at the canonical path (``audio="current"``)."""
    now = timezone.now()
    identity = (
        wx_alert_state.audio_identity(wx_alert_state.audio_path(Path(library_root).resolve()))
        if audio == "current" else audio
    )
    state = {
        "generation": generation,
        "fingerprint": f"fp-{generation}" if active else "",
        "active": active,
        "baseline": False,
        "alerts": [],
        "valid_until": wx_alert_state.format_ts(now + valid_for) if active and valid_for is not None else None,
        "published_at": wx_alert_state.format_ts(published_at or now) if active else None,
        "audio": identity if active else None,
        "last_error": None,
    }
    state.update(extra)
    wx_alert_state.write_state(wx_alert_state.state_path(data_dir), state)
    return state


def make_track(library_root, category, artist=None, *, duration=30.0, next_start=29.0, ready2air=True):
    """The canonical announcement Track (publishes placeholder audio if none exists yet)."""
    from library.models import Track

    path = wx_alert_state.audio_path(Path(library_root).resolve())
    if not path.exists():
        publish_file(library_root)
    return Track.objects.create(
        title="Weather Alert", artist=artist, category=category, ready2air=ready2air,
        filepath=str(path), filename=path.name, duration_seconds=duration, next_start_seconds=next_start,
    )


def announce(data_dir, library_root, payload: bytes | None = None, *, generation=1, **kwargs):
    """Publish new announcement audio and record it as ``generation`` (published now)."""
    publish_file(library_root, payload if payload is not None else f"generation {generation}".encode())
    return write_state(data_dir, library_root, generation=generation, **kwargs)
