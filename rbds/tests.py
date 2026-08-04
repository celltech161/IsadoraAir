import time
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from rbds.models import RBDSConfig, RBDSMessage, RBDSPSFrame
from rbds.services import ascii_protocol, charset, uecp
from rbds.services import rbds_manager
from rbds.services.content_fetch import ContentFetchCache
from rbds.services.rbds_manager import RBDSManager
from rbds.services.rotation import PSRotation, RTRotation


class FakeClock:
    """Injectable clock for deterministic rotation-timing tests -- call
    advance(seconds) to move time forward instead of real time.sleep()."""

    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class UecpCrcTests(SimpleTestCase):
    def test_crc_ccitt_genibus_check_value(self):
        # CRC-16/GENIBUS uses the exact same 4 parameters our crc_ccitt()
        # implements (poly 0x1021, init 0xFFFF, no reflection, xorout
        # 0xFFFF) -- its well-known, catalogued check value for the
        # standard "123456789" test string is 0xD64E. The UECP spec's own
        # worked example (Appendix 1) contains a literal typo
        # ("...123F0XXXX110...") in both its 1997 and 2006 revisions,
        # confirmed directly from the primary source PDF text, so it
        # can't be used as a test vector -- this named standard variant
        # is used instead as an independently-verifiable anchor.
        self.assertEqual(uecp.crc_ccitt(b"123456789"), bytes.fromhex("D64E"))

    def test_crc_ccitt_matches_spec_pascal_reference(self):
        # Independent second implementation, transliterated directly from
        # SPB490 Appendix 1's own PASCAL CRCVALUE listing (SWAP = swap
        # hi/lo byte of a 16-bit word, LO = low byte) -- cross-checking
        # two independently-written implementations against each other
        # is the verification path used here since a clean official test
        # vector isn't available (see module docstring in uecp.py).
        def swap16(w):
            return ((w & 0xFF) << 8) | ((w >> 8) & 0xFF)

        def lo(w):
            return w & 0xFF

        def crc_pascal(data):
            tempcrc = 0xFFFF
            for byte in data:
                tempcrc = swap16(tempcrc) ^ byte
                tempcrc = (tempcrc ^ (lo(tempcrc) >> 4)) & 0xFFFF
                tempcrc = (
                    tempcrc
                    ^ ((swap16(lo(tempcrc)) << 4) & 0xFFFF)
                    ^ ((lo(tempcrc) << 5) & 0xFFFF)
                ) & 0xFFFF
            return (tempcrc ^ 0xFFFF) & 0xFFFF

        for data in [
            b"123456789",
            b"",
            b"\x00",
            b"\xFF",
            bytes.fromhex("2D1112340101"),
            bytes.fromhex("AABBCCDDEEFF00112233"),
        ]:
            with self.subTest(data=data):
                expected = crc_pascal(data)
                actual = int.from_bytes(uecp.crc_ccitt(data), "big")
                self.assertEqual(actual, expected)

    def test_byte_stuffing_round_trip(self):
        def unstuff(data):
            out = bytearray()
            i = 0
            while i < len(data):
                if data[i] == 0xFD:
                    marker = data[i + 1]
                    out.append({0x00: 0xFD, 0x01: 0xFE, 0x02: 0xFF}[marker])
                    i += 2
                else:
                    out.append(data[i])
                    i += 1
            return bytes(out)

        original = bytes([0x01, 0xFD, 0x02, 0xFE, 0x03, 0xFF, 0x04])
        stuffed = uecp.byte_stuff(original)
        self.assertEqual(unstuff(stuffed), original)

    def test_byte_stuffing_literal_values(self):
        self.assertEqual(uecp.byte_stuff(bytes([0xFD])), bytes([0xFD, 0x00]))
        self.assertEqual(uecp.byte_stuff(bytes([0xFE])), bytes([0xFD, 0x01]))
        self.assertEqual(uecp.byte_stuff(bytes([0xFF])), bytes([0xFD, 0x02]))
        self.assertEqual(uecp.byte_stuff(bytes([0x41])), bytes([0x41]))


class UecpMecBuilderTests(SimpleTestCase):
    """Every command's byte layout is checked against the SPB490 spec's
    own literal worked example for that exact command (sections 3.1.x/
    3.3.x), not just internal shape -- a real bug was caught this way
    after the first version of this module shipped without DSN/PSN on
    any command, with RT's fields in the wrong order, and with DI's
    bits swapped, none of which shape-only tests had caught."""

    def test_mec_pi_matches_spec_example(self):
        # <01><00><01><C2><01> -- PI=C201, current data set, service 1.
        self.assertEqual(uecp.mec_pi(0xC201, dsn=0x00, psn=0x01), bytes.fromhex("0100 01 C201".replace(" ", "")))

    def test_mec_ps_matches_spec_example(self):
        # <02><00><02><52><41><44><49><4F><20><31><20> -- current data
        # set, service 2, PS "RADIO 1 " (spec's own OCR-mangled text
        # decodes to this via the literal hex bytes, not the prose).
        result = uecp.mec_ps("RADIO 1 ", dsn=0x00, psn=0x02)
        self.assertEqual(result, bytes.fromhex("020002") + b"RADIO 1 ")

    def test_mec_ps_pads_and_truncates(self):
        self.assertEqual(uecp.mec_ps("ABC"), bytes([0x02, 0x00, 0x00]) + b"ABC     ")
        self.assertEqual(uecp.mec_ps("ABCDEFGHIJ"), bytes([0x02, 0x00, 0x00]) + b"ABCDEFGH")

    def test_mec_ta_tp_matches_spec_example(self):
        # <03><00><05><02> -- current data set, service 5, TP=1 TA=0.
        self.assertEqual(uecp.mec_ta_tp(ta=False, tp=True, dsn=0x00, psn=0x05), bytes.fromhex("03000502"))

    def test_mec_ta_tp_bits(self):
        self.assertEqual(uecp.mec_ta_tp(ta=False, tp=False), bytes([0x03, 0x00, 0x00, 0x00]))
        self.assertEqual(uecp.mec_ta_tp(ta=True, tp=False), bytes([0x03, 0x00, 0x00, 0x01]))
        self.assertEqual(uecp.mec_ta_tp(ta=False, tp=True), bytes([0x03, 0x00, 0x00, 0x02]))
        self.assertEqual(uecp.mec_ta_tp(ta=True, tp=True), bytes([0x03, 0x00, 0x00, 0x03]))

    def test_mec_di_matches_spec_example(self):
        # <04><00><03><01> -- current data set, service 3, stereo=1,
        # artificial head/compressed/dynamic-PTYI all 0.
        result = uecp.mec_di(dynamic_pty=False, compressed=False, artificial_head=False, stereo=True,
                              dsn=0x00, psn=0x03)
        self.assertEqual(result, bytes.fromhex("04000301"))

    def test_mec_di_bit_assignment(self):
        # bit0=stereo, bit1=artificial head, bit2=compressed, bit3=dynamic PTYI
        # -- confirmed against spec section 3.1.3, NOT the order used in
        # this module's first (buggy) version.
        self.assertEqual(uecp.mec_di(False, False, False, False)[3], 0x00)
        self.assertEqual(uecp.mec_di(False, False, False, True)[3], 0x01)   # stereo
        self.assertEqual(uecp.mec_di(False, False, True, False)[3], 0x02)   # artificial head
        self.assertEqual(uecp.mec_di(False, True, False, False)[3], 0x04)   # compressed
        self.assertEqual(uecp.mec_di(True, False, False, False)[3], 0x08)   # dynamic PTYI
        self.assertEqual(uecp.mec_di(True, True, True, True)[3], 0x0F)

    def test_mec_ms_matches_spec_example(self):
        # <05><00><01><01> -- current data set, service 1, MS=1.
        self.assertEqual(uecp.mec_ms(music=True, dsn=0x00, psn=0x01), bytes.fromhex("05000101"))

    def test_mec_pty_matches_spec_example(self):
        # <07><00><05><08> -- current data set, service 5, PTY=8.
        self.assertEqual(uecp.mec_pty(8, dsn=0x00, psn=0x05), bytes.fromhex("07000508"))

    def test_mec_rt_matches_spec_example_full_flags_byte(self):
        # <0A><00><01><04><00><52><44><53> -- current data set, service
        # 1, buffer config = flush-then-load (bits6-5=00, confirmed
        # directly against SPB490 p.31-32's own bit table 2026-08-02;
        # 01/11 are RESERVED, 10 is "add to cyclic buffer" -- neither
        # applies here since this engine sends one current message and
        # does its own rotation in Python, not an on-device buffer),
        # A/B toggle=0 (bit0), MEL=4 (1 flags byte + 3 text chars
        # "RDS"), text "RDS". Checks the COMPLETE flags byte (not
        # masked) -- a prior version of this module sent the RESERVED
        # value 0b01 in bits 6-5, which byte-masked tests never caught.
        result = uecp.mec_rt("RDS", ab_flag=False, dsn=0x00, psn=0x01)
        self.assertEqual(result, bytes.fromhex("0A000104" "00" "524453"))

    def test_mec_rt_flags_byte_never_reserved(self):
        for text, ab in [("A", True), ("B", False), ("", True), ("x" * 64, False)]:
            with self.subTest(text=text, ab=ab):
                flags = uecp.mec_rt(text, ab_flag=ab)[4]
                self.assertEqual(flags & 0x60, 0x00, "bits 6-5 must be 00 (flush); 01/11 are reserved")
                self.assertEqual(flags & 0x9E, 0x00, "only bit0 may be set")

    def test_mec_rt_ab_flag(self):
        rt_off = uecp.mec_rt("Hello", ab_flag=False)
        rt_on = uecp.mec_rt("Hello", ab_flag=True)
        self.assertEqual(rt_off[4], 0x00)
        self.assertEqual(rt_on[4], 0x01)
        self.assertEqual(rt_off[5:], b"Hello")
        self.assertEqual(rt_off[3], 1 + len("Hello"))

    def test_mec_rt_mel_is_flags_byte_plus_text_length(self):
        result = uecp.mec_rt("A" * 64, ab_flag=False)
        self.assertEqual(result[3], 65)  # 1 flags byte + 64 text chars

    def test_mec_ct_matches_spec_example(self):
        # <0D><5C><09><0C><0A><12><21><0F><02> -- spec section 3.3.37's
        # own worked example: 1992-09-12 10:18:33.15 UTC, local offset +1h.
        import datetime
        dt = datetime.datetime(1992, 9, 12, 10, 18, 33, 150000, tzinfo=datetime.timezone.utc)
        result = uecp.mec_ct(dt, offset_minutes=60)
        self.assertEqual(result, bytes.fromhex("0D5C090C0A12210F02"))

    def test_mec_ct_no_dsn_psn(self):
        # Unlike every other mec_* here, CT has no DSN/PSN -- exactly 9
        # bytes total (1 MEC + 8 MED), confirmed against the spec example.
        import datetime
        dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        result = uecp.mec_ct(dt, offset_minutes=0)
        self.assertEqual(len(result), 9)
        self.assertEqual(result[0], 0x0D)

    def test_mec_ct_negative_offset(self):
        # -300 minutes = -5h = -10 half-hours -> sign bit set, magnitude 10.
        import datetime
        dt = datetime.datetime(2026, 1, 1, 0, 0, 0, tzinfo=datetime.timezone.utc)
        result = uecp.mec_ct(dt, offset_minutes=-300)
        offset_byte = result[8]
        self.assertEqual(offset_byte & 0x20, 0x20)  # sign bit = negative
        self.assertEqual(offset_byte & 0x1F, 10)     # 5h = 10 half-hours

    def test_mec_ct_on_off_matches_spec_example(self):
        # <19><01> -- spec section 3.3.39's own worked example: "enable
        # transmission of type 4A group." Confirmed 2026-08-02: MEC
        # 0x0D only sets the clock VALUE, this distinct command is what
        # actually enables/disables 4A transmission.
        self.assertEqual(uecp.mec_ct_on_off(True), bytes.fromhex("1901"))
        self.assertEqual(uecp.mec_ct_on_off(False), bytes.fromhex("1900"))

    def test_freq_to_af_code(self):
        self.assertEqual(uecp.freq_to_af_code(87.6), 1)
        self.assertEqual(uecp.freq_to_af_code(107.9), 204)

    def test_mec_af_envelope_has_dsn_psn_mel(self):
        # Only the UECP envelope (MEC/DSN/PSN/MEL/start-location/
        # terminator) is checked against the spec here -- the AF *data*
        # content's exact list-encoding scheme is a known, documented
        # simplification (see mec_af's own docstring) since it depends
        # on IEC EN 62106, a separate standard this UECP spec defers to.
        result = uecp.mec_af([87.6, 107.9], dsn=0x00, psn=0x01, start_location=0x0000)
        self.assertEqual(result[0:3], bytes.fromhex("130001"))  # MEC DSN PSN
        mel = result[3]
        med = result[4:]
        self.assertEqual(len(med), mel)
        self.assertEqual(med[0:2], bytes.fromhex("0000"))  # start location
        self.assertEqual(med[2:4], bytes([1, 204]))  # the two AF codes
        self.assertEqual(med[-1], 0x00)  # terminator

    def test_mec_slow_labelling_matches_spec_example(self):
        # <1A><04><00><E2> -- spec section 3.3.12's own worked example:
        # data set 4, variant code 0, data set to 0x0E2.
        self.assertEqual(
            uecp.mec_slow_labelling(variant=0, data=0x0E2, dsn=0x04),
            bytes.fromhex("1A0400E2"),
        )

    def test_mec_slow_labelling_no_psn(self):
        # Confirmed against the spec's own format table for this
        # command: MEC DSN MED MED only, no PSN -- 4 bytes total.
        result = uecp.mec_slow_labelling(variant=0, data=0, dsn=0x00)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[0], 0x1A)

    def test_mec_slow_labelling_variant_and_data_packed_correctly(self):
        # variant=5 (0b101), data=0xABC -> MED1 = 0b0101_1010 = 0x5A,
        # MED2 = 0xBC.
        result = uecp.mec_slow_labelling(variant=5, data=0xABC, dsn=0x00)
        self.assertEqual(result[2], 0x5A)
        self.assertEqual(result[3], 0xBC)

    def test_mec_slow_labelling_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            uecp.mec_slow_labelling(variant=8, data=0)
        with self.assertRaises(ValueError):
            uecp.mec_slow_labelling(variant=0, data=0x1000)

    def test_mec_ecc_is_variant_0_of_slow_labelling(self):
        # ECC=0xA0 (USA) -> variant 0, data=0x0A0 -> MED1=0x00, MED2=0xA0.
        self.assertEqual(uecp.mec_ecc(0xA0, dsn=0x00), bytes.fromhex("1A0000A0"))
        # Same value must equal calling mec_slow_labelling directly --
        # mec_ecc is a thin convenience wrapper, not a separate command.
        self.assertEqual(uecp.mec_ecc(0xA0), uecp.mec_slow_labelling(variant=0, data=0xA0))

    def test_mec_ptyn_matches_spec_example(self):
        # <3E><00><02><46><6F><6F><74><62><61><6C><6C> -- current data
        # set, service 2, PTYN="Football" (8 chars exactly).
        result = uecp.mec_ptyn("Football", dsn=0x00, psn=0x02)
        self.assertEqual(result, bytes.fromhex("3E0002") + b"Football")

    def test_mec_ptyn_pads_and_truncates(self):
        self.assertEqual(uecp.mec_ptyn("SPORT"), bytes([0x3E, 0x00, 0x00]) + b"SPORT   ")
        self.assertEqual(uecp.mec_ptyn("ABCDEFGHIJ"), bytes([0x3E, 0x00, 0x00]) + b"ABCDEFGH")


class UecpFrameTests(SimpleTestCase):
    def test_build_frame_structure(self):
        msg = uecp.mec_ps("TESTPS  ")
        frame = uecp.build_frame(site_address=1, encoder_address=0, sqc=1, msg=msg)
        self.assertEqual(frame[0], uecp.STA)
        self.assertEqual(frame[-1], uecp.STP)

    def test_build_frame_crc_is_verifiable(self):
        # Rebuild the frame's own CRC independently and confirm it
        # matches what build_frame() embedded -- requires reversing the
        # byte-stuffing first since CRC is computed pre-stuffing.
        def unstuff(data):
            out = bytearray()
            i = 0
            while i < len(data):
                if data[i] == 0xFD:
                    marker = data[i + 1]
                    out.append({0x00: 0xFD, 0x01: 0xFE, 0x02: 0xFF}[marker])
                    i += 2
                else:
                    out.append(data[i])
                    i += 1
            return bytes(out)

        msg = uecp.mec_pi(0x1000) + uecp.mec_ps("KOGR-LP ")
        frame = uecp.build_frame(site_address=1, encoder_address=0, sqc=5, msg=msg)
        inner_stuffed = frame[1:-1]
        inner = unstuff(inner_stuffed)
        core, crc_bytes = inner[:-2], inner[-2:]
        self.assertEqual(uecp.crc_ccitt(core), crc_bytes)


class AsciiProtocolTests(SimpleTestCase):
    def test_rt_plus_tag_length_minus_one(self):
        # Matches StereoTool's own documented example exactly:
        # "RT+=4,12,16,1,32,4" tags a 17-char artist at offset 12 and a
        # 5-char title at offset 32 -- the third number is length MINUS
        # ONE, a real documented gotcha.
        artist_tag = ascii_protocol.build_rt_plus_tag(ascii_protocol.RT_PLUS_ARTIST, 12, 17)
        title_tag = ascii_protocol.build_rt_plus_tag(ascii_protocol.RT_PLUS_TITLE, 32, 5)
        self.assertEqual(artist_tag, "4,12,16")
        self.assertEqual(title_tag, "1,32,4")

    def test_build_ascii_commands_never_includes_ta(self):
        commands = ascii_protocol.build_ascii_commands(
            pi_code="1000", ps="KOGR-LP ", rt="Now Playing", pty=11,
            music=True, di_dynamic_pty=False, di_compressed=False,
            di_artificial_head=False, di_stereo=True,
        )
        joined = " ".join(commands)
        self.assertNotIn("TA=", joined)
        self.assertNotIn("TP=", joined)

    def test_build_ascii_commands_basic_fields(self):
        commands = ascii_protocol.build_ascii_commands(
            pi_code="1000", ps="KOGR-LP ", rt="Artist - Title", pty=11,
            music=True, di_dynamic_pty=False, di_compressed=False,
            di_artificial_head=False, di_stereo=True,
        )
        self.assertIn("PS=KOGR-LP ", commands)
        self.assertIn("RT=Artist - Title", commands)
        self.assertIn("PI=1000", commands)
        self.assertIn("PTY=11", commands)
        self.assertIn("MS=1", commands)
        self.assertIn("DI=8", commands)  # stereo bit only

    def test_build_ascii_commands_with_rt_plus(self):
        tags = [
            ascii_protocol.build_rt_plus_tag(ascii_protocol.RT_PLUS_ARTIST, 0, 6),
            ascii_protocol.build_rt_plus_tag(ascii_protocol.RT_PLUS_TITLE, 9, 5),
        ]
        commands = ascii_protocol.build_ascii_commands(
            pi_code="1000", ps="KOGR-LP ", rt="Artist - Title", pty=11,
            music=True, di_dynamic_pty=False, di_compressed=False,
            di_artificial_head=False, di_stereo=True, rt_plus_tags=tags,
        )
        self.assertIn("RT+=4,0,5,1,9,4", commands)


class PSRotationTests(SimpleTestCase):
    def test_zero_frames_returns_none(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        self.assertIsNone(rotation.advance([]))

    def test_single_frame_never_advances(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        frames = [("FRAME1  ", 8)]
        self.assertEqual(rotation.advance(frames), "FRAME1  ")
        clock.advance(100)
        self.assertEqual(rotation.advance(frames), "FRAME1  ")

    def test_advances_after_hold_seconds(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        frames = [("FRAME1  ", 8), ("FRAME2  ", 8)]
        self.assertEqual(rotation.advance(frames), "FRAME1  ")
        clock.advance(5)
        self.assertEqual(rotation.advance(frames), "FRAME1  ")  # not yet
        clock.advance(4)  # total 9s >= 8s hold
        self.assertEqual(rotation.advance(frames), "FRAME2  ")

    def test_wraps_around(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        frames = [("FRAME1  ", 1), ("FRAME2  ", 1)]
        self.assertEqual(rotation.advance(frames), "FRAME1  ")
        clock.advance(1)
        self.assertEqual(rotation.advance(frames), "FRAME2  ")
        clock.advance(1)
        self.assertEqual(rotation.advance(frames), "FRAME1  ")

    def test_frame_list_change_resets_to_index_zero(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        frames = [("FRAME1  ", 1), ("FRAME2  ", 1)]
        rotation.advance(frames)
        clock.advance(1)
        self.assertEqual(rotation.advance(frames), "FRAME2  ")
        # Admin edit: a new frame list entirely
        new_frames = [("NEWFRAME", 5)]
        self.assertEqual(rotation.advance(new_frames), "NEWFRAME")


class RTRotationTests(SimpleTestCase):
    def test_no_promos_stays_nowplaying(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock)
        for _ in range(5):
            clock.advance(100)
            self.assertEqual(rotation.advance([]), ("nowplaying", None))

    def test_promo_does_not_interrupt_before_floor(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock, nowplaying_min_seconds=20)
        promos = [("Promo A", 10)]
        self.assertEqual(rotation.advance(promos), ("nowplaying", None))
        clock.advance(19)
        self.assertEqual(rotation.advance(promos), ("nowplaying", None))

    def test_promo_interrupts_after_floor(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock, nowplaying_min_seconds=20)
        promos = [("Promo A", 10)]
        rotation.advance(promos)
        clock.advance(20)
        self.assertEqual(rotation.advance(promos), ("promo", "Promo A"))

    def test_rotation_returns_to_nowplaying_after_all_promos_shown(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock, nowplaying_min_seconds=20)
        promos = [("Promo A", 10), ("Promo B", 10)]
        rotation.advance(promos)
        clock.advance(20)
        self.assertEqual(rotation.advance(promos), ("promo", "Promo A"))
        clock.advance(10)
        self.assertEqual(rotation.advance(promos), ("promo", "Promo B"))
        clock.advance(10)
        self.assertEqual(rotation.advance(promos), ("nowplaying", None))

    def test_all_promos_disabled_mid_rotation_falls_back_immediately(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock, nowplaying_min_seconds=20)
        promos = [("Promo A", 10)]
        rotation.advance(promos)
        clock.advance(20)
        self.assertEqual(rotation.advance(promos), ("promo", "Promo A"))
        # Admin disables the only promo mid-display
        self.assertEqual(rotation.advance([]), ("nowplaying", None))

    def test_promo_end_date_expiring_mid_display_restarts_rotation(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock, nowplaying_min_seconds=20)
        promos = [("Promo A", 10), ("Promo B", 10)]
        rotation.advance(promos)
        clock.advance(20)
        self.assertEqual(rotation.advance(promos), ("promo", "Promo A"))
        clock.advance(5)
        # Promo A's end_date rolls over mid-display -- caller now passes
        # a shrunk active_promos list reflecting that.
        shrunk = [("Promo B", 10)]
        self.assertEqual(rotation.advance(shrunk), ("promo", "Promo B"))

    def test_nowplaying_min_seconds_floor_resets_after_returning(self):
        clock = FakeClock()
        rotation = RTRotation(clock=clock, nowplaying_min_seconds=20)
        promos = [("Promo A", 10)]
        rotation.advance(promos)
        clock.advance(20)
        rotation.advance(promos)  # now in promo mode
        clock.advance(10)
        self.assertEqual(rotation.advance(promos), ("nowplaying", None))  # back to nowplaying
        clock.advance(19)
        self.assertEqual(rotation.advance(promos), ("nowplaying", None))  # floor not met yet
        clock.advance(1)
        self.assertEqual(rotation.advance(promos), ("promo", "Promo A"))


class ContentFetchCacheTests(SimpleTestCase):
    def test_fetches_once_then_uses_cache_within_interval(self):
        clock = FakeClock()
        cache = ContentFetchCache(clock=clock)
        calls = []

        def fetch():
            calls.append(1)
            return "fetched text"

        self.assertEqual(cache.get("msg1", 30, fetch), "fetched text")
        clock.advance(10)
        self.assertEqual(cache.get("msg1", 30, fetch), "fetched text")
        self.assertEqual(len(calls), 1)

    def test_refetches_after_interval_elapses(self):
        clock = FakeClock()
        cache = ContentFetchCache(clock=clock)
        calls = [0]

        def fetch():
            calls[0] += 1
            return f"fetch #{calls[0]}"

        self.assertEqual(cache.get("msg1", 30, fetch), "fetch #1")
        clock.advance(31)
        self.assertEqual(cache.get("msg1", 30, fetch), "fetch #2")

    def test_fetch_failure_keeps_last_good_value(self):
        clock = FakeClock()
        cache = ContentFetchCache(clock=clock)

        def good_fetch():
            return "good text"

        def bad_fetch():
            raise OSError("file missing")

        self.assertEqual(cache.get("msg1", 30, good_fetch), "good text")
        clock.advance(31)
        # File went missing on the next poll -- keep the last-good value.
        self.assertEqual(cache.get("msg1", 30, bad_fetch), "good text")

    def test_fetch_failure_with_no_prior_cache_returns_empty_string(self):
        clock = FakeClock()
        cache = ContentFetchCache(clock=clock)

        def bad_fetch():
            raise OSError("file missing")

        self.assertEqual(cache.get("msg1", 30, bad_fetch), "")

    def test_independent_keys_have_independent_caches(self):
        clock = FakeClock()
        cache = ContentFetchCache(clock=clock)
        self.assertEqual(cache.get("a", 30, lambda: "text A"), "text A")
        self.assertEqual(cache.get("b", 30, lambda: "text B"), "text B")


class CharsetTests(SimpleTestCase):
    """RDS's G0 character table (EN 50067:1998 Annex E) is NOT Latin-1
    and NOT UTF-8 -- confirmed 2026-08-02 against redsea's reference
    decode table (an independent, working RDS decoder implementation;
    see charset.py's own docstring). These lock in the specific,
    verified byte positions so a future edit can't silently drift back
    to a Latin-1/ASCII assumption."""

    def test_normalize_smart_quotes(self):
        self.assertEqual(charset.normalize_text("‘Rock’ “n” Roll"), "'Rock' \"n\" Roll")

    def test_normalize_dashes(self):
        self.assertEqual(charset.normalize_text("A–B—C―D"), "A-B-C-D")

    def test_normalize_ellipsis_expands_length(self):
        # Deliberately changes string length -- this is why
        # normalize_text() must run BEFORE any 64/8-char truncation or
        # RT+ offset math, never after.
        self.assertEqual(charset.normalize_text("Wait…"), "Wait...")
        self.assertEqual(len(charset.normalize_text("…")), 3)

    def test_normalize_strips_control_chars(self):
        self.assertEqual(charset.normalize_text("Line1\r\nLine2\tTab\x00Nul"), "Line1  Line2 Tab Nul")

    def test_normalize_embedded_newline_cannot_survive_to_ascii_command_injection(self):
        # The concrete injection scenario finding #9 describes: a
        # file/URL source's content contains an embedded newline that
        # would otherwise inject a spurious extra "KEY=value" line
        # into StereoTool's newline-delimited ASCII command stream.
        malicious = "Normal Text\nPI=FFFF"
        normalized = charset.normalize_text(malicious)
        self.assertNotIn("\n", normalized)
        self.assertEqual(normalized, "Normal Text PI=FFFF")

    def test_encode_rds_g0_dollar_sign_is_not_ascii_byte(self):
        # G0 byte 0x24 is the generic currency sign "¤", NOT '$' --
        # '$' has its own distinct G0 code at 0xAB. Sending the naive
        # ASCII/Latin-1 byte for '$' would render as "¤" on a real
        # RDS receiver.
        self.assertEqual(charset.encode_rds_g0("$"), bytes([0xAB]))
        self.assertNotEqual(charset.encode_rds_g0("$"), bytes([0x24]))
        self.assertEqual(charset.encode_rds_g0("¤"), bytes([0x24]))

    def test_encode_rds_g0_accented_characters(self):
        self.assertEqual(charset.encode_rds_g0("á"), bytes([0x80]))
        self.assertEqual(charset.encode_rds_g0("Ñ"), bytes([0x8A]))
        self.assertEqual(charset.encode_rds_g0("ñ"), bytes([0x9A]))
        self.assertEqual(charset.encode_rds_g0("Café"), bytes([0x43, 0x61, 0x66, 0x82]))

    def test_encode_rds_g0_euro_pound_degree(self):
        self.assertEqual(charset.encode_rds_g0("€"), bytes([0xA9]))
        self.assertEqual(charset.encode_rds_g0("£"), bytes([0xAA]))
        self.assertEqual(charset.encode_rds_g0("75°F"), bytes([0x37, 0x35, 0xBB, 0x46]))

    def test_encode_rds_g0_ampersand_and_plain_ascii_passthrough(self):
        self.assertEqual(charset.encode_rds_g0("Rock & Roll 123"), b"Rock & Roll 123")

    def test_encode_rds_g0_unsupported_chars_fall_back_predictably(self):
        # Emoji / non-Latin scripts have no G0 representation --
        # must become a predictable fallback byte (space, matching
        # mec_ps/mec_ptyn's existing non-conforming-byte convention),
        # never be silently dropped (dropping would shift every RT+
        # offset computed against this same string).
        self.assertEqual(charset.encode_rds_g0("A\U0001F600B"), b"A B")
        self.assertEqual(charset.encode_rds_g0("日本語"), b"   ")

    def test_encode_rds_g0_is_length_preserving(self):
        for text in ["", "plain", "á€$日\U0001F600", "Wait..."]:
            with self.subTest(text=text):
                self.assertEqual(len(charset.encode_rds_g0(text)), len(text))

    def test_space_encodes_to_real_space_byte(self):
        # Several codes < 0x20 render as " " in the DECODE table
        # purely as control-code display artifacts -- a literal space
        # character must still map to the real space code 0x20, not
        # one of those (the encode table deliberately excludes codes
        # < 0x20 from consideration; see charset.py's own comment).
        self.assertEqual(charset.encode_rds_g0(" "), bytes([0x20]))


def _split_uecp_frames(payload):
    """Splits a concatenated multi-frame UECP payload (as produced by
    RBDSManager._frames_for -- one MEC per frame) into a list of raw,
    unstuffed, CRC-verified MSG byte blocks, one per frame. General
    frame parser (handles byte-stuffing properly, unlike a raw
    substring search) so RBDSManager-level tests can decode exactly
    what was queued for transmission."""
    def unstuff(data):
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == 0xFD:
                marker = data[i + 1]
                out.append({0x00: 0xFD, 0x01: 0xFE, 0x02: 0xFF}[marker])
                i += 2
            else:
                out.append(data[i])
                i += 1
        return bytes(out)

    frames = []
    i = 0
    while i < len(payload):
        assert payload[i] == uecp.STA
        j = i + 1
        while payload[j] != uecp.STP:
            j += 1
        inner = unstuff(payload[i + 1:j])
        core, crc = inner[:-2], inner[-2:]
        assert uecp.crc_ccitt(core) == crc
        mfl = core[3]
        frames.append(core[4:4 + mfl])
        i = j + 1
    return frames


def _find_mec(frames, mec, subtype=None):
    """subtype disambiguates MEC 0x24, which carries both the RT+ ODA
    registration (subtype byte 0x06) and RT+ tag data (subtype byte
    0x16) as two DIFFERENT frames sharing the same outer MEC value."""
    for msg in frames:
        if msg and msg[0] == mec and (subtype is None or (len(msg) > 1 and msg[1] == subtype)):
            return msg
    return None


class RtPlusTextBoundaryTests(SimpleTestCase):
    """_build_rt_plus_text computes the final "artist - title" RT
    string AND the artist/title substrings actually surviving
    truncation to the 64-char RadioText limit FROM THAT SAME STRING --
    the 2026-08-02 fix for findings #5 (offsets computed from
    pre-truncation lengths could point past the end of the real
    transmitted text)."""

    def setUp(self):
        self.mgr = RBDSManager()

    def test_short_text_unaffected(self):
        text, artist, title = self.mgr._build_rt_plus_text("Rush", "Tom Sawyer")
        self.assertEqual(text, "Rush - Tom Sawyer")
        self.assertEqual(artist, "Rush")
        self.assertEqual(title, "Tom Sawyer")

    def test_artist_nearly_filling_rt(self):
        artist = "A" * 60
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, "X")
        self.assertEqual(len(text), 64)
        self.assertEqual(out_artist, artist)
        self.assertEqual(out_title, "X")
        self.assertTrue(text.startswith(out_artist))
        self.assertTrue(text.endswith(out_title))

    def test_title_partially_truncated(self):
        artist = "A" * 10
        title = "B" * 60
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, title)
        self.assertEqual(len(text), 64)
        self.assertEqual(out_artist, artist)
        # 64 - 10 - len(" - ") = 51 chars of title survive.
        self.assertEqual(out_title, "B" * 51)
        self.assertEqual(text, artist + " - " + "B" * 51)
        # Every start+length must remain inside the final text.
        self.assertIn(out_artist, text)
        self.assertIn(out_title, text)

    def test_separator_truncated_omits_rt_plus(self):
        artist = "A" * 63  # leaves only 1 char of " - " before the 64-char cap
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, "Title")
        self.assertEqual(len(text), 64)
        # Nothing meaningful survives for the title side -- omit
        # rather than emit a broken/zero-length tag.
        self.assertEqual(out_artist, "")
        self.assertEqual(out_title, "")

    def test_empty_surviving_title_omits(self):
        artist = "A" * 61  # exactly fills through the separator, 0 chars of title survive
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, "Title")
        self.assertEqual(out_artist, "")
        self.assertEqual(out_title, "")

    def test_omit_sentinel_is_empty_string_not_none(self):
        # Distinguishable from "never had an artist/title concept"
        # (None) -- see _build_rt_plus_text's own docstring for why
        # this distinction matters (finding #4: don't let a degenerate
        # song fall back to the generic non-song RT+ tag).
        _, out_artist, out_title = self.mgr._build_rt_plus_text("A" * 63, "Title")
        self.assertIsNotNone(out_artist)
        self.assertIsNotNone(out_title)


class RtPlusOmitVsGenericTests(SimpleTestCase):
    """A real song whose artist doesn't fit the two-tag format (either
    >32 chars, or "" after truncation defeated the split) must omit
    RT+ entirely, NOT fall back to mec_rt_plus_tags_generic -- that tag
    is reserved for genuinely non-song RT (weather/promo/station-ID/
    file/url), and reusing it for a song mislabels it (finding #4)."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.config = mock.Mock(
            pi_code="", ecc="", ta=False, tp=False,
            di_dynamic_pty=False, di_compressed=False, di_artificial_head=False, di_stereo=True,
            ms=True, pty=0, use_rt_plus=True, af_frequencies_mhz="",
            uecp_site_address=1, uecp_encoder_address=0,
        )

    def _rt_plus_frames(self, rt, artist, title):
        payload = self.mgr._build_uecp_payload(self.config, "PS      ", rt, artist, title)
        return _split_uecp_frames(payload)

    def test_normal_song_gets_two_tag_format(self):
        frames = self._rt_plus_frames("Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        # Vendor MEC 0xAA is never sent (settled 2026-08-04 -- see
        # RtPlusMecCompositionTests for the dedicated regression
        # coverage); this class only cares about 0x24's tag geometry.
        self.assertIsNone(_find_mec(frames, 0xAA))
        tags = _find_mec(frames, 0x24, subtype=0x16)
        self.assertEqual(tags[2], 0x08)  # two-tag "song entry" marker

    def test_long_artist_song_omits_rt_plus_entirely(self):
        artist = "A" * 40  # exceeds the 32-char field limit
        rt = f"{artist} - Title"
        frames = self._rt_plus_frames(rt, artist, "Title")
        self.assertIsNone(_find_mec(frames, 0xAA))
        # Only the ODA registration MEC (0x24 subtype 0x06) may still
        # ride along -- no tag data (subtype 0x16) for this send.
        for msg in frames:
            if msg and msg[0] == 0x24:
                self.assertEqual(msg[1], 0x06, "no RT+ tag data may be sent for an omitted song")

    def test_truncation_degenerated_song_omits_rt_plus_entirely(self):
        # artist/title arrive here as "" (not None) -- the sentinel
        # _build_rt_plus_text uses when truncation ate the split.
        frames = self._rt_plus_frames("A" * 64, "", "")
        self.assertIsNone(_find_mec(frames, 0xAA))
        for msg in frames:
            if msg and msg[0] == 0x24:
                self.assertEqual(msg[1], 0x06)

    def test_genuine_non_song_rt_gets_generic_tag(self):
        # artist/title are None -- weather/promo/station-ID shape.
        frames = self._rt_plus_frames("Temp: 86F, Wind: SE", None, None)
        self.assertIsNone(_find_mec(frames, 0xAA))
        tags = _find_mec(frames, 0x24, subtype=0x16)
        self.assertIsNotNone(tags)
        self.assertEqual(tags[2], 0x0B)  # single-tag "generic" marker


@mock.patch("rbds.services.rbds_manager.close_old_connections", new=lambda: None)
class RBDSManagerOneShotToggleTests(TestCase):
    """SPB490 p.31: RT flags bit0 is a ONE-SHOT TOGGLE COMMAND ("0=do
    not toggle, 1=toggle"), not a persistent state bit -- confirmed via
    primary-source review 2026-08-02. rt_changed must be computed fresh
    each tick and never re-sent as True for a resend of unchanged
    text (the pre-fix behavior: a stored, flipped boolean kept getting
    resent on every subsequent send, spuriously toggling A/B on every
    receiver on every unconditional 30s full-resend of unchanged RT).

    close_old_connections() is patched to a no-op for this whole class
    -- calling the real one mid-_tick() closes the connection Django's
    TestCase wraps in an atomic block, which is fine/intended in the
    real long-running engine process but breaks the test transaction
    here; it's pure connection-pool hygiene with no bearing on what
    these tests actually verify."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.sent_payloads = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: self.sent_payloads.append(payload))
        self.mgr._read_category_state = mock.Mock(return_value={"pty_override": None, "ptyn": ""})
        config = RBDSConfig.load()
        config.protocol = "uecp"
        config.use_rt_plus = False
        config.send_ct = False
        config.save()
        # Pre-seed so the (correct, separately-tested) CT On/Off logic
        # doesn't add extra non-RT payloads and confuse "how many/which
        # payload" here -- this class isn't testing CT at all (see
        # RBDSManagerCtOnOffTests). ALL FOUR of these need seeding
        # (2026-08-03 fix): CT reassertion now also fires on
        # due_for_full_resend (a fresh manager's _last_full_resend
        # starts at 0.0, making due_for_full_resend unconditionally
        # True on the very first tick) AND on a fresh reconnect (a
        # fresh manager's very first successful send is itself a
        # connected-state transition, which would otherwise also read
        # as "just reconnected"). Pretending the manager was already
        # connected, in the same episode _last_ct_synced_connected_since
        # already reflects, suppresses that first-tick reconnect signal
        # the same way the other three seeds suppress their triggers.
        self.mgr._last_send_ct_state = False
        self.mgr._last_full_resend = time.time()
        self.mgr._connected = True
        self.mgr._connected_since = 1.0
        self.mgr._last_ct_synced_connected_since = 1.0

    def _set_now_playing(self, title, artist=""):
        self.mgr._read_now_playing = mock.Mock(return_value={"title": title, "artist": artist})

    def _main_content_payloads(self):
        """Payloads that carry an RT MEC -- i.e. an actual main
        content send, not an auxiliary CT on/off (0x19) or CT value
        (0x0D) frame. A full-resend or reconnect tick can now
        legitimately also emit a separate CT reassertion payload
        alongside the main content payload (2026-08-03 fix), so a raw
        len(self.sent_payloads) is no longer the same thing as "how
        many main content sends happened," which is what these tests
        actually care about."""
        return [p for p in self.sent_payloads if _find_mec(_split_uecp_frames(p), 0x0A) is not None]

    def _last_rt_ab_bit(self):
        # Searches backward for the last payload that actually CARRIES
        # an RT MEC, rather than assuming it's simply the last payload
        # transmitted -- a full-resend or reconnect tick can now
        # legitimately also emit a separate CT on/off reassertion
        # payload (2026-08-03 fix) alongside the main content payload,
        # so "last payload" and "last RT-bearing payload" are no
        # longer always the same thing.
        for payload in reversed(self.sent_payloads):
            frames = _split_uecp_frames(payload)
            rt = _find_mec(frames, 0x0A)
            if rt is not None:
                return rt[4] & 0x01
        self.fail("expected an RT MEC in some transmitted payload")

    def test_first_new_rt_toggles(self):
        self._set_now_playing("First Song")
        self.mgr._tick()
        self.assertEqual(self._last_rt_ab_bit(), 1)

    def test_second_different_rt_also_toggles(self):
        self._set_now_playing("First Song")
        self.mgr._tick()
        self._set_now_playing("Second Song")
        self.mgr._tick()
        self.assertEqual(self._last_rt_ab_bit(), 1)

    def test_unchanged_periodic_full_resend_does_not_toggle(self):
        self._set_now_playing("Same Song")
        self.mgr._tick()
        self.assertEqual(self._last_rt_ab_bit(), 1)
        # Force the 30s full-resend gate open without waiting 30s and
        # without changing now-playing at all. This now also
        # legitimately reasserts CT state (2026-08-03 fix) alongside
        # the main content resend, hence checking main-content sends
        # specifically rather than the raw payload count.
        self.mgr._last_full_resend = 0.0
        self.mgr._tick()
        self.assertEqual(len(self._main_content_payloads()), 2, "the forced full-resend must still have sent")
        self.assertEqual(self._last_rt_ab_bit(), 0, "unchanged RT resend must NOT toggle A/B")

    def test_reconnect_resend_of_unchanged_rt_does_not_toggle(self):
        self._set_now_playing("Same Song")
        self.mgr._tick()
        self.assertEqual(self._last_rt_ab_bit(), 1)
        # Simulate "just reconnected" -- _connected is False, forcing
        # a send even though nothing (including RT) changed. This now
        # also legitimately reasserts CT state (2026-08-03 fix), hence
        # checking main-content sends specifically.
        self.mgr._connected = False
        self.mgr._tick()
        self.assertEqual(len(self._main_content_payloads()), 2)
        self.assertEqual(self._last_rt_ab_bit(), 0, "reconnect resend of unchanged RT must NOT toggle A/B")


@mock.patch("rbds.services.rbds_manager.close_old_connections", new=lambda: None)
class RBDSManagerCtOnOffTests(TestCase):
    """MEC 0x19 (CT On/Off) is distinct from MEC 0x0D (Real time clock,
    value only) -- confirmed via primary-source review 2026-08-02 (see
    uecp.mec_ct_on_off's docstring). RBDSManager must send the explicit
    enable/disable whenever the operator's send_ct setting changes --
    AND reassert it idempotently on startup, on every periodic full
    resend, and on a fresh reconnect, not just on that local config
    change. Edge-trigger-only tracking was a confirmed defect: the
    remote encoder (StereoTool) can reset its own group-4A-enable
    state independently (e.g. a preset switch), with config.send_ct
    never changing on this side at all -- silently stopping 4A with
    nothing here to notice or correct it. A live restart experiment
    (2026-08-03, CT_RESTART_EXPERIMENT_RESULT.md in the RBDS bench
    scratch area) confirmed a fresh process (which resets
    _last_send_ct_state to None) restores 4A with no config change --
    this test class is the regression coverage for that fix."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.sent = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: self.sent.append(payload))
        self.mgr._read_now_playing = mock.Mock(return_value={"title": "", "artist": ""})
        self.mgr._read_category_state = mock.Mock(return_value={"pty_override": None, "ptyn": ""})
        self.config = RBDSConfig.load()
        self.config.protocol = "uecp"
        self.config.use_rt_plus = False

    def _ct_on_off_values_sent(self):
        values = []
        for payload in self.sent:
            for msg in _split_uecp_frames(payload):
                if msg and msg[0] == 0x19:
                    values.append(msg[1])
        return values

    # 1. Fresh manager sends MEC 0x19 on first eligible tick -- even
    # when send_ct is the (default False) unchanged value, since the
    # desired STATE, not just "enabled," must be asserted on startup.
    def test_fresh_manager_asserts_ct_state_on_first_tick_even_when_disabled(self):
        self.config.save()  # send_ct left at its default (False)
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x00])
        self.assertEqual(self.mgr._last_send_ct_state, False)

    def test_enabling_send_ct_sends_enable(self):
        self.config.send_ct = True
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01])

    # 5/6. Config change in either direction sends the matching state.
    def test_disabling_after_enabled_sends_disable(self):
        self.config.send_ct = True
        self.config.save()
        self.mgr._tick()
        self.config.send_ct = False
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01, 0x00])

    # 2. Ordinary unchanged ticks (no config change, not due for full
    # resend, no reconnect) must not repeatedly spam MEC 0x19.
    def test_ordinary_unchanged_tick_does_not_resend(self):
        self.config.send_ct = True
        self.config.save()
        self.mgr._tick()
        self.mgr._tick()  # nothing changed, not due for full resend
        self.assertEqual(self._ct_on_off_values_sent(), [0x01], "an ordinary unchanged tick must not resend")

    # 3. The existing periodic full-state resend must include MEC 0x19
    # even when send_ct itself is unchanged -- this is the corrected
    # behavior; the old edge-trigger-only version deliberately did NOT
    # do this, which is exactly the confirmed defect being fixed here.
    def test_periodic_full_resend_reasserts_ct_state_even_when_unchanged(self):
        self.config.send_ct = True
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01])
        self.mgr._last_full_resend = 0.0  # force the existing full-resend gate open
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01, 0x01],
                          "a full resend must reassert CT state even though the cached value already matches")

    # 9. The disabled state is a first-class value too -- a full
    # resend must reassert send_ct=False just as it reasserts True.
    def test_periodic_full_resend_reasserts_disabled_state_too(self):
        self.config.save()  # send_ct stays False
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x00])
        self.mgr._last_full_resend = 0.0
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x00, 0x00])

    # 4. A fresh reconnect (an actual _connected_since transition, not
    # just any ordinary tick) must reassert CT state.
    def test_reconnect_forces_ct_reassertion(self):
        self.config.send_ct = True
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01])
        # Simulate a real disconnect/reconnect cycle, same pattern
        # already established in RBDSManagerOneShotToggleTests: force
        # _connected False so _send()'s own "not self._connected" gate
        # fires and _mark_up() runs for real, producing a genuinely
        # new _connected_since -- not just re-ticking the same episode.
        self.mgr._connected = False
        self.mgr._connected_since = None
        time.sleep(0.01)  # guarantee a distinguishable time.time() from the first _mark_up()
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01, 0x01],
                          "a fresh reconnect must reassert CT state even though the value is unchanged")

    # 7/8. Failed send leaves the desired state pending (not committed
    # as "last sent"); the next eligible tick retries it, and only a
    # successful transmission commits _last_send_ct_state.
    def test_failed_ct_send_does_not_update_last_state_and_is_retried(self):
        self.config.send_ct = True
        self.config.save()

        def fail_on_ct(config, payload):
            frames = _split_uecp_frames(payload)
            if _find_mec(frames, 0x19) is not None:
                raise ConnectionError("simulated TCP failure for CT on/off")
            self.sent.append(payload)

        self.mgr._transmit = mock.Mock(side_effect=fail_on_ct)
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [], "the failed CT send must not appear as sent")
        self.assertIsNone(self.mgr._last_send_ct_state, "a failed send must not commit the desired state")
        self.assertIn("CT on/off send failed", self.mgr._last_error)

        # Next eligible tick (still "unsynced" since _last_send_ct_state
        # is still None) retries automatically, now with a transmit
        # that succeeds.
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: self.sent.append(payload))
        self.mgr._tick()
        self.assertEqual(self._ct_on_off_values_sent(), [0x01], "the retry must succeed and be recorded")
        self.assertEqual(self.mgr._last_send_ct_state, True, "a successful retry must commit the state")

    # 10. MEC 0x0D minute-value sending must be unaffected by any of
    # the on/off reassertion logic above -- still gated purely on the
    # minute-rollover check, once per minute, independent of full
    # resends/reconnects/on-off retries.
    def test_mec_0d_minute_send_cadence_unaffected_by_on_off_changes(self):
        def mec_0d_count():
            count = 0
            for payload in self.sent:
                for msg in _split_uecp_frames(payload):
                    if msg and msg[0] == 0x0D:
                        count += 1
            return count

        self.config.send_ct = True
        self.config.save()
        self.mgr._tick()
        self.assertEqual(mec_0d_count(), 1, "the first tick must send exactly one CT value frame")
        # More ticks in the SAME minute (including a forced full
        # resend and a forced reconnect) must not send another 0x0D.
        self.mgr._last_full_resend = 0.0
        self.mgr._tick()
        self.mgr._connected = False
        self.mgr._connected_since = None
        self.mgr._tick()
        self.assertEqual(mec_0d_count(), 1, "0x0D must stay gated on minute rollover, not on/off reassertion")
        # A genuine minute rollover still sends exactly one more.
        self.mgr._last_ct_sent_minute = (self.mgr._last_ct_sent_minute - 1) % 60
        self.mgr._tick()
        self.assertEqual(mec_0d_count(), 2, "a real minute rollover must still send CT value")


class RtPlusMecCompositionTests(SimpleTestCase):
    """Permanent regression coverage for the settled RT+ architecture
    (2026-08-04, after a controlled 5-mode bench isolation experiment
    -- see scratchpad/rbds_bench/rtplus_isolation_experiment/
    RTPLUS_0X24_0XAA_ISOLATION_REPORT.md and
    RTPLUS_0XAA_REMOVAL_DEPLOYMENT_REPORT.md): MEC 0x24 only, vendor
    MEC 0xAA never sent. Replaces the temporary diagnostic-mode test
    class (RT_PLUS_MEC_0X24_ENABLED/0XAA_ENABLED and mec_song_info
    have both been removed from the codebase -- see those reports for
    why). Every assertion here inspects real, decoded UECP frames
    (never a payload/MEC count), per the standing rule that a count
    alone can't prove which specific MEC did or didn't appear."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.config = mock.Mock(
            pi_code="", ecc="", ta=False, tp=False,
            di_dynamic_pty=False, di_compressed=False, di_artificial_head=False, di_stereo=True,
            ms=True, pty=0, use_rt_plus=True, af_frequencies_mhz="",
            uecp_site_address=1, uecp_encoder_address=0,
        )

    def _song_frames(self, rt_ab_toggle=False):
        payload = self.mgr._build_uecp_payload(
            self.config, "PS      ", "Rush - Tom Sawyer", "Rush", "Tom Sawyer",
            rt_ab_toggle=rt_ab_toggle,
        )
        return _split_uecp_frames(payload)

    def _nonsong_frames(self):
        payload = self.mgr._build_uecp_payload(
            self.config, "PS      ", "Temp: 86F, Wind: SE", None, None,
        )
        return _split_uecp_frames(payload)

    # 1-4: default full UECP payload composition.
    def test_full_payload_contains_ordinary_rt(self):
        self.assertIsNotNone(_find_mec(self._song_frames(), 0x0A), "ordinary RT must always be present")

    def test_full_payload_contains_oda_registration(self):
        oda = _find_mec(self._song_frames(), 0x24, subtype=0x06)
        self.assertIsNotNone(oda, "ODA registration (0x24/06) must be present")
        self.assertEqual(bytes(oda), bytes([0x24, 0x06, 0x16, 0x00, 0x00, 0x4B, 0xD7]))

    def test_full_payload_contains_song_tag_frame(self):
        self.assertIsNotNone(_find_mec(self._song_frames(), 0x24, subtype=0x16), "song tag geometry must be present")

    def test_full_payload_does_not_contain_vendor_song_info(self):
        self.assertIsNone(_find_mec(self._song_frames(), 0xAA), "MEC 0xAA must never be sent")

    # 5-6: RT+-only maintenance payload (the ~2s resend).
    def test_rt_plus_only_payload_contains_0x24(self):
        sent = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: sent.append(payload))
        self.mgr._send_rt_plus_only(self.config, "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        self.assertEqual(len(sent), 1)
        frames = _split_uecp_frames(sent[0])
        self.assertIsNotNone(_find_mec(frames, 0x24, subtype=0x06))
        self.assertIsNotNone(_find_mec(frames, 0x24, subtype=0x16))

    def test_rt_plus_only_payload_does_not_contain_vendor_song_info(self):
        sent = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: sent.append(payload))
        self.mgr._send_rt_plus_only(self.config, "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        frames = _split_uecp_frames(sent[0])
        self.assertIsNone(_find_mec(frames, 0xAA))

    # 7: song tag geometry unchanged (same bit layout as before 0xAA's removal).
    def test_song_tag_geometry_unchanged(self):
        tags = _find_mec(self._song_frames(), 0x24, subtype=0x16)
        self.assertEqual(tags[2], 0x08, "two-tag 'song entry' marker must be unchanged")

    # 8: non-song generic tag unchanged.
    def test_nonsong_generic_tag_unchanged(self):
        frames = self._nonsong_frames()
        tags = _find_mec(frames, 0x24, subtype=0x16)
        self.assertIsNotNone(tags)
        self.assertEqual(tags[2], 0x0B, "single-tag 'generic' marker must be unchanged")
        self.assertIsNone(_find_mec(frames, 0xAA), "0xAA was never sent for non-song content even before removal")

    # 9: ordinary RT (0x0A) byte-for-byte unaffected by 0xAA's removal --
    # compares the full-payload RT frame against uecp.mec_rt() called
    # directly, which 0xAA's removal cannot have touched.
    def test_ordinary_rt_bytes_match_direct_builder_call(self):
        frames = self._song_frames(rt_ab_toggle=True)
        rt_frame = _find_mec(frames, 0x0A)
        expected = uecp.mec_rt("Rush - Tom Sawyer", ab_flag=True)
        self.assertEqual(bytes(rt_frame), expected)

    # 10: resend cadence constants unchanged (a plain regression guard --
    # 0xAA's removal touched neither of these).
    def test_resend_cadence_constants_unchanged(self):
        self.assertEqual(rbds_manager.FULL_RESEND_SECONDS, 30)
        self.assertEqual(rbds_manager.RT_PLUS_RESEND_SECONDS, 2)

    # 11: RT A/B toggle bit still correctly reflects rt_ab_toggle, with
    # 0xAA confirmed absent from the same payload -- ties the two
    # together in one assertion per the "at least one test must inspect
    # actual frames" requirement.
    def test_rt_ab_toggle_bit_unaffected_by_0xaa_removal(self):
        frames_toggled = self._song_frames(rt_ab_toggle=True)
        frames_not_toggled = self._song_frames(rt_ab_toggle=False)
        self.assertEqual(_find_mec(frames_toggled, 0x0A)[4] & 0x01, 1)
        self.assertEqual(_find_mec(frames_not_toggled, 0x0A)[4] & 0x01, 0)
        self.assertIsNone(_find_mec(frames_toggled, 0xAA))
        self.assertIsNone(_find_mec(frames_not_toggled, 0xAA))
        # Full end-to-end A/B bookkeeping (rt_changed computation, one-shot
        # edge semantics) is covered by RBDSManagerOneShotToggleTests,
        # unmodified by this cleanup and still passing.

    # 12-14: failed-send retry, CT, and AF are unaffected by this
    # cleanup (no code touched in any of those paths) -- covered by the
    # existing, unmodified RBDSManagerFailedSendPreservesToggleTests,
    # RBDSManagerCtOnOffTests, and RBDSAfRuntimeBlockTests respectively,
    # all still passing (see the full suite run in
    # RTPLUS_0XAA_REMOVAL_DEPLOYMENT_REPORT.md).


class RBDSConfigValidationTests(SimpleTestCase):
    def test_af_frequencies_blocked(self):
        config = RBDSConfig(af_frequencies_mhz="89.5, 91.3")
        with self.assertRaises(ValidationError):
            config.clean()

    def test_blank_af_frequencies_ok(self):
        config = RBDSConfig(af_frequencies_mhz="", pi_code="", ecc="")
        config.clean()  # must not raise

    def test_uecp_site_address_out_of_range_rejected(self):
        config = RBDSConfig(uecp_site_address=1024, uecp_encoder_address=0)
        with self.assertRaises(ValidationError):
            config.clean()

    def test_uecp_encoder_address_out_of_range_rejected(self):
        config = RBDSConfig(uecp_site_address=1, uecp_encoder_address=64)
        with self.assertRaises(ValidationError):
            config.clean()

    def test_valid_addresses_accepted(self):
        config = RBDSConfig(uecp_site_address=1023, uecp_encoder_address=63, pi_code="", ecc="")
        config.clean()  # must not raise

    def test_ta_without_tp_rejected(self):
        config = RBDSConfig(ta=True, tp=False, pi_code="", ecc="")
        with self.assertRaises(ValidationError):
            config.clean()

    def test_ta_with_tp_accepted(self):
        config = RBDSConfig(ta=True, tp=True, pi_code="", ecc="")
        config.clean()  # must not raise

    def test_ta_false_never_requires_tp(self):
        config = RBDSConfig(ta=False, tp=False, pi_code="", ecc="")
        config.clean()  # must not raise


class RBDSPSFrameValidationTests(SimpleTestCase):
    def test_hold_seconds_below_floor_rejected(self):
        frame = RBDSPSFrame(text="TEST", hold_seconds=1)
        with self.assertRaises(ValidationError):
            frame.clean()

    def test_hold_seconds_zero_rejected(self):
        frame = RBDSPSFrame(text="TEST", hold_seconds=0)
        with self.assertRaises(ValidationError):
            frame.clean()

    def test_hold_seconds_at_floor_accepted(self):
        frame = RBDSPSFrame(text="TEST", hold_seconds=4)
        frame.clean()  # must not raise


class RtNormalizationGapTests(SimpleTestCase):
    """Artist/title/message text are normalized before use, but two
    spots were missed: the FINAL formatted now_playing_format result,
    and admin-configured PTYN/RT+-delimiter fields that never pass
    through _resolve_rt_content at all. 2026-08-02 second-opinion
    review fix."""

    def setUp(self):
        self.mgr = RBDSManager()

    def test_now_playing_format_final_result_is_normalized(self):
        # The template string itself (not just artist/title) can
        # contain a raw newline or smart-punctuation character typed
        # directly into the admin field -- normalizing artist/title
        # alone doesn't sanitize that.
        config = mock.Mock(use_rt_plus=False, now_playing_format="{artist}\n{title}…")
        now_playing = {"title": "Song", "artist": "Artist"}
        rt_text, artist, title = self.mgr._resolve_rt_content(config, now_playing, "nowplaying", None, {})
        self.assertNotIn("\n", rt_text)
        self.assertEqual(rt_text, "Artist Song...")

    def test_ptyn_smart_quote_normalizes_to_a_real_g0_character(self):
        # Without normalizing first, encode_rds_g0 would fall back to
        # a space for the unsupported curly apostrophe -- G0 DOES have
        # a real code point for a plain one.
        result = uecp.mec_ptyn(charset.normalize_text("DJ’s"))
        self.assertEqual(result, bytes([0x3E, 0x00, 0x00]) + b"DJ's    ")

    def test_manager_normalizes_ptyn_at_the_actual_call_site(self):
        # The test above only proves normalize_text()+mec_ptyn()
        # compose correctly -- it doesn't exercise
        # _build_uecp_payload() itself, so it wouldn't catch a future
        # accidental removal of normalize_text(ptyn) from that actual
        # call site. This one does, by building the real payload with
        # a raw (un-normalized) ptyn and inspecting the resulting
        # MEC 0x3E bytes.
        config = mock.Mock(
            pi_code="", ecc="", ta=False, tp=False,
            di_dynamic_pty=False, di_compressed=False, di_artificial_head=False, di_stereo=True,
            ms=True, pty=0, use_rt_plus=False, af_frequencies_mhz="",
            uecp_site_address=1, uecp_encoder_address=0,
        )
        payload = self.mgr._build_uecp_payload(config, "PS      ", "RT text", ptyn="DJ’s")
        ptyn_mec = _find_mec(_split_uecp_frames(payload), 0x3E)
        self.assertIsNotNone(ptyn_mec)
        self.assertEqual(ptyn_mec, bytes([0x3E, 0x00, 0x00]) + b"DJ's    ")

    def test_rt_plus_delimiter_with_smart_dash_still_splits(self):
        # A stale, un-normalized "–" delimiter would never match
        # raw_text after raw_text's OWN normalization already
        # collapsed "–" to "-", silently defeating the split.
        message = mock.Mock(source_type="static", text="Artist – Title", rt_plus_delimiter="–")
        config = mock.Mock(use_rt_plus=False)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertEqual(rt_text, "Artist - Title")


@mock.patch("rbds.services.rbds_manager.close_old_connections", new=lambda: None)
class RBDSManagerFailedSendPreservesToggleTests(TestCase):
    """A failed transmission must not be recorded as if it succeeded
    -- 2026-08-02 second-opinion fix. The original one-shot A/B fix
    updated _last_sent_ps/_last_sent_rt unconditionally right after
    calling _send(), even though _send() swallows its own
    transmission exceptions. A failed send of a real RT change was
    therefore recorded as "sent"; the next tick's retry computed
    rt_changed=False for a change the encoder never actually
    received, and the eventual successful retry silently ate the A/B
    toggle for it."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.sent_payloads = []
        self.fail_next = False

        def transmit(config, payload):
            if self.fail_next:
                self.fail_next = False
                raise ConnectionError("simulated TCP failure")
            self.sent_payloads.append(payload)

        self.mgr._transmit = mock.Mock(side_effect=transmit)
        self.mgr._read_category_state = mock.Mock(return_value={"pty_override": None, "ptyn": ""})
        # Not testing CT here -- see RBDSManagerCtOnOffTests. ALL FOUR
        # of these need seeding (2026-08-03 fix): CT reassertion now
        # also fires on due_for_full_resend (a fresh manager's
        # _last_full_resend starts at 0.0, unconditionally True on the
        # first tick) and on a fresh reconnect (a fresh manager's own
        # first successful send, or -- relevant to this class
        # specifically -- a fail_next-triggered mark_down/mark_up
        # cycle on retry, both read as "just reconnected" otherwise).
        self.mgr._last_send_ct_state = False
        self.mgr._last_full_resend = time.time()
        self.mgr._connected = True
        self.mgr._connected_since = 1.0
        self.mgr._last_ct_synced_connected_since = 1.0
        config = RBDSConfig.load()
        config.protocol = "uecp"
        config.use_rt_plus = False
        config.send_ct = False
        config.save()

    def _set_now_playing(self, title):
        self.mgr._read_now_playing = mock.Mock(return_value={"title": title, "artist": ""})

    def _main_content_payloads(self):
        """See RBDSManagerOneShotToggleTests's identical helper -- a
        retry-after-failure tick is itself a mark_down/mark_up
        transition, which now (correctly) also reasserts CT state
        (2026-08-03 fix), so raw sent-payload counts no longer equal
        "how many main content sends happened.\""""
        return [p for p in self.sent_payloads if _find_mec(_split_uecp_frames(p), 0x0A) is not None]

    def _last_rt_ab_bit(self):
        # Searches backward for the RT-bearing payload -- see
        # RBDSManagerOneShotToggleTests's identical helper for why.
        for payload in reversed(self.sent_payloads):
            frames = _split_uecp_frames(payload)
            rt = _find_mec(frames, 0x0A)
            if rt is not None:
                return rt[4] & 0x01
        self.fail("expected an RT MEC in some transmitted payload")

    def test_failed_first_send_then_successful_retry_still_toggles(self):
        self._set_now_playing("First Song")
        self.fail_next = True
        self.mgr._tick()
        self.assertEqual(self.sent_payloads, [], "a failed transmit must not be recorded as sent")

        self.mgr._tick()  # retry, same pending RT change
        self.assertEqual(len(self._main_content_payloads()), 1)
        self.assertEqual(self._last_rt_ab_bit(), 1, "the pending change must still toggle A/B on the retry")

    def test_last_sent_rt_does_not_advance_on_failure(self):
        self._set_now_playing("Some Song")
        self.fail_next = True
        self.mgr._tick()
        self.assertIsNone(self.mgr._last_sent_rt)

    def test_last_full_resend_does_not_advance_on_failure(self):
        self._set_now_playing("Some Song")
        self.fail_next = True
        before = self.mgr._last_full_resend
        self.mgr._tick()
        self.assertEqual(self.mgr._last_full_resend, before)

    def test_initial_engine_send_fails_then_retry_succeeds(self):
        # Engine just started (_last_sent_rt is still None) and the
        # very first send fails -- the subsequent successful retry
        # must still be treated as the "first" transmission (toggle=1),
        # not silently skipped.
        self._set_now_playing("Startup Song")
        self.fail_next = True
        self.mgr._tick()
        self.assertEqual(self.sent_payloads, [])
        self.mgr._tick()
        self.assertEqual(len(self._main_content_payloads()), 1)
        self.assertEqual(self._last_rt_ab_bit(), 1)


class RBDSAfRuntimeBlockTests(SimpleTestCase):
    """RBDSConfig.clean() blocks AF at the admin/form layer, but
    Django's save() doesn't call clean() automatically -- a direct ORM
    write, or a value already stored before validation existed, could
    otherwise still reach the runtime payload. _build_uecp_payload
    must independently refuse to ever emit the AF MEC, regardless of
    whether clean() ran. 2026-08-02 second-opinion fix."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.config = mock.Mock(
            pi_code="", ecc="", ta=False, tp=False,
            di_dynamic_pty=False, di_compressed=False, di_artificial_head=False, di_stereo=True,
            ms=True, pty=0, use_rt_plus=False,
            af_frequencies_mhz="89.5, 91.3",  # bypasses clean() entirely -- a bare mock, not a real .save()
            uecp_site_address=1, uecp_encoder_address=0,
        )

    def test_af_never_reaches_the_wire_even_when_configured(self):
        payload = self.mgr._build_uecp_payload(self.config, "PS      ", "RT text")
        frames = _split_uecp_frames(payload)
        self.assertIsNone(_find_mec(frames, 0x13), "AF (MEC 0x13) must never be transmitted")

    def test_other_content_still_sends_when_af_is_set(self):
        # A stray/legacy AF value must not take down the rest of the
        # payload -- only AF itself is skipped.
        payload = self.mgr._build_uecp_payload(self.config, "TESTPS  ", "Test RT")
        frames = _split_uecp_frames(payload)
        self.assertIsNotNone(_find_mec(frames, 0x02))  # PS
        self.assertIsNotNone(_find_mec(frames, 0x0A))  # RT


class EffectiveDynamicPtyTests(TestCase):
    """RBDSConfig.di_dynamic_pty was a fully independent static flag,
    never derived from whether Category.rbds_pty_override can actually
    make PTY vary track-to-track -- a station using category overrides
    could transmit PTY changes while DI told receivers "PTY is
    static." 2026-08-02 fix: _effective_dynamic_pty() auto-derives
    True whenever any category has an override configured, OR'd with
    (never replacing) the admin's own manual setting."""

    def setUp(self):
        self.mgr = RBDSManager()
        from library.models import Category, CategoryKind
        # A real "music" CategoryKind is already seeded by a data
        # migration in this project's DB -- use an isolated test-only
        # code so this test never collides with (or is confused for)
        # real seeded/production data.
        self.kind = CategoryKind.objects.create(code="__rbds_test_kind__", name="RBDS Test Kind")

    def test_no_overrides_no_manual_flag_is_false(self):
        from library.models import Category
        Category.objects.create(code="__rbds_test_a__", name="Test A", kind=self.kind, rbds_pty_override=None)
        config = mock.Mock(di_dynamic_pty=False)
        self.assertFalse(self.mgr._effective_dynamic_pty(config))

    def test_no_overrides_manual_flag_true_is_respected(self):
        from library.models import Category
        Category.objects.create(code="__rbds_test_a__", name="Test A", kind=self.kind, rbds_pty_override=None)
        config = mock.Mock(di_dynamic_pty=True)
        self.assertTrue(self.mgr._effective_dynamic_pty(config))

    def test_any_category_override_forces_true_even_if_manual_flag_false(self):
        from library.models import Category
        Category.objects.create(code="__rbds_test_a__", name="Test A", kind=self.kind, rbds_pty_override=None)
        Category.objects.create(code="__rbds_test_wx__", name="Test Weather", kind=self.kind, rbds_pty_override=29)
        config = mock.Mock(di_dynamic_pty=False)
        self.assertTrue(self.mgr._effective_dynamic_pty(config))

    def test_category_override_and_manual_flag_both_true(self):
        from library.models import Category
        Category.objects.create(code="__rbds_test_wx__", name="Test Weather", kind=self.kind, rbds_pty_override=29)
        config = mock.Mock(di_dynamic_pty=True)
        self.assertTrue(self.mgr._effective_dynamic_pty(config))

    @mock.patch("rbds.services.rbds_manager.close_old_connections", new=lambda: None)
    def test_tick_resolves_and_sends_the_effective_value(self):
        # _effective_dynamic_pty() is only ever called from _tick()
        # (builders take a plain dynamic_pty parameter, no DB query
        # buried inside them -- see _send's own docstring) -- so the
        # real regression to guard against is a future _tick() call
        # site reverting to config.di_dynamic_pty directly. Drives an
        # actual tick and inspects the transmitted MEC 0x04, the same
        # way RBDSManagerOneShotToggleTests exercises _tick().
        from library.models import Category
        Category.objects.create(code="__rbds_test_wx__", name="Test Weather", kind=self.kind, rbds_pty_override=29)

        config = RBDSConfig.load()
        config.protocol = "uecp"
        config.use_rt_plus = False
        config.send_ct = False
        config.di_dynamic_pty = False  # manual flag says static...
        config.save()

        sent_payloads = []
        self.mgr._transmit = mock.Mock(side_effect=lambda cfg, payload: sent_payloads.append(payload))
        self.mgr._read_now_playing = mock.Mock(return_value={"title": "Song", "artist": ""})
        self.mgr._read_category_state = mock.Mock(return_value={"pty_override": None, "ptyn": ""})
        # All four need seeding -- see RBDSManagerOneShotToggleTests
        # .setUp for why (2026-08-03 fix: CT reassertion now also
        # fires on due_for_full_resend and on a fresh reconnect, not
        # just on a config.send_ct change).
        self.mgr._last_send_ct_state = False
        self.mgr._last_full_resend = time.time()
        self.mgr._connected = True
        self.mgr._connected_since = 1.0
        self.mgr._last_ct_synced_connected_since = 1.0

        self.mgr._tick()

        self.assertEqual(len(sent_payloads), 1)
        di_mec = _find_mec(_split_uecp_frames(sent_payloads[0]), 0x04)
        self.assertIsNotNone(di_mec)
        self.assertEqual(di_mec[3] & 0x08, 0x08, "dynamic-PTY bit must be set: a category override exists")
