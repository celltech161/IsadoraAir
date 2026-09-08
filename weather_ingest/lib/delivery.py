"""Shared delivery helper for ported syndicated-show fetch scripts.

Replaces every original script's "mount NextKast Samba share, copy file"
tail: IsadoraAir's library now lives on this same box, so delivery is a
plain local write. Two things this handles that a plain shutil.copy
wouldn't:

  1. Atomic replace (temp file + os.replace) -- the playback engine's
     deck loader (library/services/engine.py's _create_deck) checks
     Path(filepath).is_file() with no retry; landing on a partially
     written file would silently skip that queue item for good.
  2. Triggering fresh analysis via IsadoraAir's own venv/manage.py --
     import_songs never re-analyzes a same-path file replace, and
     analyze_tracks' default scope skips already-analyzed (but stale)
     tracks. sync_track_file (library/management/commands/
     sync_track_file.py) always recomputes waveform/cue points, run
     right here at ingest time rather than deferred into the real-time
     playback engine.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

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


def deliver(local_path, category_code, filename):
    """Move `local_path` into LIBRARY_ROOT/<category_code>/<filename>
    (atomically) and trigger a fresh sync_track_file run for it.

    Returns the final destination Path. Raises on any failure -- callers
    (each show's run() function) already wrap their own pipeline in
    try/except and email-alert on failure, so this deliberately doesn't
    swallow errors itself.
    """
    dest_dir = LIBRARY_ROOT / category_code
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / filename

    # Temp file in the SAME directory as the final destination, so the
    # final os.replace() below is a same-filesystem rename (atomic)
    # regardless of where local_path came from. shutil.copy2 (not
    # os.replace) gets it there since local_path is typically under
    # /tmp -- a different filesystem than /srv/isadoraair/music, and
    # os.replace/os.rename raise OSError across filesystems (no
    # copy-then-delete fallback, unlike shutil.move).
    tmp_path = dest_dir / f".{filename}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        shutil.copy2(str(local_path), str(tmp_path))
        os.replace(str(tmp_path), str(dest_path))
    finally:
        tmp_path.unlink(missing_ok=True)

    subprocess.run(
        [str(ISADORAAIR_PYTHON), "manage.py", "sync_track_file", str(dest_path)],
        cwd=str(ISADORAAIR_DIR),
        check=True,
        capture_output=True,
        text=True,
    )

    return dest_path
