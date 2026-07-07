"""StereoTool's own simplified ASCII RDS dialect -- plain newline-terminated
'KEY=value' text commands, no CRC/framing at all (unlike binary UECP).
Confirmed via StereoTool's developer on their own support forum.

Commands: PS=..., RT=..., RT+=type,start,length-1[,type,start,length-1],
PI=..., PTY=..., DI=..., MS=.... TA has no ASCII-mode command at all
(confirmed via a 2016 forum thread) -- deliberately never emitted here,
not silently dropped as a bug.

RT+ content-type codes (matches StereoTool's own documented example,
"RT+=4,12,16,1,32,4" tagging a 17-char artist at offset 12 and a 5-char
title at offset 32): 1 = ITEM.TITLE, 4 = ITEM.ARTIST.
"""

RT_PLUS_TITLE = 1
RT_PLUS_ARTIST = 4


def build_rt_plus_tag(content_type: int, start: int, length: int) -> str:
    """Formats one 'type,start,length-1' triple. The -1 encoding is
    applied here (not by callers) since StereoTool's own forum confirms
    this is a real, easy-to-miss gotcha -- the third number is length
    MINUS ONE, not the raw length."""
    return f"{content_type},{start},{max(length - 1, 0)}"


def build_ascii_commands(
    pi_code: str,
    ps: str,
    rt: str,
    pty: int,
    music: bool,
    di_dynamic_pty: bool,
    di_compressed: bool,
    di_artificial_head: bool,
    di_stereo: bool,
    rt_plus_tags: list | None = None,
) -> list:
    """Returns a list of newline-terminated 'KEY=value' command strings.
    TA/TP are never included -- StereoTool's ASCII dialect has no TA
    command, and this project doesn't have a documented ASCII TP command
    either, so both are UECP-mode-only features in this version."""
    commands = []
    if ps:
        commands.append(f"PS={ps[:8]}")
    if rt:
        commands.append(f"RT={rt[:64]}")
    if rt_plus_tags:
        commands.append("RT+=" + ",".join(rt_plus_tags))
    if pi_code:
        commands.append(f"PI={pi_code}")
    commands.append(f"PTY={pty}")
    commands.append(f"MS={1 if music else 0}")
    di_value = (
        (0x01 if di_dynamic_pty else 0x00)
        | (0x02 if di_compressed else 0x00)
        | (0x04 if di_artificial_head else 0x00)
        | (0x08 if di_stereo else 0x00)
    )
    commands.append(f"DI={di_value}")
    return commands
