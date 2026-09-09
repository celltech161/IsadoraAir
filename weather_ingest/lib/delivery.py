"""Shared delivery helper for ported syndicated-show fetch scripts and
Weather's routine generated assets.

Replaces every original script's "mount NextKast Samba share, copy file"
tail: IsadoraAir's library now lives on this same box, so delivery is a
plain local hand-off to the main IsadoraAir app's own `manage.py
publish_weather_asset` command (weather/publication.py), run via
IsadoraAir's own venv/manage.py since this script's own venv has no
Django/library-app access at all. That command owns:

  1. Atomic replace (temp file + os.replace) -- the playback engine's
     deck loader (library/services/engine.py's _create_deck) checks
     Path(filepath).is_file() with no retry; landing on a partially
     written file would silently skip that queue item for good.
  2. Triggering fresh analysis via sync_track_file (library/management/
     commands/sync_track_file.py) -- import_songs never re-analyzes a
     same-path file replace, and analyze_tracks' default scope skips
     already-analyzed (but stale) tracks.
  3. (P1 2.4 Pass G) Last-known-good rollback: file + Track + analysis
     are all-or-nothing, and provenance is recorded for the published
     artifact -- see weather/publication.py's own docstring for the
     full sequence. Non-Weather ported-show callers of this same
     `deliver()` function do not pass the Weather-specific provenance
     kwargs and get plain publication with the same rollback safety.
"""

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# /opt/isadoraair is the canonical IsadoraAir application root (a real
# checkout or a symlink to one -- production has it as a symlink to
# /home/jreed/isadoraair-django; tooling must not assume which). Env-
# overridable so a differently-laid-out install doesn't require editing
# source (IsadoraAir 1.2 Phase 3 path audit, 2026-08-12) -- current
# production behavior is unchanged, since /opt/isadoraair already
# resolves to the exact same directory this constant used to hardcode.
ISADORAAIR_DIR = Path(os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair"))
ISADORAAIR_PYTHON = ISADORAAIR_DIR / "venv" / "bin" / "python"
LIBRARY_ROOT = Path(os.environ.get("LIBRARY_ROOT", "/srv/isadoraair/music"))


def deliver(
    local_path, category_code, filename, *,
    producer=None, voice=None, source_kind=None, source_age_seconds=None,
    used_fallback=False, alert_family=None,
):
    """Hand `local_path` (an already-rendered candidate file) to the
    main IsadoraAir app's `publish_weather_asset` command, which owns
    validation, staging, last-known-good rollback, Track sync, and (for
    Weather callers passing the optional kwargs below) provenance
    recording -- see weather/publication.py.

    Returns the final destination Path (LIBRARY_ROOT/<category_code>/
    <filename>). Raises subprocess.CalledProcessError on any
    publication failure -- callers (each show's run() function)
    already wrap their own pipeline in try/except and email-alert on
    failure, so this deliberately doesn't swallow errors itself. On
    failure, the previous last-known-good artifact (if any) is left
    exactly as it was; nothing here needs to clean up on this side,
    since publish_weather_asset() never leaves partial state behind.

    A SUCCESSFUL publish (return code 0) can still carry a non-fatal
    provenance-write warning on stderr (P1 2.4 Pass G -- see
    publish_weather_asset's own docstring: a provenance failure never
    undoes an otherwise-successful audio/Track publish). That warning
    would otherwise be invisible -- capture_output swallows it and a
    clean return code raises nothing -- so any non-empty stderr on a
    successful run is logged here at WARNING level, reaching whichever
    calling script's own log/journal via the standard `logging` module
    (every entry point in this project either configures the root
    logger itself, in which case this propagates there, or configures
    none at all, in which case Python's own last-resort handler prints
    it to stderr) rather than being silently discarded. A genuinely
    clean run has empty stderr and logs nothing extra.

    The optional kwargs are Weather-specific provenance metadata
    (P1 2.4 Pass G) -- omit them entirely for a non-Weather caller of
    this same helper; the command defaults every one to blank/None.
    """
    dest_path = LIBRARY_ROOT / category_code / filename

    cmd = [
        str(ISADORAAIR_PYTHON), "manage.py", "publish_weather_asset",
        str(local_path), category_code, filename,
    ]
    if producer:
        cmd += ["--producer", str(producer)]
    if voice:
        cmd += ["--voice", str(voice)]
    if source_kind:
        cmd += ["--source-kind", str(source_kind)]
    if source_age_seconds is not None:
        cmd += ["--source-age-seconds", str(source_age_seconds)]
    if used_fallback:
        cmd += ["--used-fallback"]
    if alert_family:
        cmd += ["--alert-family", str(alert_family)]

    result = subprocess.run(
        cmd,
        cwd=str(ISADORAAIR_DIR),
        check=True,
        capture_output=True,
        text=True,
    )

    if result.stderr:
        log.warning(
            "publish_weather_asset reported a non-fatal warning for %s/%s: %s",
            category_code, filename, result.stderr.strip(),
        )

    return dest_path
