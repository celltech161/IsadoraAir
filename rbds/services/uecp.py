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

CRC verification note: the spec's own worked example (Appendix 1, both
the 1997 v5.1 and 2006 v6.02 revisions) contains a literal typo --
"2D111234010105ABCD123F0XXXX11069212491000320066" -- confirmed directly
from the official PDF text, not an OCR artifact. This can't be used as a
test vector. Instead, crc_ccitt() below was verified by (1) transliterating
the spec's own Appendix 1 PASCAL reference implementation independently
and confirming it agrees with this implementation across multiple inputs,
and (2) confirming both agree with the well-known, catalogued CRC-16/
GENIBUS standard check value for "123456789" -> 0xD64E (GENIBUS uses the
exact same four parameters: poly 0x1021, init 0xFFFF, no reflection,
xorout 0xFFFF). See rbds/tests.py for both checks.
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


def mec_pi(pi_code: int) -> bytes:
    """MEC 0x01 -- Program Identification, 2-byte MED."""
    return bytes([0x01]) + struct.pack(">H", pi_code & 0xFFFF)


def mec_ps(text: str) -> bytes:
    """MEC 0x02 -- Program Service name, 8-byte MED, padded/truncated,
    chars restricted to 0x20-0xFE per spec (non-conforming chars are
    replaced with a space rather than silently sent as invalid bytes)."""
    padded = text[:8].ljust(8)
    med = bytes(b if 0x20 <= b <= 0xFE else 0x20 for b in padded.encode("latin-1", errors="replace"))
    return bytes([0x02]) + med


def mec_ta_tp(ta: bool, tp: bool) -> bytes:
    """MEC 0x03 -- bit0=TA, bit1=TP."""
    value = (0x01 if ta else 0x00) | (0x02 if tp else 0x00)
    return bytes([0x03, value])


def mec_di(dynamic_pty: bool, compressed: bool, artificial_head: bool, stereo: bool) -> bytes:
    """MEC 0x04 -- Decoder Information: bit0=Dynamic PTY, bit1=Compressed,
    bit2=Artificial Head, bit3=Stereo."""
    value = (
        (0x01 if dynamic_pty else 0x00)
        | (0x02 if compressed else 0x00)
        | (0x04 if artificial_head else 0x00)
        | (0x08 if stereo else 0x00)
    )
    return bytes([0x04, value])


def mec_ms(music: bool) -> bytes:
    """MEC 0x05 -- Music/Speech, bit0 (1=Music, 0=Speech)."""
    return bytes([0x05, 0x01 if music else 0x00])


def mec_pty(pty: int) -> bytes:
    """MEC 0x07 -- Program Type, 0-0x1F."""
    return bytes([0x07, pty & 0x1F])


def mec_rt(text: str, ab_flag: bool) -> bytes:
    """MEC 0x0A -- RadioText, up to 64 chars. MED byte 1's bit0 is the
    A/B toggle flag -- must flip whenever the transmitted text actually
    changes, so receivers know to refresh their display."""
    text = text[:64]
    med0 = 0x01 if ab_flag else 0x00
    return bytes([0x0A, len(text) + 1, med0]) + text.encode("latin-1", errors="replace")


def mec_af(frequencies_mhz: list) -> bytes:
    """MEC 0x13 -- Alternative Frequencies, a start-location byte + AF
    code list + 0x00 terminator (flat list only, no Method B tuning-
    frequency-relative framing in this version)."""
    codes = [freq_to_af_code(f) for f in frequencies_mhz]
    return bytes([0x13, 0x00]) + bytes(codes) + bytes([0x00])
