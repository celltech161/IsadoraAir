"""encoders/services/lkg.py -- candidate rendering, fingerprint, and
persistent last-known-good (LKG) state. CANDIDATE_DIR/LKG_DIR are
always patched to a temporary directory (never the real /run or
/var/lib paths) via the fixture mixin below."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from encoders.models import Encoder
from encoders.services import lkg


def make_encoder(**overrides):
    defaults = dict(
        name="test", enabled=True, protocol="shoutcast2",
        host="192.168.1.112", port=8000, mount="/4", username="source",
        password="secret", format="mp3", bitrate_kbps=192,
        station_name="Test Station", genre="", url="", public=False,
    )
    defaults.update(overrides)
    return Encoder(**defaults)


class LkgDirFixtureMixin:
    def setUp(self):
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        base = Path(self._tmpdir.name)
        self.candidate_dir = base / "candidate"
        self.lkg_dir = base / "lkg"
        for patcher in (
            patch.object(lkg, "CANDIDATE_DIR", self.candidate_dir),
            patch.object(lkg, "LKG_DIR", self.lkg_dir),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------
class ComputeFingerprintTests(SimpleTestCase):
    def test_deterministic_for_same_input(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder()])
        b = lkg.compute_fingerprint("airtap", [make_encoder()])
        self.assertEqual(a, b)

    def test_different_host_different_fingerprint(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder(host="1.2.3.4")])
        b = lkg.compute_fingerprint("airtap", [make_encoder(host="5.6.7.8")])
        self.assertNotEqual(a, b)

    def test_different_password_different_fingerprint(self):
        """Password IS runtime-affecting (a changed password is a
        genuinely different configuration that must re-qualify) --
        included in the hashed payload even though it's a one-way
        digest and never leaks the actual value."""
        a = lkg.compute_fingerprint("airtap", [make_encoder(password="secret1")])
        b = lkg.compute_fingerprint("airtap", [make_encoder(password="secret2")])
        self.assertNotEqual(a, b)

    def test_different_input_device_different_fingerprint(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder()])
        b = lkg.compute_fingerprint("plughw:3,1", [make_encoder()])
        self.assertNotEqual(a, b)

    def test_row_order_does_not_affect_fingerprint(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder(name="a", mount="/1"), make_encoder(name="b", mount="/2")])
        b = lkg.compute_fingerprint("airtap", [make_encoder(name="b", mount="/2"), make_encoder(name="a", mount="/1")])
        self.assertEqual(a, b)

    def test_name_does_not_affect_fingerprint(self):
        """`name` is not in RUNTIME_AFFECTING_FIELDS -- a pure rename
        must not change the fingerprint."""
        a = lkg.compute_fingerprint("airtap", [make_encoder(name="Alpha")])
        b = lkg.compute_fingerprint("airtap", [make_encoder(name="Beta")])
        self.assertEqual(a, b)

    def test_sort_order_does_not_affect_fingerprint(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder(sort_order=1)])
        b = lkg.compute_fingerprint("airtap", [make_encoder(sort_order=99)])
        self.assertEqual(a, b)

    def test_description_does_not_affect_fingerprint(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder(description="old")])
        b = lkg.compute_fingerprint("airtap", [make_encoder(description="new")])
        self.assertEqual(a, b)

    def test_row_order_does_not_affect_fingerprint_when_rows_tie_on_host_port_mount(self):
        """Review-fix regression: the sort key used to be just (host,
        port, mount) -- not fully canonical. Two rows sharing all three
        of those but differing in another runtime field (protocol,
        here) used to have their relative order determined by
        `encoders`' own input order (a DB queryset's order isn't
        guaranteed stable), which could make the SAME effective set of
        rows hash differently across two runs. Sorting by the FULL row
        instead means order can only ever matter for truly identical
        rows -- this must produce the same fingerprint regardless of
        which order the two distinguishable-only-by-protocol rows
        arrive in."""
        row_a = make_encoder(name="a", host="1.2.3.4", port=8000, mount="/1", protocol="icecast")
        row_b = make_encoder(name="b", host="1.2.3.4", port=8000, mount="/1", protocol="shoutcast1")
        first = lkg.compute_fingerprint("airtap", [row_a, row_b])
        second = lkg.compute_fingerprint("airtap", [row_b, row_a])
        self.assertEqual(first, second)

    def test_tied_host_port_mount_rows_still_change_fingerprint_if_a_field_differs(self):
        """Companion to the above: this isn't testing that the tied
        field no longer matters -- a genuinely different set of rows
        (different protocol on one of the tied pair) must still
        produce a DIFFERENT fingerprint than an otherwise-identical set
        without that difference."""
        tied_same = [
            make_encoder(name="a", host="1.2.3.4", port=8000, mount="/1", protocol="icecast"),
            make_encoder(name="b", host="1.2.3.4", port=8000, mount="/1", protocol="icecast"),
        ]
        tied_different = [
            make_encoder(name="a", host="1.2.3.4", port=8000, mount="/1", protocol="icecast"),
            make_encoder(name="b", host="1.2.3.4", port=8000, mount="/1", protocol="shoutcast1"),
        ]
        self.assertNotEqual(
            lkg.compute_fingerprint("airtap", tied_same),
            lkg.compute_fingerprint("airtap", tied_different),
        )

    def test_empty_encoder_list_is_deterministic(self):
        a = lkg.compute_fingerprint("airtap", [])
        b = lkg.compute_fingerprint("airtap", [])
        self.assertEqual(a, b)

    def test_returns_hex_string(self):
        fp = lkg.compute_fingerprint("airtap", [make_encoder()])
        self.assertIsInstance(fp, str)
        int(fp, 16)  # raises ValueError if not valid hex
        self.assertEqual(len(fp), 64)  # sha256 hex digest length


# ---------------------------------------------------------------------
# Candidate rendering
# ---------------------------------------------------------------------
class CandidateTests(LkgDirFixtureMixin, SimpleTestCase):
    def test_write_candidate_creates_file_with_content(self):
        path = lkg.write_candidate("airtap", "source = blank()\n")
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(), "source = blank()\n")

    def test_candidate_directory_mode_0700(self):
        lkg.write_candidate("airtap", "x")
        mode = self.candidate_dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_candidate_file_mode_0600(self):
        path = lkg.write_candidate("airtap", "x")
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_two_candidates_for_same_slug_do_not_collide(self):
        a = lkg.write_candidate("airtap", "first")
        b = lkg.write_candidate("airtap", "second")
        self.assertNotEqual(a, b)
        self.assertEqual(a.read_text(), "first")
        self.assertEqual(b.read_text(), "second")

    def test_candidate_path_never_touches_lkg_dir(self):
        path = lkg.write_candidate("airtap", "x")
        self.assertNotEqual(path.parent, self.lkg_dir)
        self.assertFalse(self.lkg_dir.exists())

    def test_cleanup_candidate_removes_file(self):
        path = lkg.write_candidate("airtap", "x")
        lkg.cleanup_candidate(path)
        self.assertFalse(path.exists())

    def test_cleanup_candidate_missing_file_does_not_raise(self):
        lkg.cleanup_candidate(self.candidate_dir / "does-not-exist.liq")  # no exception


# ---------------------------------------------------------------------
# LKG persistence
# ---------------------------------------------------------------------
class LkgPersistenceTests(LkgDirFixtureMixin, SimpleTestCase):
    def test_no_lkg_returns_none_none(self):
        script, meta = lkg.read_lkg("airtap")
        self.assertIsNone(script)
        self.assertIsNone(meta)

    def test_lkg_exists_false_when_absent(self):
        self.assertFalse(lkg.lkg_exists("airtap"))

    def test_write_then_read_round_trips(self):
        lkg.write_lkg("airtap", "source = blank()\n", {"fingerprint": "abc123"})
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "source = blank()\n")
        self.assertEqual(meta, {"fingerprint": "abc123"})

    def test_lkg_exists_true_after_write(self):
        lkg.write_lkg("airtap", "x", {})
        self.assertTrue(lkg.lkg_exists("airtap"))

    def test_lkg_directory_mode_0700(self):
        lkg.write_lkg("airtap", "x", {})
        mode = self.lkg_dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_lkg_script_file_mode_0600(self):
        lkg.write_lkg("airtap", "x", {})
        mode = (self.lkg_dir / "airtap.liq").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_lkg_meta_file_mode_0644(self):
        lkg.write_lkg("airtap", "x", {})
        mode = (self.lkg_dir / "airtap.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_second_write_overwrites_first(self):
        lkg.write_lkg("airtap", "old script", {"fingerprint": "old"})
        lkg.write_lkg("airtap", "new script", {"fingerprint": "new"})
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "new script")
        self.assertEqual(meta["fingerprint"], "new")

    def test_different_slugs_do_not_collide(self):
        lkg.write_lkg("airtap", "airtap script", {"slug": "airtap"})
        lkg.write_lkg("plughw_3_1", "other script", {"slug": "plughw_3_1"})
        a_script, a_meta = lkg.read_lkg("airtap")
        b_script, b_meta = lkg.read_lkg("plughw_3_1")
        self.assertEqual(a_script, "airtap script")
        self.assertEqual(b_script, "other script")
        self.assertNotEqual(a_meta, b_meta)

    def test_read_lkg_meta_returns_metadata_only(self):
        lkg.write_lkg("airtap", "secret script contents", {"fingerprint": "abc"})
        meta = lkg.read_lkg_meta("airtap")
        self.assertEqual(meta, {"fingerprint": "abc"})

    def test_read_lkg_meta_none_when_absent(self):
        self.assertIsNone(lkg.read_lkg_meta("airtap"))

    def test_corrupt_metadata_does_not_crash_script_read(self):
        """A script with unreadable/corrupt metadata is still usable
        for a rollback launch -- metadata is informational, not
        required to relaunch."""
        lkg.write_lkg("airtap", "valid script", {"fingerprint": "abc"})
        (self.lkg_dir / "airtap.json").write_text("{not valid json", encoding="utf-8")
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "valid script")
        self.assertIsNone(meta)

    def test_no_tmp_files_left_behind(self):
        lkg.write_lkg("airtap", "x", {})
        leftovers = list(self.lkg_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_metadata_json_is_human_readable_on_disk(self):
        """Confirms the metadata file is plain readable JSON (not,
        say, accidentally the same restrictive format as the script)
        -- admin/monitoring code reads this directly."""
        lkg.write_lkg("airtap", "x", {"fingerprint": "abc", "accepted_at": 123.0})
        raw = (self.lkg_dir / "airtap.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertEqual(parsed["fingerprint"], "abc")

    def test_write_lkg_uses_os_replace_not_direct_write(self):
        """Confirms the actual mechanism is atomic rename, matching
        encoder_manager.py's own _atomic_write_json precedent -- a
        reader landing mid-write must never see a truncated script."""
        with patch("encoders.services.lkg.os.replace", wraps=lkg.os.replace) as mock_replace:
            lkg.write_lkg("airtap", "x", {})
        self.assertEqual(mock_replace.call_count, 2)  # script + meta


class LkgWithRealEncoderFingerprintIntegrationTests(LkgDirFixtureMixin, TestCase):
    """End-to-end: fingerprint a real Encoder queryset, persist it,
    confirm a later re-fingerprint of an unchanged DB row set matches
    what was stored -- the actual comparison encoder_manager.py's
    bootstrap path (Phase 2L) will perform."""

    def test_matching_configuration_produces_matching_fingerprint(self):
        enc = Encoder.objects.create(
            name="a", protocol="shoutcast2", host="h", port=8000, mount="/4",
            password="secret", format="mp3", bitrate_kbps=192, station_name="s",
        )
        fp1 = lkg.compute_fingerprint("airtap", [enc])
        lkg.write_lkg("airtap", "script", {"fingerprint": fp1})

        # Re-fetch fresh from the DB (simulating a new process reading
        # the same, unchanged row) -- must reproduce the identical fingerprint.
        enc_refetched = Encoder.objects.get(pk=enc.pk)
        fp2 = lkg.compute_fingerprint("airtap", [enc_refetched])
        self.assertEqual(fp1, fp2)
        self.assertEqual(lkg.read_lkg_meta("airtap")["fingerprint"], fp2)

    def test_edited_configuration_produces_different_fingerprint(self):
        enc = Encoder.objects.create(
            name="a", protocol="shoutcast2", host="h", port=8000, mount="/4",
            password="secret", format="mp3", bitrate_kbps=192, station_name="s",
        )
        fp1 = lkg.compute_fingerprint("airtap", [enc])
        lkg.write_lkg("airtap", "script", {"fingerprint": fp1})

        enc.bitrate_kbps = 320
        enc.save()
        fp2 = lkg.compute_fingerprint("airtap", [enc])
        self.assertNotEqual(fp1, fp2)
        self.assertNotEqual(lkg.read_lkg_meta("airtap")["fingerprint"], fp2)
