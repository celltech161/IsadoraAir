"""Asks systemd to run one road-audio generation check.

deploy/isadoraair-generate-road-condition-audio.path watches this file
(PathChanged=) and starts the existing generator service when it is
replaced. sync_road_conditions replaces it only after a real upstream
retrieval has been committed, so this is the only ordinary automatic
trigger. It needs no privilege beyond writing the station's own runtime
directory.

A request made while the generator is already running is not queued; the
next successful retrieval makes another."""
import logging
import os
from pathlib import Path

from django.utils import timezone as dj_timezone

logger = logging.getLogger(__name__)

# Must match PathChanged= in the .path unit.
TRIGGER_PATH = Path("/run/isadoraair/road_generation.trigger")


def request_generation():
    """True if the request was written. Never raises."""
    tmp = TRIGGER_PATH.with_name(f".{TRIGGER_PATH.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(f"{dj_timezone.now().isoformat()}\n", encoding="utf-8")
        os.replace(tmp, TRIGGER_PATH)
        return True
    except OSError as exc:
        logger.warning("road_conditions: could not request road audio generation: %s", exc)
        return False
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
