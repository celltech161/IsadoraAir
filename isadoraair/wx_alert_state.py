"""Combined Weather/AMBER spoken-alert state, shared by the producer and the engine.

The single announcement Track (``LIBRARY_ROOT/WxAlert/wx_alert.flac``) is rebuilt by ONE coordinator
(``weather_ingest/wx_alert.py``) from both source snapshots: NWS watches/warnings and AMBER-family alerts.
After each decision the coordinator writes a small state record next to the snapshots
(``WEATHER_DATA_DIR/wx_alert_state.json``).  The playback engine reads the same record, locally and without
network access, when it reconciles an ``insert_urgent`` WxAlert nudge and again at the final deck-open boundary.

This module is pure Python on purpose: ``weather_ingest`` runs in its own virtualenv without Django and imports
it the same way it imports ``isadoraair.engine_commands``.

The record is the authority for WHAT should air:

* ``generation`` -- a monotonic logical identity, bumped on every published content change and on deactivation.
  It is never a filename.
* ``active`` -- an announcement for this generation is current and may begin playback.
* ``valid_until`` -- the earliest authoritative source expiration of the included alerts.  Nothing begins
  playback at or after it, even when both pollers have stopped.
* ``published_at`` -- when this generation's audio became the published file.  A playback claim
  (``LogItem.playback_claimed_at``) at or after it proves the generation was handed to a deck: the engine opens
  the file only while this record names that file (see ``audio``) and while holding the publication lock
  shared, so an earlier generation's claim always precedes the later generation's ``published_at``.
* ``audio`` -- the published file's (size, mtime_ns).  Used only to VALIDATE that the file a deck is about to
  open is the one this record describes; never as the logical delivery identity.  ``publish_weather_asset``
  copies with ``shutil.copy2`` (mtime preserved) and rolls back by restoring that copy, so a rollback keeps the
  identity while a new publication changes it.

Delivery (whether a generation aired) is deliberately NOT recorded here: the engine derives it from its own
persisted playback claims, which survive an engine restart.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
STATE_FILENAME = "wx_alert_state.json"
CATEGORY_CODE = "WxAlert"
AUDIO_FILENAME = "wx_alert.flac"


def state_path(data_dir) -> Path:
    return Path(data_dir) / STATE_FILENAME


def audio_path(library_root) -> Path:
    """The one canonical announcement file (and Track filepath)."""
    return Path(library_root) / CATEGORY_CODE / AUDIO_FILENAME


def format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_ts(value):
    """An aware datetime, or None for missing/unparseable input (callers fail closed)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def read_state(path):
    """``(state_dict, None)`` or ``(None, "missing" | "malformed: ...")``.  Never raises for ordinary
    missing/corrupt data."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "missing"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"malformed: {exc}"
    if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
        return None, "malformed: unexpected schema"
    return data, None


def write_state(path, state: dict) -> None:
    """Atomic replace (same-directory temp file + fsync + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(state, schema=SCHEMA_VERSION)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def audio_identity(path):
    """``{"size", "mtime_ns"}`` of the file at ``path`` now, or None when it does not exist."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


@dataclass(frozen=True)
class PlaybackDecision:
    allowed: bool
    reason: str | None = None
    published_at: datetime | None = None
    generation: int | None = None


def playback_decision(state, identity, now: datetime) -> PlaybackDecision:
    """May the CURRENT published announcement begin playback now?  Database- and network-free.

    Fails closed: a missing/unknown record, an inactive or expired generation, or a file that is not the one
    the record describes never authorizes playback."""
    if not isinstance(state, dict) or state.get("schema") != SCHEMA_VERSION:
        return PlaybackDecision(False, "no combined Weather/AMBER alert state")
    generation = state.get("generation")
    if not state.get("active"):
        return PlaybackDecision(False, f"no Weather/AMBER alert is active (generation {generation})")
    valid_until = parse_ts(state.get("valid_until"))
    if valid_until is None:
        return PlaybackDecision(False, f"alert generation {generation} has no validity deadline")
    if now >= valid_until:
        return PlaybackDecision(False, f"alert generation {generation} expired at {state.get('valid_until')}")
    published_at = parse_ts(state.get("published_at"))
    if published_at is None:
        return PlaybackDecision(False, f"alert generation {generation} has no publication time")
    expected = state.get("audio")
    if not isinstance(expected, dict) or identity is None or (
        identity.get("size"), identity.get("mtime_ns")
    ) != (expected.get("size"), expected.get("mtime_ns")):
        return PlaybackDecision(
            False, f"published alert audio is not the audio of generation {generation} (publication pending)"
        )
    return PlaybackDecision(True, None, published_at, generation)


@contextmanager
def shared_publication_lock(lock_path):
    """Non-blocking SHARED flock on the publication's own destination lock file.

    ``weather.publication.publish_weather_asset`` holds the same file EXCLUSIVELY from the backup through the
    file replacement, Track/analysis commit (or rollback) and provenance.  Holding it shared while a deck is
    authorized and its ``filesrc`` opens the file means the deck sees either the previous committed publication
    or the next one -- never a file whose Track row is not yet committed or that is about to be rolled back.
    Yields False (never blocks) when a publication currently holds it."""
    fd = os.open(str(lock_path), os.O_RDONLY | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
