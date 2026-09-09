"""Generated-Weather-asset provenance -- P1 2.4 Pass G.

One small sidecar JSON file per generated artifact, written atomically
by weather/publication.py immediately after a successful publish.
Deliberately one sidecar PER artifact under WEATHER_DATA_DIR/provenance/
rather than one shared document multiple independent producer timers
(current_temp.py, wx_forecast.py x2 modes, wx_alert.py, amber_alert.py)
could race on writing.

Contains only operational facts -- never speech text or secrets:
schema version, category/artifact identity, canonical final path,
producer, generation timestamp (UTC), logical StationTTSVoice name,
the final artifact's own SHA-256, source kind, source timestamp/age,
and whether a fallback/cache was used. WxAlert additionally records
which alert family produced it (nws_watch_warning / ipaws_amber).

A missing sidecar (a legacy artifact published before this existed, or
before its producer's first post-r0057 regeneration) is NOT an error
-- weather.diagnostics reports that as `degraded`, not
`needs_attention`; see diagnostics.py's _apply_provenance_overlay().
A provenance WRITE failure must be visible (the caller logs/notifies)
but must never undo an otherwise successfully published playable
asset -- see publication.py's best-effort call site for that
contract.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
PROVENANCE_SUBDIR = "provenance"

_HASH_CHUNK_BYTES = 1024 * 1024


def provenance_path(data_dir: Path, category_code: str) -> Path:
    return Path(data_dir) / PROVENANCE_SUBDIR / f"{category_code}.json"


def sha256_of(path: Path) -> str:
    """Streams the file in fixed-size chunks rather than reading it
    whole -- Weather artifacts are short clips (well under a few MB),
    but there's no reason to hold the whole thing in memory just to
    hash it."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_provenance(
    data_dir,
    *,
    category_code: str,
    filename: str,
    final_path,
    producer: str,
    generated_at: str,
    voice: str = "",
    source_kind: str = "",
    source_age_seconds: float | None = None,
    used_fallback: bool = False,
    alert_family: str | None = None,
) -> Path:
    """Writes the sidecar atomically (temp file + os.replace, same
    directory as the final sidecar so the replace is same-filesystem).
    Computes the artifact's own SHA-256 at write time -- callers MUST
    call this only after `final_path` is the actual published file, so
    the recorded hash is truthful.

    Raises on failure (missing/unreadable final_path, directory not
    writable, etc.) -- callers must treat this as best-effort
    diagnostics: catch this, log/notify it, but never let it undo an
    otherwise-successful publish."""
    final_path = Path(final_path)
    path = provenance_path(data_dir, category_code)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "category_code": category_code,
        "filename": filename,
        "final_path": str(final_path),
        "producer": producer,
        "generated_at": generated_at,
        "voice": voice,
        "sha256": sha256_of(final_path),
        "source_kind": source_kind,
        "source_age_seconds": source_age_seconds,
        "used_fallback": used_fallback,
        "alert_family": alert_family,
    }
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.tmp"
    try:
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return path


def read_provenance(data_dir, category_code: str):
    """Returns (payload_dict, error). error is None on success; a
    short string describing why reading failed otherwise ("missing" /
    "malformed JSON: ..."). Never raises -- diagnostics.py must stay
    side-effect-free and exception-free for ordinary missing/malformed
    data, exactly like its other _load_json_file() reads."""
    path = provenance_path(data_dir, category_code)
    if not path.is_file():
        return None, "missing"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"malformed JSON: {exc}"
