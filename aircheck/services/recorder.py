"""Aircheck recorder -- spawn/kill ffmpeg for on-demand recording.

Design: Django API views call start_recording() / stop_recording() /
current_session() directly; there's no separate daemon. ffmpeg runs as
a detached subprocess (start_new_session=True) so a gunicorn worker
restart while a recording is in progress does not kill the recording.
The AircheckSession row is the authoritative record of what's running;
the ffmpeg_pid on the row is how a subsequent worker (or a different
worker in the same restart) locates the running process.

Format choices map to ffmpeg encoder args here. HE-AAC uses libfdk_aac
(the fdkaac binary the encoders' liquidsoap also depends on -- see
encoders/services/encoder_manager.py). MP3 uses libmp3lame. FLAC/WAV
skip bitrate entirely.
"""
import os
import signal
import subprocess
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from aircheck.models import AircheckConfig, AircheckSession


def _process_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _reap_stale_running_session():
    """If a session row is marked still_running but its ffmpeg PID is
    dead (worker crash mid-record, host reboot, kill from outside),
    close the row so a new session can start cleanly. Called at the
    top of start_recording() to avoid the "already recording" gate
    tripping on a ghost."""
    stale = AircheckSession.objects.filter(still_running=True)
    for s in stale:
        if not _process_alive(s.ffmpeg_pid):
            s.still_running = False
            s.ended_at = timezone.now()
            s.exit_note = f"reaped stale row (pid {s.ffmpeg_pid} not alive)"
            if s.filename and Path(s.filename).is_file():
                try:
                    s.size_bytes = Path(s.filename).stat().st_size
                except OSError:
                    pass
            s.save()


def current_session():
    """Return the currently-running AircheckSession or None. Runs the
    stale-reaper first so a caller polling status doesn't see a ghost."""
    _reap_stale_running_session()
    return AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()


def _bitrate_to_bps(bitrate):
    """'64k' -> 64000, '320k' -> 320000, '128000' -> 128000. fdkaac
    takes raw bps on the CLI; ffmpeg takes '64k' shorthand. This
    normalizes the config value for fdkaac."""
    if not bitrate:
        return 64000
    s = str(bitrate).strip().lower()
    if s.endswith("k"):
        return int(s[:-1]) * 1000
    return int(s)


def _spawn_recorder(cfg, out_path):
    """Spawn the recording process(es) and return (pid_to_track, err).
    For he_aac we pipe ffmpeg's raw PCM into /usr/local/bin/fdkaac
    (Ubuntu ffmpeg is not built with libfdk_aac; the standalone
    fdkaac binary is the same one the encoders' liquidsoap uses --
    see encoders/services/encoder_manager.py's %external fdkaac
    process). The pipeline is spawned via `sh -c` in a new session
    so killing the session leader signals both ffmpeg and fdkaac
    cleanly -- stop_recording targets the process GROUP, not a
    single PID.

    For mp3/flac/wav a plain ffmpeg suffices."""
    if cfg.audio_format == "he_aac":
        bps = _bitrate_to_bps(cfg.effective_bitrate())
        # -p 29 = HE-AAC v1 (SBR), same profile the shoutcast HE-AAC
        # stream uses. -f 5 = MP4 container (m4a). -a 1 = afterburner
        # on for higher quality at low bitrates. -S = silent.
        cmd = (
            f"ffmpeg -hide_banner -nostats -loglevel warning "
            f"-f alsa -ac 2 -ar 44100 -i {_shell_quote(cfg.source_device)} "
            f"-f s16le -ar 44100 -ac 2 - "
            f"| /usr/local/bin/fdkaac -R --raw-channels 2 --raw-rate 44100 "
            f"--raw-format S16L -p 29 -a 1 -S -b {bps} "
            f"-o {_shell_quote(str(out_path))} -"
        )
        argv = ["sh", "-c", cmd]
    else:
        argv = [
            "ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning",
            "-f", "alsa", "-ac", "2", "-ar", "44100", "-i", cfg.source_device,
        ]
        if cfg.audio_format == "mp3":
            argv += ["-c:a", "libmp3lame", "-b:a", cfg.effective_bitrate()]
        elif cfg.audio_format == "flac":
            argv += ["-c:a", "flac"]
        elif cfg.audio_format == "wav":
            argv += ["-c:a", "pcm_s16le"]
        argv += [str(out_path)]

    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError) as exc:
        return None, f"failed to spawn recorder: {exc}"
    return proc.pid, None


def _shell_quote(s):
    """Minimal safe shell quoting for ALSA device names / paths that
    might contain spaces or special chars. Not a substitute for shlex
    in general, but fine for the constrained inputs here."""
    return "'" + str(s).replace("'", "'\\''") + "'"


def start_recording():
    """Start a new aircheck session using the singleton AircheckConfig.
    Returns (session, error) -- one is None. Idempotent-ish: if a
    session is already running the caller gets it back with a note,
    not an error."""
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

    # Guard: don't clobber a file at the same path (rare -- only if the
    # operator hit Start twice within the same second and the template
    # doesn't include sub-second granularity).
    if out_path.exists():
        out_path = out_dir / f"{stamp}-{datetime.now().microsecond}.{cfg.file_extension()}"

    pid, err = _spawn_recorder(cfg, out_path)
    if pid is None:
        return None, err

    session = AircheckSession.objects.create(
        filename=str(out_path),
        audio_format=cfg.audio_format,
        bitrate=cfg.effective_bitrate(),
        source_device=cfg.source_device,
        ffmpeg_pid=pid,
        still_running=True,
    )
    return session, None


def stop_recording():
    """Stop the currently-running session, if any. Returns
    (session, error). Sends SIGINT so ffmpeg finalizes container
    trailer + closes the file cleanly; SIGKILL as fallback if ffmpeg
    doesn't respect the SIGINT within a short deadline."""
    session = AircheckSession.objects.filter(still_running=True).order_by("-started_at").first()
    if session is None:
        return None, "no active session"

    if _process_alive(session.ffmpeg_pid):
        # For HE-AAC we start ffmpeg | fdkaac in a `sh -c` inside a
        # new session -- the ffmpeg_pid is the shell's PID, but we
        # want to signal BOTH the shell's ffmpeg AND fdkaac child.
        # Targeting the process GROUP catches both. Falls back to
        # the single PID if the group lookup fails (defensive; the
        # start-time start_new_session=True should guarantee a group
        # equal to the PID).
        try:
            pgid = os.getpgid(session.ffmpeg_pid)
        except OSError:
            pgid = session.ffmpeg_pid
        # SIGTERM (not SIGINT) -- ffmpeg's SIGINT handler expects a TTY
        # and can miss the signal when stdin=DEVNULL, whereas SIGTERM
        # is caught by its shutdown hook reliably. Also gives the
        # pipeline enough time to flush -- for HE-AAC the moov atom
        # write during fdkaac's SIGPIPE handshake can take a beat.
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError as exc:
            session.exit_note = f"SIGTERM failed: {exc}"

        import time
        for _ in range(60):  # up to ~6s
            if not _process_alive(session.ffmpeg_pid):
                break
            time.sleep(0.1)
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
                session.exit_note = (session.exit_note + "; " if session.exit_note else "") + "SIGKILL after SIGTERM timeout"
            except OSError:
                pass

    session.still_running = False
    session.ended_at = timezone.now()
    if Path(session.filename).is_file():
        try:
            session.size_bytes = Path(session.filename).stat().st_size
        except OSError:
            pass
    session.save()
    return session, None
