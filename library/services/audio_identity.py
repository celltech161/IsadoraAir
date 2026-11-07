"""The decoded-audio identity of a media file (roadmap 1.21, r0123 Package B).

An audio revision is the SHA-256 of the file's *decoded* audio in one canonical form, so it changes
when the sound changes and not when only the container or its tags do:

* the first audio stream (``-map 0:a:0``; video/cover art, subtitles and data are ignored);
* decoded by the station's ffmpeg at the stream's own sample rate (``ffprobe``; no resampling --
  a stream that changes rate mid-file is resampled to its first rate, deterministically);
* two channels, interleaved left/right: a mono source is upmixed and a multichannel source is
  downmixed by ffmpeg's default (deterministic) rematrix;
* signed 32-bit little-endian integers (``pcm_s32le``): 16- and 24-bit sources are carried
  losslessly, float and lossy decoders are converted deterministically;
* hashed as ``b"isadoraair-audio/v1\\n" + b"<rate>\\n2\\ns32le\\n" + PCM`` (``ALGORITHM``).

Decoding is strict: a missing audio stream, an ffmpeg error message (``-xerror``), a non-zero exit,
no decoded frames or a timeout yields no identity at all (``IdentityUnavailable``), never a partial
one. Equivalent decoded audio has the same identity with the same ffmpeg; an ffmpeg upgrade that
changes a decoder's output changes identities, which only makes caches fail closed (refresh them).

Cost: the PCM is streamed through the hash in 1 MiB chunks (memory is bounded regardless of
length); CPU is one native-rate decode plus SHA-256 -- about 0.25 s per 4-minute song on the
development host, about 4 s per hour of programme. Nothing is stored except the 64-character
digest, the rate and the frame count in the waveform cache.

The source's ``os.stat`` identity (``stat_source``) is the inexpensive race detector around a
decode and the witness that a file is still the one an identity was computed from; it is never a
substitute for the decoded identity. ``Track.file_hash``, file names, sizes and times are never
treated as audio identity.

``compute`` gives ffprobe and ffmpeg the pathname (each opens it itself); ``compute_fd`` gives them a
file the caller already holds open (inherited, read through ``/proc/self/fd/N``), so the identity is of
exactly that open file -- whatever happens to the pathname meanwhile -- and the caller can compare the
open file's ``fstat`` with the witness it expects.

Time: ``timeout`` bounds the whole computation. One ``time.monotonic()`` deadline is shared by the
probe (at most ``PROBE_TIMEOUT_SECONDS`` of it) and the decode, which gets only what the probe left;
nothing is started once it has passed, and ffmpeg is killed when it is reached."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import stat as _stat
import threading
import time
from pathlib import Path

ALGORITHM = "isadoraair-audio/v1:pcm_s32le:2ch:native-rate:sha256"
CHANNELS, SAMPLE_FORMAT, BYTES_PER_FRAME = 2, "s32le", 8
CHUNK_BYTES = 1 << 20
DEFAULT_TIMEOUT_SECONDS = 1800
PROBE_TIMEOUT_SECONDS = 60
_STDERR_LIMIT = 4096
_decoder_version = None

#: The fields of a source witness (``stat_source``); all must match for "the same file".
WITNESS_FIELDS = ("path", "dev", "ino", "size", "mtime_ns", "ctime_ns")
#: Fields that stay the same when a file is only renamed within its filesystem.
CONTENT_FIELDS = ("dev", "ino", "size", "mtime_ns")


class IdentityUnavailable(Exception):
    """The file's decoded audio cannot be identified (the reason is the message)."""


def stat_source(path) -> dict | None:
    """The source witness of a regular file (not following a final symlink), or None."""
    try:
        info = os.stat(path, follow_symlinks=False)
    except (OSError, ValueError):
        return None
    if not _stat.S_ISREG(info.st_mode):
        return None
    return {"path": str(path), "dev": info.st_dev, "ino": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "ctime_ns": info.st_ctime_ns}


def same_witness(a, b) -> bool:
    return isinstance(a, dict) and isinstance(b, dict) and all(a.get(k) == b.get(k) for k in WITNESS_FIELDS)


def same_content_witness(a, b) -> bool:
    """The same inode with the same size and modification time (a rename keeps these)."""
    return isinstance(a, dict) and isinstance(b, dict) and all(a.get(k) == b.get(k) for k in CONTENT_FIELDS)


def well_formed(identity) -> bool:
    return (isinstance(identity, dict) and isinstance(identity.get("algorithm"), str)
            and isinstance(identity.get("sha256"), str) and len(identity["sha256"]) == 64
            and all(c in "0123456789abcdef" for c in identity["sha256"])
            and _positive_int(identity.get("sample_rate")) and _positive_int(identity.get("frames"))
            and identity.get("channels") == CHANNELS and identity.get("sample_format") == SAMPLE_FORMAT)


def same_audio(a, b) -> bool:
    """Both are well-formed identities of this algorithm and describe the same decoded audio."""
    return (well_formed(a) and well_formed(b) and a["algorithm"] == b["algorithm"] == ALGORITHM
            and (a["sha256"], a["sample_rate"], a["frames"]) == (b["sha256"], b["sample_rate"], b["frames"]))


def revision(identity) -> str | None:
    """The short form recorded beside approvals: the digest of a well-formed current identity."""
    return identity["sha256"] if well_formed(identity) and identity["algorithm"] == ALGORITHM else None


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def decoder_version() -> str:
    global _decoder_version
    if _decoder_version is None:
        try:
            out = subprocess.run(["ffmpeg", "-hide_banner", "-version"], capture_output=True, text=True,
                                 timeout=10).stdout
            _decoder_version = (out.splitlines() or ["ffmpeg (unknown)"])[0][:120]
        except (OSError, subprocess.SubprocessError):
            _decoder_version = "ffmpeg (unknown)"
    return _decoder_version


def _remaining(deadline, step) -> float:
    """The time left before ``deadline`` (``time.monotonic``); IdentityUnavailable if none is left."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise IdentityUnavailable(f"the time allowed for identifying the audio ran out before {step}")
    return left


def _probe_rate(source, deadline, pass_fds=()) -> int:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate",
             "-of", "json", source], capture_output=True, stdin=subprocess.DEVNULL, pass_fds=pass_fds,
            timeout=min(_remaining(deadline, "probing"), PROBE_TIMEOUT_SECONDS))
    except subprocess.TimeoutExpired:
        raise IdentityUnavailable("ffprobe timed out") from None
    except OSError as exc:
        raise IdentityUnavailable(f"ffprobe unavailable: {exc.strerror}") from None
    if result.returncode != 0:
        raise IdentityUnavailable("ffprobe could not read the file")
    try:
        streams = json.loads(result.stdout or b"{}").get("streams") or []
        rate = int(streams[0]["sample_rate"]) if streams else 0
    except (ValueError, KeyError, TypeError, AttributeError):
        rate = 0
    if rate <= 0:
        raise IdentityUnavailable("no audio stream")
    return rate


def compute(path, *, timeout=DEFAULT_TIMEOUT_SECONDS) -> dict:
    """The identity of the decoded audio of ``path``; IdentityUnavailable if there is none."""
    deadline = time.monotonic() + timeout
    path = Path(path)
    if stat_source(path) is None:
        raise IdentityUnavailable("not a regular file")
    return _identify(str(path), (), deadline, timeout)


def compute_fd(fd, *, timeout=DEFAULT_TIMEOUT_SECONDS) -> dict:
    """The identity of the decoded audio of the regular file open as ``fd``: ffprobe and ffmpeg inherit the
    descriptor and read that open file (``/proc/self/fd/N``, a new read position for each), never a pathname.
    No pathname fallback: if they cannot read it so, IdentityUnavailable."""
    deadline = time.monotonic() + timeout
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise IdentityUnavailable("not a regular file")
    except (OSError, TypeError, ValueError):
        raise IdentityUnavailable("not an open file") from None
    return _identify(f"/proc/self/fd/{fd}", (fd,), deadline, timeout)


def _identify(source, pass_fds, deadline, timeout) -> dict:
    rate = _probe_rate(source, deadline, pass_fds)
    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror", "-i", source,
           "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", str(CHANNELS), "-ar", str(rate),
           "-c:a", "pcm_s32le", "-f", SAMPLE_FORMAT, "pipe:1"]
    digest = hashlib.sha256(b"isadoraair-audio/v1\n" + f"{rate}\n{CHANNELS}\n{SAMPLE_FORMAT}\n".encode())
    left = _remaining(deadline, "decoding")
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                pass_fds=pass_fds)
    except OSError as exc:
        raise IdentityUnavailable(f"ffmpeg unavailable: {exc.strerror}") from None
    errors = bytearray()

    def drain():
        for chunk in iter(lambda: proc.stderr.read(4096), b""):
            if len(errors) < _STDERR_LIMIT:
                errors.extend(chunk[:_STDERR_LIMIT - len(errors)])
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = threading.Event()
    watchdog = threading.Timer(left, lambda: (timed_out.set(), proc.kill()))
    watchdog.start()
    total = 0
    try:
        for chunk in iter(lambda: proc.stdout.read(CHUNK_BYTES), b""):
            digest.update(chunk)
            total += len(chunk)
        proc.wait()
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(5)
        proc.stdout.close()
        proc.stderr.close()
    if timed_out.is_set():
        raise IdentityUnavailable(f"decoding took longer than the {round(timeout, 3):g}s allowed")
    if proc.returncode != 0 or errors:
        detail = bytes(errors).decode("utf-8", "replace").strip().splitlines()
        raise IdentityUnavailable("the audio does not decode cleanly" + (f": {detail[0][:200]}" if detail else ""))
    if total == 0 or total % BYTES_PER_FRAME:
        raise IdentityUnavailable("no complete decoded frames")
    return {"algorithm": ALGORITHM, "sha256": digest.hexdigest(), "sample_rate": rate, "channels": CHANNELS,
            "sample_format": SAMPLE_FORMAT, "frames": total // BYTES_PER_FRAME, "decoder": decoder_version()}


def compute_or_none(path, **kwargs) -> tuple[dict | None, str]:
    """``(identity, "")`` or ``(None, reason)``."""
    try:
        return compute(path, **kwargs), ""
    except IdentityUnavailable as exc:
        return None, str(exc)


def compute_fd_or_none(fd, **kwargs) -> tuple[dict | None, str]:
    """``compute_fd`` as ``(identity, "")`` or ``(None, reason)``."""
    try:
        return compute_fd(fd, **kwargs), ""
    except IdentityUnavailable as exc:
        return None, str(exc)
