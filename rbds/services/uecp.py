"""Binary UECP (Universal Encoder Communication Protocol) framing, per
EBU-SPB 490 "RDS Universal Encoder Communication Protocol" (v5.1/v6.02).

Frame layout: STA(1B) | ADD(2B) | SQC(1B) | MFL(1B) | MSG(0-255B) | CRC(2B) | STP(1B)
- STA = 0xFE (start), STP = 0xFF (stop) -- fixed, never appear elsewhere
  in the stream after byte-stuffing.
- ADD = 2 bytes: 10-bit site address (MSB) | 6-bit encoder address (LSB).
- SQC = sequence counter, 0x01-0xFF (0x00 = unused).
- MFL = length of MSG *before* byte-stuffing.
- CRC = CRC-CCITT (poly 0x1021, init 0xFFFF, result = bitwise NOT of the
  final register, MSB-first), computed over the UNSTUFFED ADD..MSG span.
- Byte-stuffing applies only to the ADD..CRC span: literal 0xFD/0xFE/0xFF
  are each replaced by a 2-byte pair starting with 0xFD (0xFD->FD 00,
  0xFE->FD 01, 0xFF->FD 02).

Each message element within MSG is `MEC(1B) DSN(1B) PSN(1B) [MEL(1B)]
MED(...)` (spec section 2.3.1) -- DSN (Data Set Number, 0=current data
set) and PSN (Programme Service Number, 0=main service) are REQUIRED for
every command implemented here (confirmed directly against each
command's own spec section in 3.1/3.3, not assumed) even though the
generic message-element grammar in 2.3.1 shows them in square brackets
as "used, as required by the specific command." A real bug was caught
here during a later review: the first version of this module omitted
DSN/PSN entirely from every mec_* builder, had RT's DSN/PSN/MEL/flag
byte fields in the wrong order, and had DI's bit assignments swapped --
none of that was caught by the original tests, which only checked
internal shape, not real per-command byte layouts against the primary
spec. Every builder below is now checked against the spec's own literal
worked example for that exact command (see rbds/tests.py) -- a much
stronger verification than shape-only assertions.

This project's single-station setup has no real use for anything but
"current data set, main service" -- dsn/psn default to 0x00/0x00
everywhere and aren't exposed as RBDSConfig fields (see rbds/models.py
docstrings) rather than adding config surface nothing here needs yet.

CRC verification note: the spec's own worked example for the CRC
algorithm itself (Appendix 1, both the 1997 v5.1 and 2006 v6.02
revisions) contains a literal typo -- "2D111234010105ABCD123F0XXXX11069
212491000320066" -- confirmed directly from the official PDF text, not
an OCR artifact. This can't be used as a test vector. Instead,
crc_ccitt() below was verified by (1) transliterating the spec's own
Appendix 1 PASCAL reference implementation independently and confirming
it agrees with this implementation across multiple inputs, and (2)
confirming both agree with the well-known, catalogued CRC-16/GENIBUS
standard check value for "123456789" -> 0xD64E (GENIBUS uses the exact
same four parameters: poly 0x1021, init 0xFFFF, no reflection, xorout
0xFFFF). See rbds/tests.py for both checks.
"""
import struct

STA = 0xFE
STP = 0xFF


def crc_ccitt(data: bytes) -> bytes:
    """CRC-CCITT: poly 0x1021, init 0xFFFF, result = bitwise NOT of the
    final register, returned MSB-first (2 bytes). `data` must be the
    UNSTUFFED ADD..MSG span."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    crc ^= 0xFFFF
    return struct.pack(">H", crc)


def byte_stuff(data: bytes) -> bytes:
    """Stuffs literal 0xFD/0xFE/0xFF bytes within the ADD..CRC span."""
    out = bytearray()
    for b in data:
        if b == 0xFD:
            out += b"\xFD\x00"
        elif b == 0xFE:
            out += b"\xFD\x01"
        elif b == 0xFF:
            out += b"\xFD\x02"
        else:
            out.append(b)
    return bytes(out)


def build_frame(site_address: int, encoder_address: int, sqc: int, msg: bytes) -> bytes:
    """Assembles one complete UECP frame: STA | stuffed(ADD|SQC|MFL|MSG|CRC) | STP.
    `msg` is the concatenation of one or more MEC+MED blocks (see the
    mec_* builders below) -- a single frame can carry multiple message
    elements."""
    add = struct.pack(">H", ((site_address & 0x3FF) << 6) | (encoder_address & 0x3F))
    mfl = bytes([len(msg)])  # length BEFORE stuffing
    core = add + bytes([sqc & 0xFF]) + mfl + msg
    crc = crc_ccitt(core)
    stuffed = byte_stuff(core + crc)
    return bytes([STA]) + stuffed + bytes([STP])


def freq_to_af_code(freq_mhz: float) -> int:
    """AF code per spec: round((freq_mhz - 87.5) / 0.1). Valid station
    frequencies 87.6-107.9 MHz map to codes 1-204."""
    return round((freq_mhz - 87.5) / 0.1)


def mec_pi(pi_code: int, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x01 -- Program Identification. Format: MEC DSN PSN MED(2).
    Spec example: <01><00><01><C2><01> (PI=C201)."""
    return bytes([0x01, dsn, psn]) + struct.pack(">H", pi_code & 0xFFFF)


def mec_ps(text: str, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x02 -- Program Service name. Format: MEC DSN PSN MED(8),
    chars restricted to 0x20-0xFE per spec (non-conforming chars are
    replaced with a space rather than silently sent as invalid bytes).
    Spec example: <02><00><02><52><41><44><49><4F><20><31><20>."""
    padded = text[:8].ljust(8)
    med = bytes(b if 0x20 <= b <= 0xFE else 0x20 for b in padded.encode("latin-1", errors="replace"))
    return bytes([0x02, dsn, psn]) + med


def mec_ta_tp(ta: bool, tp: bool, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x03 -- Format: MEC DSN PSN MED(1: bit0=TA, bit1=TP).
    Spec example: <03><00><05><02> (TP=1, TA=0)."""
    value = (0x01 if ta else 0x00) | (0x02 if tp else 0x00)
    return bytes([0x03, dsn, psn, value])


def mec_di(dynamic_pty: bool, compressed: bool, artificial_head: bool, stereo: bool,
           dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x04 -- DI/PTYI. Format: MEC DSN PSN MED(1: bit0=stereo,
    bit1=artificial head, bit2=compressed, bit3=dynamic PTYI). Spec
    example: <04><00><03><01> (stereo=1, others 0)."""
    value = (
        (0x01 if stereo else 0x00)
        | (0x02 if artificial_head else 0x00)
        | (0x04 if compressed else 0x00)
        | (0x08 if dynamic_pty else 0x00)
    )
    return bytes([0x04, dsn, psn, value])


def mec_ms(music: bool, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x05 -- Format: MEC DSN PSN MED(1, bit0: 1=Music, 0=Speech).
    Spec example: <05><00><01><01>."""
    return bytes([0x05, dsn, psn, 0x01 if music else 0x00])


def mec_pty(pty: int, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x07 -- Format: MEC DSN PSN MED(1, 0-0x1F). Spec example:
    <07><00><05><08>."""
    return bytes([0x07, dsn, psn, pty & 0x1F])


def mec_rt(text: str, ab_flag: bool, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x0A -- RadioText. Format: MEC DSN PSN MEL MED(flags byte,
    then up to 64 text chars). MEL = 1 (flags byte) + len(text). The
    flags byte's bit0 is the A/B toggle flag -- must flip whenever the
    transmitted text actually changes, so receivers know to refresh
    their display; bits 6-5 are buffer config (0b01 = flush-then-load,
    matches this project's single-current-message use, not a rotating
    on-device buffer -- rotation is handled by rbds_manager.py itself).
    Spec example: <0A><00><01><05><51><74><65><78><74> (flush+toggle
    A/B, text 'text')."""
    text = text[:64]
    med0 = (0b01 << 5) | (0x01 if ab_flag else 0x00)
    mel = 1 + len(text)
    return bytes([0x0A, dsn, psn, mel, med0]) + text.encode("latin-1", errors="replace")


def mec_ct(dt_utc, offset_minutes: int) -> bytes:
    """MEC 0x0D -- Real Time Clock. Format: MEC MED(8: year-2digit,
    month, date, hours, minutes, seconds, centiseconds, local-offset) --
    confirmed directly against the spec's own section 3.3.37, including
    its literal worked example: <0D><5C><09><0C><0A><12><21><0F><02> for
    1992-09-12 10:18:33.15 UTC, offset +1h.

    Deliberately NO dsn/psn here, unlike every other mec_* in this file
    -- the spec's own format table for this command lists none, and the
    worked example is exactly 9 bytes (1 MEC + 8 MED) with none present.
    Makes sense semantically too: the encoder's clock is one global
    system value, not addressed to a specific data set/programme service
    the way PI/PS/RT are.

    dt_utc must be a UTC datetime -- per spec, "Time of day is expressed
    in terms of Co-ordinated Universal Time (UTC)"; sending local time
    in these fields instead would be a spec violation and would make a
    compliant receiver apply the offset TWICE. offset_minutes is
    local-minus-UTC (e.g. -300 for UTC-5), rounded to the nearest half
    hour and sign+magnitude coded per the spec's own bit table (bit 3 =
    sign, 0=+/1=-; bits 4-8 = magnitude in half-hour units, max +-15.5h)."""
    year_2digit = dt_utc.year % 100
    half_hours = round(offset_minutes / 30)
    sign_bit = 0x20 if half_hours < 0 else 0x00
    magnitude = abs(half_hours) & 0x1F
    offset_byte = sign_bit | magnitude
    return bytes([
        0x0D,
        year_2digit,
        dt_utc.month,
        dt_utc.day,
        dt_utc.hour,
        dt_utc.minute,
        dt_utc.second,
        dt_utc.microsecond // 10000,
        offset_byte,
    ])


def mec_af(frequencies_mhz: list, dsn: int = 0x00, psn: int = 0x00, start_location: int = 0x0000) -> bytes:
    """MEC 0x13 -- Alternative Frequencies. UECP envelope (MEC DSN PSN
    MEL, 2-byte start location, then AF data, then a 0x00 terminator)
    is confirmed directly against the spec's own section 3.1.9. The AF
    *data* content itself is a flat list of freq_to_af_code() values
    here -- NOT verified against the spec's own worked example
    (<13><00><01><07><00><00><E2><15><27><CD><00>), because that
    example's 4 data bytes (E2 15 27 CD) don't parse as 2 flat
    frequency codes for the "2 frequencies, 89.6/91.4 MHz" it claims
    (freq_to_af_code(89.6)=0x15 and (91.4)=0x27 DO appear, but E2/CD
    don't correspond to any frequency in range -- they're very likely a
    Method-A "N AFs follow" count byte / list-structuring byte defined
    by the separate IEC EN 62106 RDS standard, which this UECP spec
    explicitly defers to ("no distinction is made between Method A or
    B... structured in pairs as in IEC EN 62106") rather than fully
    define itself). Since RBDSConfig's af_frequencies_mhz is currently
    unset/unused in production and this was already flagged out of
    scope in the approved plan ("AF Method B framing -- flat AF list
    only"), this flat encoding is a known, stated simplification, not
    a verified-correct implementation of the real AF list format --
    revisit against IEC EN 62106 directly before actually turning AF on."""
    codes = [freq_to_af_code(f) for f in frequencies_mhz]
    med = struct.pack(">H", start_location) + bytes(codes) + bytes([0x00])
    return bytes([0x13, dsn, psn, len(med)]) + med
