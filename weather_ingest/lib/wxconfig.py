"""Read Django-owned weather configuration from the companion venv.

``WEATHER_DATA_DIR`` deliberately uses this same narrow management-command
bridge.  That keeps IsadoraAir's admin-editable setting authoritative without
loading its complete (secret-bearing) ``.env`` into companion processes.
"""

import json
import os
import subprocess
from functools import lru_cache
from pathlib import Path

# See lib/delivery.py's identical constant for the full rationale
# (IsadoraAir 1.2 Phase 3 path audit, 2026-08-12) -- unchanged current
# behavior, just portable to a different install layout.
ISADORAAIR_DIR = Path(os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair"))
ISADORAAIR_PYTHON = ISADORAAIR_DIR / "venv" / "bin" / "python"
DEFAULT_WEATHER_DATA_DIR = Path("/var/lib/isadoraair/weather")


@lru_cache(maxsize=1)
def load_weather_config():
    result = subprocess.run(
        [str(ISADORAAIR_PYTHON), "manage.py", "dump_weather_config"],
        cwd=str(ISADORAAIR_DIR),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


@lru_cache(maxsize=1)
def load_amber_alert_config():
    """Companion to load_weather_config for the AMBER/BLU/MEP pipeline.
    Same cross-venv shape."""
    result = subprocess.run(
        [str(ISADORAAIR_PYTHON), "manage.py", "dump_amber_alert_config"],
        cwd=str(ISADORAAIR_DIR),
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def resolve_weather_data_dir(config=None, *, create=True):
    """Return the one Django-configured weather runtime-data directory.

    ``config`` lets callers which already loaded ``dump_weather_config`` reuse
    that payload. Other entry points fetch it through the same narrow bridge,
    so direct/manual execution has the same behavior as systemd execution.
    An older IsadoraAir checkout without the new payload key converges on the
    canonical default; it must never fall back into this source checkout.
    """
    if config is None:
        config = load_weather_config()
    configured = config.get("weather_data_dir")
    data_dir = Path(configured) if configured else DEFAULT_WEATHER_DATA_DIR
    if not data_dir.is_absolute():
        raise ValueError(f"WEATHER_DATA_DIR must be absolute, got {configured!r}")
    if create:
        data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir
