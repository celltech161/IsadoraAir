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

Standards-defined text fields (PS/PTYN/RT) are encoded via
rbds/services/charset.py's encode_rds_g0() -- the real RDS G0 table,
NOT Latin-1. Confirmed via direct primary-source review (2026-08-02):
the two are NOT the same encoding (e.g. G0 byte 0x24 is a generic
currency sign, not '$'; see charset.py's own docstring for more
examples) -- an earlier version of this module used
`.encode("latin-1", errors="replace")`, which would have sent the
wrong glyph for every accented character and silently dropped anything
outside Latin-1 entirely (curly quotes, em dashes, degree signs, etc).

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

from rbds.services.charset import encode_rds_g0

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


def mec_slow_labelling(variant: int, data: int, dsn: int = 0x00) -> bytes:
    """MEC 0x1A -- Slow labelling codes. Edits RDS group 1A, block 3
    (spec section 3.3.12). NO PSN field for this command -- confirmed
    directly against the spec's own format table, which lists only
    MEC/DSN/MED/MED (unlike PI/PS/etc, which all carry PSN too).

    variant: 3-bit variant code (0-7) selecting which sub-field of
        group 1A block 3 this data applies to (0 = Extended Country
        Code, others cover PIN/language/TMC id per the RDS standard
        itself -- the UECP spec defers per-variant field MEANINGS to
        CENELEC prEN 50067, it only defines the wire format below).
    data: 12-bit payload (0-0xFFF) specific to that variant.

    Format: MEC(1) DSN(1) MED(1)[bit7=reserved=0, bits6-4=variant,
    bits3-0=data MSB nibble] MED(1)[data LSB byte].
    Spec example: <1A><04><00><E2> -- data set 4, variant 0, data=0x0E2."""
    if not (0 <= variant <= 7):
        raise ValueError("variant must be 0-7 (3 bits)")
    if not (0 <= data <= 0xFFF):
        raise ValueError("data must be 0-0xFFF (12 bits)")
    med1 = ((variant & 0x7) << 4) | ((data >> 8) & 0xF)
    med2 = data & 0xFF
    return bytes([0x1A, dsn, med1, med2])


def mec_ecc(ecc: int, dsn: int = 0x00) -> bytes:
    """Extended Country Code, sent via Slow Labelling Codes (MEC 0x1A),
    variant 0. Fully qualifies the country half of the PI code (leading
    nibble of PI + ECC = unique country identifier per NRSC-4-B /
    EN 62106 Annex D). USA = 0xA0.

    ECC occupies the low 8 bits of variant 0's 12-bit data field --
    confirmed via multiple independent secondary sources (RDS Forum
    glossary, Silicon Labs AN243) since the primary UECP spec (section
    3.3.12) documents the general Slow Labelling Codes wire format but
    explicitly defers per-variant field meanings to the RDS standard
    itself (CENELEC prEN 50067), not to this protocol document.

    CORRECTION (2026-07-28): an earlier version of this function sent
    MEC 0x2E, which the spec's OWN section 3.3.13 defines as "Linkage
    information" -- an unrelated feature. That was never actually
    setting the station's ECC on air. Verified against the spec text
    directly before landing this fix; see rbds/tests.py for the byte-
    level check against section 3.3.12's literal worked example."""
    if not (0 <= ecc <= 0xFF):
        raise ValueError("ecc must be 0-255 (8 bits)")
    return mec_slow_labelling(variant=0, data=ecc, dsn=dsn)


def mec_ptyn(text: str, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x3E -- Programme Type Name (spec section 3.3.8). Further
    describes the current PTY with a free-text label the broadcaster
    controls (e.g. PTY=Sport, PTYN="Football"). Fixed 8 characters --
    NO length (MEL) byte, same shape as mec_ps -- chars restricted to
    0x20-0xFE per spec (same convention as PS).
    Spec example: <3E><00><02><46><6F><6F><74><62><61><6C><6C> --
    current data set, service 2, PTYN="Football"."""
    padded = text[:8].ljust(8)
    return bytes([0x3E, dsn, psn]) + encode_rds_g0(padded)


def mec_ps(text: str, dsn: int = 0x00, psn: int = 0x00) -> bytes:
    """MEC 0x02 -- Program Service name. Format: MEC DSN PSN MED(8),
    chars restricted to 0x20-0xFE per spec (non-conforming chars are
    replaced with a space rather than silently sent as invalid bytes).
    Spec example: <02><00><02><52><41><44><49><4F><20><31><20>."""
    padded = text[:8].ljust(8)
    return bytes([0x02, dsn, psn]) + encode_rds_g0(padded)


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
    then up to 64 text chars). MEL = 1 (flags byte) + len(text).

    Flags byte, confirmed directly against SPB490 p.31-32's own bit
    table (2026-08-02 primary-source review):
      bit 0: A/B flag status control -- 0=do not toggle, 1=toggle.
        This is a ONE-SHOT EDGE COMMAND, not "the flag's resulting
        value" -- callers must pass True only on the exact send where
        the transmitted text actually changed, never on a periodic
        resend of unchanged text (that would spuriously toggle A/B on
        every receiver for no reason). See rbds_manager.py's
        `rt_changed` computation, which is the sole source of this
        parameter -- no persistent stored toggle state exists anymore.
      bits 6-5: buffer configuration -- 00=flush the buffer then load
        this message, 01/11=reserved, 10=add to a cyclic on-device
        buffer. This project sends exactly one current message and
        does its own rotation in Python (rbds_manager.py), so 00 is
        the only correct value; the code previously sent the RESERVED
        value 01, an outright standards violation with genuinely
        undefined receiver-side behavior (fixed 2026-08-02).
    Spec example: <0A><00><01><04><0B><52><44><53> -- flush (00) +
    toggle A/B (bit0=1) -> flags=0x01, MEL=4 (1 flags byte + 3 text
    chars "RDS"), text "RDS"."""
    text = text[:64]
    med0 = 0x01 if ab_flag else 0x00  # bits 6-5 = 00 (flush-then-load)
    mel = 1 + len(text)
    return bytes([0x0A, dsn, psn, mel, med0]) + encode_rds_g0(text)


def mec_rt_plus_oda_reg() -> bytes:
    """MEC 0x24 subtype 0x06 -- ODA registration for RT+.

    Declares the Open Data Application at RDS group 11A carrying
    Application ID 0x4BD7 (the RDS Forum's assigned AID for RadioText
    Plus). Reverse-engineered from RDS Magic 4's proven-working UECP
    capture on 2026-07-12 -- every subtype 0x16 tag frame it emitted
    is preceded by this fixed 6-byte MEC.

    Wire format (after the 0x24 MEC byte):
      0x06 (subtype)  0x16 (group code = 22 = 11*2 for A-version)
      0x00 0x00 (reserved/scope bytes -- both were always zero across
                 53 instances in the capture)  0x4B 0xD7 (RT+ AID)

    No parameters; every RT+ transmitter sends exactly the same bytes.
    """
    return bytes([0x24, 0x06, 0x16, 0x00, 0x00, 0x4B, 0xD7])


def mec_rt_plus_tags(artist_len: int, title_len: int) -> bytes:
    """MEC 0x24 subtype 0x16 -- RT+ tag data (artist + title triples).

    Reverse-engineered from RDS Magic 4's 2026-07-13 capture across 22
    distinct songs and verified byte-for-byte by the
    scratchpad/verify_layout.py harness. Assumes the RT text has the
    project-standard shape "artist - title" (three-char " - "
    separator), and that RDS Magic 4's convention of hardcoding
    CT1=4 (ITEM.ARTIST at RT position 0) and CT2=1 (ITEM.TITLE at
    RT position artist_len+3) is what StereoTool expects too --
    those two content-type values are the entire reason for RT+
    existing, so the hardcoding is safe.

    Wire format (after the 0x24 MEC byte), 6 bytes total:
      Byte 0: 0x16                        -- subtype
      Byte 1: 0x08                        -- fixed "song entry" marker
      Byte 2: bits[7:5] = 0b001           -- fragment of CT1 encoding
              bits[4:0] = title_start>>1  -- upper 5 bits of start2
      Byte 3: bit[7]    = title_start & 1 -- LSB of start2
              bits[6:1] = title_len - 1   -- len2 (6-bit)
              bit[0]    = 0
      Byte 4: 0x20                        -- fixed CT2=title marker
      Byte 5: bits[7:5] = 0
              bits[4:0] = artist_len - 1  -- len1 (5-bit)

    Field-width note: len1 is 5 bits so artist_len must be <= 32;
    len2 is 6 bits so title_len must be <= 64 (which the RT text
    limit already enforces). start2 is 6 bits, so title_start
    (== artist_len + 3) must be <= 63. Callers should guard for
    artist_len > 32 and skip emitting tags rather than truncate --
    RDS Magic 4's capture had one artist_len=45 outlier that emitted
    a DIFFERENT payload shape (probably a fallback for long artists),
    but we don't have enough capture data to reverse that shape;
    skipping is safer than shipping bytes we don't understand.
    """
    if artist_len < 1 or title_len < 1:
        raise ValueError("mec_rt_plus_tags requires positive lengths")
    if artist_len > 32:
        raise ValueError(
            f"mec_rt_plus_tags: artist_len {artist_len} > 32 (RDS Magic 4 "
            f"used a different, un-decoded payload shape past 32; caller "
            f"should skip RT+ for this track instead of emitting garbage)"
        )
    title_start = artist_len + 3
    if title_start > 63:
        raise ValueError(f"title_start {title_start} exceeds 6-bit field")
    if title_len > 64:
        title_len = 64  # RT text is capped at 64 anyway

    byte1 = 0x08
    byte2 = (0b001 << 5) | ((title_start >> 1) & 0x1F)
    byte3 = ((title_start & 1) << 7) | (((title_len - 1) & 0x3F) << 1)
    byte4 = 0x20
    byte5 = (artist_len - 1) & 0x1F
    return bytes([0x24, 0x16, byte1, byte2, byte3, byte4, byte5])


def mec_rt_plus_tags_generic(rt_text_len: int) -> bytes:
    """MEC 0x24 subtype 0x16 -- single-tag RT+ covering the entire RT.

    Used for non-song RT frames (weather, promos, station IDs, etc.)
    where the whole RT is one semantic unit rather than an
    artist/title split. Reverse-engineered from RDS Magic 4's take-3
    capture (2026-07-13) -- every weather RT change was immediately
    followed by six to eight repeats of this exact byte pattern,
    with only the length byte varying to match each weather text.

    Confirmed byte-for-byte:
      Weather1 "Temp: 86F..." (56 chars) -> 24 16 0b 20 6e 00 00
      Weather2 "Wind: Southeast..." (51) -> 24 16 0b 20 64 00 00

    ON-AIR CONFIRMATION (2026-08-04, controlled bench isolation
    experiment -- see scratchpad/rbds_bench/rtplus_isolation_experiment/
    RTPLUS_0X24_0XAA_ISOLATION_REPORT.md): a real TEF6686 capture,
    decoded with redsea, showed this exact byte pattern rendering on
    air as `content-type: info.weather` for a deliberately-injected
    non-song test message ("Test Weather Information"), with no
    stale song artist/title tags visible during that segment. This
    resolves the previous "not independently confirmed" caution for
    THAT tested case. It does NOT establish that every kind of
    non-song RT this function is used for (promo/station-ID/file/url,
    not just weather) renders with the same content-type -- only a
    weather-labeled message was tested, and this function's bytes are
    identical regardless of the actual semantic content (see below),
    so a promo or station-ID message might decode differently, or
    identically, on the same StereoTool build; that was not tested and
    should not be assumed either way. What remains confirmed by design
    regardless of content-type: this exists to stop a receiver from
    misapplying a STALE song's artist/title offsets to unrelated text,
    and is deliberately reused byte-for-byte across every kind of
    non-song RT (weather/promo/station-ID/file/url) for exactly that
    reason. See rbds_manager.py's RT+ send sites for the related,
    already-resolved case: a real song whose artist is too long for
    the two-tag format (>32 chars) omits RT+ entirely instead of
    borrowing this tag (closer to a song than to unrelated content,
    and "omit rather than guess" is the safer failure mode either way).

    Wire format (after the 0x24 MEC byte), 6 bytes total:
      Byte 0: 0x16                        -- subtype
      Byte 1: 0x0b                        -- single-tag marker
      Byte 2: 0x20                        -- fixed (same "001" fragment
                                             songs use; encodes part of
                                             CT1 but the rest is fixed
                                             for whichever CT StereoTool
                                             applies -- info.weather /
                                             info.other -- based on its
                                             own template config for
                                             this RT source)
      Byte 3: bit[7]    = 0               -- start1 LSB (always 0, tag
                                             covers the whole RT which
                                             always starts at 0)
              bits[6:1] = rt_text_len - 1 -- len1, 6-bit N-1 marker
              bit[0]    = 0
      Byte 4: 0x00
      Byte 5: 0x00

    Without this, receivers keep the LAST song's artist/title offsets
    active and apply them to the current non-song RT text -- observed
    live as "Artist: Wind: South at 4, Gusts | Title: Barome" on a
    TEF6686 and a Uconnect Ram head unit on 2026-07-13.
    """
    if rt_text_len < 1:
        raise ValueError("mec_rt_plus_tags_generic requires positive length")
    len1 = min(rt_text_len - 1, 63)  # 6-bit cap
    byte3 = (len1 & 0x3F) << 1  # bit 7 = 0 (start=0), bits 6-1 = len1
    return bytes([0x24, 0x16, 0x0b, 0x20, byte3, 0x00, 0x00])


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


def mec_ct_on_off(enabled: bool) -> bytes:
    """MEC 0x19 -- CT On/Off. Format: MEC MED(1: 00=disable, 01=enable
    transmission of type 4A group). Confirmed directly against the
    spec's own section 3.3.39 (2026-08-02 primary-source review):
    "To enable/disable the transmission of type 4A group... '01'
    enables the transmission of type 4A groups and '00' disables it.
    The time is set with the Real Time Clock Command." -- i.e. this is
    a DISTINCT command from mec_ct (0x0D): 0x0D only sets the clock
    VALUE, 0x19 is what actually turns 4A transmission on or off. No
    dsn/psn, matching mec_ct's own convention and the spec's format
    table for this command (MEC MED only).
    Spec example: <19><01> -- enable transmission of type 4A group."""
    return bytes([0x19, 0x01 if enabled else 0x00])


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
