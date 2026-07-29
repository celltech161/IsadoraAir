from django.test import SimpleTestCase

from rbds.services import ascii_protocol, uecp
from rbds.services.content_fetch import ContentFetchCache
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

    def test_mec_rt_matches_spec_examples(self):
        # <0A><00><01><04><0B><52><44><53> -- current data set, service 1,
        # flush buffer (bits6-5=00) + toggle A/B (bit0=1) -> flags=0x01,
        # MEL=4 (1 flags byte + 3 text chars "RDS"), text "RDS".
        result = uecp.mec_rt("RDS", ab_flag=True, dsn=0x00, psn=0x01)
        # This module always uses buffer-config 0b01 ("add to buffer"),
        # not the spec example's 0b00 ("flush") -- a deliberate choice
        # documented in mec_rt's own docstring (this engine always sends
        # one current message, not an on-device rotating buffer), so
        # only the DSN/PSN/MEL/text/A-B-bit portions are checked against
        # the spec example here, not the buffer-config bits.
        self.assertEqual(result[0:3], bytes.fromhex("0A0001"))  # MEC DSN PSN
        self.assertEqual(result[3], 4)  # MEL = 1 + len("RDS")
        self.assertEqual(result[4] & 0x01, 1)  # A/B toggle bit
        self.assertEqual(result[5:], b"RDS")

    def test_mec_rt_ab_flag(self):
        rt_off = uecp.mec_rt("Hello", ab_flag=False)
        rt_on = uecp.mec_rt("Hello", ab_flag=True)
        self.assertEqual(rt_off[4] & 0x01, 0)
        self.assertEqual(rt_on[4] & 0x01, 1)
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
