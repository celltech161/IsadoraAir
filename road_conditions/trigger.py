"""Wake the road generator. The edge is disposable, the work is not."""
import logging
import os
from pathlib import Path

from django.utils import timezone as dj_timezone

from .synthesis import road_report_path, road_report_pending_path

logger = logging.getLogger(__name__)
TRIGGER_PATH = Path("/run/isadoraair/road_generation.trigger")
MAX_AUTOMATIC_RECOVERY_ATTEMPTS = 3


def _write_stamp(path, value):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(value + "\n", encoding="ascii")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_stamp(path):
    try:
        return path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return ""


def request_generation(*, recovery_only=False):
    mode = "recover" if recovery_only else "generate"
    try:
        _write_stamp(TRIGGER_PATH, f"{mode} {dj_timezone.now().isoformat()}")
        return True
    except OSError as exc:
        logger.warning("road_conditions: could not wake the road generator: %s", exc)
        return False


def dispatch_mode():
    return _read_stamp(TRIGGER_PATH).partition(" ")[0]


def pending_sync(config):
    """The committed fetch timestamp is the generation request.

    An acknowledgment describes exactly the snapshot the worker took under
    its lock. The sync can advance the timestamp while that worker runs.
    """
    if config.last_fetch_succeeded_at is not None:
        ticket = config.last_fetch_succeeded_at.isoformat()
        if ticket != _read_stamp(road_report_path().with_name("road_generation.ack")):
            return ticket
    return None


def acknowledge_sync(ticket):
    path = road_report_path().with_name("road_generation.ack")
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_stamp(path, ticket)


def recovery_due(*, claim=False):
    """Limit automatic retries per journal per boot. A claim requires the generator lock."""
    try:
        info = road_report_pending_path().stat()
    except FileNotFoundError:
        return False
    witness = f"{info.st_dev}:{info.st_ino}:{info.st_mtime_ns}"
    state = TRIGGER_PATH.with_suffix(".recovery")
    previous, _, attempts = _read_stamp(state).partition(" ")
    count = int(attempts) if previous == witness and attempts.isdecimal() else 0
    if count >= MAX_AUTOMATIC_RECOVERY_ATTEMPTS:
        return False
    if claim:
        _write_stamp(state, f"{witness} {count + 1}")
    return True
