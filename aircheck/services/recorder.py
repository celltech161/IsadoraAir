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

Working-buffer maintenance (maintain_idle_buffer): because output.file
above runs continuously, the tmpfs working file at
AIRCHECK_CURRENT_PATH grows until it is cut. Idle files are discarded
with the existing reopen operation. Active logical sessions use the
same bounded working-file policy, but each closed segment is copied to
persistent per-session staging before its /run handoff is removed.
See AIRCHECK_LOCK_PATH below for cross-process serialization.
"""
import errno
import fcntl
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from django.conf import settings
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
FINALIZATION_PENDING_NOTE = "finalization in progress"
REMUX_INTERMEDIATE_DIR = Path("/run/isadoraair")

# Substrings that show up in exit_note ONLY when finalization definitively
# failed to produce usable audio at the session's destination -- matched
# against the literal text the finalize helpers below already write, not
# invented separately. Anything else non-empty (e.g. a telnet hiccup at
# Stop that didn't actually stop the move/remux from succeeding) is a
# "warning", not an "error" -- see classify_finalization().
#   " failed: "  -- "remux failed: ..." (_mark_remux_failed) and
#                    "move ... failed: ..." (_finalize_direct_move).
#                    NOT a substring of "...failed at Stop: ..." (the
#                    telnet-at-Stop note), which is deliberate.
#   "no audio to" -- "no audio to remux" / "no audio to move" (working
#                    file was already missing at Stop -- nothing to save).
#   "could not stage intermediate" -- he_aac: couldn't even begin the remux.
FINALIZATION_ERROR_MARKERS = (
    " failed: ",
    "no audio to",
    "could not stage intermediate",
    "finalization failed:",
)

# Runtime state file the /monitoring/ Aircheck card reads (via
# aircheck:api-status) to show idle-buffer-guard health. Written by
# maintain_aircheck_buffer on every invocation -- see
# record_buffer_heartbeat. Deliberately NOT read or written by
# encoders/services/encoder_manager.py or the Liquidsoap script itself;
# this is pure Django-side reporting about the guard, not something
# Liquidsoap needs to know about.
AIRCHECK_BUFFER_STATE_PATH = "/run/isadoraair/aircheck_buffer_state.json"

# How long a maintenance heartbeat can go unrefreshed before the status
# API calls it stale. The timer fires every 60s (isadoraair-aircheck-
# buffer.timer); 165s is a bit under 3 missed cycles' worth of margin --
# comfortably past ordinary jitter/one skipped cycle, without waiting so
# long that a genuinely dead timer looks healthy for minutes.
AIRCHECK_BUFFER_HEARTBEAT_STALE_SECONDS = 165

# Cross-process advisory lock serializing every operation that touches
# the aircheck working file or AircheckSession state: Start, Stop, and
# idle-buffer maintenance all go through _aircheck_lock. Start/Stop
# acquire it BLOCKING (they must eventually run); idle maintenance
# acquires it NON-BLOCKING and just skips this cycle if a real
# operation currently owns it -- a missed idle rollover costs nothing,
# it retries next cycle, and it must never make a Start/Stop press
# wait on it.
AIRCHECK_LOCK_PATH = "/run/isadoraair/aircheck.lock"

# Safety ceiling for the always-on working file (idle or active). The
# timer observes this once a minute, so this is a cut trigger rather
# than a mathematical hard maximum: one timer interval of overshoot is
# expected. Active segments leave /run after each successful cut.
AIRCHECK_WORKING_FILE_MAX_BYTES = 64 * 1024 * 1024
# Backward-compatible import name for older callers/tests. There is now
# one policy for both idle and active working files.
AIRCHECK_IDLE_BUFFER_MAX_BYTES = AIRCHECK_WORKING_FILE_MAX_BYTES

STAGING_ROOT_NAME = ".isadoraair-aircheck-staging"
SEGMENT_NAME_PREFIX = "segment-"
SEGMENT_PARTIAL_SUFFIX = ".partial"
HANDOFF_NAME_PREFIX = "aircheck-segment-"
FFMPEG_TIMEOUT_SECONDS = 3600

# Bounded full-decode validation (see _decode_validate_audio) runs after
# the concat/remux step and before source cleanup, so it needs its own
# timeout independent of FFMPEG_TIMEOUT_SECONDS above. A fixed ceiling
# sized for a short recording would be too tight for a very long one; a
# duration-derived budget with a floor and a ceiling keeps both ends
# sane. Real decode is virtually always much faster than realtime for
# every format this module produces (HE-AAC/MP3/FLAC/WAV), so budgeting
# a quarter of the program's own duration is generous headroom even on
# degraded hardware, while the ceiling keeps a worst-case multi-day
# recording bounded rather than open-ended. The floor covers short
# recordings plus fixed process-spawn/teardown overhead.
DECODE_VALIDATION_MIN_SECONDS = 120
DECODE_VALIDATION_MAX_SECONDS = 6 * 3600
DECODE_VALIDATION_SECONDS_PER_DURATION_SECOND = 0.25

SOURCE_EXTENSION_BY_FORMAT = {
    "he_aac": "aac",  # Liquidsoap/fdkaac source is ADTS; final is M4A.
    "mp3": "mp3",
    "flac": "flac",
    "wav": "wav",
}


class SegmentError(RuntimeError):
    """A preservation-safe segment cut, transfer, or finalization failure."""


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
    destination; the current bounded source segment lives at
    AIRCHECK_CURRENT_PATH, while completed long-session segments live
    in persistent session staging until Stop finalizes them.

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

        # Guard against second-precision collisions on rapid/back-to-back
        # sessions. A prior segmented session can be finalizing before its
        # destination exists, so the session row also reserves that path.
        suffix = None
        while out_path.exists() or AircheckSession.objects.filter(filename=str(out_path)).exists():
            suffix = datetime.now().microsecond if suffix is None else suffix + 1
            out_path = out_dir / f"{stamp}-{suffix}.{cfg.file_extension()}"

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


def _source_extension(session):
    return SOURCE_EXTENSION_BY_FORMAT.get(session.audio_format, "audio")


def _staging_dir(session):
    """Persistent, session-owned segment directory derived from the
    immutable destination captured on the session row -- never from a
    later AircheckConfig edit."""
    dest = Path(session.filename)
    return dest.parent / STAGING_ROOT_NAME / str(session.id)


def _segment_path(session, sequence):
    return _staging_dir(session) / (
        f"{SEGMENT_NAME_PREFIX}{sequence:06d}.{_source_extension(session)}"
    )


def _handoff_path(session, sequence):
    working = Path(AIRCHECK_CURRENT_PATH)
    return working.with_name(
        f"{HANDOFF_NAME_PREFIX}{session.id}-{sequence:06d}.handoff"
    )


def _parse_handoff_sequence(session, path):
    prefix = f"{HANDOFF_NAME_PREFIX}{session.id}-"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(".handoff"):
        return None
    raw = name[len(prefix):-len(".handoff")]
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_segment_sequence(session, path):
    prefix = SEGMENT_NAME_PREFIX
    suffix = f".{_source_extension(session)}"
    name = path.name
    if not name.startswith(prefix) or not name.endswith(suffix):
        return None
    raw = name[len(prefix):-len(suffix)]
    if not raw.isdigit():
        return None
    return int(raw)


def _discover_segments(session):
    """Return committed persistent segments in deterministic order.
    Temporary copy artifacts deliberately do not match this pattern."""
    staging = _staging_dir(session)
    ext = _source_extension(session)
    found = []
    for path in staging.glob(f"{SEGMENT_NAME_PREFIX}*.{ext}"):
        sequence = _parse_segment_sequence(session, path)
        if sequence is not None:
            found.append((sequence, path))
    return [path for _, path in sorted(found)]


def _pending_handoffs(session):
    working = Path(AIRCHECK_CURRENT_PATH)
    matches = []
    for path in working.parent.glob(f"{HANDOFF_NAME_PREFIX}{session.id}-*.handoff"):
        sequence = _parse_handoff_sequence(session, path)
        if sequence is not None:
            matches.append((sequence, path))
    return sorted(matches)


def _next_segment_sequence(session):
    sequences = []
    for path in _discover_segments(session):
        sequences.append(_parse_segment_sequence(session, path))
    sequences.extend(sequence for sequence, _ in _pending_handoffs(session))
    return max(sequences, default=0) + 1


def _fsync_directory(path):
    """Best-effort directory durability matching the module's atomic
    heartbeat convention without making unsupported filesystems fatal."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _copy_handoff_to_staging(session, sequence, handoff):
    """Copy a closed /run handoff to persistent storage safely.

    The committed segment name appears only after the copy is flushed
    and fsynced. The /run source is removed only after that atomic
    publish. A retry after a crash recognizes an already-committed,
    same-sized segment and removes only the redundant handoff.
    """
    staging = _staging_dir(session)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SegmentError(f"cannot create staging directory {staging}: {exc}")

    committed = _segment_path(session, sequence)
    partial = staging / f".{committed.name}{SEGMENT_PARTIAL_SUFFIX}"
    if committed.exists():
        # Size equality is NOT a general content-integrity proof -- it is
        # sufficient here only because of stronger invariants elsewhere:
        # a given (session.id, sequence) handoff name is produced by
        # exactly one cut, is never reused for different audio, and its
        # content is fixed the moment Liquidsoap closes it (see
        # _isolate_active_working_file); committed segment publication
        # is itself atomic (partial -> os.replace). So if both names
        # exist, they can only ever describe the same logical segment,
        # possibly copied twice across a crash/retry -- never two
        # different segments that happen to collide in size.
        try:
            same_size = committed.stat().st_size == handoff.stat().st_size
        except OSError as exc:
            raise SegmentError(f"cannot compare committed segment {committed}: {exc}")
        if not same_size:
            raise SegmentError(
                f"committed segment collision for session {session.id} sequence {sequence}"
            )
        try:
            handoff.unlink()
        except OSError as exc:
            raise SegmentError(f"committed segment exists but handoff cleanup failed: {exc}")
        return committed

    try:
        with open(handoff, "rb") as src, open(partial, "wb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(partial, committed)
        _fsync_directory(staging)
    except OSError as exc:
        raise SegmentError(f"persistent segment copy failed: {exc}")

    try:
        handoff.unlink()
    except OSError as exc:
        # The durable segment is authoritative; a later recovery pass
        # will size-match and remove this duplicate handoff safely.
        raise SegmentError(f"segment committed but /run handoff cleanup failed: {exc}")
    return committed


def _recover_pending_handoffs(session):
    """Recover transfer/cut artifacts after a maintenance-process exit.

    If the fixed working path exists, Liquidsoap has completed the
    reopen and any handoff is closed, so finish its persistent copy. If
    the fixed path is absent, the writer may still own the renamed inode;
    restore that pathname instead of treating live audio as closed.
    """
    recovered = []
    working = Path(AIRCHECK_CURRENT_PATH)
    pending = _pending_handoffs(session)
    for sequence, handoff in pending:
        committed = _segment_path(session, sequence)
        if committed.exists():
            recovered.append(_copy_handoff_to_staging(session, sequence, handoff))
            continue
        if not working.exists():
            try:
                handoff.rename(working)
            except OSError as exc:
                raise SegmentError(f"cannot restore pending active handoff {handoff}: {exc}")
            raise SegmentError(
                "restored a pending handoff to the active working path; retry segmentation later"
            )
        recovered.append(_copy_handoff_to_staging(session, sequence, handoff))
    return recovered


def _isolate_active_working_file(session, sequence):
    """Perform the proven rename-before-reopen cut primitive.

    Liquidsoap keeps writing the renamed inode until reopen is processed,
    so samples produced between these operations remain in the old
    segment. On an acknowledged failure with no new fixed path, rename
    that still-open inode back so capture continues coherently.
    """
    working = Path(AIRCHECK_CURRENT_PATH)
    handoff = _handoff_path(session, sequence)
    if handoff.exists():
        raise SegmentError(f"handoff already exists: {handoff}")
    try:
        working.rename(handoff)
    except FileNotFoundError:
        raise SegmentError(f"working file {working} is missing")
    except OSError as exc:
        raise SegmentError(f"cannot isolate working file {working}: {exc}")

    try:
        _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
    except TelnetError as exc:
        if not working.exists():
            try:
                handoff.rename(working)
            except OSError as restore_exc:
                raise SegmentError(
                    f"reopen failed ({exc}); active handoff retained at {handoff}; "
                    f"working-path restore also failed: {restore_exc}"
                )
            raise SegmentError(
                f"reopen failed ({exc}); active working pathname restored"
            )
        # The command can be processed even if its response is lost. A
        # new fixed path is objective evidence that the boundary happened;
        # preserve the closed handoff but still report the control failure.
        raise SegmentError(
            f"reopen response failed ({exc}) after a new working path appeared; "
            f"closed handoff retained at {handoff}"
        )
    return handoff


def _cut_and_stage_active_segment(session):
    """Cut and persist exactly one active-session segment."""
    # Preflight persistent ownership before moving the only active path.
    staging = _staging_dir(session)
    try:
        staging.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SegmentError(f"cannot create staging directory {staging}: {exc}")

    _recover_pending_handoffs(session)
    sequence = _next_segment_sequence(session)
    handoff = _isolate_active_working_file(session, sequence)
    return _copy_handoff_to_staging(session, sequence, handoff)


def stop_recording():
    """Stop the currently-running session, if any. Returns
    (session, error). The active working inode is renamed before reopen,
    using the same preservation-safe boundary as maintenance.

    Finalization branches on format:
      - an ordinary short MP3/FLAC/WAV session keeps the direct-move
        fast path;
      - an ordinary HE-AAC session keeps the existing async ADTS->M4A
        remux path;
      - any session with persistent segments (or an unexpectedly large
        final working file) stages the final source and asynchronously
        finalizes the ordered set into one destination container.

    The lock is released before multi-segment ffmpeg finalization starts,
    so another logical session can begin immediately. All essential
    source state lives in persistent staging, not only in the daemon
    thread."""
    segmented_job = None
    with _aircheck_lock(blocking=True):
        session = AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()
        if session is None:
            return None, "no active session"

        try:
            _recover_pending_handoffs(session)
        except SegmentError as exc:
            return None, f"cannot recover pending Aircheck segment: {exc}"

        working = Path(AIRCHECK_CURRENT_PATH)
        dest = Path(session.filename)
        prior_segments = _discover_segments(session)
        try:
            working_size = working.stat().st_size
        except OSError:
            working_size = None
        use_segmented_finalization = bool(prior_segments) or (
            working_size is not None and working_size >= AIRCHECK_WORKING_FILE_MAX_BYTES
        )

        # Preserve the established short-session missing-file behavior:
        # there is no source inode to abandon, so end the row and record
        # the ordinary "no audio to move/remux" finalization error. With
        # earlier committed segments, however, a missing current path is
        # an unsafe/incomplete final boundary and Stop must remain retryable.
        if not working.is_file() and not prior_segments:
            telnet_note = ""
            try:
                _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
            except TelnetError as exc:
                telnet_note = f"telnet reopen failed at Stop: {exc}; "
            session.still_running = False
            session.ended_at = timezone.now()
            if session.audio_format == "he_aac":
                _finalize_he_aac_async(session, working, dest, telnet_note)
            else:
                _finalize_direct_move(session, working, dest, telnet_note)
            return session, None

        sequence = _next_segment_sequence(session)
        try:
            handoff = _isolate_active_working_file(session, sequence)
        except SegmentError as exc:
            # Stop did not obtain a safe boundary. Keep the logical
            # session running so the operator can retry without losing
            # the still-active source.
            return None, f"could not stop Aircheck safely: {exc}"

        if use_segmented_finalization:
            try:
                _copy_handoff_to_staging(session, sequence, handoff)
            except SegmentError as exc:
                # A new working file is already capturing. Keep the
                # logical session active and the handoff recoverable;
                # the next maintenance/Stop call will retry the copy.
                emit_event(
                    category="aircheck", level="warning",
                    title="Aircheck final segment staging failed",
                    detail={"session_id": session.id, "error": str(exc)},
                    dedupe_key=f"aircheck|final-segment-stage|{session.id}",
                )
                return None, f"could not persist final Aircheck segment: {exc}"
            session.still_running = False
            session.ended_at = timezone.now()
            session.exit_note = FINALIZATION_PENDING_NOTE
            session.save(update_fields=["still_running", "ended_at", "exit_note"])
            segmented_job = (session.id, str(dest))
        elif session.audio_format == "he_aac":
            session.still_running = False
            session.ended_at = timezone.now()
            _finalize_he_aac_async(session, handoff, dest, "")
        else:
            session.still_running = False
            session.ended_at = timezone.now()
            _finalize_direct_move(session, handoff, dest, "")

    if segmented_job is not None:
        t = threading.Thread(
            target=_segmented_finalize_worker,
            args=segmented_job,
            daemon=True,
            name=f"aircheck-finalize-{session.id}",
        )
        t.start()
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

    if dest.is_file():
        # .start() returns immediately -- the actual library-sync work
        # (below) runs concurrently in its own thread and never
        # contends for AIRCHECK_LOCK_PATH itself, so it can't make
        # Stop's still-held lock (or a subsequent Start/idle
        # maintenance) wait on it. Library analysis (waveform/cue
        # points) can take real time on a large recording, and this
        # step is best-effort/optional -- never something Stop itself
        # should be blocked on.
        threading.Thread(
            target=_sync_finalized_recording_to_library,
            args=(dest,),
            daemon=True,
            name=f"aircheck-library-sync-{session.id}",
        ).start()


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

        # Already running in a background thread (the remux itself) --
        # no separate thread needed here, unlike the direct-move path.
        if dest.is_file():
            _sync_finalized_recording_to_library(dest)
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


def _write_concat_manifest(path, segments):
    """Atomically publish an ffconcat list. Committed segments have
    deterministic names, while this manifest and transfer partials are
    deliberately distinguishable from source audio discovery."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    lines = []
    for segment in segments:
        escaped = str(segment.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise SegmentError(f"cannot write concat manifest: {exc}")


def _validate_final_audio(path):
    if not path.is_file() or path.stat().st_size <= 0:
        raise SegmentError(f"final output is missing or empty: {path}")
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_type:format=duration", "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SegmentError(
            f"ffprobe rejected final output: {(exc.stderr or '').strip()[:300]}"
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise SegmentError(f"ffprobe failed for final output: {exc}")
    try:
        probe = json.loads(result.stdout)
        streams = probe.get("streams") or []
        duration = float((probe.get("format") or {}).get("duration", 0))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SegmentError(f"ffprobe returned invalid final-output metadata: {exc}")
    if not streams or streams[0].get("codec_type") != "audio":
        raise SegmentError(f"final output has no decodable audio stream: {path}")
    if duration <= 0:
        raise SegmentError(f"final output has no positive duration: {path}")

    _decode_validate_audio(path, duration)


def _decode_validation_timeout(duration_seconds):
    """Duration-derived decode-validation budget -- see the constants'
    own comment above for the reasoning. Never shorter than the floor,
    never longer than the ceiling, regardless of how long the program
    itself ran."""
    return min(
        DECODE_VALIDATION_MAX_SECONDS,
        max(DECODE_VALIDATION_MIN_SECONDS, duration_seconds * DECODE_VALIDATION_SECONDS_PER_DURATION_SECOND),
    )


def _decode_validate_audio(path, duration_seconds):
    """Full decode of the selected audio stream -- metadata/ffprobe
    checks alone accept a file with a valid-enough header and a
    plausible duration even if the actual audio data is corrupt partway
    through. This is the gate between concat/remux and source cleanup:
    raising here leaves every committed segment and the staging
    directory exactly as _finalize_segment_set's caller already treats
    any other validation failure -- nothing is published, nothing is
    deleted. No output file is written (``-f null -``); this only
    proves the stream decodes cleanly end to end.

    Matches the full-decode argument convention already established by
    library/services/media_health.py's own _ffmpeg_decode (-xerror
    turns any decode error into a nonzero exit rather than a warning
    logged and ignored)."""
    timeout = _decode_validation_timeout(duration_seconds)
    try:
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-xerror",
                "-threads", "1", "-i", str(path), "-map", "0:a:0", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=timeout, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SegmentError(
            f"final output failed full decode validation: {(exc.stderr or '').strip()[:300]}"
        )
    except subprocess.TimeoutExpired:
        raise SegmentError(
            f"final output decode validation timed out after {timeout:.0f}s"
        )
    except OSError as exc:
        raise SegmentError(f"final output decode validation failed to start: {exc}")


def _finalize_segment_set(session, dest):
    """Stream-copy all committed source segments into one final file.

    The concat demuxer normalizes packet ordering for MP3/ADTS and writes
    one correct output container. WAV likewise becomes one WAV mux with
    one final header -- source containers are never byte-appended.
    HE-AAC adds the standard ADTS-to-ASC bitstream filter for M4A. FLAC
    is the one exception to stream-copy: its STREAMINFO total-samples
    metadata remains that of the first source under ffmpeg ``-c copy``,
    so ffmpeg performs a lossless FLAC decode/re-encode to write a
    truthful single-stream header.
    """
    segments = _discover_segments(session)
    if not segments:
        raise SegmentError("no committed segments available for finalization")
    staging = _staging_dir(session)
    manifest = staging / "segments.ffconcat"
    _write_concat_manifest(manifest, segments)

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial_dest = dest.parent / (
        f".{dest.stem}.aircheck-partial-{session.id}{dest.suffix}"
    )
    try:
        partial_dest.unlink(missing_ok=True)
    except OSError as exc:
        raise SegmentError(f"cannot clear prior finalization partial: {exc}")

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
        "-f", "concat", "-safe", "0", "-i", str(manifest),
        "-map", "0:a:0", "-c:a", "flac" if session.audio_format == "flac" else "copy",
    ]
    if session.audio_format == "he_aac":
        cmd += ["-bsf:a", "aac_adtstoasc"]
    if session.audio_format == "wav":
        # The installed ffmpeg's WAV muxer defaults -rf64 to "never" --
        # a long enough logical recording would silently produce an
        # invalid/overflowed classic-RIFF header past ~4 GiB without
        # this. "auto" promotes to an RF64 header only once the output
        # actually grows large enough to need it, so an ordinary short
        # WAV stays plain RIFF and PCM sample format is unaffected
        # either way -- this only changes which container header is
        # written, never how samples are encoded.
        cmd += ["-rf64", "auto"]
    cmd.append(str(partial_dest))
    try:
        subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS, check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise SegmentError(
            f"ffmpeg concat exit {exc.returncode}: {(exc.stderr or '').strip()[:300]}"
        )
    except subprocess.TimeoutExpired:
        raise SegmentError(
            f"ffmpeg concat timed out after {FFMPEG_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        raise SegmentError(f"ffmpeg concat spawn failed: {exc}")

    _validate_final_audio(partial_dest)
    try:
        os.replace(partial_dest, dest)
        _fsync_directory(dest.parent)
    except OSError as exc:
        raise SegmentError(f"cannot publish final Aircheck output: {exc}")

    # Only a validated, atomically-published destination authorizes
    # source cleanup.
    for segment in segments:
        segment.unlink()
    manifest.unlink(missing_ok=True)
    for partial in staging.glob(f".*{SEGMENT_PARTIAL_SUFFIX}"):
        partial.unlink(missing_ok=True)
    try:
        staging.rmdir()
        staging.parent.rmdir()
    except OSError:
        pass
    return dest.stat().st_size


def _mark_segmented_finalization_failed(session_id, err):
    message = f"finalization failed: {str(err)[:170]}"
    try:
        session = AircheckSession.objects.get(id=session_id)
        session.exit_note = message
        session.save(update_fields=["exit_note"])
    except AircheckSession.DoesNotExist:
        pass
    emit_event(
        category="aircheck", level="warning",
        title="Aircheck segmented finalization failed",
        detail={"session_id": session_id, "error": str(err)},
        dedupe_key=f"aircheck|segmented-finalization|{session_id}",
    )


def _segmented_finalize_worker(session_id, dest_path):
    """Finalize persistent segments outside AIRCHECK_LOCK_PATH.

    A daemon-thread/process exit can leave the row pending, but never
    destroys the deterministic on-disk sources -- P2 1.13B's maintain_
    aircheck_recovery command retries a stranded pending session using
    this exact function, never a second implementation.

    Acquires aircheck.services.recovery's per-session finalization_lock
    (blocking -- this is the original, normally-uncontended owner) for
    the duration of the real work, so a concurrent 1.13B recovery pass's
    own non-blocking attempt correctly observes "already owned" and
    never duplicates this finalization. Imported lazily to avoid a
    circular import (recovery imports this module for its own reuse of
    _finalize_segment_set etc.)."""
    close_old_connections()
    try:
        try:
            session = AircheckSession.objects.get(id=session_id)
        except AircheckSession.DoesNotExist:
            return
        dest = Path(dest_path)
        from aircheck.services import recovery  # lazy: avoid recorder<->recovery import cycle
        with recovery.finalization_lock(session, blocking=True):
            try:
                size = _finalize_segment_set(session, dest)
            except Exception as exc:
                _mark_segmented_finalization_failed(session_id, exc)
                return

            session.size_bytes = size
            session.exit_note = ""
            session.save(update_fields=["size_bytes", "exit_note"])
        _sync_finalized_recording_to_library(dest)
    finally:
        close_old_connections()


def _sync_finalized_recording_to_library(dest):
    """Best-effort: if AircheckConfig.output_directory happens to live
    under LIBRARY_ROOT with a matching Category (see sync_track_file's
    own convention: LIBRARY_ROOT/<category_code>/...), index the
    just-finalized recording into the library so it shows up on the
    track detail page. A silent no-op for the common/default case where
    output_directory is NOT under LIBRARY_ROOT (e.g. the original
    default, /srv/isadoraair/aircheck) -- this feature only activates
    once an operator deliberately points output_directory at a
    LIBRARY_ROOT/<Category> path and creates that Category.

    ready2air=False (unlike sync_track_file's own command-line default
    of True, meant for unattended pipelines delivering NEW programming)
    -- an Aircheck recording is archival, a record of what already
    aired, not new content ready to air again. It requires the same
    human review as any manually-added track before it could ever enter
    rotation, and rotation eligibility separately requires an explicit
    RotationSlot for the category, which nothing here creates.

    Never raises. Called from a background thread (either the direct-
    move path's own dedicated thread, or already-in-progress inside the
    he_aac remux thread) with nothing waiting on its result -- Stop
    itself has already fully succeeded and returned by the time this
    runs; a failure here must never look like Stop failed, and there's
    no retry -- an operator can always run `manage.py sync_track_file
    <path>` by hand later if this best-effort step didn't fire."""
    close_old_connections()
    try:
        root = Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")).resolve()
        try:
            dest.resolve().relative_to(root)
        except ValueError:
            return  # output_directory isn't under LIBRARY_ROOT -- feature not opted into, stay silent

        from library.management.commands.sync_track_file import sync_track_file  # lazy: aircheck
        # has no need to import library's management-command modules (and
        # everything they pull in) unless this feature is actually in use.
        track, created = sync_track_file(str(dest), ready2air=False)
        print(f"  Aircheck: synced finalized recording into library as track id={track.id} ({'created' if created else 'updated'}, ready2air=False)")
    except Exception as exc:
        print(f"  Aircheck: library sync failed for {dest} (non-fatal): {exc}")
        emit_event(
            category="aircheck", level="warning", title="Aircheck library sync failed",
            detail={"path": str(dest), "error": str(exc)},
        )
    finally:
        close_old_connections()


def maintain_idle_buffer(max_bytes=None):
    """Bound the always-on Liquidsoap working file in idle and active use.

    Idle rollover retains the established reopen-and-discard behavior.
    During a logical recording, an oversized working inode is instead
    isolated with rename-before-reopen and copied into persistent,
    session-owned staging. The session stays active and the newly opened
    fixed path continues capture. The timer cadence means ``max_bytes``
    is an observed cut trigger, not an exact upper bound.

    Returns one of:
      "lock_busy"      -- a real Start/Stop (or another maintenance
                           run) currently holds AIRCHECK_LOCK_PATH;
                           skipped harmlessly, retried next cycle.
      "missing"         -- AIRCHECK_CURRENT_PATH doesn't exist (e.g.
                           encoders not up yet); nothing to do.
      "idle_below_limit" / "active_below_limit" -- no-op.
      "idle_rolled"     -- issued one idle aircheck.reopen.
      "active_segmented" -- one active segment reached persistent storage.
      "idle_error" / "active_segment_failed" -- preservation-safe
                           failure; a deduplicated warning is emitted
                           for actionable cut/transfer failures."""
    if max_bytes is None:
        max_bytes = AIRCHECK_WORKING_FILE_MAX_BYTES

    with _aircheck_lock(blocking=False) as acquired:
        if not acquired:
            return "lock_busy"

        session = (
            AircheckSession.objects.filter(still_running=True)
            .order_by("-started_at")
            .first()
        )
        recovered = []
        if session is not None and _pending_handoffs(session):
            try:
                recovered = _recover_pending_handoffs(session)
            except SegmentError as exc:
                emit_event(
                    category="aircheck", level="warning",
                    title="Active Aircheck segment recovery failed",
                    detail={"session_id": session.id, "error": str(exc)},
                    dedupe_key=f"aircheck|active-segment-failed|{session.id}",
                )
                return "active_segment_failed"

        working = Path(AIRCHECK_CURRENT_PATH)
        try:
            size = working.stat().st_size
        except FileNotFoundError:
            return "missing"
        except OSError:
            return "active_segment_failed" if session else "idle_error"

        if size < max_bytes:
            if recovered:
                return "active_segmented"
            return "active_below_limit" if session else "idle_below_limit"

        if session is not None:
            try:
                _cut_and_stage_active_segment(session)
            except SegmentError as exc:
                emit_event(
                    category="aircheck", level="warning",
                    title="Active Aircheck segmentation failed",
                    detail={
                        "session_id": session.id,
                        "error": str(exc),
                        "size_bytes": size,
                        "max_bytes": max_bytes,
                    },
                    dedupe_key=f"aircheck|active-segment-failed|{session.id}",
                )
                return "active_segment_failed"
            return "active_segmented"

        try:
            _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
        except TelnetError as exc:
            emit_event(
                category="aircheck", level="warning",
                title="Idle aircheck buffer rollover failed",
                detail={"error": str(exc), "size_bytes": size, "max_bytes": max_bytes},
                dedupe_key="aircheck|idle-buffer-reopen-failed",
            )
            return "idle_error"

        return "idle_rolled"


def classify_finalization(session):
    """Explicit finalization classification for one AircheckSession, so
    the /monitoring/ card and its JS render a known enum rather than
    parsing exit_note text client-side. Returns one of "recording",
    "finalizing", "error", "warning", "complete", or None for
    session=None.

    Follows the recorder's own semantics above, not a separate guess:
    still_running means _create/finalize haven't run yet at all; the
    REMUX_PENDING_NOTE marker means _finalize_he_aac_async handed off
    to its async worker and hasn't heard back; FINALIZATION_ERROR_MARKERS
    are the exact substrings the finalize helpers write on a definitive
    failure; any other non-empty note (e.g. a telnet hiccup at Stop that
    didn't actually block the move/remux from succeeding) is a warning,
    not an error; a clean stop with no note at all is complete."""
    if session is None:
        return None
    if session.still_running:
        return "recording"
    note = session.exit_note or ""
    if REMUX_PENDING_NOTE in note or FINALIZATION_PENDING_NOTE in note:
        return "finalizing"
    if any(marker in note for marker in FINALIZATION_ERROR_MARKERS):
        return "error"
    if note:
        return "warning"
    return "complete"


def _atomic_write_json(path, data):
    """Write-tmp-then-atomic-replace -- same idiom as
    encoders/services/encoder_manager.py's own _atomic_write_json,
    reimplemented locally (not imported) so this module keeps no
    dependency on the encoder manager, matching the rest of this file's
    stance that Liquidsoap/encoders know nothing about Aircheck beyond
    the telnet client. No reader can ever observe a partially-written
    or truncated state file."""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _read_prior_heartbeat():
    try:
        with open(AIRCHECK_BUFFER_STATE_PATH, "r", encoding="utf-8") as f:
            prior = json.load(f)
    except (OSError, ValueError):
        return {}
    return prior if isinstance(prior, dict) else {}


# Bounded, fixed set of recovery-summary keys the less-frequent 1.13B
# recovery pass writes -- see record_recovery_heartbeat. Listed
# explicitly (rather than merging an arbitrary dict) so this state file
# can never grow unbounded no matter how recovery.inventory_summary()'s
# own shape evolves.
_RECOVERY_HEARTBEAT_KEYS = (
    "recovery_checked_at", "recovery_pending_count", "recovery_retry_succeeded",
    "recovery_retry_failed", "recovery_failed_recovery_count",
    "recovery_failed_recovery_bytes", "recovery_deleted_sets", "recovery_deleted_bytes",
)


def record_buffer_heartbeat(result, size_bytes, max_bytes, reconciliation=None):
    """Persist the outcome of one maintain_idle_buffer invocation (plus,
    since P2 1.13B, the same cycle's bounded /run reconciliation) to
    AIRCHECK_BUFFER_STATE_PATH for the /monitoring/ Aircheck card to
    read. Called by the maintain_aircheck_buffer management command
    AFTER maintain_idle_buffer has already made its rollover/segmentation
    decision -- this function is pure reporting and never influences
    that decision.

    Idle rollover and active segmentation timestamps are preserved
    independently across other results. The separate, less-frequent
    recovery pass's own fields (see record_recovery_heartbeat) are
    preserved here too -- both maintenance cadences update the SAME
    fixed-shape state file rather than each inventing its own channel.

    Best-effort both ways: a missing or malformed prior state file is
    treated as "no prior state" rather than raised, and a write failure
    (e.g. /run momentarily unwritable) is swallowed -- this heartbeat
    must never be able to fail the maintenance command itself."""
    prior = _read_prior_heartbeat()
    checked_at = time.time()
    state = {
        "checked_at": checked_at,
        "result": result,
        "size_bytes": size_bytes,
        "max_bytes": max_bytes,
        "last_rollover_at": checked_at if result == "idle_rolled" else prior.get("last_rollover_at"),
        "last_segmented_at": checked_at if result == "active_segmented" else prior.get("last_segmented_at"),
    }
    for key in _RECOVERY_HEARTBEAT_KEYS:
        if key in prior:
            state[key] = prior[key]
    if reconciliation:
        state["run_handoffs_evacuated"] = reconciliation.get("handoffs_evacuated")
        state["run_legacy_evacuated"] = reconciliation.get("legacy_evacuated")
    try:
        _atomic_write_json(Path(AIRCHECK_BUFFER_STATE_PATH), state)
    except OSError:
        pass  # heartbeat is best-effort reporting, never fatal


def record_recovery_heartbeat(recovery_result):
    """Same fixed-name state file as record_buffer_heartbeat, updated
    by the separate, less-frequent maintain_aircheck_recovery command.
    Overlays only its own bounded key set (_RECOVERY_HEARTBEAT_KEYS)
    onto whatever the 1-minute buffer heartbeat most recently wrote, so
    neither writer clobbers the other's fields. Best-effort, never
    fatal to the calling command."""
    prior = _read_prior_heartbeat()
    retry = recovery_result.get("retry", {})
    retention = recovery_result.get("retention", {})
    prior.update({
        "recovery_checked_at": time.time(),
        "recovery_pending_count": retry.get("candidates"),
        "recovery_retry_succeeded": retry.get("succeeded"),
        "recovery_retry_failed": retry.get("failed"),
        "recovery_failed_recovery_count": retention.get("total_sets"),
        "recovery_failed_recovery_bytes": retention.get("total_bytes"),
        "recovery_deleted_sets": retention.get("deleted_sets"),
        "recovery_deleted_bytes": retention.get("deleted_bytes"),
    })
    try:
        _atomic_write_json(Path(AIRCHECK_BUFFER_STATE_PATH), prior)
    except OSError:
        pass
