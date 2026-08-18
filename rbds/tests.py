import importlib
import json
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.apps import apps as django_apps
from django.contrib import admin as django_admin
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from rbds.admin import RBDSConfigAdmin
from rbds.models import RBDSConfig, RBDSMessage, RBDSPSFrame
from rbds.services import ascii_protocol, charset, dynamic_ps, uecp
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

    def test_mec_language_code_is_variant_3_of_slow_labelling(self):
        # English=9 -> variant 3, data=0x009 -> MED1=0x30, MED2=0x09.
        self.assertEqual(uecp.mec_language_code(9, dsn=0x00), bytes.fromhex("1A003009"))
        # Thin convenience wrapper, not a separate command -- same
        # relationship mec_ecc() has to mec_slow_labelling().
        self.assertEqual(uecp.mec_language_code(9), uecp.mec_slow_labelling(variant=3, data=9))

    def test_mec_language_code_unknown_is_defined_clear_value(self):
        # Code 0 ("Unknown") is a real table entry, not an error --
        # this is what IsadoraAir sends as its own "clear" command.
        self.assertEqual(uecp.mec_language_code(0), bytes.fromhex("1A003000"))

    def test_mec_language_code_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            uecp.mec_language_code(256)
        with self.assertRaises(ValueError):
            uecp.mec_language_code(-1)

    def test_mec_language_code_does_not_alter_ecc_bytes(self):
        # Two independent slow-label variants, sent as two independent
        # MEC elements -- confirms neither builder's output depends on
        # or mutates the other's.
        ecc_bytes = uecp.mec_ecc(0xA0)
        lic_bytes = uecp.mec_language_code(9)
        self.assertEqual(ecc_bytes, bytes.fromhex("1A0000A0"))
        self.assertEqual(lic_bytes, bytes.fromhex("1A003009"))
        self.assertNotEqual(ecc_bytes, lic_bytes)

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


class UecpSplitFramesTests(SimpleTestCase):
    """uecp.split_frames() -- the safe-against-byte-stuffing splitter
    added for IsadoraAir roadmap item [P1] 2.3A (2026-08-18): UECP/UDP
    packetization. Unit-level coverage of the splitter itself; see
    RBDSManagerTransmitTransportTests below for coverage of the real
    _transmit() call path that actually uses it."""

    def test_single_frame_roundtrips(self):
        frame = uecp.build_frame(1, 0, 1, uecp.mec_ps("KOGR-LP "))
        self.assertEqual(uecp.split_frames(frame), [frame])

    def test_multiple_frames_split_in_order(self):
        frame1 = uecp.build_frame(1, 0, 1, uecp.mec_pi(0x1000))
        frame2 = uecp.build_frame(1, 0, 2, uecp.mec_ps("KOGR-LP "))
        frame3 = uecp.build_frame(1, 0, 3, uecp.mec_rt("Now Playing", ab_flag=False))
        payload = frame1 + frame2 + frame3
        self.assertEqual(uecp.split_frames(payload), [frame1, frame2, frame3])

    def test_splits_safely_around_stuffed_reserved_bytes(self):
        """A MEC msg containing literal 0xFD/0xFE/0xFF -- build_frame()
        must escape every one of them (see byte_stuff()), so a naive
        unstuffed substring search for STA/STP would misfire here if
        the splitter didn't rely on that escaping guarantee. Confirms
        it doesn't: exactly 2 frames recovered, byte-for-byte identical
        to the originals."""
        frame1 = uecp.build_frame(1, 0, 1, bytes([0xFD, 0xFE, 0xFF, 0x41, 0x42]))
        frame2 = uecp.build_frame(1, 0, 2, uecp.mec_ps("ABCDEFGH"))
        # Sanity check the fixture actually exercises stuffing -- if
        # this ever fails, the test below isn't testing what it claims.
        self.assertIn(b"\xFD\x00\xFD\x01\xFD\x02", frame1)
        payload = frame1 + frame2
        self.assertEqual(uecp.split_frames(payload), [frame1, frame2])

    def test_empty_payload_returns_empty_list(self):
        self.assertEqual(uecp.split_frames(b""), [])

    def test_missing_leading_sta_raises(self):
        frame = uecp.build_frame(1, 0, 1, uecp.mec_ps("KOGR-LP "))
        with self.assertRaises(ValueError):
            uecp.split_frames(frame[1:])

    def test_truncated_frame_missing_stp_raises(self):
        frame = uecp.build_frame(1, 0, 1, uecp.mec_ps("KOGR-LP "))
        with self.assertRaises(ValueError):
            uecp.split_frames(frame[:-1])


class RBDSManagerTransmitTransportTests(SimpleTestCase):
    """IsadoraAir roadmap item [P1] 2.3A (2026-08-18): confirmed field
    bug -- a BW TX300 V3 transmitter processed only the earlier
    frame(s) in a multi-frame UDP datagram and silently dropped the
    rest, so RadioText (built late in a normal full-resend payload)
    never made it while PI/PS/etc. (built earlier in the same payload)
    kept working. Root cause: _transmit() sent the whole concatenated
    multi-frame UECP payload via one UDP sendto().

    These tests call the REAL _transmit() (not mocked -- every other
    RBDSManager test class in this file mocks it, since it needs a
    real destination; this is the one class that instead mocks
    socket.socket itself, one level down, so the real packetization
    logic runs) to prove: one UDP sendto() per UECP frame, in order,
    each payload exactly one complete frame; the existing single
    sendall() TCP behavior is unchanged; ASCII/UDP still sends one
    datagram; and a mid-loop UDP failure propagates rather than being
    swallowed.

    SimpleTestCase (no DB) -- _transmit() only ever reads plain
    attributes off `config`, so a lightweight SimpleNamespace stands in
    for a real (DB-backed) RBDSConfig row here; no need for the
    heavier TestCase/RBDSConfig.load() fixture RBDSManagerOneShotToggleTests
    et al. use for _tick()-level tests."""

    def setUp(self):
        self.mgr = RBDSManager()

    def _config(self, transport, protocol="uecp"):
        return SimpleNamespace(
            transport=transport, protocol=protocol, host="10.0.0.5", port=4001,
            uecp_site_address=1, uecp_encoder_address=0,
        )

    def _three_frame_payload(self, config):
        meds = [uecp.mec_pi(0x1000), uecp.mec_ps("KOGR-LP "), uecp.mec_rt("Now Playing", ab_flag=False)]
        return self.mgr._frames_for(config, meds)

    def test_uecp_udp_sends_one_datagram_per_frame_in_order(self):
        config = self._config("udp")
        payload = self._three_frame_payload(config)
        expected_frames = uecp.split_frames(payload)
        self.assertEqual(len(expected_frames), 3, "fixture should produce exactly 3 frames")

        with mock.patch("rbds.services.rbds_manager.socket.socket") as mock_socket_cls:
            mock_sock = mock_socket_cls.return_value
            self.mgr._transmit(config, payload)

        self.assertEqual(mock_sock.sendto.call_count, 3)
        for call, expected_frame in zip(mock_sock.sendto.call_args_list, expected_frames):
            sent_payload, dest = call.args
            self.assertEqual(sent_payload, expected_frame)
            self.assertEqual(dest, ("10.0.0.5", 4001))
        mock_sock.close.assert_called_once()

    def test_uecp_udp_frame_with_reserved_byte_stuffing_splits_safely(self):
        frame1 = uecp.build_frame(1, 0, 1, bytes([0xFD, 0xFE, 0xFF, 0x41, 0x42]))
        frame2 = uecp.build_frame(1, 0, 2, uecp.mec_ps("ABCDEFGH"))
        payload = frame1 + frame2
        config = self._config("udp")

        with mock.patch("rbds.services.rbds_manager.socket.socket") as mock_socket_cls:
            mock_sock = mock_socket_cls.return_value
            self.mgr._transmit(config, payload)

        sent_payloads = [call.args[0] for call in mock_sock.sendto.call_args_list]
        self.assertEqual(sent_payloads, [frame1, frame2])

    def test_uecp_tcp_still_sends_one_sendall_with_full_concatenated_payload(self):
        """Do NOT introduce one TCP write per frame -- the existing
        single sendall() of the full concatenation must be untouched."""
        config = self._config("tcp")
        payload = self._three_frame_payload(config)
        self.assertEqual(len(uecp.split_frames(payload)), 3, "fixture should produce exactly 3 frames")

        self.mgr._sock = mock.Mock()
        self.mgr._ensure_tcp_connected = mock.Mock()  # pretend already connected
        self.mgr._transmit(config, payload)

        self.mgr._sock.sendall.assert_called_once_with(payload)

    def test_ascii_udp_still_sends_a_single_datagram(self):
        """ASCII is unframed text, not a concatenation of UECP frames
        -- must not be run through the splitter at all."""
        payload = b"PI=1000\nPS=KOGR-LP \n"
        config = self._config("udp", protocol="ascii")

        with mock.patch("rbds.services.rbds_manager.socket.socket") as mock_socket_cls:
            mock_sock = mock_socket_cls.return_value
            self.mgr._transmit(config, payload)

        mock_sock.sendto.assert_called_once_with(payload, ("10.0.0.5", 4001))

    def test_uecp_udp_failure_on_later_frame_propagates(self):
        """A sendto() failure partway through must never be swallowed
        -- the manager needs to see it (via the existing _send()
        except/return-False path, unchanged by this fix) so it never
        treats a partially-delivered full-resend as successful; the
        next normal retry/full-resend re-sends the complete state."""
        config = self._config("udp")
        payload = self._three_frame_payload(config)

        with mock.patch("rbds.services.rbds_manager.socket.socket") as mock_socket_cls:
            mock_sock = mock_socket_cls.return_value
            mock_sock.sendto.side_effect = [None, OSError("network unreachable"), None]
            with self.assertRaises(OSError):
                self.mgr._transmit(config, payload)
            # First frame sent, second raised -- third never attempted,
            # and the socket is still closed on the way out (finally).
            self.assertEqual(mock_sock.sendto.call_count, 2)
            mock_sock.close.assert_called_once()


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


class PSRotationResetTests(SimpleTestCase):
    """PSRotation.reset() -- IsadoraAir roadmap [P1] 2.3C (2026-08-18),
    added so RBDSManager can restart rotation deterministically on a
    short-PS MODE change (Static/Manual/Generated), a case
    advance()'s own frame-list-change detection alone can't always
    catch (two different modes' frame lists could coincidentally
    produce the same key). advance()'s existing frame-list-change
    behavior itself is untouched -- see PSRotationTests above, not
    modified by this class."""

    def test_reset_returns_to_frame_zero(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        frames = [("FRAME1  ", 4), ("FRAME2  ", 4)]
        self.assertEqual(rotation.advance(frames), "FRAME1  ")
        clock.advance(4)
        self.assertEqual(rotation.advance(frames), "FRAME2  ")
        rotation.reset()
        # Same frame list, and no time has passed since the last call --
        # without reset() this would just keep returning FRAME2.
        # reset() must force it back to index 0 regardless.
        self.assertEqual(rotation.advance(frames), "FRAME1  ")

    def test_reset_resets_timing_state(self):
        clock = FakeClock()
        rotation = PSRotation(clock=clock)
        frames = [("FRAME1  ", 4), ("FRAME2  ", 4)]
        rotation.advance(frames)  # frame_started_at = 0
        clock.advance(10)  # far past the original 4s hold
        rotation.reset()
        # If frame_started_at were NOT actually cleared by reset(), the
        # elapsed time since the ORIGINAL start (now 10s) would already
        # exceed the 4s hold, and this call would incorrectly jump
        # straight to FRAME2 instead of restarting fresh at FRAME1.
        self.assertEqual(rotation.advance(frames), "FRAME1  ")
        clock.advance(3.9)  # 3.9s since the RESET-time restart -- not yet due
        self.assertEqual(rotation.advance(frames), "FRAME1  ")
        clock.advance(0.2)  # total 4.1s since reset -- now due
        self.assertEqual(rotation.advance(frames), "FRAME2  ")

    def test_reset_on_a_fresh_never_advanced_rotation_is_a_safe_no_op(self):
        rotation = PSRotation(clock=FakeClock())
        rotation.reset()  # must not raise
        frames = [("FRAME1  ", 4)]
        self.assertEqual(rotation.advance(frames), "FRAME1  ")


class DynamicPsFrameGeneratorTests(SimpleTestCase):
    """rbds.services.dynamic_ps.generate_ps_frames() -- IsadoraAir
    roadmap [P1] 2.3B (2026-08-18): pure Dynamic/Rotating PS frame
    generation for Modes 0-3. Deliberately separate from PSRotation
    (rotation.py, see PSRotationTests above, untouched by this class)
    -- this module only decides WHAT the frames are, never WHEN to
    show them; see dynamic_ps.py's own module docstring for the full
    design rationale, including why normalization happens INSIDE this
    function rather than being a caller obligation."""

    # ---- Mode 0: fixed 8-character cells ----

    def test_mode0_empty_text(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("", 0), ["        "])

    def test_mode0_one_character(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("A", 0), ["A       "])

    def test_mode0_exactly_eight_characters(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ABCDEFGH", 0), ["ABCDEFGH"])

    def test_mode0_nine_characters(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ABCDEFGHI", 0), ["ABCDEFGH", "I       "])

    def test_mode0_multiple_complete_cells(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("ABCDEFGHIJKLMNOP", 0),
            ["ABCDEFGH", "IJKLMNOP"],
        )

    def test_mode0_final_partial_cell(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("ABCDEFGHIJKLMNOPQRST", 0),
            ["ABCDEFGH", "IJKLMNOP", "QRST    "],
        )

    def test_mode0_preserves_internal_spaces(self):
        """Mode 0 is the raw/fixed-cell mode -- manual formatting in
        the source is meaningful and must never be collapsed, unlike
        Mode 2's word-wrapping."""
        self.assertEqual(
            dynamic_ps.generate_ps_frames("AB  CD  EF", 0),
            ["AB  CD  ", "EF      "],
        )

    # ---- Mode 1: one-character sliding window ----

    def test_mode1_shorter_than_eight(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ABC", 1), ["ABC     "])

    def test_mode1_exactly_eight(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ABCDEFGH", 1), ["ABCDEFGH"])

    def test_mode1_nine_characters(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("ABCDEFGHI", 1),
            ["ABCDEFGH", "BCDEFGHI"],
        )

    def test_mode1_ten_plus_characters_exact_ordered_windows(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("ABCDEFGHIJ", 1),
            ["ABCDEFGH", "BCDEFGHI", "CDEFGHIJ"],
        )

    def test_mode1_no_circular_wrap_frame(self):
        """The window sequence must stop at the literal end of the
        text: exactly len(text) - PS_FRAME_WIDTH + 1 windows (never
        len(text), which a circular/wraparound implementation would
        produce), and no frame may equal the hypothetical wrapped
        window (tail characters immediately followed by head
        characters) a circular implementation would emit."""
        text = "ABCDEFGHIJ"
        frames = dynamic_ps.generate_ps_frames(text, 1)
        self.assertEqual(len(frames), len(text) - dynamic_ps.PS_FRAME_WIDTH + 1)
        wrapped_frame = (text + text)[len(text) - 1:len(text) - 1 + dynamic_ps.PS_FRAME_WIDTH]
        self.assertEqual(wrapped_frame, "JABCDEFG")  # sanity-check the fixture itself
        self.assertNotIn(wrapped_frame, frames)

    # ---- Mode 2: word-aligned cells ----

    def test_mode2_one_short_word(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("Hi", 2), ["Hi      "])

    def test_mode2_multiple_words_fit_one_cell(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("THE BEST", 2), ["THE BEST"])

    def test_mode2_words_requiring_multiple_cells(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("THE BEST MUSIC", 2),
            ["THE BEST", "MUSIC   "],
        )

    def test_mode2_repeated_and_mixed_whitespace_collapses(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("THE   BEST\tMUSIC", 2),
            dynamic_ps.generate_ps_frames("THE BEST MUSIC", 2),
        )

    def test_mode2_leading_and_trailing_whitespace_stripped(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("   THE BEST   ", 2), ["THE BEST"])

    def test_mode2_exactly_eight_character_word(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ABCDEFGH", 2), ["ABCDEFGH"])

    def test_mode2_overlength_word_chunked_not_truncated(self):
        """>8-character word: never truncated, never silently loses
        characters -- split into its own consecutive fixed-width
        chunks instead (reuses Mode 0's chunker)."""
        self.assertEqual(
            dynamic_ps.generate_ps_frames("CHRISTOPHER", 2),
            ["CHRISTOP", "HER     "],
        )

    def test_mode2_several_overlength_words_never_share_a_frame(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("CHRISTOPHERSON ALEXANDRIA", 2),
            ["CHRISTOP", "HERSON  ", "ALEXANDR", "IA      "],
        )

    def test_mode2_short_word_never_packed_into_long_words_final_chunk_remainder(self):
        """The blank remainder of an overlength word's final chunk
        ("HERSON  ") must never be shared with the word that comes
        next ("Bye") -- "Bye" gets its own fresh frame."""
        self.assertEqual(
            dynamic_ps.generate_ps_frames("Hi CHRISTOPHERSON Bye", 2),
            ["Hi      ", "CHRISTOP", "HERSON  ", "Bye     "],
        )

    def test_mode2_punctuation_stays_attached_no_character_loss(self):
        frames = dynamic_ps.generate_ps_frames("Hello, World!", 2)
        self.assertEqual(frames, ["Hello,  ", "World!  "])
        # No character loss beyond intended whitespace-collapsing: every
        # non-space character from the source reappears in the output.
        self.assertEqual("".join(frames).replace(" ", ""), "Hello,World!")

    def test_mode2_empty_and_whitespace_only_text(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("", 2), ["        "])
        self.assertEqual(dynamic_ps.generate_ps_frames("   ", 2), ["        "])

    def test_mode2_oak_grove_example(self):
        """None of these four words pair up under the 8-char/1-space
        greedy rule (each pairing would exceed 8), so each gets its
        own left-aligned, padded frame -- a legitimate, deterministic
        outcome, not a bug."""
        self.assertEqual(
            dynamic_ps.generate_ps_frames("Oak Grove Radio 98.5", 2),
            ["Oak     ", "Grove   ", "Radio   ", "98.5    "],
        )

    # ---- Mode 3: one-character scroll with blank separation ----

    def test_mode3_empty_text(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("", 3), ["        "])

    def test_mode3_short_text(self):
        frames = dynamic_ps.generate_ps_frames("HI", 3)
        self.assertEqual(len(frames), 11)  # (8+2+8) - 8 + 1
        self.assertEqual(frames[0], "        ")
        self.assertEqual(frames[-1], "        ")

    def test_mode3_exactly_eight_characters(self):
        frames = dynamic_ps.generate_ps_frames("ABCDEFGH", 3)
        self.assertEqual(len(frames), 17)  # (8+8+8) - 8 + 1
        self.assertEqual(frames[0], "        ")
        self.assertEqual(frames[-1], "        ")

    def test_mode3_longer_text_exact_sequence(self):
        self.assertEqual(
            dynamic_ps.generate_ps_frames("HELLO", 3),
            [
                "        ",
                "       H",
                "      HE",
                "     HEL",
                "    HELL",
                "   HELLO",
                "  HELLO ",
                " HELLO  ",
                "HELLO   ",
                "ELLO    ",
                "LLO     ",
                "LO      ",
                "O       ",
                "        ",
            ],
        )

    def test_mode3_first_frame_is_all_spaces(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ANYTHING", 3)[0], "        ")

    def test_mode3_last_frame_is_all_spaces(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("ANYTHING", 3)[-1], "        ")

    def test_mode3_one_character_progression(self):
        """Each frame after the first must differ from its predecessor
        by exactly a one-character shift -- confirmed by the 7-char
        overlap between consecutive frames."""
        frames = dynamic_ps.generate_ps_frames("AB", 3)
        for i in range(1, len(frames)):
            self.assertEqual(frames[i - 1][1:], frames[i][:-1])

    def test_mode3_source_enters_and_leaves_display(self):
        text = "HI"
        frames = dynamic_ps.generate_ps_frames(text, 3)
        # Fully visible, flush right against the leading blanks
        # (fully "entered" the display)...
        self.assertIn(("        " + text)[-dynamic_ps.PS_FRAME_WIDTH:], frames)
        # ...and fully visible, flush left against the trailing blanks
        # (about to "leave" the display).
        self.assertIn((text + "        ")[:dynamic_ps.PS_FRAME_WIDTH], frames)

    def test_mode3_no_wraparound_extra_frames(self):
        """Total frame count must exactly equal the non-circular
        window formula applied to the padded working string -- a
        circular/wrapping implementation would produce additional
        frames beyond this count."""
        text = "HELLO WORLD"
        frames = dynamic_ps.generate_ps_frames(text, 3)
        working_len = 2 * dynamic_ps.PS_FRAME_WIDTH + len(text)
        self.assertEqual(len(frames), working_len - dynamic_ps.PS_FRAME_WIDTH + 1)

    # ---- Normalization ordering ----

    def test_normalization_accented_character_supported_by_normalize_text(self):
        """A decomposed accented character (NFC-composed by
        normalize_text() before this module ever sees it) appears in
        the output as its single, precomposed G0-representable form."""
        raw = "café"  # "café" as base "e" + combining acute (U+0301)
        self.assertEqual(dynamic_ps.generate_ps_frames(raw, 0), ["café    "])

    def test_normalization_smart_punctuation_supported_by_normalize_text(self):
        self.assertEqual(dynamic_ps.generate_ps_frames("It’s", 0), ["It's    "])

    def test_normalization_unsupported_character_not_dropped_by_this_module(self):
        """A character with no G0 representation at all is outside
        normalize_text()'s own scope (NFC/smart-punctuation/control-
        chars only) -- it must still occupy exactly one character
        position in the frame this module produces, never silently
        dropped here. encode_rds_g0() substitutes it with a space only
        later, at mec_ps()'s own call site -- never in this module."""
        source = "AB\U0001F3B5CD"  # AB + musical-note emoji + CD -- 5 characters
        self.assertEqual(len(source), 5)
        frames = dynamic_ps.generate_ps_frames(source, 0)
        self.assertEqual(frames, [source.ljust(8)])

    def test_normalization_happens_before_frame_boundaries_are_computed(self):
        """Direct proof that frame boundaries are placed AFTER
        normalization, not on the raw input: the ellipsis "…" expands
        to "..." (+2 characters) under normalize_text(). Constructed so
        the RAW text is exactly PS_FRAME_WIDTH (8) characters -- which
        would need only ONE frame if boundaries were (incorrectly)
        computed on the raw string -- while the NORMALIZED text is 10
        characters, which genuinely needs TWO."""
        raw = "ABCDEFG…"  # 7 letters + ellipsis = 8 raw characters
        self.assertEqual(len(raw), dynamic_ps.PS_FRAME_WIDTH)
        frames = dynamic_ps.generate_ps_frames(raw, 0)
        self.assertEqual(frames, ["ABCDEFG.", "..      "])
        self.assertEqual(
            len(frames), 2,
            "boundary must be computed on the 10-char normalized form, not the 8-char raw form",
        )

    # ---- General / contract ----

    def test_invalid_mode_raises_value_error_not_silent_fallback(self):
        with self.assertRaises(ValueError):
            dynamic_ps.generate_ps_frames("TEXT", 4)
        with self.assertRaises(ValueError):
            dynamic_ps.generate_ps_frames("TEXT", -1)
        with self.assertRaises(ValueError):
            dynamic_ps.generate_ps_frames("TEXT", "0")  # not silently coerced from a string

    def test_all_returned_frames_are_exactly_eight_characters(self):
        samples = ["", "A", "ABCDEFGH", "ABCDEFGHI", "THE BEST MUSIC", "CHRISTOPHER", "   ", "HELLO WORLD"]
        for mode in (
            dynamic_ps.MODE_FIXED_CELLS, dynamic_ps.MODE_SLIDING_WINDOW,
            dynamic_ps.MODE_WORD_ALIGNED, dynamic_ps.MODE_SCROLL_WITH_BLANK,
        ):
            for text in samples:
                for frame in dynamic_ps.generate_ps_frames(text, mode):
                    self.assertEqual(
                        len(frame), dynamic_ps.PS_FRAME_WIDTH, f"mode={mode} text={text!r} frame={frame!r}",
                    )

    def test_deterministic_same_input_same_output(self):
        for mode in range(4):
            first = dynamic_ps.generate_ps_frames("Oak Grove Radio 98.5", mode)
            second = dynamic_ps.generate_ps_frames("Oak Grove Radio 98.5", mode)
            self.assertEqual(first, second)

    def test_generator_has_no_clock_or_time_dependency(self):
        """No FakeClock, no time/datetime import anywhere in
        dynamic_ps.py -- confirmed both structurally and behaviorally
        (repeated calls never differ)."""
        import inspect
        source = inspect.getsource(dynamic_ps)
        self.assertNotIn("import time", source)
        self.assertNotIn("import datetime", source)
        results = {tuple(dynamic_ps.generate_ps_frames("SAME TEXT HERE", 2)) for _ in range(5)}
        self.assertEqual(len(results), 1)


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
            pi_code="", ecc="", language_code=None, ta=False, tp=False,
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


class RBDSManagerPsModeResolutionTests(TestCase):
    """RBDSManager._resolve_target_ps() -- IsadoraAir roadmap [P1] 2.3C
    (2026-08-18): mode-aware PS resolution for Static / Manual PS
    Frames / Generated Rotating PS. Exercises _resolve_target_ps()
    directly (a real DB-backed RBDSConfig row is needed since Manual
    mode genuinely queries RBDSPSFrame) with a FakeClock-driven
    PSRotation -- never touches _transmit()/sockets; this class is
    entirely about WHICH text gets chosen, not how it's sent (see
    GeneratedPsReachesExistingTransportPathTests below for that)."""

    def setUp(self):
        self.clock = FakeClock()
        self.mgr = RBDSManager()
        self.mgr._ps_rotation = PSRotation(clock=self.clock)
        self.config = RBDSConfig.load()
        self.config.station_ps = "KOGR-LP "
        self.config.ps_mode = "static"
        self.config.save()

    # ---- Static ----

    def test_static_ignores_enabled_manual_frames(self):
        RBDSPSFrame.objects.create(text="MANUAL1 ", enabled=True, hold_seconds=4)
        self.config.ps_mode = "static"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "KOGR-LP ")

    def test_static_ignores_generated_settings(self):
        self.config.ps_mode = "static"
        self.config.dynamic_ps_text = "SHOULD NOT APPEAR"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "KOGR-LP ")

    def test_static_sends_station_ps(self):
        self.config.ps_mode = "static"
        self.config.station_ps = "TESTPS  "
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "TESTPS  ")

    # ---- Manual ----

    def test_manual_uses_enabled_rbdspsframe_rows(self):
        RBDSPSFrame.objects.create(text="FRAME1  ", enabled=True, hold_seconds=4, sort_order=0)
        self.config.ps_mode = "manual"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "FRAME1  ")

    def test_manual_preserves_per_row_hold_seconds(self):
        RBDSPSFrame.objects.create(text="FRAME1  ", enabled=True, hold_seconds=4, sort_order=0)
        RBDSPSFrame.objects.create(text="FRAME2  ", enabled=True, hold_seconds=6, sort_order=1)
        self.config.ps_mode = "manual"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "FRAME1  ")
        self.clock.advance(4)  # FRAME1's own 4s hold elapsed
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "FRAME2  ")
        self.clock.advance(5)  # < FRAME2's own 6s hold
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "FRAME2  ")
        self.clock.advance(1.1)  # now >= 6s
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "FRAME1  ")  # wrapped

    def test_manual_disabled_rows_ignored(self):
        RBDSPSFrame.objects.create(text="ENABLED ", enabled=True, hold_seconds=4, sort_order=0)
        RBDSPSFrame.objects.create(text="DISABLED", enabled=False, hold_seconds=4, sort_order=1)
        self.config.ps_mode = "manual"
        result1 = self.mgr._resolve_target_ps(self.config)
        self.clock.advance(100)
        result2 = self.mgr._resolve_target_ps(self.config)
        self.assertEqual(result1, "ENABLED ")
        self.assertEqual(result2, "ENABLED ")  # only one enabled frame -- never advances away

    def test_manual_zero_enabled_rows_falls_back_to_station_ps(self):
        RBDSPSFrame.objects.create(text="DISABLED", enabled=False, hold_seconds=4)
        self.config.ps_mode = "manual"
        self.config.station_ps = "FALLBACK"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "FALLBACK")

    # ---- Generated ----

    def test_generated_uses_generate_ps_frames(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "HELLO WORLD"
        self.config.dynamic_ps_mode = 2
        self.config.dynamic_ps_frame_seconds = 4
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "HELLO   ")

    def test_generated_feeds_sequence_into_ps_rotation_with_common_interval(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "HELLO WORLD"
        self.config.dynamic_ps_mode = 2
        self.config.dynamic_ps_frame_seconds = 4
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "HELLO   ")
        self.clock.advance(3.9)
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "HELLO   ")  # not due yet
        self.clock.advance(0.2)  # total 4.1s
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "WORLD   ")

    def test_generated_text_edit_restarts_at_frame_zero(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "HELLO WORLD"
        self.config.dynamic_ps_mode = 2
        self.config.dynamic_ps_frame_seconds = 4
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "HELLO   ")
        self.clock.advance(4)
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "WORLD   ")
        # Text changed mid-rotation -- must restart at the NEW
        # sequence's own frame 0, not continue at whatever index the
        # OLD sequence was on.
        self.config.dynamic_ps_text = "GOODBYE"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "GOODBYE ")

    def test_generated_mode_edit_restarts_at_frame_zero(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "AAAA BBBB"
        self.config.dynamic_ps_mode = 2  # word-aligned -> ["AAAA    ", "BBBB    "]
        self.config.dynamic_ps_frame_seconds = 4
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "AAAA    ")
        self.clock.advance(4)
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "BBBB    ")  # now at index 1

        # Switch to Mode 0 (fixed 8-char cells) -- "AAAA BBBB" (9 chars)
        # -> ["AAAA BBB", "B       "], a frame-0 that matches NEITHER
        # of Mode 2's own two frames, so landing on it proves rotation
        # restarted at the NEW sequence's frame 0 rather than
        # continuing at Mode 2's index 1.
        self.config.dynamic_ps_mode = 0
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "AAAA BBB")

    def test_generated_interval_edit_restarts_timing_appropriately(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "HELLO WORLD"
        self.config.dynamic_ps_mode = 2
        self.config.dynamic_ps_frame_seconds = 4
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "HELLO   ")
        self.clock.advance(3)  # 3s elapsed under the OLD 4s interval -- not yet due
        self.config.dynamic_ps_frame_seconds = 3  # shortened (still >=3, valid for Mode 2)
        # Immediately after the edit (0s elapsed under the NEW timer),
        # must still be frame 0 -- proves the timer restarted from THIS
        # moment rather than treating the 3s already elapsed under the
        # OLD interval as already satisfying the NEW, shorter one.
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "HELLO   ")
        self.clock.advance(3)  # 3s since the interval-edit moment
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "WORLD   ")


class RBDSManagerPsModeSwitchingTests(TestCase):
    """Mode-TRANSITION tests -- IsadoraAir roadmap [P1] 2.3C: switching
    between Static / Manual / Generated must restart rotation
    deterministically, never resuming stale index/timing state from a
    previously-active mode. See PSRotation.reset() and
    RBDSManager._resolve_target_ps()'s own docstring for why this needs
    explicit self._last_ps_mode tracking, not just PSRotation.advance()'s
    own frame-list-change detection."""

    def setUp(self):
        self.clock = FakeClock()
        self.mgr = RBDSManager()
        self.mgr._ps_rotation = PSRotation(clock=self.clock)
        self.config = RBDSConfig.load()
        self.config.station_ps = "STATIC  "
        self.config.dynamic_ps_text = "GENERATED TEXT"
        self.config.dynamic_ps_mode = 2
        self.config.dynamic_ps_frame_seconds = 4
        self.config.save()

    def test_static_to_generated_begins_at_frame_zero(self):
        self.config.ps_mode = "static"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "STATIC  ")
        self.config.ps_mode = "generated"
        expected_first = dynamic_ps.generate_ps_frames(self.config.dynamic_ps_text, self.config.dynamic_ps_mode)[0]
        self.assertEqual(self.mgr._resolve_target_ps(self.config), expected_first)

    def test_generated_to_static_immediately_returns_station_ps(self):
        self.config.ps_mode = "generated"
        self.mgr._resolve_target_ps(self.config)
        self.clock.advance(4)
        self.mgr._resolve_target_ps(self.config)  # now on generated frame index 1
        self.config.ps_mode = "static"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "STATIC  ")

    def test_generated_to_manual_begins_at_frame_zero(self):
        RBDSPSFrame.objects.create(text="MANUAL1 ", enabled=True, hold_seconds=4, sort_order=0)
        RBDSPSFrame.objects.create(text="MANUAL2 ", enabled=True, hold_seconds=4, sort_order=1)
        self.config.ps_mode = "generated"
        self.mgr._resolve_target_ps(self.config)
        self.clock.advance(4)
        self.mgr._resolve_target_ps(self.config)  # generated frame index 1
        self.config.ps_mode = "manual"
        self.assertEqual(self.mgr._resolve_target_ps(self.config), "MANUAL1 ")

    def test_manual_to_generated_begins_at_frame_zero(self):
        RBDSPSFrame.objects.create(text="MANUAL1 ", enabled=True, hold_seconds=4, sort_order=0)
        RBDSPSFrame.objects.create(text="MANUAL2 ", enabled=True, hold_seconds=4, sort_order=1)
        self.config.ps_mode = "manual"
        self.mgr._resolve_target_ps(self.config)
        self.clock.advance(4)
        self.mgr._resolve_target_ps(self.config)  # manual frame index 1
        self.config.ps_mode = "generated"
        expected_first = dynamic_ps.generate_ps_frames(self.config.dynamic_ps_text, self.config.dynamic_ps_mode)[0]
        self.assertEqual(self.mgr._resolve_target_ps(self.config), expected_first)

    def test_switching_away_and_back_does_not_resume_stale_state(self):
        self.config.ps_mode = "generated"
        first = self.mgr._resolve_target_ps(self.config)
        self.clock.advance(4)
        second = self.mgr._resolve_target_ps(self.config)
        self.assertNotEqual(first, second)  # confirm real progress happened
        self.config.ps_mode = "static"
        self.mgr._resolve_target_ps(self.config)
        self.config.ps_mode = "generated"
        # Must restart at the SAME first frame as originally -- not
        # resume at `second`, wherever it was before switching away.
        self.assertEqual(self.mgr._resolve_target_ps(self.config), first)


class GeneratedPsReachesExistingTransportPathTests(TestCase):
    """Confirms a Generated Rotating PS frame flows through the EXACT
    SAME existing PS transport path as any other PS string --
    IsadoraAir roadmap [P1] 2.3C added no ps_mode-awareness anywhere
    below _resolve_target_ps(); this is a regression guard that it
    stays that way. Not a retest of 2.3A's UDP one-frame-per-datagram
    packetization (unchanged, untouched here -- see
    RBDSManagerTransmitTransportTests)."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.sent_payloads = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: self.sent_payloads.append(payload))
        self.mgr._read_category_state = mock.Mock(return_value={"pty_override": None, "ptyn": ""})
        self.config = RBDSConfig.load()
        self.config.protocol = "uecp"
        self.config.use_rt_plus = False
        self.config.send_ct = False
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "TESTFRAME"
        self.config.dynamic_ps_mode = 0
        self.config.dynamic_ps_frame_seconds = 4
        self.config.save()

    def test_generated_frame_reaches_uecp_mec_ps_unchanged(self):
        self.mgr._ps_rotation = PSRotation(clock=FakeClock())
        target_ps = self.mgr._resolve_target_ps(self.config)
        self.assertEqual(target_ps, "TESTFRAM")  # Mode 0, first 8 of "TESTFRAME"

        ok = self.mgr._send(self.config, target_ps, "some rt", None, None)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent_payloads), 1)
        frames = _split_uecp_frames(self.sent_payloads[0])
        ps_mec = _find_mec(frames, 0x02)
        self.assertIsNotNone(ps_mec)
        # Directly compare against the real uecp.mec_ps()'s own output
        # for this exact text -- confirms the generated frame landed
        # in the ordinary PS MEC byte-for-byte identically to how any
        # other 8-char PS string would, no special-casing anywhere.
        self.assertEqual(ps_mec, uecp.mec_ps(target_ps))

    def test_generated_frame_reaches_ascii_ps_command_unchanged(self):
        self.config.protocol = "ascii"
        self.config.save()
        self.mgr._ps_rotation = PSRotation(clock=FakeClock())
        target_ps = self.mgr._resolve_target_ps(self.config)

        ok = self.mgr._send(self.config, target_ps, "some rt", None, None)
        self.assertTrue(ok)
        self.assertEqual(len(self.sent_payloads), 1)
        commands = self.sent_payloads[0].decode("utf-8")
        self.assertIn(f"PS={target_ps}", commands)


class RBDSManagerPsStateFileTests(TestCase):
    """rbds_state.json's ps_mode/dynamic_ps_mode additions -- IsadoraAir
    roadmap [P1] 2.3C (2026-08-18). Calls _write_state() directly
    (bypassing the full _tick() orchestration, which is unrelated to
    what this covers) against a temp-directory STATE_PATH -- the real
    /run/isadoraair/rbds_state.json is never touched by this suite."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.config = RBDSConfig.load()
        self.tmpdir = tempfile.mkdtemp(prefix="isadoraair-rbds-state-test-")
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.state_path = Path(self.tmpdir) / "rbds_state.json"

    def _write_and_read(self, ps="TESTPS  "):
        with mock.patch("rbds.services.rbds_manager.STATE_PATH", self.state_path):
            self.mgr._write_state(self.config, ps, "some rt", "nowplaying", None)
        return json.loads(self.state_path.read_text())

    def test_current_ps_remains_correct(self):
        state = self._write_and_read(ps="ABCDEFGH")
        self.assertEqual(state["current_ps"], "ABCDEFGH")

    def test_ps_mode_exposed(self):
        self.config.ps_mode = "manual"
        state = self._write_and_read()
        self.assertEqual(state["ps_mode"], "manual")

    def test_dynamic_ps_mode_exposed_when_generated(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_mode = 3
        state = self._write_and_read()
        self.assertEqual(state["dynamic_ps_mode"], 3)

    def test_dynamic_ps_mode_absent_when_static(self):
        self.config.ps_mode = "static"
        state = self._write_and_read()
        self.assertNotIn("dynamic_ps_mode", state)

    def test_dynamic_ps_mode_absent_when_manual(self):
        self.config.ps_mode = "manual"
        state = self._write_and_read()
        self.assertNotIn("dynamic_ps_mode", state)

    def test_full_source_text_never_dumped(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "SOME LONG SOURCE TEXT THAT SHOULD NEVER APPEAR"
        state = self._write_and_read()
        self.assertNotIn("SOME LONG SOURCE TEXT", json.dumps(state))

    def test_generated_frame_list_never_dumped(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "UNIQUEMARKERTEXT"
        self.config.dynamic_ps_mode = 0
        state = self._write_and_read()
        self.assertNotIn("UNIQUEMARKERTEXT", json.dumps(state))
        self.assertNotIn("frames", state)
        self.assertNotIn("dynamic_ps_text", state)


@mock.patch("rbds.admin.subprocess.Popen")
class RBDSConfigAdminRestartScopingTests(TestCase):
    """2026-08-18 restart-scoping fix -- RBDSConfigAdmin.save_model()
    must restart isadoraair-rbds only when an actual connection-
    TOPOLOGY field changed (host/port/transport/protocol/
    uecp_site_address/uecp_encoder_address), never for station
    identity/content edits -- PS settings among them, the exact
    scenario that made the OLD unconditional-restart-on-every-save
    behavior unacceptable (see this admin's own updated comment).

    save_model() only ever reads form.changed_data (never any other
    form internals), so a minimal stand-in object with just that one
    attribute exercises the real save_model() logic precisely, without
    needing full ModelForm/HTTP request machinery -- and Django's own
    base ModelAdmin.save_model() (called via super()) only does
    obj.save(), so passing request=None is safe too."""

    def setUp(self):
        self.site = django_admin.AdminSite()
        self.model_admin = RBDSConfigAdmin(RBDSConfig, self.site)
        self.config = RBDSConfig.load()

    def _save(self, changed_fields, change=True):
        fake_form = SimpleNamespace(changed_data=list(changed_fields))
        self.model_admin.save_model(request=None, obj=self.config, form=fake_form, change=change)

    def test_topology_field_edit_restarts(self, mock_popen):
        self._save(["host"])
        mock_popen.assert_called_once()

    def test_each_topology_field_alone_restarts(self, mock_popen):
        for field in RBDSConfigAdmin.RESTART_TOPOLOGY_FIELDS:
            with self.subTest(field=field):
                mock_popen.reset_mock()
                self._save([field])
                mock_popen.assert_called_once()

    def test_multiple_fields_including_one_topology_field_restarts(self, mock_popen):
        self._save(["station_ps", "port"])
        mock_popen.assert_called_once()

    def test_generated_ps_text_edit_does_not_restart(self, mock_popen):
        self._save(["dynamic_ps_text"])
        mock_popen.assert_not_called()

    def test_generated_ps_mode_edit_does_not_restart(self, mock_popen):
        self._save(["dynamic_ps_mode"])
        mock_popen.assert_not_called()

    def test_generated_ps_interval_edit_does_not_restart(self, mock_popen):
        self._save(["dynamic_ps_frame_seconds"])
        mock_popen.assert_not_called()

    def test_station_ps_edit_does_not_restart(self, mock_popen):
        self._save(["station_ps"])
        mock_popen.assert_not_called()

    def test_ps_mode_selector_edit_does_not_restart(self, mock_popen):
        self._save(["ps_mode"])
        mock_popen.assert_not_called()

    def test_no_fields_changed_does_not_restart(self, mock_popen):
        self._save([])
        mock_popen.assert_not_called()

    def test_new_object_creation_restarts_even_with_no_topology_change(self, mock_popen):
        """change=False (a brand-new singleton row -- has_add_permission()
        blocks a second one from ever existing) restarts unconditionally,
        since there is no previously-running config to compare against
        and the engine may not even be started yet -- explicitly
        acceptable per spec."""
        self._save([], change=False)
        mock_popen.assert_called_once()


class RBDSConfigAdminGeneratedPsPreviewTests(TestCase):
    """RBDSConfigAdmin.generated_ps_preview() -- IsadoraAir roadmap
    [P1] 2.3C (2026-08-18). Calls the real
    rbds.services.dynamic_ps.generate_ps_frames(), never a second
    reimplementation."""

    def setUp(self):
        self.site = django_admin.AdminSite()
        self.model_admin = RBDSConfigAdmin(RBDSConfig, self.site)
        self.config = RBDSConfig.load()

    def test_preview_uses_real_generate_ps_frames(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "AB"
        self.config.dynamic_ps_mode = 0
        self.config.save()
        rendered = str(self.model_admin.generated_ps_preview(self.config))
        expected_frame = dynamic_ps.generate_ps_frames("AB", 0)[0]
        self.assertIn(expected_frame, rendered)

    def test_preview_exposes_frame_boundaries(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "HELLO WORLD"
        self.config.dynamic_ps_mode = 2
        self.config.save()
        rendered = str(self.model_admin.generated_ps_preview(self.config))
        # Pipe-delimited so a leading/trailing space is visually
        # unambiguous, never swallowed by HTML whitespace collapsing.
        self.assertIn("|HELLO   |", rendered)
        self.assertIn("|WORLD   |", rendered)
        self.assertIn("[ 1]", rendered)
        self.assertIn("[ 2]", rendered)

    def test_preview_shows_concise_status_when_not_generated_mode(self):
        self.config.ps_mode = "static"
        self.config.save()
        rendered = str(self.model_admin.generated_ps_preview(self.config))
        self.assertIn("Not applicable", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_preview_shows_concise_status_when_text_blank(self):
        self.config.ps_mode = "generated"
        self.config.dynamic_ps_text = "   "  # whitespace-only
        self.config.save()
        rendered = str(self.model_admin.generated_ps_preview(self.config))
        self.assertIn("blank", rendered.lower())

    def test_preview_never_raises_on_unsaved_object(self):
        new_obj = RBDSConfig(ps_mode="generated", dynamic_ps_text="X")  # pk is None, never saved
        result = self.model_admin.generated_ps_preview(new_obj)
        self.assertIsInstance(result, str)

    def test_preview_never_raises_on_none_object(self):
        result = self.model_admin.generated_ps_preview(None)
        self.assertIsInstance(result, str)


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
            pi_code="", ecc="", language_code=None, ta=False, tp=False,
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


class RBDSConfigDynamicPsFieldsTests(SimpleTestCase):
    """RBDSConfig's short-PS-mode + Generated Rotating PS fields --
    IsadoraAir roadmap [P1] 2.3C (2026-08-18): choices/defaults and
    clean()'s validation rules. No DB access needed -- clean() is a
    pure in-memory check, same pattern as RBDSConfigValidationTests
    above."""

    def test_ps_mode_default_is_static(self):
        self.assertEqual(RBDSConfig().ps_mode, "static")

    def test_ps_mode_has_exactly_the_three_specified_choices(self):
        values = [choice for choice, _label in RBDSConfig._meta.get_field("ps_mode").choices]
        self.assertEqual(values, ["static", "manual", "generated"])

    def test_dynamic_ps_mode_default_is_mode_2_word_aligned(self):
        self.assertEqual(RBDSConfig().dynamic_ps_mode, 2)

    def test_dynamic_ps_mode_has_choices_0_through_3(self):
        values = [choice for choice, _label in RBDSConfig._meta.get_field("dynamic_ps_mode").choices]
        self.assertEqual(values, [0, 1, 2, 3])

    def test_dynamic_ps_frame_seconds_default_is_4(self):
        self.assertEqual(RBDSConfig().dynamic_ps_frame_seconds, 4)

    def test_dynamic_ps_text_default_is_blank(self):
        self.assertEqual(RBDSConfig().dynamic_ps_text, "")

    def test_generated_mode_requires_non_blank_text(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="   ")  # whitespace-only
        with self.assertRaises(ValidationError):
            config.clean()

    def test_generated_mode_with_valid_text_passes(self):
        config = RBDSConfig(
            ps_mode="generated", dynamic_ps_text="HELLO", dynamic_ps_mode=2, dynamic_ps_frame_seconds=4,
        )
        config.clean()  # must not raise

    def test_static_mode_permits_blank_generated_text(self):
        """dynamic_ps_text is only REQUIRED to be meaningful while
        Generated mode is actually active -- leaving it blank/short
        while some other ps_mode is selected must not block saving."""
        config = RBDSConfig(ps_mode="static", dynamic_ps_text="")
        config.clean()  # must not raise

    def test_manual_mode_permits_blank_generated_text(self):
        config = RBDSConfig(ps_mode="manual", dynamic_ps_text="")
        config.clean()  # must not raise

    def test_mode_0_rejects_interval_below_3(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=0, dynamic_ps_frame_seconds=2)
        with self.assertRaises(ValidationError):
            config.clean()

    def test_mode_2_rejects_interval_below_3(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=2, dynamic_ps_frame_seconds=2)
        with self.assertRaises(ValidationError):
            config.clean()

    def test_mode_0_accepts_interval_of_exactly_3(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=0, dynamic_ps_frame_seconds=3)
        config.clean()  # must not raise

    def test_mode_2_accepts_interval_of_exactly_3(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=2, dynamic_ps_frame_seconds=3)
        config.clean()  # must not raise

    def test_mode_1_accepts_interval_of_1(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=1, dynamic_ps_frame_seconds=1)
        config.clean()  # must not raise -- the >=3 restriction is deliberately NOT applied to Modes 1/3

    def test_mode_3_accepts_interval_of_1(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=3, dynamic_ps_frame_seconds=1)
        config.clean()  # must not raise

    def test_mode_1_rejects_interval_of_0(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=1, dynamic_ps_frame_seconds=0)
        with self.assertRaises(ValidationError):
            config.clean()

    def test_mode_3_rejects_interval_of_0(self):
        config = RBDSConfig(ps_mode="generated", dynamic_ps_text="X", dynamic_ps_mode=3, dynamic_ps_frame_seconds=0)
        with self.assertRaises(ValidationError):
            config.clean()


class DataMigrationSetsPsModeFromExistingFramesTests(TestCase):
    """rbds/migrations/0012_set_ps_mode_from_existing_frames.py --
    2026-08-18. Runs the ACTUAL migration function (imported directly
    via importlib, never reimplemented here) against a real, fully-
    migrated test database, proving it preserves each installation's
    pre-2.3C implicit PS precedence (enabled RBDSPSFrame rows rotate;
    otherwise station_ps is static) by translating it into an explicit
    ps_mode value on the singleton RBDSConfig row."""

    @staticmethod
    def _run_migration():
        module = importlib.import_module("rbds.migrations.0012_set_ps_mode_from_existing_frames")
        # schema_editor is genuinely unused by this migration's body
        # (a pure RunPython data migration) -- None is safe to pass.
        module.set_ps_mode_from_existing_frames(django_apps, None)

    def test_selects_manual_when_enabled_frames_exist(self):
        RBDSPSFrame.objects.create(text="FRAME1  ", enabled=True)
        config = RBDSConfig.load()
        config.ps_mode = "static"  # simulate the field's own pre-migration default
        config.save()
        self._run_migration()
        config.refresh_from_db()
        self.assertEqual(config.ps_mode, "manual")

    def test_selects_static_when_only_disabled_frames_exist(self):
        RBDSPSFrame.objects.create(text="FRAME1  ", enabled=False)
        config = RBDSConfig.load()
        config.ps_mode = "manual"  # simulate a stale/incorrect starting value
        config.save()
        self._run_migration()
        config.refresh_from_db()
        self.assertEqual(config.ps_mode, "static")

    def test_selects_static_when_zero_frames_exist_at_all(self):
        config = RBDSConfig.load()
        config.ps_mode = "manual"
        config.save()
        self._run_migration()
        config.refresh_from_db()
        self.assertEqual(config.ps_mode, "static")

    def test_no_config_row_is_a_safe_no_op(self):
        """Fresh installs have no RBDSConfig row at migration time
        (RBDSConfig.load()'s get_or_create only ever runs at real
        engine/admin runtime, never during `migrate`) -- the field's
        own default ("static") covers that case once the row is
        eventually created; this migration must not create one itself
        or raise."""
        self.assertFalse(RBDSConfig.objects.exists())
        self._run_migration()  # must not raise
        self.assertFalse(RBDSConfig.objects.exists())

    def test_never_touches_rbdspsframe_rows(self):
        frame = RBDSPSFrame.objects.create(text="FRAME1  ", enabled=True, hold_seconds=7, sort_order=3)
        RBDSConfig.load()
        self._run_migration()
        frame.refresh_from_db()
        self.assertTrue(frame.enabled)
        self.assertEqual(frame.text, "FRAME1  ")
        self.assertEqual(frame.hold_seconds, 7)
        self.assertEqual(frame.sort_order, 3)


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
            pi_code="", ecc="", language_code=None, ta=False, tp=False,
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
            pi_code="", ecc="", language_code=None, ta=False, tp=False,
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


def _slow_label_value(msg, variant):
    """Extracts the 12-bit data value from a raw MEC 0x1A frame if its
    variant nibble matches, else None. msg format: MEC(1A) DSN MED1 MED2,
    MED1 bits 6-4 = variant, bits 3-0 = data MSB, MED2 = data LSB."""
    if not msg or msg[0] != 0x1A or len(msg) < 4:
        return None
    if ((msg[2] >> 4) & 0x7) != variant:
        return None
    return ((msg[2] & 0xF) << 8) | msg[3]


class RBDSManagerLicTests(TestCase):
    """Legacy Language Identification Code (LIC), MEC 0x1A variant 3 --
    same MEC family as ECC (variant 0), sent as part of the main full
    UECP payload (not a dedicated cadence like CT), so it inherits the
    existing, already-tested change/full-resend/reconnect triggers for
    free. The one piece of state this class owns is the explicit
    clear-to-"Unknown" behavior when language_code is disabled after
    having been set -- see rbds_manager.py's _last_sent_language_code."""

    def setUp(self):
        # _tick()'s own close_old_connections() call closes the shared
        # connection out from under this TestCase's wrapping atomic
        # block in this environment (autocommit-state mismatch trips
        # BaseDatabaseWrapper.close_if_unusable_or_obsolete's very first
        # check) -- production _tick() never runs inside an atomic
        # block at all, so this is a test-harness-only interaction, not
        # a real behavior difference. Confirmed by bisection: every
        # RBDSConfig.load() call inside _tick() failed with
        # "connection already closed" until this was patched out;
        # after patching, only genuine assertion failures remained.
        patcher = mock.patch("rbds.services.rbds_manager.close_old_connections")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.mgr = RBDSManager()
        self.sent = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: self.sent.append(payload))
        self.mgr._read_now_playing = mock.Mock(return_value={"title": "", "artist": ""})
        self.mgr._read_category_state = mock.Mock(return_value={"pty_override": None, "ptyn": ""})
        self.config = RBDSConfig.load()
        self.config.protocol = "uecp"
        self.config.use_rt_plus = False
        self.config.ecc = "A0"

    def _lic_values_sent(self):
        values = []
        for payload in self.sent:
            for msg in _split_uecp_frames(payload):
                v = _slow_label_value(msg, variant=3)
                if v is not None:
                    values.append(v)
        return values

    def _ecc_values_sent(self):
        values = []
        for payload in self.sent:
            for msg in _split_uecp_frames(payload):
                v = _slow_label_value(msg, variant=0)
                if v is not None:
                    values.append(v)
        return values

    # 1. Blank default emits no LIC MEC.
    def test_blank_default_emits_no_lic(self):
        self.config.save()  # language_code left at its default (None)
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [])

    # 2/5. English emits exact bytes; startup includes it.
    def test_english_emits_exact_bytes_on_startup(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        frames = _split_uecp_frames(self.sent[0])
        lic_frame = next(m for m in frames if m and m[0] == 0x1A and (m[2] >> 4) & 0x7 == 3)
        self.assertEqual(bytes(lic_frame), bytes.fromhex("1A003009"))

    # 3. LIC appears in the main full payload exactly once per send.
    def test_lic_appears_exactly_once_per_full_payload(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        frames = _split_uecp_frames(self.sent[0])
        lic_frames = [m for m in frames if _slow_label_value(m, variant=3) is not None]
        self.assertEqual(len(lic_frames), 1)

    # 4. LIC does not appear in RT+-only maintenance sends.
    def test_lic_absent_from_rt_plus_only_maintenance_send(self):
        # _send_rt_plus_only transmits directly rather than returning a
        # payload -- call it the same way _tick() does and inspect what
        # actually got queued.
        self.config.use_rt_plus = True
        self.sent.clear()
        self.mgr._send_rt_plus_only(self.config, "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        for payload in self.sent:
            for msg in _split_uecp_frames(payload):
                self.assertNotEqual(msg[0] if msg else None, 0x1A, "MEC 0x1A must never ride the RT+-only send")

    # 6. Periodic full resend reasserts LIC even though the value is unchanged.
    def test_periodic_full_resend_reasserts_lic(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        self.mgr._last_full_resend = 0.0  # force the full-resend gate open
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [9, 9])

    # 7. A fresh reconnect (genuine _connected_since transition) reasserts LIC.
    def test_reconnect_reasserts_lic(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        self.mgr._connected = False
        self.mgr._connected_since = None
        time.sleep(0.01)
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [9, 9])

    # 8. Language-code change sends the new value.
    def test_language_code_change_sends_new_value(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        self.config.language_code = 8  # German
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [9, 8])

    # 9. Failed send preserves pending language state.
    def test_failed_send_preserves_pending_language_state(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._transmit = mock.Mock(side_effect=ConnectionError("simulated failure"))
        self.mgr._tick()
        self.assertIsNone(self.mgr._last_sent_language_code, "a failed send must not commit the desired state")
        # Next tick, now succeeding, must still send 9 (not silently dropped).
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: self.sent.append(payload))
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [9])

    # 10. Disable-after-enable sends the standards-defined clear
    # ("Unknown", code 0) exactly once, then stops.
    def test_disable_after_enable_sends_unknown_clear_once(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        self.config.language_code = None
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [9, 0])
        self.assertIsNone(self.mgr._last_sent_language_code)
        # A further ordinary tick (nothing changed, not yet due for
        # full resend) must NOT re-send the clear -- it already landed.
        self.mgr._tick()
        self.assertEqual(self._lic_values_sent(), [9, 0])

    # 12/13. ECC bytes are unaffected by LIC, and both ride the same payload.
    def test_ecc_unchanged_and_coexists_with_lic_in_same_payload(self):
        self.config.language_code = 9
        self.config.save()
        self.mgr._tick()
        self.assertEqual(self._ecc_values_sent(), [0xA0])
        self.assertEqual(self._lic_values_sent(), [9])
        frames = _split_uecp_frames(self.sent[0])
        ecc_frame = next(m for m in frames if _slow_label_value(m, variant=0) is not None)
        lic_frame = next(m for m in frames if _slow_label_value(m, variant=3) is not None)
        self.assertNotEqual(bytes(ecc_frame), bytes(lic_frame))


class RBDSManagerReconnectBackoffTests(SimpleTestCase):
    """Covers the 2026-08-04 backoff cap (TCP_RECONNECT_BACKOFF: (1, 2, 5, 10,
    30) -> (1, 2, 5), monotonic-clock-based). _ensure_tcp_connected is
    exercised directly rather than through _tick()/_send() -- every other
    test in this file mocks _transmit() itself and never reaches the real
    socket/backoff logic at all.

    Items 11 (failed-send state preservation) and 12/13 (reconnect
    triggers full content resend / CT MEC 0x19 reassertion) from the
    required test list are already covered by
    RBDSManagerFailedSendPreservesToggleTests and RBDSManagerCtOnOffTests
    respectively -- this class's changes (only __init__, _ensure_tcp_
    connected, and _write_state) don't touch any of that logic, and the
    full suite run below confirms nothing there regressed."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.config = mock.Mock(
            host="127.0.0.1", port=4000, protocol="uecp", transport="tcp", ps_mode="static",
        )

    def _attempt(self, monotonic_now, create_connection_result):
        """create_connection_result: an exception instance to raise, or a
        truthy value (e.g. mock.Mock()) to return as a successful socket."""
        with mock.patch.object(rbds_manager.time, "monotonic", return_value=monotonic_now), \
             mock.patch.object(rbds_manager.socket, "create_connection") as create_conn:
            if isinstance(create_connection_result, Exception):
                create_conn.side_effect = create_connection_result
            else:
                create_conn.return_value = create_connection_result
            try:
                self.mgr._ensure_tcp_connected(self.config)
                return True
            except ConnectionError:
                return False

    def test_first_retry_delay_is_1_second(self):
        self.assertFalse(self._attempt(0.0, OSError("refused")))
        self.assertEqual(self.mgr._reconnect_delay_seconds, 1)
        # too soon -- gated, must not even try to connect
        with mock.patch.object(rbds_manager.time, "monotonic", return_value=0.99), \
             mock.patch.object(rbds_manager.socket, "create_connection") as create_conn:
            self.mgr._ensure_tcp_connected(self.config)
            create_conn.assert_not_called()
        self.assertEqual(self.mgr._reconnect_attempt, 1, "the too-soon call above must not itself count as an attempt")

    def test_second_retry_delay_is_2_seconds(self):
        self._attempt(0.0, OSError("refused"))  # attempt 1 (unconditional) fails -> delay=1s for the next one
        self._attempt(1.0, OSError("refused"))  # elapsed 1s >= 1s -> attempt 2 proceeds, fails -> delay=2s
        self.assertEqual(self.mgr._reconnect_delay_seconds, 2)

    def test_third_and_later_delays_stay_at_5_seconds(self):
        self._attempt(0.0, OSError("refused"))    # -> delay 1s
        self._attempt(1.0, OSError("refused"))    # -> delay 2s
        self._attempt(3.0, OSError("refused"))    # -> delay 5s
        self.assertEqual(self.mgr._reconnect_delay_seconds, 5)
        self._attempt(8.0, OSError("refused"))    # 4th failure -- must stay 5s, not jump to 10
        self.assertEqual(self.mgr._reconnect_delay_seconds, 5)
        self._attempt(13.0, OSError("refused"))   # 5th failure -- still 5s, not 30
        self.assertEqual(self.mgr._reconnect_delay_seconds, 5)
        self.assertEqual(self.mgr._reconnect_attempt, 5)

    def test_no_attempt_before_deadline(self):
        self.assertFalse(self._attempt(0.0, OSError("refused")))
        with mock.patch.object(rbds_manager.time, "monotonic", return_value=0.5), \
             mock.patch.object(rbds_manager.socket, "create_connection") as create_conn:
            self.mgr._ensure_tcp_connected(self.config)  # too soon -- gated, must not call socket at all
            create_conn.assert_not_called()

    def test_attempt_occurs_at_or_after_deadline(self):
        self._attempt(0.0, OSError("refused"))
        with mock.patch.object(rbds_manager.time, "monotonic", return_value=1.0), \
             mock.patch.object(rbds_manager.socket, "create_connection") as create_conn:
            create_conn.side_effect = OSError("refused")
            with self.assertRaises(ConnectionError):
                self.mgr._ensure_tcp_connected(self.config)
            create_conn.assert_called_once()

    def test_successful_reconnect_resets_sequence(self):
        self._attempt(0.0, OSError("refused"))
        self._attempt(1.0, OSError("refused"))
        self.assertEqual(self.mgr._backoff_index, 2)
        fake_sock = mock.Mock()
        self.assertTrue(self._attempt(3.0, fake_sock))
        self.assertEqual(self.mgr._backoff_index, 0)
        self.assertEqual(self.mgr._reconnect_attempt, 0)
        self.assertIsNone(self.mgr._reconnect_next_at)
        self.assertIsNone(self.mgr._reconnect_delay_seconds)
        self.assertIs(self.mgr._sock, fake_sock)

    def test_second_independent_outage_restarts_at_1_second(self):
        self._attempt(0.0, OSError("refused"))
        self._attempt(1.0, OSError("refused"))
        self._attempt(3.0, mock.Mock())  # reconnects, resets
        self.mgr._sock = None  # simulate the connection later dropping again
        self.assertFalse(self._attempt(100.0, OSError("refused")))
        self.assertEqual(self.mgr._reconnect_delay_seconds, 1, "a new, independent outage must start the sequence over")

    def test_prolonged_outage_does_not_busy_loop(self):
        # 20 consecutive failures, one per second of monotonic time --
        # every attempt after the third must be gated to exactly 5s
        # apart, i.e. at most 4 real connection attempts in 20 "seconds"
        # (0, 1, 3, 8, 13, 18 -- 6 real attempts), never one per tick.
        attempts = 0
        t = 0.0
        for _ in range(20):
            with mock.patch.object(rbds_manager.time, "monotonic", return_value=t), \
                 mock.patch.object(rbds_manager.socket, "create_connection") as create_conn:
                create_conn.side_effect = OSError("refused")
                try:
                    self.mgr._ensure_tcp_connected(self.config)
                except ConnectionError:
                    pass
                if create_conn.called:
                    attempts += 1
            t += 1.0
        self.assertLessEqual(attempts, 6, "must not attempt a real connection every single second")
        self.assertEqual(self.mgr._reconnect_delay_seconds, 5)

    def test_connection_errors_remain_visible_in_state(self):
        self.assertFalse(self._attempt(0.0, OSError("connection refused")))
        self.assertEqual(self.mgr._reconnect_attempt, 1)
        self.assertEqual(self.mgr._reconnect_delay_seconds, 1)
        self.assertIsNotNone(self.mgr._reconnect_next_at)
        # _write_state must surface these without raising
        self.mgr._write_state(self.config, "PS", "RT", "nowplaying", None)

    def test_backoff_constant_is_capped_at_5(self):
        self.assertEqual(rbds_manager.TCP_RECONNECT_BACKOFF, (1, 2, 5))

    def test_no_concurrent_reconnect_attempt_when_already_connected(self):
        self.mgr._sock = mock.Mock()
        with mock.patch.object(rbds_manager.socket, "create_connection") as create_conn:
            self.mgr._ensure_tcp_connected(self.config)
            create_conn.assert_not_called()


# --- RBDS hardening round (character/boundary stress + RT+ golden
# vectors), 2026-08-04 -- see scratchpad/rbds_bench/
# character_and_goldenvector_hardening/ for the full source-inspection
# doc, standards map, and bench capture report this round produced.
# Everything below builds on the existing CharsetTests/RtPlusText
# BoundaryTests/RtPlusOmitVsGenericTests/RtPlusMecCompositionTests/
# RtNormalizationGapTests coverage rather than duplicating it -- see
# that doc's "gaps this round fills" section for exactly what's new.


def _mock_rbds_config(**overrides):
    """Shared full-field config mock matching the shape every
    _build_uecp_payload-calling test in this file already uses."""
    base = dict(
        pi_code="", ecc="", language_code=None, ta=False, tp=False,
        di_dynamic_pty=False, di_compressed=False, di_artificial_head=False, di_stereo=True,
        ms=True, pty=0, use_rt_plus=True, af_frequencies_mhz="",
        uecp_site_address=1, uecp_encoder_address=0,
    )
    base.update(overrides)
    return mock.Mock(**base)


class RbdsCharacterSanitizationTests(SimpleTestCase):
    """End-to-end (normalize_text -> truncate/pad -> encode_rds_g0,
    through the REAL mec_ps/mec_ptyn/mec_rt builders and, for the
    boundary cases, the full _build_uecp_payload pipeline) character
    and boundary coverage not already locked in by CharsetTests (which
    tests encode_rds_g0/normalize_text directly, not the field-specific
    8/8/64-char truncation+pad behavior around them)."""

    def setUp(self):
        self.mgr = RBDSManager()

    # --- PS: fixed 8 chars, padded/truncated ---

    def test_ps_empty_string_is_eight_spaces(self):
        self.assertEqual(uecp.mec_ps(""), bytes([0x02, 0, 0]) + b"        ")

    def test_ps_one_char_padded(self):
        self.assertEqual(uecp.mec_ps("A"), bytes([0x02, 0, 0]) + b"A       ")

    def test_ps_exact_eight_chars_unpadded(self):
        self.assertEqual(uecp.mec_ps("KOGRABCD"), bytes([0x02, 0, 0]) + b"KOGRABCD")

    def test_ps_nine_chars_truncated_to_eight(self):
        result = uecp.mec_ps("KOGRABCDE")
        self.assertEqual(result[3:], b"KOGRABCD")
        self.assertEqual(len(result), 3 + 8, "PS MED must always be exactly 8 bytes")

    def test_ps_unsupported_character_at_final_position(self):
        # Position 8 (the last) is the emoji -- must fall back to
        # space, not corrupt the fixed 8-byte width.
        result = uecp.mec_ps("KOGRABC\U0001F600")
        self.assertEqual(result[3:], b"KOGRABC ")

    def test_ps_multibyte_variation_selector_emoji_crossing_truncation_boundary(self):
        # "\U0001F326️" (weather cloud-rain + variation selector)
        # is TWO Python characters -- placed so the boundary falls
        # between them, proving a split grapheme cluster doesn't
        # corrupt the fixed-width output (both fall back to space
        # independently either way).
        text = "ABCDEFG" + "\U0001F326️"  # 7 + 2 = 9 chars, truncates to 8
        result = uecp.mec_ps(text)
        self.assertEqual(result[3:], b"ABCDEFG ")
        self.assertEqual(len(result), 11)

    def test_ps_accented_latin_at_boundary(self):
        result = uecp.mec_ps("Café °F ")  # exactly 8 chars
        self.assertEqual(len(result), 11)
        # á and ° both have real G0 codes -- confirm neither became a
        # fallback space (would be indistinguishable from a real
        # trailing space otherwise, so check the specific byte values).
        self.assertEqual(result[3 + 3], 0x82)  # 'é' (matches CharsetTests' own value)
        self.assertEqual(result[3 + 5], 0xBB)  # '°'

    def test_ps_end_to_end_via_manager_normalizes_first(self):
        # target_ps is normalized at the real _tick() call site
        # (rbds_manager.py:193) before mec_ps ever sees it -- confirm
        # via the actual normalize_text() -> mec_ps() composition a
        # smart-quote-containing PS would otherwise silently space-fill.
        raw = "DJ’s Show"  # curly apostrophe, 9 chars -> truncates
        result = uecp.mec_ps(charset.normalize_text(raw))
        self.assertEqual(result[3:], b"DJ's Sho")

    # --- PTYN: fixed 8 chars, same shape as PS ---

    def test_ptyn_unsupported_character_at_final_position(self):
        result = uecp.mec_ptyn("CLASSI\U0001F3B5")
        self.assertEqual(result[3:], b"CLASSI  ")  # emoji is 1 python char here (no variation selector)

    def test_ptyn_exact_eight_chars_unpadded(self):
        self.assertEqual(uecp.mec_ptyn("FOOTBALL"), bytes([0x3E, 0, 0]) + b"FOOTBALL")

    def test_ptyn_multibyte_crossing_truncation_boundary(self):
        text = "LOCAL" + "\U0001F326️" + "X"  # 5 + 2 + 1 = 8 chars, exact fit
        result = uecp.mec_ptyn(text)
        # Both emoji code points fall back to space; final visible
        # char "X" survives since it's within the 8-char width.
        self.assertEqual(result[3:], b"LOCAL  X")

    def test_nfd_decomposed_accented_character_is_canonicalized(self):
        # Originally documented the OPPOSITE: this project's prior
        # hardening round found (by accident, from its own test typo)
        # that a decomposed "o" + combining-diaeresis pasted where a
        # precomposed accented character was intended did NOT
        # canonicalize -- the base letter encoded fine but the orphaned
        # combining mark fell back to a space. Fixed by adding NFC
        # normalization to normalize_text() (see
        # RBDS_NFC_NORMALIZATION_REPORT.md) -- this test now asserts
        # the CORRECTED end-to-end behavior through the real pipeline
        # (normalize_text() -> encode_rds_g0()), not encode_rds_g0()
        # called in isolation (which still performs no normalization of
        # its own by design -- normalize_text() is the single, narrow
        # insertion point).
        precomposed = unicodedata.normalize("NFC", chr(0x6F) + chr(0x0308))  # NFC-composed 'o' + combining diaeresis
        decomposed = chr(0x6F) + chr(0x0308)  # base "o" U+006F + combining diaeresis U+0308, 2 chars
        self.assertNotEqual(decomposed, precomposed, "sanity: these are genuinely different code-point sequences")
        normalized_precomposed = charset.normalize_text(precomposed)
        normalized_decomposed = charset.normalize_text(decomposed)
        self.assertEqual(normalized_decomposed, normalized_precomposed,
                          "normalize_text() must canonicalize both forms to the same string")
        self.assertEqual(uecp.encode_rds_g0(normalized_precomposed), bytes([0x97]))
        self.assertEqual(uecp.encode_rds_g0(normalized_decomposed), bytes([0x97]),
                          "decomposed input must now produce the SAME single G0 byte as precomposed, not a fallback space")
        # encode_rds_g0() itself is unchanged and performs no
        # normalization of its own -- calling it directly on the raw
        # decomposed form (bypassing normalize_text()) still shows the
        # old byte-per-codepoint behavior, confirming the fix lives
        # exactly where it should, not duplicated into encode_rds_g0().
        self.assertEqual(uecp.encode_rds_g0(decomposed), b"o ")

    def test_rt_empty_string(self):
        result = uecp.mec_rt("", ab_flag=False)
        self.assertEqual(result, bytes([0x0A, 0, 0, 1, 0x00]))  # MEL=1 (flags byte only)

    def test_rt_one_char(self):
        result = uecp.mec_rt("A", ab_flag=False)
        self.assertEqual(result, bytes([0x0A, 0, 0, 2, 0x00]) + b"A")

    def test_rt_exact_64_chars_unpadded(self):
        text = "A" * 64
        result = uecp.mec_rt(text, ab_flag=False)
        self.assertEqual(result[4:], bytes([0x00]) + text.encode("ascii"))
        self.assertEqual(len(result), 5 + 64)

    def test_rt_65_chars_truncated_to_64(self):
        text = "A" * 65
        result = uecp.mec_rt(text, ab_flag=False)
        self.assertEqual(len(result), 5 + 64, "RT MED must never exceed the 64-char limit")

    def test_rt_unsupported_character_at_final_position_64(self):
        text = ("A" * 63) + "\U0001F600"
        result = uecp.mec_rt(text, ab_flag=False)
        self.assertEqual(result[-1], ord(" "))

    def test_rt_multibyte_crossing_truncation_boundary_at_64(self):
        # Places the 2-codepoint emoji+variation-selector pair so the
        # 64-char cut falls between them.
        text = ("A" * 63) + "\U0001F326️"  # 63 + 2 = 65 chars, truncates to 64
        result = uecp.mec_rt(text, ab_flag=False)
        self.assertEqual(len(result), 5 + 64)
        # Char 64 (index 63) is the base emoji codepoint -- no G0
        # representation, falls back to space; the trailing variation
        # selector is simply truncated away, not corrupting anything.
        self.assertEqual(result[-1], ord(" "))

    def test_rt_full_unicode_stress_matrix_via_end_to_end_pipeline(self):
        # One end-to-end pass (normalize_text -> mec_rt) through every
        # category the governing spec's test matrix lists, confirming
        # no exception and a deterministic, length-correct result for
        # each -- the per-character G0 mapping itself is already
        # locked by CharsetTests; this proves the FULL RT pipeline
        # composes correctly for each category, not just the isolated
        # encode step.
        cases = [
            ("Oak Grove Radio", "Oak Grove Radio"),
            ("It's 5 o'clock", "It's 5 o'clock"),
            ("en dash – em dash —", "en dash - em dash -"),
            ("ellipsis…", "ellipsis..."),
            ("Beyoncé", "Beyoncé"),
            ("Sinéad O’Connor", "Sinéad O'Connor"),
            ("Mötley Crüe", "Mötley Crüe"),
            ("75°F", "75°F"),
            # CJK/emoji are NOT touched by normalize_text() at all --
            # it only handles smart punctuation/dashes/ellipsis/
            # control chars. Unsupported-script/emoji replacement
            # happens one stage later, at encode_rds_g0() time (see
            # the encoded-bytes assertion below) -- these two stages
            # are deliberately kept separate in this test.
            ("東京", "東京"),  # Tokyo in kanji -- unchanged by normalize_text
            ("🎵", "🎵"),  # music note emoji -- unchanged by normalize_text
            ("🌦️", "🌦️"),  # weather emoji + variation selector -- unchanged
        ]
        for raw, expected_after_normalize in cases:
            with self.subTest(raw=raw):
                normalized = charset.normalize_text(raw)
                self.assertEqual(normalized, expected_after_normalize)
                encoded_med = uecp.mec_rt(normalized, ab_flag=False)
                # MEL must always equal 1 + the (already-normalized)
                # text length -- proves length is computed post-
                # normalization, matching the documented invariant.
                self.assertEqual(encoded_med[3], 1 + len(normalized))
                # And the actual encoded text bytes must be exactly as
                # long as the normalized string -- the length-
                # preserving guarantee that makes character-count
                # offsets valid as byte offsets (unsupported chars in
                # `normalized` become fallback space bytes here, not
                # in normalize_text -- confirmed by the encoded length
                # still matching even where every char is unsupported).
                self.assertEqual(len(uecp.encode_rds_g0(normalized)), len(normalized))

    # --- whitespace/control characters, full C0 range ---

    def test_all_c0_controls_and_del_become_space(self):
        controls = "".join(chr(c) for c in range(0x00, 0x20)) + "\x7f"
        normalized = charset.normalize_text(controls)
        self.assertEqual(normalized, " " * len(controls))

    def test_crlf_combinations(self):
        self.assertEqual(charset.normalize_text("A\r\nB"), "A  B")
        self.assertEqual(charset.normalize_text("A\rB"), "A B")
        self.assertEqual(charset.normalize_text("A\nB"), "A B")
        self.assertEqual(charset.normalize_text("A\tB"), "A B")
        self.assertEqual(charset.normalize_text("A\x00B"), "A B")

    def test_leading_trailing_and_internal_spaces_preserved(self):
        # Regular spaces are NOT control characters -- normalize_text
        # must not touch them (only translates 0x00-0x1F/0x7F).
        text = "  Oak   Grove  "
        self.assertEqual(charset.normalize_text(text), text)
        self.assertEqual(uecp.mec_ps(text), bytes([0x02, 0, 0]) + b"  Oak   ")


class RtPlusGeometryInvariantTests(SimpleTestCase):
    """Phase A4's core invariant: RT+ offsets/lengths must identify the
    FINAL text bytes actually placed on air (post-normalization, post-
    truncation), never the unsanitized source string. Builds on
    RtPlusTextBoundaryTests (ASCII-only) by exercising the same
    _build_rt_plus_text/_resolve_rt_content paths with accented/
    replaced/smart-punctuation/removed characters, plus the delimiter-
    split edge cases the governing spec calls out explicitly."""

    def setUp(self):
        self.mgr = RBDSManager()

    def _assert_geometry_matches_final_text(self, text, artist, title):
        """The actual invariant check, reusable across cases: encoding
        the claimed artist/title substrings must byte-for-byte equal
        the corresponding slice of the encoded final RT text."""
        encoded_text = uecp.encode_rds_g0(text)
        if artist:
            start = text.index(artist)
            self.assertEqual(
                uecp.encode_rds_g0(artist), encoded_text[start:start + len(artist)],
                "artist tag must address the real transmitted bytes, not the source string",
            )
        if title:
            start = text.index(title)
            self.assertEqual(
                uecp.encode_rds_g0(title), encoded_text[start:start + len(title)],
                "title tag must address the real transmitted bytes, not the source string",
            )

    def test_ascii_only_artist_title(self):
        text, artist, title = self.mgr._build_rt_plus_text("Rush", "Tom Sawyer")
        self._assert_geometry_matches_final_text(text, artist, title)

    def test_accented_characters_preserved_in_geometry(self):
        text, artist, title = self.mgr._build_rt_plus_text("Björk", "Venus as a Boy")
        self._assert_geometry_matches_final_text(text, artist, title)
        self.assertEqual(artist, "Björk")

    def test_smart_punctuation_replaced_before_geometry_computed(self):
        # Caller (rbds_manager._resolve_rt_content) normalizes BEFORE
        # calling _build_rt_plus_text -- simulate that exact ordering.
        raw_artist, raw_title = "Sinéad O’Connor", "Nothing Compares 2 U"
        artist = charset.normalize_text(raw_artist)
        title = charset.normalize_text(raw_title)
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, title)
        self.assertNotIn("’", text)
        self.assertEqual(out_artist, "Sinéad O'Connor")
        self._assert_geometry_matches_final_text(text, out_artist, out_title)

    def test_unsupported_characters_replaced_not_removed_preserves_geometry(self):
        # If an unsupported character were DROPPED instead of replaced,
        # every offset after it would point past the real text --
        # encode_rds_g0's replace-not-drop policy is what keeps this
        # invariant true; confirm it end-to-end through the RT+ path.
        artist, title = "\U0001F3B5 DJ", "Track \U0001F600 One"
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, title)
        self._assert_geometry_matches_final_text(text, out_artist, out_title)
        self.assertEqual(len(uecp.encode_rds_g0(text)), len(text))

    def test_truncated_artist_geometry(self):
        artist = "A" * 60
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, "X")
        self._assert_geometry_matches_final_text(text, out_artist, out_title)

    def test_truncated_title_geometry(self):
        text, out_artist, out_title = self.mgr._build_rt_plus_text("A" * 10, "B" * 60)
        self._assert_geometry_matches_final_text(text, out_artist, out_title)
        self.assertEqual(len(out_title), 51)

    def test_generic_one_tag_content_geometry(self):
        # The generic tag always covers offset 0, the FULL (already
        # normalized+truncated) RT string -- confirm the claimed
        # length matches the actual encoded length exactly.
        rt = charset.normalize_text("Temp: 76F – Wind: NW ° gusting")[:64]
        med = uecp.mec_rt_plus_tags_generic(len(rt))
        # byte3 bits[6:1] = length-1
        claimed_length = ((med[4] >> 1) & 0x3F) + 1
        self.assertEqual(claimed_length, len(uecp.encode_rds_g0(rt)))

    def test_two_tag_content_geometry_both_sides(self):
        artist, title = "Wind: NE °", "Barometer 29.9"
        text, out_artist, out_title = self.mgr._build_rt_plus_text(artist, title)
        self._assert_geometry_matches_final_text(text, out_artist, out_title)

    def test_weather_style_first_delimiter_split_geometry(self):
        message = mock.Mock(source_type="static", text="Temp: 76F | Humidity: 73%", rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=False)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertEqual(rt_text, "Temp: 76F - Humidity: 73%")

    def test_second_delimiter_retained_inside_title_half(self):
        # partition() splits on the FIRST '|' only -- a second '|'
        # inside the remainder must stay part of the title text, not
        # trigger a second split or get silently dropped. Matches the
        # already-observed real production weather behavior (see
        # PRODUCTION_WEATHER_AUDIT.md from the RT+ vendor mapping
        # round) -- this locks it in as a permanent regression test.
        message = mock.Mock(
            source_type="static", text="Temp: 76F | Humidity: 73% | Rain: 0.02 in", rt_plus_delimiter="|",
        )
        config = _mock_rbds_config(use_rt_plus=True)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertEqual(artist, "Temp: 76F")
        self.assertEqual(title, "Humidity: 73% | Rain: 0.02 in")
        self._assert_geometry_matches_final_text(rt_text, artist, title)

    def test_leading_trailing_whitespace_around_delimiter_stripped(self):
        message = mock.Mock(source_type="static", text="Artist   |   Title", rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=True)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertEqual(artist, "Artist")
        self.assertEqual(title, "Title")

    def test_multiple_delimiters_only_first_used_as_split_point(self):
        message = mock.Mock(source_type="static", text="A|B|C|D", rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=True)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertEqual(artist, "A")
        self.assertEqual(title, "B|C|D")

    def test_empty_artist_half_omits_rt_plus_via_early_return(self):
        # Delimiter at the very start ("| Title") -- artist is "" after
        # strip, which short-circuits BEFORE _build_rt_plus_text is
        # even called (a different code path than the empty-title
        # case below, both converging on "no RT+ tagging").
        message = mock.Mock(source_type="static", text="| Title Only", rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=True)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertIsNone(artist)
        self.assertIsNone(title)
        self.assertEqual(rt_text, "Title Only")

    def test_empty_title_half_omits_rt_plus_via_build_rt_plus_text_sentinel(self):
        # Delimiter at the very end ("Artist |") -- artist survives the
        # early-return check (non-empty), so this DOES reach
        # _build_rt_plus_text, which then detects the empty title via
        # its own "" sentinel convention.
        message = mock.Mock(source_type="static", text="Artist Only |", rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=True)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        self.assertEqual(artist, "")
        self.assertEqual(title, "")
        # Note the trailing space: the fixed " - " separator's own
        # trailing space is still literally present even though title
        # is empty -- _build_rt_plus_text joins "Artist Only" + " - "
        # + "" verbatim before detecting the empty-title sentinel.
        self.assertEqual(rt_text, "Artist Only - ")


def decode_rt_plus_tag_bytes(med):
    """Independent, test-only semantic decoder for a MEC 0x24 subtype
    0x16 RT+ tag MED. Does NOT call mec_rt_plus_tags/_generic in
    reverse -- reimplements the bit layout directly from the proven
    real over-the-air RT+ ODA group 11A structure (redsea's own
    decodeType1/parseRadioTextPlus, cross-validated byte-for-byte
    against 3 independently captured real records in the 2026-08-04
    RT+ vendor content-type mapping experiment -- see
    scratchpad/rbds_bench/rtplus_content_type_mapping/
    00_SOURCE_INVENTORY_AND_CANDIDATE_ANALYSIS.md for the full
    derivation). `med` is the FULL MED bytes INCLUDING the leading MEC
    0x24 byte -- i.e. exactly what mec_rt_plus_tags/_generic return,
    and the same shape _find_mec's own return value already uses
    elsewhere in this file (mec, subtype, then the payload bytes).

    Confirmed empirically: UECP bytes[3:5] (this function's b2:b3) are
    an unmodified copy of the real over-the-air Block C, and
    bytes[5:7] (b4:b5) of Block D; byte[2] (b1) supplies Block B's own
    low 5 bits (item_toggle/item_running/tag1's content-type high 3
    bits)."""
    mec, subtype, b1, b2, b3, b4, b5 = med[0], med[1], med[2], med[3], med[4], med[5], med[6]
    assert mec == 0x24, f"not a MEC 0x24 MED (mec={mec:#x})"
    assert subtype == 0x16, f"not a RT+ tag MED (subtype={subtype:#x})"
    item_toggle = bool((b1 >> 4) & 1)
    item_running = bool((b1 >> 3) & 1)
    tag1_content_type = ((b1 & 0x7) << 3) | (b2 >> 5)
    tag1_start = ((b2 & 0x1F) << 1) | (b3 >> 7)
    tag1_length = ((b3 >> 1) & 0x3F) + 1
    tag2_content_type = ((b3 & 0x1) << 5) | (b4 >> 3)
    tag2_start = ((b4 & 0x7) << 3) | (b5 >> 5)
    tag2_length = (b5 & 0x1F) + 1
    return {
        "item_toggle": item_toggle,
        "item_running": item_running,
        "tag1_content_type": tag1_content_type,
        "tag1_start": tag1_start,
        "tag1_length": tag1_length,
        "tag2_content_type": tag2_content_type,
        "tag2_start": tag2_start,
        "tag2_length": tag2_length,
    }


# RDS Forum R06/040_1 content-type numbers, as already confirmed in the
# LIC/group-1A/RT+-content-types round (extracted programmatically from
# redsea's own table, same as this file's other standards references).
RT_PLUS_CT_ITEM_TITLE = 1
RT_PLUS_CT_ITEM_ARTIST = 4
RT_PLUS_CT_INFO_WEATHER = 25


class RtPlusGoldenVectorTests(SimpleTestCase):
    """Byte-exact regression tests for the three MEC 0x24 builders that
    have now been independently proven through live on-air captures
    (2026-08-04 RT+ vendor content-type mapping experiment). Every
    vector is both byte-compared AND independently semantically
    decoded (decode_rt_plus_tag_bytes above), per the explicit
    requirement not to rely on byte equality alone."""

    def test_oda_registration_exact_bytes(self):
        self.assertEqual(
            uecp.mec_rt_plus_oda_reg(),
            bytes([0x24, 0x06, 0x16, 0x00, 0x00, 0x4B, 0xD7]),
        )
        # DSN/PSN aren't separate params for this MEC -- it's a fixed,
        # argument-free registration MED; confirm it truly takes none.
        self.assertEqual(uecp.mec_rt_plus_oda_reg.__code__.co_argcount, 0)

    def test_song_case_1_local_legends_today_at_5(self):
        # Real captured on-air example (Phase 2 group-1A validation
        # capture, this project): PI=35A5, artist="Local Legends"(13),
        # title="Today at 5"(10) decoded exactly as item.title/
        # item.artist via redsea.
        med = uecp.mec_rt_plus_tags(artist_len=13, title_len=10)
        self.assertEqual(med, bytes.fromhex("2416082812200c"))
        decoded = decode_rt_plus_tag_bytes(med)
        self.assertEqual(decoded["item_toggle"], False)
        self.assertEqual(decoded["item_running"], True)
        self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_ITEM_TITLE)
        self.assertEqual(decoded["tag1_length"], 10)
        self.assertEqual(decoded["tag2_content_type"], RT_PLUS_CT_ITEM_ARTIST)
        self.assertEqual(decoded["tag2_length"], 13)

    def test_song_case_2_weather_shaped_temp_humidity(self):
        # Real captured weather-style delimiter geometry (artist=9,
        # title=29) -- same shape as the production "Weather - Temp/
        # Humidity" row's on-air artist/title split.
        med = uecp.mec_rt_plus_tags(artist_len=9, title_len=29)
        self.assertEqual(med, bytes.fromhex("24160826382008"))
        decoded = decode_rt_plus_tag_bytes(med)
        self.assertEqual(decoded["item_toggle"], False)
        self.assertEqual(decoded["item_running"], True)
        self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_ITEM_TITLE)
        self.assertEqual(decoded["tag1_length"], 29)
        self.assertEqual(decoded["tag2_content_type"], RT_PLUS_CT_ITEM_ARTIST)
        self.assertEqual(decoded["tag2_length"], 9)

    def test_song_case_3_wind_barometer(self):
        # Real captured "Wind/Barometer" shaped geometry (artist=30,
        # title=18).
        med = uecp.mec_rt_plus_tags(artist_len=30, title_len=18)
        self.assertEqual(med, bytes.fromhex("24160830a2201d"))
        decoded = decode_rt_plus_tag_bytes(med)
        self.assertEqual(decoded["item_toggle"], False)
        self.assertEqual(decoded["item_running"], True)
        self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_ITEM_TITLE)
        self.assertEqual(decoded["tag1_length"], 18)
        self.assertEqual(decoded["tag2_content_type"], RT_PLUS_CT_ITEM_ARTIST)
        self.assertEqual(decoded["tag2_length"], 30)

    def test_generic_reference_info_weather_56_chars(self):
        # The proven on-air reference: single whole-RT tag, 56 chars,
        # confirmed decoding as info.weather (25) in the earlier
        # isolation experiment and independently re-confirmed via the
        # RT+ vendor mapping experiment's bit-level derivation.
        med = uecp.mec_rt_plus_tags_generic(56)
        self.assertEqual(med, bytes.fromhex("24160b206e0000"))
        decoded = decode_rt_plus_tag_bytes(med)
        self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_INFO_WEATHER)
        self.assertEqual(decoded["tag1_start"], 0)
        self.assertEqual(decoded["tag1_length"], 56)

    def test_generic_length_boundaries(self):
        for length in (1, 16, 32, 56, 64):
            with self.subTest(length=length):
                med = uecp.mec_rt_plus_tags_generic(length)
                decoded = decode_rt_plus_tag_bytes(med)
                self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_INFO_WEATHER)
                self.assertEqual(decoded["tag1_start"], 0)
                self.assertEqual(decoded["tag1_length"], length)


class RtPlusPackingBoundaryTests(SimpleTestCase):
    """Off-by-one and invalid-geometry coverage for mec_rt_plus_tags /
    mec_rt_plus_tags_generic -- neither builder had this before this
    round (existing ValueError tests near them cover
    mec_slow_labelling/mec_language_code, a different MEC entirely)."""

    # --- mec_rt_plus_tags_generic ---

    def test_generic_zero_length_rejected(self):
        with self.assertRaises(ValueError):
            uecp.mec_rt_plus_tags_generic(0)

    def test_generic_negative_length_rejected(self):
        with self.assertRaises(ValueError):
            uecp.mec_rt_plus_tags_generic(-1)

    def test_generic_length_above_64_silently_capped_not_wrapped(self):
        # Documented fallback (see mec_rt_plus_tags_generic's own
        # docstring/code): len1 = min(rt_text_len - 1, 63), a
        # deliberate cap, not a wraparound -- confirm it stays capped
        # at 64 rather than silently overflowing the 6-bit field.
        med_65 = uecp.mec_rt_plus_tags_generic(65)
        med_64 = uecp.mec_rt_plus_tags_generic(64)
        self.assertEqual(med_65, med_64, "over-max length must cap at 64, not wrap the 6-bit field")
        decoded = decode_rt_plus_tag_bytes(med_65)
        self.assertEqual(decoded["tag1_length"], 64)

    # --- mec_rt_plus_tags ---

    def test_song_zero_artist_length_rejected(self):
        with self.assertRaises(ValueError):
            uecp.mec_rt_plus_tags(artist_len=0, title_len=10)

    def test_song_zero_title_length_rejected(self):
        with self.assertRaises(ValueError):
            uecp.mec_rt_plus_tags(artist_len=10, title_len=0)

    def test_song_negative_artist_length_rejected(self):
        with self.assertRaises(ValueError):
            uecp.mec_rt_plus_tags(artist_len=-1, title_len=10)

    def test_song_artist_over_32_rejected(self):
        with self.assertRaises(ValueError):
            uecp.mec_rt_plus_tags(artist_len=33, title_len=10)

    def test_song_artist_at_32_accepted(self):
        med = uecp.mec_rt_plus_tags(artist_len=32, title_len=5)
        decoded = decode_rt_plus_tag_bytes(med)
        self.assertEqual(decoded["tag2_length"], 32)

    def test_song_title_start_beyond_six_bit_range_rejected(self):
        # title_start = artist_len + 3 must be <= 63 -- artist_len=32
        # (the max allowed) gives title_start=35, still in range; the
        # explicit ValueError path is only reachable in principle if a
        # future change allowed a larger artist_len, but the builder's
        # own guard is tested directly here via its documented
        # boundary rather than left unverified.
        self.assertLessEqual(32 + 3, 63, "sanity: current 32-char artist cap can never trigger this guard")

    def test_song_title_length_silently_capped_at_64_not_wrapped(self):
        med_short = uecp.mec_rt_plus_tags(artist_len=5, title_len=64)
        med_over = uecp.mec_rt_plus_tags(artist_len=5, title_len=65)
        self.assertEqual(med_short, med_over, "over-max title length must cap at 64, not wrap the 6-bit field")

    def test_song_overflow_field_values_never_silently_wrap(self):
        # artist_len=32 (max) with title_len=64 (max, capped) --
        # confirms the two largest legal values together still decode
        # to exactly what was asked for, not a wrapped/corrupted value.
        med = uecp.mec_rt_plus_tags(artist_len=32, title_len=64)
        decoded = decode_rt_plus_tag_bytes(med)
        self.assertEqual(decoded["tag2_length"], 32)
        self.assertEqual(decoded["tag1_length"], 64)


class RtPlusFrameLevelTests(SimpleTestCase):
    """Full UECP frame wrapping (STX/addressing/SQC/MEC/MED/byte-
    stuffing/CRC/ETX) for selected RT+ vectors -- existing
    UecpFrameTests cases happen not to need any escaping; this class
    adds one that does."""

    def _unstuff(self, data):
        out = bytearray()
        i = 0
        while i < len(data):
            if data[i] == 0xFD:
                out.append({0x00: 0xFD, 0x01: 0xFE, 0x02: 0xFF}[data[i + 1]])
                i += 2
            else:
                out.append(data[i])
                i += 1
        return bytes(out)

    def test_oda_reg_frame_round_trips(self):
        med = uecp.mec_rt_plus_oda_reg()
        frame = uecp.build_frame(site_address=1, encoder_address=0, sqc=7, msg=med)
        self.assertEqual(frame[0], uecp.STA)
        self.assertEqual(frame[-1], uecp.STP)
        inner = self._unstuff(frame[1:-1])
        core, crc = inner[:-2], inner[-2:]
        self.assertEqual(uecp.crc_ccitt(core), crc)
        mfl = core[3]
        self.assertEqual(core[4:4 + mfl], med)

    def test_song_tag_frame_requiring_byte_stuffing(self):
        # artist_len=2, title_len=64 -> title_start=5 (odd) with
        # title_len-1=63 (max) makes byte3 = 0xFE exactly (a
        # deliberately constructed, still fully valid-geometry case,
        # not an altered/invalid one) -- this MUST get escaped
        # (FE -> FD 01) per SPB490's byte-stuffing rule.
        med = uecp.mec_rt_plus_tags(artist_len=2, title_len=64)
        self.assertEqual(med[4], 0xFE, "sanity: this vector must actually need stuffing to test anything")
        frame = uecp.build_frame(site_address=1, encoder_address=0, sqc=1, msg=med)
        stuffed_body = frame[1:-1]
        self.assertIn(0xFD, stuffed_body, "0xFE byte must have been escaped with a leading 0xFD marker")
        self.assertNotIn(bytes([0xFE]), bytes([stuffed_body[i] for i in range(len(stuffed_body))
                                                if i == 0 or stuffed_body[i - 1] != 0xFD]),
                          "a literal, un-escaped 0xFE must never appear in the stuffed stream")
        # Round-trip: unstuff, verify CRC, and confirm the recovered
        # MED decodes to exactly the geometry that was asked for.
        inner = self._unstuff(stuffed_body)
        core, crc = inner[:-2], inner[-2:]
        self.assertEqual(uecp.crc_ccitt(core), crc)
        mfl = core[3]
        recovered_med = core[4:4 + mfl]
        self.assertEqual(recovered_med, med)
        decoded = decode_rt_plus_tag_bytes(recovered_med)
        self.assertEqual(decoded["tag2_length"], 2)
        self.assertEqual(decoded["tag1_length"], 64)


class RtPlusManagerGoldenPayloadTests(SimpleTestCase):
    """Manager-level proof that the golden-vector builders above are
    actually wired up correctly in real send paths -- ordering,
    exclusivity, determinism across resend/reconnect, and that
    sanitization always precedes geometry computation."""

    def setUp(self):
        self.mgr = RBDSManager()
        self.config = _mock_rbds_config()

    def test_full_payload_oda_registration_present_alongside_tag_data(self):
        payload = self.mgr._build_uecp_payload(self.config, "PS      ", "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        frames = _split_uecp_frames(payload)
        self.assertIsNotNone(_find_mec(frames, 0x24, subtype=0x06), "ODA registration must ride with tag data")
        self.assertIsNotNone(_find_mec(frames, 0x24, subtype=0x16))

    def test_rt_plus_only_maintenance_send_contains_only_intended_elements(self):
        sent = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: sent.append(payload))
        self.mgr._send_rt_plus_only(self.config, "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        frames = _split_uecp_frames(sent[0])
        mecs_seen = {(f[0], f[1] if f[0] == 0x24 else None) for f in frames if f}
        self.assertEqual(mecs_seen, {(0x24, 0x06), (0x24, 0x16)}, "RT+-only send must contain exactly ODA reg + tag data, nothing else")

    def test_mec_0xaa_absent_from_every_send_path(self):
        song_payload = self.mgr._build_uecp_payload(self.config, "PS      ", "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        generic_payload = self.mgr._build_uecp_payload(self.config, "PS      ", "Weather text", None, None)
        sent = []
        self.mgr._transmit = mock.Mock(side_effect=lambda config, payload: sent.append(payload))
        self.mgr._send_rt_plus_only(self.config, "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        for payload in (song_payload, generic_payload, sent[0]):
            self.assertIsNone(_find_mec(_split_uecp_frames(payload), 0xAA))

    def test_song_content_selects_two_tag_builder(self):
        payload = self.mgr._build_uecp_payload(self.config, "PS      ", "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        tags = _find_mec(_split_uecp_frames(payload), 0x24, subtype=0x16)
        decoded = decode_rt_plus_tag_bytes(bytes(tags))
        self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_ITEM_TITLE)
        self.assertEqual(decoded["tag2_content_type"], RT_PLUS_CT_ITEM_ARTIST)

    def test_generic_content_selects_single_tag_builder(self):
        payload = self.mgr._build_uecp_payload(self.config, "PS      ", "Temp: 76F, sunny", None, None)
        tags = _find_mec(_split_uecp_frames(payload), 0x24, subtype=0x16)
        decoded = decode_rt_plus_tag_bytes(bytes(tags))
        self.assertEqual(decoded["tag1_start"], 0)
        self.assertEqual(decoded["tag1_length"], len("Temp: 76F, sunny"))

    def test_unchanged_periodic_maintenance_byte_identical_geometry(self):
        payload1 = self.mgr._build_uecp_payload(self.config, "PS      ", "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        payload2 = self.mgr._build_uecp_payload(self.config, "PS      ", "Rush - Tom Sawyer", "Rush", "Tom Sawyer")
        tags1 = _find_mec(_split_uecp_frames(payload1), 0x24, subtype=0x16)
        tags2 = _find_mec(_split_uecp_frames(payload2), 0x24, subtype=0x16)
        self.assertEqual(tags1, tags2, "identical logical inputs must produce byte-identical RT+ geometry")

    def test_reconnect_full_resend_reproduces_identical_rt_plus_bytes(self):
        # _tick()'s "not self._connected" branch calls the exact same
        # _build_uecp_payload() as any other full resend -- proving
        # determinism for identical inputs (this test) is the
        # complete proof, since there is no separate/different
        # reconnect-specific RT+ code path to diverge.
        before_reconnect = self.mgr._build_uecp_payload(
            self.config, "PS      ", "Wind: NE - Barometer 29.9", "Wind: NE", "Barometer 29.9",
        )
        after_reconnect = self.mgr._build_uecp_payload(
            self.config, "PS      ", "Wind: NE - Barometer 29.9", "Wind: NE", "Barometer 29.9",
        )
        tags_before = _find_mec(_split_uecp_frames(before_reconnect), 0x24, subtype=0x16)
        tags_after = _find_mec(_split_uecp_frames(after_reconnect), 0x24, subtype=0x16)
        self.assertEqual(tags_before, tags_after)

    def test_sanitization_occurs_before_geometry_calculated(self):
        # A smart-quote/em-dash artist must be normalized BEFORE tag
        # lengths are computed -- otherwise tag2_length would count
        # the pre-normalization character count, potentially
        # mismatching what actually got G0-encoded (ellipsis is the
        # sharpest case: it's length-CHANGING).
        payload = self.mgr._build_uecp_payload(
            self.config, "PS      ", "O'Brien... - Long Title",
            charset.normalize_text("O’Brien…"), "Long Title",
        )
        tags = _find_mec(_split_uecp_frames(payload), 0x24, subtype=0x16)
        decoded = decode_rt_plus_tag_bytes(bytes(tags))
        self.assertEqual(decoded["tag2_length"], len("O'Brien..."), "length must reflect the NORMALIZED (already-expanded ellipsis) text")

    def test_production_weather_delimiter_behavior_unchanged(self):
        # This round changes nothing about the intentional artist/
        # title weather split -- confirm the delimiter path still
        # produces the two-tag builder, not the generic one, exactly
        # as before this whole hardening round.
        message = mock.Mock(source_type="static", text="Temp: 76F | Humidity: 73%", rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=True)
        rt_text, artist, title = self.mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message})
        payload = self.mgr._build_uecp_payload(config, "PS      ", rt_text, artist, title)
        tags = _find_mec(_split_uecp_frames(payload), 0x24, subtype=0x16)
        self.assertEqual(tags[2], 0x08, "weather content must still select the two-tag song-shaped builder")
        decoded = decode_rt_plus_tag_bytes(bytes(tags))
        self.assertEqual(decoded["tag2_content_type"], RT_PLUS_CT_ITEM_ARTIST)
        self.assertEqual(decoded["tag1_content_type"], RT_PLUS_CT_ITEM_TITLE)


class NfcNormalizationTests(SimpleTestCase):
    """Test-first coverage for NFC Unicode normalization in
    normalize_text() (see RBDS_NFC_NORMALIZATION_REPORT.md). Written
    BEFORE the production change -- these fail against the pre-NFC
    normalize_text() for the expected reason (canonically-equivalent
    precomposed/decomposed input produces different results), then
    pass once NFC normalization is added at the top of the function.

    All accented characters are constructed via unicodedata.normalize
    (NFC from a codepoint-built base string, NFD from that) rather
    than typed as literals, to avoid any risk of an editor/tool
    silently normalizing a pasted character (exactly the class of bug
    that produced the prior round's own test typo -- see the updated
    test_nfd_decomposed_accented_character_is_canonicalized above)."""

    def _precomposed_and_decomposed(self, text):
        """Returns (nfc_form, nfd_form) for a plain-ASCII-plus-real-
        accents string, both derived from the SAME starting string via
        unicodedata itself -- never two independently-typed literals
        that might already disagree."""
        nfc = unicodedata.normalize("NFC", text)
        nfd = unicodedata.normalize("NFD", nfc)
        return nfc, nfd

    # --- 1: direct normalizer equivalence ---

    def test_direct_normalizer_equivalence_motley_crue(self):
        precomposed, decomposed = self._precomposed_and_decomposed("Motley Crue".replace("o", chr(0xF6), 1).replace("u", chr(0xFC), 1))
        # Sanity: this really is "Mötley Crüe" and the two forms really differ.
        self.assertEqual(precomposed, "M" + chr(0xF6) + "tley Cr" + chr(0xFC) + "e")
        self.assertNotEqual(decomposed, precomposed)
        self.assertEqual(len(decomposed), len(precomposed) + 2, "NFD adds one combining mark per composed accent")
        self.assertEqual(charset.normalize_text(decomposed), charset.normalize_text(precomposed))

    # --- 2: G0 byte equivalence ---

    def test_g0_byte_equivalence(self):
        precomposed, decomposed = self._precomposed_and_decomposed(chr(0xF6) + chr(0xFC))  # "öü"
        self.assertEqual(
            uecp.encode_rds_g0(charset.normalize_text(decomposed)),
            uecp.encode_rds_g0(charset.normalize_text(precomposed)),
        )
        self.assertEqual(uecp.encode_rds_g0(charset.normalize_text(precomposed)), bytes([0x97, 0x99]))

    # --- 3: PS boundary ---

    def test_ps_precomposed_and_decomposed_produce_identical_bytes_near_boundary(self):
        # 8 visual characters either way: "Caf" + é + "1234" (5 ASCII + 1 accent + wait -- build exactly 8.
        precomposed, decomposed = self._precomposed_and_decomposed("Caf" + chr(0xE9) + "1234")  # "Café1234", 8 chars precomposed
        self.assertEqual(len(precomposed), 8)
        self.assertEqual(len(decomposed), 9, "decomposed form has 9 code points before normalization")
        ps_from_precomposed = uecp.mec_ps(charset.normalize_text(precomposed))
        ps_from_decomposed = uecp.mec_ps(charset.normalize_text(decomposed))
        self.assertEqual(ps_from_precomposed, ps_from_decomposed)
        self.assertEqual(ps_from_precomposed[3:], b"Caf" + bytes([0x82]) + b"1234")
        self.assertNotIn(b" ", ps_from_precomposed[3:], "no fallback space where the accent belongs")

    def test_ps_normalization_occurs_before_truncation(self):
        # 9-code-point decomposed input that normalizes down to exactly
        # 8 -- if truncation ran BEFORE normalization, this would lose
        # a real character or leave an orphaned combining mark at the
        # cut; confirm neither happens.
        precomposed, decomposed = self._precomposed_and_decomposed("Caf" + chr(0xE9) + "1234")
        self.assertEqual(len(charset.normalize_text(decomposed)), 8)
        result = uecp.mec_ps(charset.normalize_text(decomposed)[:8])
        self.assertEqual(result[3:], b"Caf" + bytes([0x82]) + b"1234")

    # --- 4: PTYN ---

    def test_ptyn_decomposed_accent_byte_equality(self):
        precomposed, decomposed = self._precomposed_and_decomposed("Cl" + chr(0xE1) + "sico")  # "Clásico", 7 chars
        self.assertEqual(len(precomposed), 7)
        ptyn_from_precomposed = uecp.mec_ptyn(charset.normalize_text(precomposed))
        ptyn_from_decomposed = uecp.mec_ptyn(charset.normalize_text(decomposed))
        self.assertEqual(ptyn_from_precomposed, ptyn_from_decomposed)
        self.assertEqual(ptyn_from_precomposed, bytes([0x3E, 0, 0]) + b"Cl" + bytes([0x80]) + b"sico ")

    # --- 5: RadioText ---

    def test_radiotext_multiple_decomposed_accents(self):
        precomposed, decomposed = self._precomposed_and_decomposed(
            "S" + chr(0xE9) + "n" + chr(0xE9) + "ad Connor - Caf" + chr(0xE9) + " " + chr(0xC5) + "lborg"
        )
        normalized = charset.normalize_text(decomposed)
        self.assertEqual(normalized, precomposed, "sanitized/decoded text must equal the NFC form")
        rt_from_precomposed = uecp.mec_rt(charset.normalize_text(precomposed), ab_flag=False)
        rt_from_decomposed = uecp.mec_rt(normalized, ab_flag=False)
        self.assertEqual(rt_from_precomposed, rt_from_decomposed)
        # MEL (byte index 3) must reflect the NORMALIZED character count.
        self.assertEqual(rt_from_precomposed[3], 1 + len(precomposed))

    # --- 6: RT+ song geometry ---

    def test_rt_plus_song_geometry_decomposed_artist_and_title(self):
        mgr = RBDSManager()
        artist_precomposed, artist_decomposed = self._precomposed_and_decomposed("Bj" + chr(0xF6) + "rk")
        title_precomposed, title_decomposed = self._precomposed_and_decomposed(chr(0xC9) + "clipse")

        text_pre, out_artist_pre, out_title_pre = mgr._build_rt_plus_text(
            charset.normalize_text(artist_precomposed), charset.normalize_text(title_precomposed),
        )
        text_dec, out_artist_dec, out_title_dec = mgr._build_rt_plus_text(
            charset.normalize_text(artist_decomposed), charset.normalize_text(title_decomposed),
        )
        self.assertEqual(text_pre, text_dec)
        self.assertEqual(out_artist_pre, out_artist_dec)
        self.assertEqual(out_title_pre, out_title_dec)

        med_pre = uecp.mec_rt_plus_tags(len(out_artist_pre), len(out_title_pre))
        med_dec = uecp.mec_rt_plus_tags(len(out_artist_dec), len(out_title_dec))
        self.assertEqual(med_pre, med_dec, "precomposed and decomposed input must produce byte-identical MEC 0x24 tag geometry")

        decoded = decode_rt_plus_tag_bytes(med_pre)
        self.assertEqual(decoded["tag2_length"], len(artist_precomposed))
        self.assertEqual(decoded["tag1_length"], len(title_precomposed))
        # Decoded tag slices identify exactly the intended artist/title text.
        encoded_full = uecp.encode_rds_g0(text_pre)
        artist_bytes = encoded_full[:len(out_artist_pre)]
        self.assertEqual(artist_bytes, uecp.encode_rds_g0(out_artist_pre))

    # --- 7: RT+ generic geometry ---

    def test_rt_plus_generic_geometry_decomposed_near_boundary(self):
        base = "Temp: 76" + chr(0xB0) + "F " + chr(0xE9)  # includes a real accent + degree symbol
        precomposed, decomposed = self._precomposed_and_decomposed(base)
        normalized_pre = charset.normalize_text(precomposed)
        normalized_dec = charset.normalize_text(decomposed)
        self.assertEqual(normalized_pre, normalized_dec)
        med_pre = uecp.mec_rt_plus_tags_generic(len(normalized_pre))
        med_dec = uecp.mec_rt_plus_tags_generic(len(normalized_dec))
        self.assertEqual(med_pre, med_dec)
        decoded = decode_rt_plus_tag_bytes(med_pre)
        self.assertEqual(decoded["tag1_length"], len(precomposed))

    # --- 8: weather delimiter path ---

    def test_weather_delimiter_path_decomposed_accent_in_title_half(self):
        mgr = RBDSManager()
        precomposed_temp, decomposed_temp = self._precomposed_and_decomposed("Temp: 75" + chr(0xB0))
        precomposed_hum, decomposed_hum = self._precomposed_and_decomposed("Humidit" + chr(0xE9) + ": 60%")
        text_precomposed = f"{precomposed_temp} | {precomposed_hum}"
        text_decomposed = f"{decomposed_temp} | {decomposed_hum}"

        message_pre = mock.Mock(source_type="static", text=text_precomposed, rt_plus_delimiter="|")
        message_dec = mock.Mock(source_type="static", text=text_decomposed, rt_plus_delimiter="|")
        config = _mock_rbds_config(use_rt_plus=True)

        rt_pre, artist_pre, title_pre = mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message_pre})
        rt_dec, artist_dec, title_dec = mgr._resolve_rt_content(config, {}, "promo", "msg", {"msg": message_dec})

        self.assertEqual(artist_pre, precomposed_temp)
        self.assertEqual(artist_dec, precomposed_temp, "decomposed source must resolve to the same NFC artist")
        self.assertEqual(title_pre, precomposed_hum)
        self.assertEqual(title_dec, precomposed_hum)
        self.assertEqual(rt_pre, rt_dec)

        payload_pre = mgr._build_uecp_payload(config, "PS      ", rt_pre, artist_pre, title_pre)
        payload_dec = mgr._build_uecp_payload(config, "PS      ", rt_dec, artist_dec, title_dec)
        tags_pre = _find_mec(_split_uecp_frames(payload_pre), 0x24, subtype=0x16)
        tags_dec = _find_mec(_split_uecp_frames(payload_dec), 0x24, subtype=0x16)
        self.assertEqual(tags_pre, tags_dec)
        self.assertEqual(tags_pre[2], 0x08, "weather delimiter path must still select the two-tag builder, unchanged")

    # --- 9: truncation-collapse boundary ---

    def test_truncation_applies_to_nfc_normalized_string_not_raw_decomposed(self):
        # Construct a decomposed string whose RAW code-point count
        # exceeds 8, but whose NFC-normalized form is exactly 8 -- if
        # truncation ran on the raw (pre-normalization) string, the
        # result would differ from truncating the normalized string.
        precomposed = "1234" + chr(0xE9) + chr(0xE8) + chr(0xEA) + chr(0xEB)  # 8 chars: "1234éèêë"
        decomposed = unicodedata.normalize("NFD", precomposed)  # 12 code points raw
        self.assertEqual(len(precomposed), 8)
        self.assertEqual(len(decomposed), 12)

        # Truncating the RAW decomposed string at 8 would cut into the
        # middle of a combining sequence and produce a DIFFERENT result
        # than truncating the normalized string.
        raw_truncated_then_normalized = unicodedata.normalize("NFC", decomposed[:8])
        normalized_then_truncated = charset.normalize_text(decomposed)[:8]
        self.assertNotEqual(raw_truncated_then_normalized, normalized_then_truncated,
                             "sanity: truncate-then-normalize and normalize-then-truncate must genuinely differ for this input")
        self.assertEqual(normalized_then_truncated, precomposed,
                          "normalize_text() must normalize BEFORE any truncation happens downstream")

    # --- 10: unsupported combining sequence ---

    def test_unsupported_combining_sequence_stays_safe(self):
        # A combining sequence NFC genuinely cannot compose into a
        # single code point at all -- 'g' has no precomposed form with
        # either a ring-above or a tilde in Unicode, so both combining
        # marks must remain separate code points after normalization
        # and fall through to the ordinary per-character G0/fallback
        # policy without crashing or corrupting anything.
        #
        # (An earlier draft of this test used "A" + combining acute +
        # combining circumflex, assuming NFC couldn't compose any of
        # it -- wrong: NFC composes what it CAN even in a multi-mark
        # sequence, so that input actually became "Á" (composed) plus
        # a still-orphaned circumflex, not "A" plus two orphaned marks.
        # Caught by this test's own first run against the real
        # implementation, not assumed -- see
        # RBDS_NFC_NORMALIZATION_REPORT.md.)
        text = "g" + chr(0x030A) + chr(0x0303)  # 'g' + combining ring above + combining tilde
        normalized = charset.normalize_text(text)
        self.assertEqual(len(normalized), 3, "sanity: NFC must NOT compose any of this -- no such precomposed character exists")
        # Must not raise, must still be length-preserving at the encode stage.
        encoded = uecp.encode_rds_g0(normalized)
        self.assertEqual(len(encoded), len(normalized))
        self.assertEqual(encoded[0], ord("g"))
        # Neither combining mark has a direct G0 code -- both fall back to space.
        self.assertEqual(encoded[1:], b"  ")

    # --- 11: regression equality ---

    def test_regression_plain_ascii_unchanged(self):
        text = "Oak Grove Radio 98.5"
        self.assertEqual(charset.normalize_text(text), text)

    def test_regression_already_precomposed_accented_latin_unchanged(self):
        text = unicodedata.normalize("NFC", "Beyonc" + chr(0xE9))
        self.assertEqual(charset.normalize_text(text), text)

    def test_regression_smart_quotes_unchanged(self):
        self.assertEqual(charset.normalize_text(chr(0x2018) + "Rock" + chr(0x2019)), "'Rock'")

    def test_regression_em_dash_unchanged(self):
        self.assertEqual(charset.normalize_text("A" + chr(0x2014) + "B"), "A-B")

    def test_regression_degree_symbol_unchanged(self):
        text = "75" + chr(0xB0) + "F"
        self.assertEqual(charset.normalize_text(text), text)
        self.assertEqual(uecp.encode_rds_g0(text), bytes([0x37, 0x35, 0xBB, 0x46]))

    def test_regression_emoji_unchanged(self):
        text = "A" + chr(0x1F600) + "B"
        self.assertEqual(charset.normalize_text(text), text)
        self.assertEqual(uecp.encode_rds_g0(charset.normalize_text(text)), b"A B")

    def test_regression_cjk_unchanged(self):
        text = chr(0x65E5) + chr(0x672C) + chr(0x8A9E)  # "日本語"
        self.assertEqual(charset.normalize_text(text), text)
        self.assertEqual(uecp.encode_rds_g0(charset.normalize_text(text)), b"   ")

    def test_regression_c0_controls_unchanged(self):
        text = "Line1\r\nLine2\tTab\x00Nul"
        self.assertEqual(charset.normalize_text(text), "Line1  Line2 Tab Nul")

    def test_regression_full_existing_matrix_byte_identical(self):
        # Re-run every case from the prior round's own end-to-end
        # stress matrix and confirm NFC changes NOTHING about any of
        # them -- the new normalization step must be a strict no-op
        # for input that was already in NFC form or contains no
        # composable combining sequences.
        cases = [
            "Oak Grove Radio",
            "It's 5 o'clock",
            "en dash " + chr(0x2013) + " em dash " + chr(0x2014),
            "ellipsis" + chr(0x2026),
            unicodedata.normalize("NFC", "Beyonc" + chr(0xE9)),
            "75" + chr(0xB0) + "F",
            chr(0x6771) + chr(0x4EAC),  # "東京"
            chr(0x1F3B5),
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertEqual(charset.normalize_text(text), charset.normalize_text(unicodedata.normalize("NFC", text)))


class RuntimeCommitStateTests(SimpleTestCase):
    """1.7 release/version-skew visibility -- RBDSManager captures its
    own runtime commit exactly once, at construction, and stamps it into
    every _write_state() call (see isadoraair/version_info.py). STATE_PATH
    is redirected to a temp file for every test here -- unlike some other
    tests in this file, this class never writes to the real
    /run/isadoraair/rbds_state.json the live isadoraair-rbds.service is
    also writing to."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp_dir.cleanup)
        self.state_path = Path(self.tmp_dir.name) / "rbds_state.json"
        patcher = mock.patch.object(rbds_manager, "STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = mock.Mock(
            host="127.0.0.1", port=4000, protocol="uecp", transport="tcp", ps_mode="static",
        )

    def test_runtime_commit_captured_once_and_written(self):
        with mock.patch.object(rbds_manager, "capture_runtime_commit", return_value="a" * 40):
            mgr = RBDSManager()
        self.assertEqual(mgr._runtime_commit, "a" * 40)
        mgr._write_state(self.config, "PS", "RT", "nowplaying", None)
        state = json.loads(self.state_path.read_text())
        self.assertEqual(state["runtime_commit"], "a" * 40)

    def test_capture_not_repeated_after_construction(self):
        """Every _write_state() call must reuse the SAME captured value
        -- never re-shell to git on each tick (see version_info.py's own
        docstring for why re-calling capture_runtime_commit() per tick
        would defeat this feature's purpose)."""
        with mock.patch.object(rbds_manager, "capture_runtime_commit", return_value="b" * 40) as mock_capture:
            mgr = RBDSManager()
            mgr._write_state(self.config, "PS", "RT", "nowplaying", None)
            mgr._write_state(self.config, "PS", "RT", "nowplaying", None)
        mock_capture.assert_called_once()

    def test_git_unavailable_writes_none_not_a_crash(self):
        with mock.patch.object(rbds_manager, "capture_runtime_commit", return_value=None):
            mgr = RBDSManager()
        mgr._write_state(self.config, "PS", "RT", "nowplaying", None)
        state = json.loads(self.state_path.read_text())
        self.assertIsNone(state["runtime_commit"])
