"""Aircheck recorder -- telnet client to the liquidsoap-hosted
output.file (see encoders/services/encoder_manager.py's
_aircheck_block).

Design shift from the original ffmpeg-per-session subprocess model:
liquidsoap owns a single always-running output.file that consumes the
same in-process source the icecast/shoutcast outputs do. This module
just tells liquidsoap to cut a fresh working file (via
`aircheck.reopen` over telnet), and moves that working file to the
session's real destination on Stop. No subprocess ownership; no
dsnoop contention with the encoders.

Fixed-path-then-move (rather than a runtime-controlled getter): tried
the getter approach live -- output.file's .reopen() does not
re-invoke its filename getter on this liquidsoap version; writes stop
entirely after the first reopen. The working-file approach sidesteps
that whole class of issue -- liquidsoap always writes to one path,
and this module is responsible for shuffling files into their final
homes.

ffmpeg_pid is preserved on AircheckSession for backward compatibility
with old rows but is always None on new sessions.

Idle working-buffer maintenance (maintain_idle_buffer): because
output.file above runs continuously -- even with no session active --
the tmpfs working file at AIRCHECK_CURRENT_PATH grows without bound
until the next Start/Stop cuts it. maintain_idle_buffer periodically
rolls it over via the SAME aircheck.reopen telnet call Start/Stop
already use, purely to bound tmpfs growth, never touching
AircheckSession. See AIRCHECK_LOCK_PATH below for how this is kept
safe to run concurrently with a real Start/Stop.
"""
import errno
import fcntl
import os
import shutil
import socket
import subprocess
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from django.db import close_old_connections
from django.utils import timezone

from aircheck.models import AircheckConfig, AircheckSession
from encoders.services.encoder_manager import (
    AIRCHECK_CURRENT_PATH,
    AIRCHECK_OUTPUT_ID,
    AIRCHECK_TELNET_HOST,
    AIRCHECK_TELNET_PORT,
)
from monitoring.models import emit_event


REMUX_PENDING_NOTE = "remux in progress"
REMUX_INTERMEDIATE_DIR = Path("/run/isadoraair")

# Cross-process advisory lock serializing every operation that touches
# the aircheck working file or AircheckSession state: Start, Stop, and
# idle-buffer maintenance all go through _aircheck_lock. Start/Stop
# acquire it BLOCKING (they must eventually run); idle maintenance
# acquires it NON-BLOCKING and just skips this cycle if a real
# operation currently owns it -- a missed idle rollover costs nothing,
# it retries next cycle, and it must never make a Start/Stop press
# wait on it.
AIRCHECK_LOCK_PATH = "/run/isadoraair/aircheck.lock"

# Safety ceiling for the always-on idle working buffer (see module
# docstring). 64 MiB is generous headroom above a realistic per-minute
# maintenance cadence's worth of growth for any configured aircheck
# format, while still bounding tmpfs exhaustion risk if the timer is
# ever delayed or disabled for a while.
AIRCHECK_IDLE_BUFFER_MAX_BYTES = 64 * 1024 * 1024


@contextmanager
def _aircheck_lock(blocking):
    """Yields True once AIRCHECK_LOCK_PATH's flock is held, or False if
    blocking=False and another process currently holds it. Blocking
    acquisition always eventually yields True (or raises on a genuine
    OS error) -- callers that pass blocking=True don't need to check
    the yielded value.

    A plain flock on a fixed-path lockfile, not a DB-side lock: this
    must work even when the DB is briefly unavailable (idle
    maintenance already treats "can't reach liquidsoap" as a safe
    failure; the lock itself shouldn't add a second DB dependency),
    and it needs to be held across the same-process Start/Stop calls
    that are themselves plain function calls, not a task queue."""
    fd = os.open(AIRCHECK_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            if not blocking and exc.errno in (errno.EACCES, errno.EAGAIN):
                yield False
                return
            raise
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


TELNET_TIMEOUT_SECONDS = 3.0
# Liquidsoap's telnet server terminates every response with CRLF -- the
# marker really is "END\r\n", not "END\n". Missing the \r locks the
# reader in recv() until it hits the socket timeout.
TELNET_TERMINATOR = b"END\r\n"


class TelnetError(RuntimeError):
    pass


def _send_telnet(*commands):
    """Send one or more line-terminated commands to liquidsoap's telnet
    server and return the concatenated response text (with END markers
    stripped). Raises TelnetError on connection failure or timeout.

    Uses a fresh socket per call rather than pooling -- liquidsoap
    handles connect/close cleanly, telnet commands are cheap, and a
    per-call socket avoids the "connection went stale after encoders
    restarted" problem entirely."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TELNET_TIMEOUT_SECONDS)
    try:
        try:
            sock.connect((AIRCHECK_TELNET_HOST, AIRCHECK_TELNET_PORT))
        except (ConnectionRefusedError, TimeoutError, OSError) as exc:
            # ECONNREFUSED = encoders service down; ENETUNREACH = wrong
            # host somehow. Either way the operator sees the encoders
            # need to come up before aircheck can work.
            raise TelnetError(
                f"cannot reach liquidsoap telnet ({AIRCHECK_TELNET_HOST}:"
                f"{AIRCHECK_TELNET_PORT}): {exc}"
            )

        responses = []
        for cmd in commands:
            payload = (cmd.rstrip("\n") + "\n").encode("utf-8")
            try:
                sock.sendall(payload)
            except OSError as exc:
                raise TelnetError(f"telnet send failed: {exc}")
            responses.append(_recv_until_end(sock))

        # Bye is best-effort -- if it fails, response is already collected.
        try:
            sock.sendall(b"quit\n")
        except OSError:
            pass
        return "\n".join(responses).strip()
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _recv_until_end(sock):
    """Read from the socket until the b'END\\n' line terminator, return
    the response body (everything before END, trailing newline stripped).
    Raises TelnetError on timeout or unexpected disconnect."""
    buf = b""
    while TELNET_TERMINATOR not in buf:
        try:
            chunk = sock.recv(4096)
        except TimeoutError:
            raise TelnetError("telnet read timed out")
        except OSError as exc:
            raise TelnetError(f"telnet read failed: {exc}")
        if not chunk:
            raise TelnetError("telnet closed before END marker")
        buf += chunk
    body = buf.split(TELNET_TERMINATOR, 1)[0]
    return body.decode("utf-8", errors="replace").rstrip("\n")


def current_session():
    """Return the currently-running AircheckSession or None. Pure DB
    read, no side effects -- called on every dashboard status poll, so
    it must never mutate a row out from under a real session. Liquidsoap
    keeps writing the working file regardless of whether a still_running
    row exists or not, and Gunicorn/Django restarting does not imply any
    Aircheck session actually stopped -- there is no reconciliation to
    do here beyond reading the DB as-is."""
    return AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()


def start_recording():
    """Start a new aircheck session. Returns (session, error) with one
    being None. Genuinely idempotent: if a session is already running,
    the caller gets that SAME session back with "already recording" --
    no telnet call, no new row, the existing row is not touched in any
    way.

    There is deliberately no automatic "stale session" detection here.
    The fixed-path working file is always being written by Liquidsoap
    regardless of session state, so a still_running=True row can't be
    distinguished from a genuinely active session by inspecting the
    file -- and Liquidsoap's own output.file keeps running independently
    of Django/Gunicorn, so a still_running=True row surviving a Django
    restart does not mean it's abandoned. Treating every still_running
    row as stale at Start time (the previous behavior here) meant a
    second Start press -- a double-click, or two admins -- silently cut
    the in-progress recording and replaced it with a fresh one. If
    explicit stale-session recovery is ever needed, it belongs in its
    own deliberate mechanism, not as a side effect of every Start.

    Triggers `aircheck.reopen` over telnet -- liquidsoap closes its
    current working file (AIRCHECK_CURRENT_PATH) and starts a fresh
    one at the same path. The session row records the INTENDED final
    destination; the actual file lives at AIRCHECK_CURRENT_PATH until
    Stop moves it there.

    Runs under AIRCHECK_LOCK_PATH, blocking, for its entire body --
    serializes against Stop, against a second concurrent Start (the
    existing-session check below only works if two overlapping calls
    can't both pass it before either creates a row), and against
    idle-buffer maintenance, so nothing else can reopen/inspect the
    working file mid-Start."""
    with _aircheck_lock(blocking=True):
        existing = AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()
        if existing:
            return existing, "already recording"

        cfg = AircheckConfig.load()
        out_dir = Path(cfg.output_directory)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return None, f"cannot create output directory {out_dir}: {exc}"

        stamp = timezone.localtime().strftime(cfg.filename_template)
        out_path = out_dir / f"{stamp}.{cfg.file_extension()}"

        # Guard against second-precision collisions on rapid Start clicks.
        if out_path.exists():
            out_path = out_dir / f"{stamp}-{datetime.now().microsecond}.{cfg.file_extension()}"

        try:
            _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
        except TelnetError as exc:
            return None, f"liquidsoap telnet: {exc}"

        session = AircheckSession.objects.create(
            filename=str(out_path),
            audio_format=cfg.audio_format,
            bitrate=cfg.effective_bitrate(),
            source_device=cfg.source_device,
            ffmpeg_pid=None,  # legacy field, always None on new sessions
            still_running=True,
        )
        return session, None


def stop_recording():
    """Stop the currently-running session, if any. Returns
    (session, error). Triggers a fresh reopen so the working file
    gets closed cleanly, then finalizes the just-closed file to the
    session's destination.

    Finalization branches on format:
      - mp3/flac/wav: synchronous shutil.move from tmpfs to disk.
        Blocks for the duration of the copy (~1s per hundred MB on
        a spinning disk).
      - he_aac: fdkaac has been writing ADTS-framed AAC to the
        working file, which is streamable but not the .m4a container
        the session's dest expects. Move the ADTS working file to a
        session-tagged intermediate name in /run (near-instant on
        tmpfs), mark the session as ended with a "remux pending"
        note, and hand off to a daemon thread that ffmpeg-remuxes
        (ADTS -> m4a, `-c copy`, no re-encode) and updates the row
        on completion. Stop returns to the caller within a few ms
        rather than waiting seconds for the remux.

        Runs under AIRCHECK_LOCK_PATH, blocking, for the synchronous
        portion only -- through the point the working file has been
        moved/renamed off AIRCHECK_CURRENT_PATH and the session row
        updated. The async he_aac remux thread (which only ever
        touches its own intermediate file, never AIRCHECK_CURRENT_PATH)
        deliberately runs AFTER the lock is released -- holding it for
        up to ffmpeg's 600s timeout would starve idle maintenance and
        any subsequent Start/Stop for far too long."""
    with _aircheck_lock(blocking=True):
        session = AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()
        if session is None:
            return None, "no active session"

        telnet_note = ""
        try:
            _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
        except TelnetError as exc:
            # Working file may still be flushed on close by whatever's
            # left of liquidsoap; we can still attempt the finalization
            # below.
            telnet_note = f"telnet reopen failed at Stop: {exc}; "

        working = Path(AIRCHECK_CURRENT_PATH)
        dest = Path(session.filename)
        session.still_running = False
        session.ended_at = timezone.now()

        if session.audio_format == "he_aac":
            _finalize_he_aac_async(session, working, dest, telnet_note)
        else:
            _finalize_direct_move(session, working, dest, telnet_note)

        return session, None


def _finalize_direct_move(session, working, dest, telnet_note):
    """Move the working file straight to dest -- correct for
    mp3/flac/wav where the container is already what we want. Blocks
    on the copy (few hundred MB/s from tmpfs to disk)."""
    move_note = ""
    if working.is_file():
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(working), str(dest))
        except OSError as exc:
            move_note = f"move {working} -> {dest} failed: {exc}"
    else:
        move_note = f"working file {working} was missing at Stop -- no audio to move"
    if dest.is_file():
        try:
            session.size_bytes = dest.stat().st_size
        except OSError:
            pass
    session.exit_note = (telnet_note + move_note).strip("; ") or ""
    session.save()


def _finalize_he_aac_async(session, working, dest, telnet_note):
    """Move the working ADTS to a session-tagged intermediate name so a
    subsequent Start's reopen doesn't clobber it, mark the row as
    "remux pending," and hand off to a daemon thread that runs
    `ffmpeg -c copy` to swap ADTS -> m4a container. Stop returns
    immediately; the row's size_bytes and cleared exit_note fill in
    when the thread completes."""
    if not working.is_file():
        session.exit_note = (telnet_note + f"working file {working} was missing at Stop -- no audio to remux").strip("; ")
        session.save()
        return

    # Intermediate lives on the same tmpfs so this rename is atomic
    # and near-instant, no matter how large the ADTS file is.
    intermediate = REMUX_INTERMEDIATE_DIR / f"aircheck-remux-{session.id}.aac"
    try:
        working.rename(intermediate)
    except OSError as exc:
        session.exit_note = (telnet_note + f"could not stage intermediate: {exc}").strip("; ")
        session.save()
        return

    session.exit_note = (telnet_note + REMUX_PENDING_NOTE).strip("; ")
    session.save()

    t = threading.Thread(
        target=_remux_worker,
        args=(session.id, str(intermediate), str(dest), telnet_note),
        daemon=True,
        name=f"aircheck-remux-{session.id}",
    )
    t.start()


def _remux_worker(session_id, intermediate_path, dest_path, telnet_note):
    """Runs in a daemon thread: ffmpeg-remuxes ADTS -> m4a with -c copy,
    unlinks the intermediate on success, and updates the AircheckSession
    row via a fresh DB connection. Failures preserve the intermediate
    on-disk for manual recovery (an ADTS .aac file is playable directly
    -- an operator can rename or manually remux) and record the ffmpeg
    error in exit_note.

    close_old_connections at both ends because Django's per-thread DB
    connection cache would otherwise reuse a possibly-stale gunicorn-
    worker connection or leak this thread's connection at exit."""
    close_old_connections()
    intermediate = Path(intermediate_path)
    dest = Path(dest_path)
    try:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                    "-i", str(intermediate),
                    "-c", "copy",
                    str(dest),
                ],
                capture_output=True, text=True, timeout=600, check=True,
            )
        except subprocess.CalledProcessError as exc:
            _mark_remux_failed(session_id, telnet_note, f"ffmpeg exit {exc.returncode}: {(exc.stderr or '').strip()[:300]}")
            return
        except subprocess.TimeoutExpired:
            _mark_remux_failed(session_id, telnet_note, "ffmpeg timed out after 600s")
            return
        except (OSError, FileNotFoundError) as exc:
            _mark_remux_failed(session_id, telnet_note, f"ffmpeg spawn failed: {exc}")
            return

        # Success: unlink intermediate, stat dest, clear the pending
        # note. Losing the intermediate now is safe -- the dest is a
        # complete m4a.
        try:
            intermediate.unlink()
        except OSError:
            pass
        try:
            size = dest.stat().st_size if dest.is_file() else None
        except OSError:
            size = None
        try:
            s = AircheckSession.objects.get(id=session_id)
            s.size_bytes = size
            s.exit_note = telnet_note.strip("; ") or ""
            s.save(update_fields=["size_bytes", "exit_note"])
        except AircheckSession.DoesNotExist:
            pass
    finally:
        close_old_connections()


def _mark_remux_failed(session_id, telnet_note, err):
    """Record an ffmpeg-side failure on the session row without touching
    the intermediate on disk -- the ADTS file stays put for manual
    recovery."""
    try:
        s = AircheckSession.objects.get(id=session_id)
        s.exit_note = (telnet_note + f"remux failed: {err}").strip("; ")
        s.save(update_fields=["exit_note"])
    except AircheckSession.DoesNotExist:
        pass


def maintain_idle_buffer(max_bytes=AIRCHECK_IDLE_BUFFER_MAX_BYTES):
    """Idle-time protection against unbounded growth of the always-on
    working file at AIRCHECK_CURRENT_PATH (see module docstring and
    encoder_manager._aircheck_block -- output.file never stops writing,
    even with no Aircheck session in progress, and is only otherwise
    cut on an explicit Start/Stop). Intended to be called on a ~1min
    timer via the maintain_aircheck_buffer management command.

    Deliberately narrow: the ONLY action this ever takes is the same
    `aircheck.reopen` telnet call Start/Stop already use, and only when
    genuinely idle and oversized. Never unlinks, renames, truncates, or
    otherwise touches the working file from Python, and never creates
    or modifies an AircheckSession -- a normal idle rollover is
    invisible to session history, exactly like the file being cut by a
    real Start/Stop is the only thing that should ever appear there.

    Returns one of:
      "lock_busy"      -- a real Start/Stop (or another maintenance
                           run) currently holds AIRCHECK_LOCK_PATH;
                           skipped harmlessly, retried next cycle.
      "active_session"  -- an AircheckSession is running; never rolls
                           over a file a real session is relying on.
      "missing"         -- AIRCHECK_CURRENT_PATH doesn't exist (e.g.
                           encoders not up yet); nothing to do.
      "below_limit"     -- working file is under max_bytes; no-op.
      "rolled"          -- issued exactly one aircheck.reopen.
      "error"           -- stat failed, or the telnet reopen itself
                           failed; file/session left untouched, a
                           deduplicated warning SystemEvent is emitted
                           (telnet-failure case only)."""
    with _aircheck_lock(blocking=False) as acquired:
        if not acquired:
            return "lock_busy"

        if AircheckSession.objects.filter(still_running=True).exists():
            return "active_session"

        working = Path(AIRCHECK_CURRENT_PATH)
        try:
            size = working.stat().st_size
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "error"

        if size < max_bytes:
            return "below_limit"

        try:
            _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
        except TelnetError as exc:
            emit_event(
                category="aircheck", level="warning",
                title="Idle aircheck buffer rollover failed",
                detail={"error": str(exc), "size_bytes": size, "max_bytes": max_bytes},
                dedupe_key="aircheck|idle-buffer-reopen-failed",
            )
            return "error"

        return "rolled"
