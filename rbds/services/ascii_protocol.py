"""StereoTool's own simplified ASCII RDS dialect -- plain newline-terminated
'KEY=value' text commands, no CRC/framing at all (unlike binary UECP).
Confirmed via StereoTool's developer on their own support forum.

Commands: PS=..., RT=..., PI=..., PTY=..., DI=..., MS=.... TA has no
ASCII-mode command at all (confirmed via a 2016 forum thread) --
deliberately never emitted here, not silently dropped as a bug.

RT+ handling: StereoTool parses inline RT+ markers `\\+AR<artist>\\-` and
`\\+TI<title>\\-` embedded directly in the RT= value; caller
(rbds_manager._resolve_rt_content) builds those markers when
config.use_rt_plus is on and a real artist+title exist. The old
separate `RT+=type,start,length-1` command is not sent -- StereoTool
still supports it, but the marker-embedded form is what actually
made RT+ work end-to-end here (the explicit-offsets command's UECP
counterparts turned out unrecognized by StereoTool, so this project
standardizes on markers across both protocols for consistency).

Marker content-type codes are still defined here for reference and for
the message model's rt_plus_delimiter feature: 1 = ITEM.TITLE (\\+TI),
4 = ITEM.ARTIST (\\+AR).
"""

RT_PLUS_TITLE = 1
RT_PLUS_ARTIST = 4


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
) -> list:
    """Returns a list of newline-terminated 'KEY=value' command strings.
    TA/TP are never included -- StereoTool's ASCII dialect has no TA
    command, and this project doesn't have a documented ASCII TP command
    either, so both are UECP-mode-only features in this version.

    rt cap is 80 chars (not 64) so an rt string carrying inline RT+
    markers has room for the ~12 chars of marker overhead without
    truncating the closing `\\-`. StereoTool strips markers before
    counting toward the on-air 64-char RT length receivers ultimately
    see."""
    commands = []
    if ps:
        commands.append(f"PS={ps[:8]}")
    if rt:
        commands.append(f"RT={rt[:80]}")
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
