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
    def test_mec_ps_pads_and_truncates(self):
        self.assertEqual(uecp.mec_ps("ABC"), bytes([0x02]) + b"ABC     ")
        self.assertEqual(uecp.mec_ps("ABCDEFGHIJ"), bytes([0x02]) + b"ABCDEFGH")

    def test_mec_ta_tp_bits(self):
        self.assertEqual(uecp.mec_ta_tp(ta=False, tp=False), bytes([0x03, 0x00]))
        self.assertEqual(uecp.mec_ta_tp(ta=True, tp=False), bytes([0x03, 0x01]))
        self.assertEqual(uecp.mec_ta_tp(ta=False, tp=True), bytes([0x03, 0x02]))
        self.assertEqual(uecp.mec_ta_tp(ta=True, tp=True), bytes([0x03, 0x03]))

    def test_mec_di_bits(self):
        self.assertEqual(
            uecp.mec_di(dynamic_pty=False, compressed=False, artificial_head=False, stereo=False),
            bytes([0x04, 0x00]),
        )
        self.assertEqual(
            uecp.mec_di(dynamic_pty=True, compressed=True, artificial_head=True, stereo=True),
            bytes([0x04, 0x0F]),
        )

    def test_mec_ms(self):
        self.assertEqual(uecp.mec_ms(music=True), bytes([0x05, 0x01]))
        self.assertEqual(uecp.mec_ms(music=False), bytes([0x05, 0x00]))

    def test_mec_pty(self):
        self.assertEqual(uecp.mec_pty(11), bytes([0x07, 0x0B]))

    def test_mec_rt_ab_flag(self):
        rt_off = uecp.mec_rt("Hello", ab_flag=False)
        rt_on = uecp.mec_rt("Hello", ab_flag=True)
        self.assertEqual(rt_off[2] & 0x01, 0)
        self.assertEqual(rt_on[2] & 0x01, 1)
        self.assertEqual(rt_off[3:], b"Hello")

    def test_mec_af_terminator_and_codes(self):
        # 87.6 MHz -> round((87.6-87.5)/0.1) = 1 ; 107.9 -> 204
        result = uecp.mec_af([87.6, 107.9])
        self.assertEqual(result, bytes([0x13, 0x00, 1, 204, 0x00]))

    def test_freq_to_af_code(self):
        self.assertEqual(uecp.freq_to_af_code(87.6), 1)
        self.assertEqual(uecp.freq_to_af_code(107.9), 204)


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
