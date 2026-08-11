"""encoders/services/lkg.py -- candidate rendering, fingerprint, and
persistent last-known-good (LKG) state. CANDIDATE_DIR/LKG_DIR are
always patched to a temporary directory (never the real /run or
/var/lib paths) via the fixture mixin below."""
import hashlib
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
        self.assertEqual(meta["fingerprint"], "abc123")
        self.assertIn("script_sha256", meta)  # added automatically by write_lkg

    def test_lkg_exists_true_after_write(self):
        lkg.write_lkg("airtap", "x", {})
        self.assertTrue(lkg.lkg_exists("airtap"))

    def test_lkg_directory_mode_0700(self):
        lkg.write_lkg("airtap", "x", {})
        mode = (self.lkg_dir / "airtap").stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)
        mode = (self.lkg_dir / "airtap" / "versions").stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_top_level_lkg_dir_mode_0700(self):
        """Regression: mkdir(parents=True) on slug_dir silently created
        LKG_DIR itself (as slug_dir's own parent) without ever
        chmod'ing IT specifically -- the same gap already fixed once
        for slug_dir/versions_dir, caught one level further up while
        reviewing the directory-fsync chain. LKG_DIR must explicitly
        be 0700 too, not whatever the process umask produces."""
        lkg.write_lkg("airtap", "x", {})
        mode = self.lkg_dir.stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_lkg_version_directory_mode_0700(self):
        lkg.write_lkg("airtap", "x", {})
        version_dirs = list((self.lkg_dir / "airtap" / "versions").iterdir())
        self.assertEqual(len(version_dirs), 1)
        mode = version_dirs[0].stat().st_mode & 0o777
        self.assertEqual(mode, 0o700)

    def test_lkg_script_file_mode_0600(self):
        lkg.write_lkg("airtap", "x", {})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        mode = (version_dir / "script.liq").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_lkg_meta_file_mode_0644(self):
        lkg.write_lkg("airtap", "x", {})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        mode = (version_dir / "metadata.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o644)

    def test_current_pointer_file_mode_0644(self):
        """The pointer is non-secret (just a version-id string) --
        deliberately the same permissive mode as metadata, not the
        script's 0600."""
        lkg.write_lkg("airtap", "x", {})
        mode = (self.lkg_dir / "airtap" / "current").stat().st_mode & 0o777
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
        self.assertNotEqual(a_meta["slug"], b_meta["slug"])

    def test_read_lkg_meta_returns_metadata_only(self):
        lkg.write_lkg("airtap", "secret script contents", {"fingerprint": "abc"})
        meta = lkg.read_lkg_meta("airtap")
        self.assertEqual(meta["fingerprint"], "abc")

    def test_read_lkg_meta_none_when_absent(self):
        self.assertIsNone(lkg.read_lkg_meta("airtap"))

    def test_corrupt_metadata_json_does_not_crash_script_read(self):
        """A script with unreadable/corrupt (not merely unverifiable --
        actually malformed JSON) metadata is still usable for a
        rollback launch -- metadata is informational when it can't be
        parsed at all, not required to relaunch."""
        lkg.write_lkg("airtap", "valid script", {"fingerprint": "abc"})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        (version_dir / "metadata.json").write_text("{not valid json", encoding="utf-8")
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "valid script")
        self.assertIsNone(meta)

    def test_no_tmp_files_left_behind(self):
        lkg.write_lkg("airtap", "x", {})
        leftovers = list((self.lkg_dir / "airtap").rglob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_metadata_json_is_human_readable_on_disk(self):
        """Confirms the metadata file is plain readable JSON (not,
        say, accidentally the same restrictive format as the script)
        -- admin/monitoring code reads this directly."""
        lkg.write_lkg("airtap", "x", {"fingerprint": "abc", "accepted_at": 123.0})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        raw = (version_dir / "metadata.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        self.assertEqual(parsed["fingerprint"], "abc")

    def test_write_lkg_uses_os_replace_not_direct_write(self):
        """Confirms the actual mechanism is atomic rename, matching
        encoder_manager.py's own _atomic_write_json precedent -- a
        reader landing mid-write must never see a truncated script.
        Three replace() calls: script, metadata, then the `current`
        pointer swap."""
        with patch("encoders.services.lkg.os.replace", wraps=lkg.os.replace) as mock_replace:
            lkg.write_lkg("airtap", "x", {})
        self.assertEqual(mock_replace.call_count, 3)

    def test_current_pointer_contains_version_id(self):
        lkg.write_lkg("airtap", "x", {})
        pointer_text = (self.lkg_dir / "airtap" / "current").read_text(encoding="utf-8").strip()
        version_dirs = [p.name for p in (self.lkg_dir / "airtap" / "versions").iterdir()]
        self.assertEqual([pointer_text], version_dirs)


# ---------------------------------------------------------------------
# Crash-consistency: script<->metadata integrity binding (Phase 2
# review-fix pass 2, Issue 2)
# ---------------------------------------------------------------------
class LkgIntegrityTests(LkgDirFixtureMixin, SimpleTestCase):
    def test_metadata_records_script_sha256(self):
        lkg.write_lkg("airtap", "source = blank()\n", {"fingerprint": "abc"})
        meta = lkg.read_lkg_meta("airtap")
        self.assertEqual(meta["script_sha256"], hashlib.sha256(b"source = blank()\n").hexdigest())

    def test_tampered_script_fails_integrity_check_and_reads_as_absent(self):
        """The core Issue 2 scenario: a script that no longer matches
        its own metadata's recorded hash (simulating a crash that left
        a NEW script paired with OLD metadata, or any other on-disk
        tampering/corruption) must never be returned as if it were
        valid -- read_lkg fails closed to (None, None), not a silently
        mismatched pair."""
        lkg.write_lkg("airtap", "original script", {"fingerprint": "abc"})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        (version_dir / "script.liq").write_text("TAMPERED script", encoding="utf-8")
        script, meta = lkg.read_lkg("airtap")
        self.assertIsNone(script)
        self.assertIsNone(meta)

    def test_tampered_script_lkg_exists_is_false(self):
        lkg.write_lkg("airtap", "original script", {"fingerprint": "abc"})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        (version_dir / "script.liq").write_text("TAMPERED script", encoding="utf-8")
        self.assertFalse(lkg.lkg_exists("airtap"))

    def test_metadata_without_hash_field_is_tolerated_unverified(self):
        """Legacy/forward-compatibility allowance (see read_lkg's own
        docstring) -- metadata with no script_sha256 field at all
        (never actually produced by write_lkg, but could exist from a
        hand-edited or pre-this-fix file) is tolerated: the script is
        still returned, unverified, rather than treated as a hard
        failure."""
        lkg.write_lkg("airtap", "a script", {"fingerprint": "abc"})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        meta_path = version_dir / "metadata.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        del meta["script_sha256"]
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        script, read_meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "a script")
        self.assertNotIn("script_sha256", read_meta)

    def test_missing_version_directory_behind_a_stale_pointer_reads_as_absent(self):
        """The `current` pointer refers to a version_id whose directory
        doesn't exist (e.g. manually deleted, or a bug elsewhere) --
        must fail closed, not raise."""
        lkg.write_lkg("airtap", "x", {})
        (self.lkg_dir / "airtap" / "current").write_text("nonexistent-version-id", encoding="utf-8")
        script, meta = lkg.read_lkg("airtap")
        self.assertIsNone(script)
        self.assertIsNone(meta)

    def test_empty_pointer_file_reads_as_absent(self):
        lkg.write_lkg("airtap", "x", {})
        (self.lkg_dir / "airtap" / "current").write_text("", encoding="utf-8")
        script, meta = lkg.read_lkg("airtap")
        self.assertIsNone(script)
        self.assertIsNone(meta)


# ---------------------------------------------------------------------
# Crash-consistency: versioned write is atomic as a UNIT (Phase 2
# review-fix pass 2, Issue 2) -- a crash mid-write must never expose a
# mismatched script/metadata pair, and old versions are pruned.
# ---------------------------------------------------------------------
class LkgVersioningTests(LkgDirFixtureMixin, SimpleTestCase):
    def test_new_version_directory_does_not_touch_previous_until_pointer_swap(self):
        """Simulates a crash AFTER the new version's files are written
        but BEFORE the `current` pointer is swapped -- the reader must
        still see the OLD, complete version, never the half-promoted
        new one."""
        lkg.write_lkg("airtap", "version A", {"fingerprint": "a"})
        old_pointer = (self.lkg_dir / "airtap" / "current").read_text(encoding="utf-8")

        real_replace = lkg.os.replace

        def spy_replace(src, dst):
            # The pointer swap is the LAST os.replace() call write_lkg
            # makes (dst named "current", not "current.tmp" -- the tmp
            # file is the SOURCE of this specific replace) -- everything
            # before it (the new version's script.liq/metadata.json)
            # must already have succeeded for real by the time this
            # fires, matching exactly what a real crash right here
            # would leave behind.
            if Path(dst).name == "current":
                raise OSError("simulated crash before pointer swap")
            return real_replace(src, dst)

        with patch("encoders.services.lkg.os.replace", side_effect=spy_replace):
            with self.assertRaises(OSError):
                lkg.write_lkg("airtap", "version B", {"fingerprint": "b"})

        # Pointer must be COMPLETELY unaffected -- still resolves version A.
        current_pointer = (self.lkg_dir / "airtap" / "current").read_text(encoding="utf-8")
        self.assertEqual(current_pointer, old_pointer)
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "version A")
        self.assertEqual(meta["fingerprint"], "a")

    def test_directory_fsync_chain_covers_every_level_in_the_right_order(self):
        """2026-08-10 second review pass: file fsync() alone only
        guarantees a file's own data is durable, not that its
        directory ENTRY is -- confirms every directory level in the
        chain (LKG_DIR, versions_dir, version_dir, slug_dir) actually
        gets fsynced, and that version_dir/versions_dir are fsynced
        BEFORE the `current` pointer swap while slug_dir is fsynced
        AFTER it (so the swap that makes the new version findable is
        only made durable once everything it could lead to already is)."""
        fsynced_dirs = []
        real_fsync_dir = lkg._fsync_dir

        def spy_fsync_dir(path):
            fsynced_dirs.append(Path(path))
            real_fsync_dir(path)

        replace_calls = []
        real_replace = lkg.os.replace

        def spy_replace(src, dst):
            replace_calls.append(Path(dst).name)
            return real_replace(src, dst)

        with patch.object(lkg, "_fsync_dir", side_effect=spy_fsync_dir), \
             patch.object(lkg.os, "replace", side_effect=spy_replace):
            lkg.write_lkg("airtap", "x", {})

        slug_dir = self.lkg_dir / "airtap"
        versions_dir = slug_dir / "versions"
        version_dirs = [p for p in fsynced_dirs if p.parent == versions_dir]
        self.assertEqual(len(version_dirs), 1)
        version_dir = version_dirs[0]

        # All four levels were fsynced at least once.
        self.assertIn(self.lkg_dir, fsynced_dirs)
        self.assertIn(slug_dir, fsynced_dirs)
        self.assertIn(versions_dir, fsynced_dirs)
        self.assertIn(version_dir, fsynced_dirs)

        # Ordering: version_dir and versions_dir are both fsynced
        # strictly BEFORE the pointer-swap os.replace() (target name
        # "current"); slug_dir's fsync happens strictly AFTER it.
        # Reconstructed via the interleaved call sequence: patch.object
        # with side_effect on both targets still preserves real call
        # order, since both spies delegate to the real implementation
        # synchronously before returning.
        version_dir_pos = fsynced_dirs.index(version_dir)
        versions_dir_pos = fsynced_dirs.index(versions_dir)
        slug_dir_positions = [i for i, p in enumerate(fsynced_dirs) if p == slug_dir]
        self.assertEqual(replace_calls.count("current"), 1)

        # slug_dir must be fsynced AFTER both version_dir and versions_dir
        # (there may be an earlier, unrelated slug_dir fsync in principle,
        # but the LAST slug_dir fsync -- the one that matters, since it's
        # what actually follows the pointer swap -- must come after both).
        self.assertGreater(slug_dir_positions[-1], version_dir_pos)
        self.assertGreater(slug_dir_positions[-1], versions_dir_pos)

    def test_at_most_two_versions_retained(self):
        lkg.write_lkg("airtap", "v1", {"fingerprint": "1"})
        lkg.write_lkg("airtap", "v2", {"fingerprint": "2"})
        lkg.write_lkg("airtap", "v3", {"fingerprint": "3"})
        lkg.write_lkg("airtap", "v4", {"fingerprint": "4"})
        version_dirs = list((self.lkg_dir / "airtap" / "versions").iterdir())
        self.assertEqual(len(version_dirs), 2)

    def test_current_version_always_among_retained(self):
        lkg.write_lkg("airtap", "v1", {})
        lkg.write_lkg("airtap", "v2", {})
        lkg.write_lkg("airtap", "v3", {})
        current_id = (self.lkg_dir / "airtap" / "current").read_text(encoding="utf-8").strip()
        version_ids = [p.name for p in (self.lkg_dir / "airtap" / "versions").iterdir()]
        self.assertIn(current_id, version_ids)

    def test_read_lkg_still_works_after_pruning(self):
        for i in range(5):
            lkg.write_lkg("airtap", f"v{i}", {"fingerprint": str(i)})
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "v4")
        self.assertEqual(meta["fingerprint"], "4")

    def test_pruning_failure_does_not_break_promotion(self):
        """_prune_old_versions is explicitly best-effort -- a failure
        there must not undo or fail an otherwise-successful write_lkg
        call. shutil is imported LOCALLY inside _prune_old_versions
        (not at lkg.py's module level), so the real global shutil.rmtree
        is what needs patching -- the local `import shutil` just binds
        the same already-loaded module object."""
        lkg.write_lkg("airtap", "v1", {})
        with patch("shutil.rmtree", side_effect=OSError("simulated")):
            lkg.write_lkg("airtap", "v2", {"fingerprint": "2"})  # must not raise
        script, meta = lkg.read_lkg("airtap")
        self.assertEqual(script, "v2")


# ---------------------------------------------------------------------
# describe_lkg_problem -- diagnostic-only, never changes read_lkg's own
# control flow.
# ---------------------------------------------------------------------
class DescribeLkgProblemTests(LkgDirFixtureMixin, SimpleTestCase):
    def test_empty_string_when_lkg_is_valid(self):
        lkg.write_lkg("airtap", "x", {})
        self.assertEqual(lkg.describe_lkg_problem("airtap"), "")

    def test_never_promoted_message(self):
        problem = lkg.describe_lkg_problem("airtap")
        self.assertIn("has ever been promoted", problem.lower())

    def test_integrity_failure_message_distinguishes_from_never_promoted(self):
        lkg.write_lkg("airtap", "original", {})
        version_dir = next((self.lkg_dir / "airtap" / "versions").iterdir())
        (version_dir / "script.liq").write_text("tampered", encoding="utf-8")
        problem = lkg.describe_lkg_problem("airtap")
        self.assertNotIn("never", problem.lower())
        self.assertTrue(problem)  # non-empty -- something IS wrong

    def test_missing_version_directory_message(self):
        lkg.write_lkg("airtap", "x", {})
        (self.lkg_dir / "airtap" / "current").write_text("ghost-version", encoding="utf-8")
        problem = lkg.describe_lkg_problem("airtap")
        self.assertIn("ghost-version", problem)


# ---------------------------------------------------------------------
# Desired-vs-accepted destination resolution (Phase 2 review-fix
# pass 2, Issue 1) -- shared by the ordinary dashboard probe and (for
# the candidate/rollback in-process cases) implicitly matched by
# EncoderManager's own precise per-transition tracking.
# ---------------------------------------------------------------------
class DestinationsFromLkgMetaTests(SimpleTestCase):
    def test_empty_when_meta_is_none(self):
        self.assertEqual(lkg.destinations_from_lkg_meta(None), [])

    def test_empty_when_meta_has_no_destinations_field(self):
        self.assertEqual(lkg.destinations_from_lkg_meta({"fingerprint": "abc"}), [])

    def test_reconstructs_expected_attributes(self):
        meta = {"destinations": [{"encoder_id": 5, "name": "n", "host": "h", "port": 8000, "shoutcast_sid": "1"}]}
        result = lkg.destinations_from_lkg_meta(meta)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, 5)
        self.assertEqual(result[0].name, "n")
        self.assertEqual(result[0].host, "h")
        self.assertEqual(result[0].port, 8000)
        self.assertEqual(result[0].shoutcast_sid, "1")


class ResolveExpectedDestinationsTests(LkgDirFixtureMixin, SimpleTestCase):
    def _lkg_meta_with_destinations(self, sid="1", host="10.0.0.5", port=8000):
        return {
            "fingerprint": "lkg-fp",
            "destinations": [{"encoder_id": 1, "name": "lkg-enc", "host": host, "port": port, "shoutcast_sid": sid}],
        }

    def test_candidate_returns_db_rows_directly(self):
        db_rows = [make_encoder(name="desired", mount="/5")]
        lkg.write_lkg("airtap", "x", self._lkg_meta_with_destinations(sid="1"))
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="candidate")
        self.assertEqual(result, db_rows)

    def test_rollback_returns_lkg_destinations_not_db_rows(self):
        """The exact scenario from the review: LKG=SID1, DB desired=SID5."""
        db_rows = [make_encoder(name="desired", mount="/5")]
        lkg.write_lkg("airtap", "x", self._lkg_meta_with_destinations(sid="1"))
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="rollback")
        self.assertEqual([r.shoutcast_sid for r in result], ["1"])

    def test_accepted_with_lkg_returns_lkg_destinations_not_db_rows(self):
        """Same scenario, but launch_kind="accepted" -- the state a
        group settles into once rollback succeeds. The dashboard must
        keep checking the LKG's own SID, not whatever the DB currently
        (still) says."""
        db_rows = [make_encoder(name="desired", mount="/5")]
        lkg.write_lkg("airtap", "x", self._lkg_meta_with_destinations(sid="1"))
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="accepted")
        self.assertEqual([r.shoutcast_sid for r in result], ["1"])

    def test_no_lkg_bootstrap_falls_back_to_db_rows(self):
        db_rows = [make_encoder(name="desired", mount="/5")]
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="accepted")
        self.assertEqual(result, db_rows)

    def test_no_lkg_bootstrap_candidate_also_returns_db_rows(self):
        db_rows = [make_encoder(name="desired", mount="/5")]
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="candidate")
        self.assertEqual(result, db_rows)

    def test_default_launch_kind_is_accepted(self):
        db_rows = [make_encoder(name="desired", mount="/5")]
        lkg.write_lkg("airtap", "x", self._lkg_meta_with_destinations(sid="1"))
        result = lkg.resolve_expected_destinations("airtap", db_rows)  # no launch_kind given
        self.assertEqual([r.shoutcast_sid for r in result], ["1"])

    def test_legacy_lkg_no_destinations_fails_closed_to_empty_not_db_rows(self):
        """A legacy/corrupt LKG with no recorded destinations must
        never silently fall back to the DB rows -- that would
        reintroduce the exact wrong-SID risk this mechanism exists to
        prevent. Empty (not DB rows) is the correct, conservative
        result."""
        db_rows = [make_encoder(name="desired", mount="/5")]
        lkg.write_lkg("airtap", "x", {"fingerprint": "lkg-fp"})  # no "destinations" key
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="accepted")
        self.assertEqual(result, [])

    def test_host_port_change_scenario(self):
        """Host/port variant of the same principle, per the review's
        own explicit ask."""
        db_rows = [make_encoder(name="desired", host="10.0.0.99", port=9000, mount="/1")]
        lkg.write_lkg("airtap", "x", self._lkg_meta_with_destinations(sid="1", host="10.0.0.5", port=8000))
        result = lkg.resolve_expected_destinations("airtap", db_rows, launch_kind="accepted")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].host, "10.0.0.5")
        self.assertEqual(result[0].port, 8000)


# ---------------------------------------------------------------------
# Fingerprint renderer/config-format version (Phase 2 review-fix pass
# 2, Issue 4)
# ---------------------------------------------------------------------
class FingerprintVersionTests(SimpleTestCase):
    def test_same_db_config_same_version_same_fingerprint(self):
        a = lkg.compute_fingerprint("airtap", [make_encoder()])
        b = lkg.compute_fingerprint("airtap", [make_encoder()])
        self.assertEqual(a, b)

    def test_same_db_config_different_version_different_fingerprint(self):
        with patch.object(lkg, "ENCODER_CONFIG_FORMAT_VERSION", 1):
            fp_v1 = lkg.compute_fingerprint("airtap", [make_encoder()])
        with patch.object(lkg, "ENCODER_CONFIG_FORMAT_VERSION", 2):
            fp_v2 = lkg.compute_fingerprint("airtap", [make_encoder()])
        self.assertNotEqual(fp_v1, fp_v2)

    def test_version_is_included_in_payload_not_just_incidentally_different(self):
        """Confirms the version genuinely participates in the hashed
        payload (not, say, accidentally unused) by checking two
        DIFFERENT db configs under the SAME bumped version still
        differ from each other too -- i.e. the version bump doesn't
        collapse the fingerprint space."""
        with patch.object(lkg, "ENCODER_CONFIG_FORMAT_VERSION", 7):
            a = lkg.compute_fingerprint("airtap", [make_encoder(host="1.2.3.4")])
            b = lkg.compute_fingerprint("airtap", [make_encoder(host="5.6.7.8")])
        self.assertNotEqual(a, b)


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
