"""The accepted audio (container, codec) matrix and the content sniffer.

Authority order: the BYTES decide, never the filename or the browser's MIME
type. Two independent checks must agree before a file is accepted:

1. ``sniff_container`` reads a few leading bytes and names the container. This
   is NOT the validation verdict -- it is a security gate that selects exactly
   one of a fixed set of demuxers, so ffmpeg is never allowed to auto-probe an
   uploaded file into an exotic demuxer (HLS/DASH/concat/SDP playlists that
   reference other files or URLs, image sequences, devices ...).
2. ffprobe, restricted to that one demuxer, reports the real container and
   stream facts; ``classify`` maps them onto the allowlist below.

Whether a codec is really decodable on THIS host is proven by decoding, never
by this table: the validator decodes every file with ffmpeg and, by default,
with the engine's GStreamer pipeline, and ``decoder_names`` lets it ask ffmpeg
whether the needed decoder is installed so a missing decoder is reported as an
infrastructure problem instead of a bad file.
"""
from __future__ import annotations

from dataclasses import dataclass

# canonical container -> (ffmpeg demuxer to force, ffprobe format_name tokens that confirm it)
CONTAINERS = {
    "wav": ("wav", {"wav"}),
    "aiff": ("aiff", {"aiff"}),
    "flac": ("flac", {"flac"}),
    "mp3": ("mp3", {"mp3"}),
    "ogg": ("ogg", {"ogg"}),
    "matroska": ("matroska", {"matroska", "webm"}),
    "mp4": ("mov", {"mp4", "mov", "m4a"}),
}

# canonical container -> allowed audio codec_name values (ffprobe vocabulary)
ALLOWED_CODECS = {
    "wav": {"pcm_u8", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le"},
    "aiff": {"pcm_s16be", "pcm_s24be", "pcm_s32be"},
    "flac": {"flac"},
    "mp3": {"mp3"},
    "ogg": {"opus", "vorbis"},
    "matroska": {"opus", "vorbis"},
    "mp4": {"aac", "alac"},
}

# codec_name -> ffmpeg decoder names, any one of which suffices.
DECODERS = {
    "pcm_u8": ("pcm_u8",), "pcm_s16le": ("pcm_s16le",), "pcm_s24le": ("pcm_s24le",),
    "pcm_s32le": ("pcm_s32le",), "pcm_f32le": ("pcm_f32le",),
    "pcm_s16be": ("pcm_s16be",), "pcm_s24be": ("pcm_s24be",), "pcm_s32be": ("pcm_s32be",),
    "flac": ("flac",), "mp3": ("mp3float", "mp3"), "opus": ("opus", "libopus"),
    "vorbis": ("vorbis", "libvorbis"), "aac": ("aac",), "alac": ("alac",),
}

# What a browser <audio>/<source> element should be told. Derived from the
# VALIDATED (container, codec), never from the declared type.
_CONTENT_TYPES = {
    "wav": "audio/wav", "aiff": "audio/aiff", "flac": "audio/flac", "mp3": "audio/mpeg",
    "ogg": "audio/ogg", "matroska": "audio/webm", "mp4": "audio/mp4",
}


def content_type_for(container: str) -> str:
    return _CONTENT_TYPES.get(container, "application/octet-stream")


def decoder_names(codec: str) -> tuple[str, ...]:
    return DECODERS.get(codec, ())


def _syncsafe(four: bytes) -> int:
    return (four[0] & 0x7F) << 21 | (four[1] & 0x7F) << 14 | (four[2] & 0x7F) << 7 | (four[3] & 0x7F)


def _is_mpeg_audio_sync(first: int, second: int) -> bool:
    """MPEG-1/2/2.5 audio frame sync: 11 sync bits, a non-reserved version and
    a non-reserved layer. Raw ADTS AAC (0xFFF1/0xFFF9) has layer bits 00 and is
    therefore NOT claimed here."""
    return first == 0xFF and (second & 0xE0) == 0xE0 and ((second >> 3) & 3) != 1 and ((second >> 1) & 3) != 0


def sniff_container(head: bytes, read_at) -> str | None:
    """Name the container from leading bytes, or None if it is not on the
    allowlist. ``head`` is at least the first 16 bytes (fewer for a tiny file);
    ``read_at(offset, n)`` returns bytes at an offset (used only to look past
    a leading ID3v2 tag, which both FLAC and MP3 files may carry)."""
    if len(head) < 4:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"FORM" and head[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    if head[:4] == b"fLaC":
        return "flac"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    if len(head) >= 8 and head[4:8] == b"ftyp":
        return "mp4"
    if head[:3] == b"ID3" and len(head) >= 10:
        # Skip the tag (bounded) and look at what follows.
        offset = 10 + _syncsafe(head[6:10])
        if offset > 16 * 1024 * 1024:
            return None
        after = read_at(offset, 4)
        if after[:4] == b"fLaC":
            return "flac"
        if len(after) >= 2 and _is_mpeg_audio_sync(after[0], after[1]):
            return "mp3"
        return None
    if _is_mpeg_audio_sync(head[0], head[1]):
        return "mp3"
    return None


@dataclass(frozen=True)
class Classification:
    container: str
    codec: str


def classify(sniffed: str, format_name: str, audio_codec: str) -> tuple[Classification | None, str | None]:
    """Cross-check the sniffed container, ffprobe's format_name and the audio
    codec against the allowlist. Returns (classification, None) or
    (None, media-verdict code)."""
    if sniffed not in CONTAINERS:
        return None, "unsupported_container"
    tokens = {token.strip() for token in format_name.split(",")}
    if not tokens & CONTAINERS[sniffed][1]:
        # Bytes say one thing, the demuxer another: refuse rather than guess.
        return None, "unsupported_container"
    if audio_codec not in ALLOWED_CODECS[sniffed]:
        return None, "unsupported_codec"
    return Classification(sniffed, audio_codec), None


def supported_formats() -> list[tuple[str, str]]:
    """Every (container, codec) pair the validator is willing to accept."""
    return sorted((container, codec) for container, codecs in ALLOWED_CODECS.items() for codec in codecs)
