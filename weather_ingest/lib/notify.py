"""Failure-notification helper for the weather-ingest scripts, via
Django's own EMAIL_* settings (send_weather_notification management
command) instead of a separate smtplib/credential-file setup.
"""

import os
import subprocess
from pathlib import Path

# See lib/delivery.py's identical constant for the full rationale
# (IsadoraAir 1.2 Phase 3 path audit, 2026-08-12) -- unchanged current
# behavior, just portable to a different install layout.
ISADORAAIR_DIR = Path(os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair"))
ISADORAAIR_PYTHON = ISADORAAIR_DIR / "venv" / "bin" / "python"


def notify(subject, body):
    try:
        subprocess.run(
            [str(ISADORAAIR_PYTHON), "manage.py", "send_weather_notification", subject, body],
            cwd=str(ISADORAAIR_DIR),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception as e:
        print(f"[WARN] Failed to send notification: {e}")
