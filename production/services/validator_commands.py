"""The ONLY command lines the validation service will execute (2.22B).

``production.services.validation`` builds its tool invocations with these
functions, and the validation service (production.services.validation_service)
accepts a request only if its argv is exactly one of them, rebuilt from the
SERVICE's own tool configuration. A request therefore cannot name an
executable, add an option, point at a path, set an environment variable or
choose a limit: the media travels as an already-open file descriptor and
appears in the command line only as ``/proc/self/fd/<n>``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from production import formats

#: Stands for the media in a request's argv; the service substitutes the
#: received descriptor (``/proc/self/fd/<n>``).
MEDIA = "@media"

PROBE_ENTRIES = (
    "format=format_name,duration,nb_streams:"
    "stream=index,codec_type,codec_name,sample_rate,channels,channel_layout,duration:"
    "stream_disposition=attached_pic"
)
DEMUXERS = frozenset(spec[0] for spec in formats.CONTAINERS.values())
DECODERS = frozenset(name for names in formats.DECODERS.values() for name in names)
_DECODER_OPTION = re.compile(r"^decoder=([a-z0-9_]{1,32})$")


def tool_version(binary):
    return [binary, "-version"]


def decoder_help(ffmpeg, name):
    return [ffmpeg, "-hide_banner", "-h", f"decoder={name}"]


def probe(ffprobe, demuxer, media):
    return [ffprobe, "-v", "error", "-hide_banner", "-protocol_whitelist", "file", "-f", demuxer,
            "-show_entries", PROBE_ENTRIES, "-of", "json", str(media)]


def decode(ffmpeg, demuxer, media):
    return [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror", "-nostats",
            "-progress", "pipe:1", "-stats_period", "100000", "-threads", "1",
            "-protocol_whitelist", "file", "-f", demuxer, "-i", str(media), "-map", "0:a:0", "-f", "null", "-"]


def engine_decode(interpreter, script, child_timeout, media):
    return [*interpreter, script, "--timeout", str(child_timeout), str(media)]


@dataclass(frozen=True)
class Tools:
    """The service's own configuration -- never taken from a request."""
    ffprobe: str
    ffmpeg: str
    interpreter: tuple
    probe_script: str
    probe_child_timeout: float
    capability_seconds: float
    probe_seconds: float
    decode_seconds: float
    engine_seconds: float


@dataclass(frozen=True)
class Command:
    op: str
    needs_media: bool
    max_seconds: float


def resolve(argv, tools: Tools) -> Command | None:
    """The command ``argv`` is, or None. Exact equality with a command rebuilt
    from ``tools``; the only variable parts are a demuxer or decoder name
    from a closed allowlist and the MEDIA placeholder."""
    if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
        return None
    if argv in (tool_version(tools.ffprobe), tool_version(tools.ffmpeg)):
        return Command("version", False, tools.capability_seconds)
    if len(argv) == 4:
        match = _DECODER_OPTION.match(argv[3])
        if match and match.group(1) in DECODERS and argv == decoder_help(tools.ffmpeg, match.group(1)):
            return Command("decoder", False, tools.capability_seconds)
    if "-f" in argv[:-1]:
        demuxer = argv[argv.index("-f") + 1]
        if demuxer in DEMUXERS:
            if argv == probe(tools.ffprobe, demuxer, MEDIA):
                return Command("probe", True, tools.probe_seconds)
            if argv == decode(tools.ffmpeg, demuxer, MEDIA):
                return Command("decode", True, tools.decode_seconds)
    if argv == engine_decode(tools.interpreter, tools.probe_script, tools.probe_child_timeout, MEDIA):
        return Command("engine", True, tools.engine_seconds)
    return None
