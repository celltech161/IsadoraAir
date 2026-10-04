"""Shared test support: an isolated media root and deterministic fixtures.

No test may ever touch /srv/isadoraair/production-media or the production
database. Every DB-using test class mixes in IsolatedMediaRootMixin (a meta test
in test_isolation.py enforces that), and audio fixtures are generated with
ffmpeg's lavfi sources at test time -- nothing binary is committed to Git.
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import override_settings

PRODUCTION_DEFAULT_ROOT = "/srv/isadoraair/production-media"
SHA_A = "a" * 64


def _snapshot_real_root():
    """Existence + a recursive (path, size, mtime) listing of the REAL production
    media root. Taken once at import, before any test runs."""
    root = Path(PRODUCTION_DEFAULT_ROOT)
    if not root.exists():
        return None
    entries = []
    for path in sorted(root.rglob("*")):
        try:
            info = path.lstat()
        except OSError:
            continue
        entries.append((str(path), info.st_size, info.st_mtime_ns))
    return entries


REAL_ROOT_AT_IMPORT = _snapshot_real_root()


class IsolatedMediaRootMixin:
    """Points PRODUCTION_MEDIA_ROOT at a private temporary directory, and fails
    any test that leaves the real /srv/isadoraair/production-media changed."""

    def setUp(self):
        super().setUp()
        self.addCleanup(self.assert_real_root_untouched)
        self._root_dir = tempfile.TemporaryDirectory(prefix="prodmedia-test-")
        self.root = Path(self._root_dir.name) / "production-media"
        override = override_settings(PRODUCTION_MEDIA_ROOT=str(self.root))
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self._root_dir.cleanup)
        assert str(self.root) != PRODUCTION_DEFAULT_ROOT
        assert not str(self.root).startswith("/srv/")

    def assert_real_root_untouched(self):
        assert _snapshot_real_root() == REAL_ROOT_AT_IMPORT, "a test modified the real production-media root"

    def list_files(self, subdir):
        base = self.root / subdir
        if not base.exists():
            return []
        return sorted(str(path.relative_to(base)) for path in base.rglob("*") if path.is_file())


class _Fixtures:
    """Lazily generated, process-wide fixture directory."""

    _directory = None
    _paths = {}

    @classmethod
    def root(cls):
        if cls._directory is None:
            cls._directory = Path(tempfile.mkdtemp(prefix="prodmedia-fixtures-"))
            atexit.register(shutil.rmtree, cls._directory, ignore_errors=True)
        return cls._directory


def _ffmpeg(*args, stdout=None):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        check=True, stdout=stdout, stderr=subprocess.PIPE,
    )


def _sine(seconds=2, rate=44100, frequency=440):
    return ["-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate={rate}:duration={seconds}"]


def _truncate(source: Path, target: Path, fraction=0.55):
    data = source.read_bytes()
    target.write_bytes(data[: int(len(data) * fraction)])


def fixture(name: str) -> bytes:
    """Bytes of the named fixture, generating it on first use."""
    if name in _Fixtures._paths:
        return _Fixtures._paths[name].read_bytes()
    directory = _Fixtures.root()
    path = directory / name

    def render(*args, **kwargs):
        _ffmpeg(*args, str(path), **kwargs)

    seconds = 4   # long enough for truncation tests to be unambiguous
    if name == "wav16_mono.wav":
        render(*_sine(seconds), "-ac", "1", "-c:a", "pcm_s16le")
    elif name == "wav24_stereo.wav":
        render(*_sine(seconds, 48000), "-ac", "2", "-c:a", "pcm_s24le")
    elif name == "flac.flac":
        render(*_sine(seconds), "-c:a", "flac")
    elif name == "mp3.mp3":
        render(*_sine(seconds), "-c:a", "libmp3lame")
    elif name == "opus.ogg":
        render(*_sine(seconds), "-c:a", "libopus")
    elif name == "vorbis.ogg":
        render(*_sine(seconds), "-c:a", "libvorbis")
    elif name == "opus_seekable.webm":
        render(*_sine(seconds), "-c:a", "libopus")
    elif name == "opus_live.webm":
        # Non-seekable output: no duration in the header, like browser recorders.
        with open(path, "wb") as handle:
            _ffmpeg(*_sine(seconds), "-c:a", "libopus", "-f", "webm", "-", stdout=handle)
    elif name == "aac.m4a":
        render(*_sine(seconds), "-c:a", "aac")
    elif name == "alac.m4a":
        render(*_sine(seconds), "-c:a", "alac")
    elif name == "pcm.aiff":
        render(*_sine(seconds), "-c:a", "pcm_s16be")
    elif name == "mp3_cover.mp3":
        cover = directory / "cover.jpg"
        _ffmpeg("-f", "lavfi", "-i", "color=c=red:s=64x64", "-frames:v", "1", str(cover))
        render(*_sine(seconds), "-i", str(cover), "-map", "0:a", "-map", "1:v", "-c:a", "libmp3lame",
               "-c:v", "copy", "-disposition:v:0", "attached_pic", "-id3v2_version", "3")
    elif name == "big_tags.flac":
        render(*_sine(2), "-metadata", "comment=" + "A" * 100000, "-c:a", "flac")   # one argv element must stay < 128 KiB
    elif name == "silence.wav":
        render("-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}", "-c:a", "pcm_s16le")
    # --- unsupported / invalid topology -------------------------------------
    elif name == "video_audio.mp4":
        render("-f", "lavfi", "-i", "testsrc=size=64x64:rate=10:duration=2", *_sine(2),
               "-c:v", "mpeg4", "-c:a", "aac", "-shortest")
    elif name == "two_audio.mka":
        render(*_sine(2), *_sine(2, 44100, 880), "-map", "0:a", "-map", "1:a", "-c:a", "libopus")
    elif name == "three_channel.wav":
        render(*_sine(2), "-ac", "3", "-c:a", "pcm_s16le")
    elif name == "rate_4k.wav":
        render(*_sine(2, 4000), "-c:a", "pcm_s16le")
    elif name == "rate_384k.wav":
        render(*_sine(1, 384000), "-c:a", "pcm_s16le")
    elif name == "adpcm.wav":
        render(*_sine(2), "-c:a", "adpcm_ms")
    elif name == "flac_in_ogg.ogg":
        render(*_sine(2), "-c:a", "flac", "-f", "ogg")
    elif name == "mp2.mp2":
        render(*_sine(2), "-c:a", "mp2")
    elif name == "raw_aac.aac":
        render(*_sine(2), "-c:a", "aac", "-f", "adts")
    elif name == "tiny.wav":
        render(*_sine(1, 8000), "-t", "0.005", "-c:a", "pcm_s16le")
    # --- truncated / corrupt ---------------------------------------------------
    elif name.startswith("truncated_"):
        source_name = name[len("truncated_"):]
        fixture(source_name)
        _truncate(_Fixtures._paths[source_name], path)
    elif name == "corrupt_flac.flac":
        data = bytearray(fixture("flac.flac"))
        middle = len(data) // 2
        for offset in range(middle, middle + 400):
            data[offset] ^= 0xA5
        path.write_bytes(bytes(data))
    elif name == "lying_fmt.wav":
        # A WAV whose fmt chunk claims MP3 (0x0055) over PCM data: GStreamer
        # reports STREAM WRONG_TYPE -- a genuine media (not capability) error.
        import struct
        data = bytearray(fixture("wav16_mono.wav"))
        struct.pack_into("<H", data, 20, 0x0055)
        path.write_bytes(bytes(data))
    elif name == "garbage.bin":
        path.write_bytes(bytes((index * 37 + 11) % 256 for index in range(4096)))
    elif name == "text.wav":
        path.write_text("This is not audio.\n" * 50)
    elif name == "hls_playlist.m3u8":
        path.write_text("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\n"
                        "file:///etc/hostname\n#EXT-X-ENDLIST\n")
    elif name == "concat_list.txt":
        path.write_text("ffconcat version 1.0\nfile '/etc/hostname'\n")
    else:
        raise KeyError(name)
    _Fixtures._paths[name] = path
    return path.read_bytes()


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class ChunkedSource:
    """A file-like source that records every read request (no real file)."""

    def __init__(self, total, *, fail_after=None, fill=b"\x00"):
        self.total = total
        self.sent = 0
        self.requests = []
        self.fail_after = fail_after
        self.fill = fill

    def read(self, size=-1):
        self.requests.append(size)
        if self.fail_after is not None and self.sent >= self.fail_after:
            raise ConnectionError("client went away")
        count = min(size if size and size > 0 else self.total, self.total - self.sent)
        self.sent += count
        return self.fill * count


def make_row(**overrides):
    """A ProductionMedia row created directly (no bytes) for model-level tests."""
    import uuid

    from production.models import ProductionMedia
    from production.services import layout

    media_id = overrides.pop("id", uuid.uuid4())
    columns = dict(
        id=media_id, kind="upload", storage_key=layout.storage_key_for(media_id), sha256=SHA_A, byte_size=10,
    )
    columns.update(overrides)
    return ProductionMedia.objects.create(**columns)


VALID_FACTS = dict(
    container="wav", codec="pcm_s16le", sample_rate=44100, channels=1,
    decoded_duration_seconds="2.000000", validation_state="valid", validation_code="ok",
)


def mark_purged_leaving_bytes(media, when=None):
    """Put a row into the 'purged' state through the real explicit transition,
    WITHOUT unlinking its bytes -- i.e. exactly the state a crash between the
    purge commit and its on_commit unlink leaves behind."""
    from django.utils import timezone

    from production import transitions

    assert transitions.mark_purged(media.pk, when or timezone.now()) == 1
