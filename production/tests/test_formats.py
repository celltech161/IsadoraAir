"""The content sniffer and the (container, codec) allowlist -- pure functions."""
import struct

from django.test import SimpleTestCase

from production import formats


def sniff(data, tail=b""):
    blob = data + tail
    return formats.sniff_container(blob[:16], lambda offset, count: blob[offset:offset + count])


class SniffTests(SimpleTestCase):
    def test_every_allowlisted_container_is_recognised_by_its_bytes(self):
        cases = {
            "wav": b"RIFF\x24\x00\x00\x00WAVEfmt ",
            "aiff": b"FORM\x00\x00\x00\x24AIFFCOMM",
            "flac": b"fLaC\x00\x00\x00\x22" + b"\x00" * 8,
            "ogg": b"OggS\x00\x02" + b"\x00" * 10,
            "matroska": b"\x1a\x45\xdf\xa3" + b"\x00" * 12,
            "mp4": b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 4,
            "mp3": b"\xff\xfb\x90\x00" + b"\x00" * 12,
        }
        for expected, head in cases.items():
            with self.subTest(container=expected):
                self.assertEqual(sniff(head), expected)

    def test_an_id3_tag_is_skipped_to_find_the_real_container(self):
        def id3(size):
            sync = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F, (size >> 7) & 0x7F, size & 0x7F])
            return b"ID3\x03\x00\x00" + sync + b"\x00" * size
        self.assertEqual(sniff(id3(30), b"fLaC" + b"\x00" * 8), "flac")
        self.assertEqual(sniff(id3(30), b"\xff\xfb\x90\x00" + b"\x00" * 8), "mp3")
        self.assertIsNone(sniff(id3(30), b"RIFF....WAVE"))
        self.assertIsNone(sniff(id3(30), b""))
        self.assertIsNone(sniff(b"ID3\x03\x00\x00\x7f\x7f\x7f\x7f" + b"\x00" * 8, b""))   # absurd tag size

    def test_things_that_are_not_on_the_allowlist_are_not_recognised(self):
        cases = {
            "raw ADTS AAC": b"\xff\xf1\x50\x80" + b"\x00" * 12,
            "AC-3": b"\x0b\x77" + b"\x00" * 14,
            "text": b"This is not audio",
            "an HLS playlist": b"#EXTM3U\n#EXT-X-VERSION:3\n",
            "an ffconcat script": b"ffconcat version 1.0\nfile x",
            "an SDP description": b"v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\n",
            "a RIFF that is not WAVE": b"RIFF\x24\x00\x00\x00AVI LIST",
            "an ELF binary": b"\x7fELF" + b"\x00" * 12,
            "too short": b"RI",
            "empty": b"",
        }
        for label, head in cases.items():
            with self.subTest(label):
                self.assertIsNone(sniff(head))

    def test_mpeg_sync_excludes_reserved_version_and_layer(self):
        self.assertIsNone(sniff(b"\xff\xe9\x90\x00" + b"\x00" * 12))     # reserved version 01
        self.assertIsNone(sniff(b"\xff\xf1\x90\x00" + b"\x00" * 12))     # layer 00 (ADTS)
        self.assertEqual(sniff(b"\xff\xfd\x90\x00" + b"\x00" * 12), "mp3")   # layer II still routes to mp3 demuxer


class ClassifyTests(SimpleTestCase):
    def test_accepts_exactly_the_allowlisted_pairs(self):
        accepted = [
            ("wav", "wav", "pcm_s16le"), ("aiff", "aiff", "pcm_s16be"), ("flac", "flac", "flac"),
            ("mp3", "mp3", "mp3"), ("ogg", "ogg", "opus"), ("ogg", "ogg", "vorbis"),
            ("matroska", "matroska,webm", "opus"), ("mp4", "mov,mp4,m4a,3gp,3g2,mj2", "aac"),
            ("mp4", "mov,mp4,m4a,3gp,3g2,mj2", "alac"),
        ]
        for sniffed, format_name, codec in accepted:
            with self.subTest(sniffed=sniffed, codec=codec):
                result, code = formats.classify(sniffed, format_name, codec)
                self.assertIsNone(code)
                self.assertEqual((result.container, result.codec), (sniffed, codec))

    def test_rejections_carry_a_stable_code(self):
        self.assertEqual(formats.classify("wav", "wav", "adpcm_ms")[1], "unsupported_codec")
        self.assertEqual(formats.classify("ogg", "ogg", "flac")[1], "unsupported_codec")
        self.assertEqual(formats.classify("mp3", "mp3", "mp2")[1], "unsupported_codec")
        self.assertEqual(formats.classify("mp4", "mov,mp4,m4a", "h264")[1], "unsupported_codec")
        self.assertEqual(formats.classify("hls", "hls", "aac")[1], "unsupported_container")

    def test_sniffed_bytes_and_the_demuxer_must_agree(self):
        # Bytes say WAV, ffprobe's demuxer says flac: refuse, never guess.
        self.assertEqual(formats.classify("wav", "flac", "flac")[1], "unsupported_container")

    def test_content_type_comes_from_the_validated_container_only(self):
        self.assertEqual(formats.content_type_for("mp3"), "audio/mpeg")
        self.assertEqual(formats.content_type_for("matroska"), "audio/webm")
        self.assertEqual(formats.content_type_for(""), "application/octet-stream")
        self.assertEqual(formats.content_type_for("../../etc/passwd"), "application/octet-stream")

    def test_every_allowed_codec_has_a_named_decoder(self):
        for container, codecs in formats.ALLOWED_CODECS.items():
            for codec in codecs:
                with self.subTest(container=container, codec=codec):
                    self.assertTrue(formats.decoder_names(codec))

    def test_supported_formats_lists_nine_families(self):
        families = {container for container, _codec in formats.supported_formats()}
        self.assertEqual(families, {"wav", "aiff", "flac", "mp3", "ogg", "matroska", "mp4"})
