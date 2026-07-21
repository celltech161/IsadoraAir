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
"""
import shutil
import socket
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from aircheck.models import AircheckConfig, AircheckSession
from encoders.services.encoder_manager import (
    AIRCHECK_CURRENT_PATH,
    AIRCHECK_OUTPUT_ID,
    AIRCHECK_TELNET_HOST,
    AIRCHECK_TELNET_PORT,
)


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


def _reap_stale_running_session():
    """Close any AircheckSession row still marked still_running when
    we know the recording is over. Called ONLY from start_recording
    (never from current_session, which would race the monitoring
    dashboard's poll and silently close just-started sessions).

    We can't ask liquidsoap "is a session in progress" cleanly in the
    fixed-path design -- the working file is always being written
    regardless. Any still_running=True row we see at Start time was
    left open by a Django crash or an out-of-band interrupt, so
    reaping is safe. If a file exists at the row's declared
    destination, record its size; otherwise leave size_bytes null.
    """
    stale = AircheckSession.objects.filter(still_running=True)
    for s in stale:
        s.still_running = False
        s.ended_at = timezone.now()
        s.exit_note = "reaped stale row at Start (django crash or out-of-band interrupt)"
        if s.filename and Path(s.filename).is_file():
            try:
                s.size_bytes = Path(s.filename).stat().st_size
            except OSError:
                pass
        s.save()


def current_session():
    """Return the currently-running AircheckSession or None. Pure DB
    read -- no reaper -- because this is called on every dashboard
    status poll and a reaper race would silently close a just-started
    session. Reconciliation with liquidsoap happens in start_recording."""
    return AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()


def start_recording():
    """Start a new aircheck session. Returns (session, error) with one
    being None. Idempotent-ish: if a session is already running the
    caller gets it back with a note, not an error.

    Triggers `aircheck.reopen` over telnet -- liquidsoap closes its
    current working file (AIRCHECK_CURRENT_PATH) and starts a fresh
    one at the same path. The session row records the INTENDED final
    destination; the actual file lives at AIRCHECK_CURRENT_PATH until
    Stop moves it there."""
    _reap_stale_running_session()
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
    gets closed cleanly, then moves the just-closed file to the
    session's destination path."""
    session = AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()
    if session is None:
        return None, "no active session"

    telnet_note = ""
    try:
        _send_telnet(f"{AIRCHECK_OUTPUT_ID}.reopen")
    except TelnetError as exc:
        # Working file may still be flushed on close by whatever's left
        # of liquidsoap; we can still attempt the move below.
        telnet_note = f"telnet reopen failed at Stop: {exc}; "

    # After reopen, liquidsoap has closed AIRCHECK_CURRENT_PATH and
    # begun a fresh file at the same path. Move the just-closed file
    # to the session's declared destination. Use shutil.move (which
    # falls back to copy+unlink across filesystems) since /run is
    # tmpfs and the destination is typically on a spinning disk.
    working = Path(AIRCHECK_CURRENT_PATH)
    dest = Path(session.filename)
    move_note = ""
    if working.is_file():
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(working), str(dest))
        except OSError as exc:
            move_note = f"move {working} -> {dest} failed: {exc}"
    else:
        move_note = f"working file {working} was missing at Stop -- no audio to move"

    session.still_running = False
    session.ended_at = timezone.now()
    if dest.is_file():
        try:
            session.size_bytes = dest.stat().st_size
        except OSError:
            pass
    session.exit_note = (telnet_note + move_note).strip("; ") or ""
    session.save()
    return session, None
