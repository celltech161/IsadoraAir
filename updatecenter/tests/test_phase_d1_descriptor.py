"""D1-B: protected_bootstrap.descriptor -- the runtime bundle inventory
schema, its deterministic aggregate digest, and on-disk verification."""
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # noqa: F401 -- import triggers sys.path setup

from protected_bootstrap.descriptor import (
    DescriptorError, FileEntry, compute_bundle_sha256, generation_advances, hash_file,
    parse_descriptor_dict, validate_relative_path, verify_descriptor_against_directory,
)


def _valid_descriptor_dict(**overrides):
    files = [
        {"path": "isadoraair_updater/release.py", "sha256": "a" * 64, "mode": "0644", "size_bytes": 100},
        {"path": "protected-policy.json", "sha256": "b" * 64, "mode": "0644", "size_bytes": 50},
        {"path": "updaterd.py", "sha256": "c" * 64, "mode": "0755", "size_bytes": 200},
    ]
    files.sort(key=lambda entry: entry["path"])
    entries = tuple(FileEntry(f["path"], f["sha256"], f["mode"], f["size_bytes"]) for f in files)
    data = {
        "schema_version": 1,
        "generation": 1,
        "runtime_version": 5,
        "manifest_protocol_version": 5,
        "supported_wire_protocols": [3],
        "entrypoint": "updaterd.py",
        "files": files,
        "bundle_sha256": compute_bundle_sha256(entries),
    }
    data.update(overrides)
    return data


class ValidRelativePathTests(SimpleTestCase):
    def test_plain_relative_path_accepted(self):
        self.assertEqual(validate_relative_path("a/b/c.py", field="x"), "a/b/c.py")

    def test_absolute_path_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("/etc/passwd", field="x")

    def test_dotdot_segment_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("a/../b", field="x")

    def test_leading_dotdot_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("../b", field="x")

    def test_backslash_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("a\\b", field="x")

    def test_control_character_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("a\x00b", field="x")

    def test_empty_segment_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("a//b", field="x")

    def test_leading_slash_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("/a", field="x")

    def test_trailing_slash_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("a/", field="x")

    def test_empty_string_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path("", field="x")

    def test_non_string_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_relative_path(123, field="x")


class BundleDigestDeterminismTests(SimpleTestCase):
    def test_same_files_same_digest(self):
        entries = (
            FileEntry("a.py", "1" * 64, "0644", 10),
            FileEntry("b.py", "2" * 64, "0644", 20),
        )
        self.assertEqual(compute_bundle_sha256(entries), compute_bundle_sha256(entries))

    def test_different_content_different_digest(self):
        a = (FileEntry("a.py", "1" * 64, "0644", 10),)
        b = (FileEntry("a.py", "2" * 64, "0644", 10),)
        self.assertNotEqual(compute_bundle_sha256(a), compute_bundle_sha256(b))

    def test_mode_change_changes_digest(self):
        a = (FileEntry("a.py", "1" * 64, "0644", 10),)
        b = (FileEntry("a.py", "1" * 64, "0755", 10),)
        self.assertNotEqual(compute_bundle_sha256(a), compute_bundle_sha256(b))

    def test_order_sensitive_by_design(self):
        # compute_bundle_sha256 hashes in the given order without
        # re-sorting -- parse_descriptor_dict is what enforces order.
        a = (FileEntry("a.py", "1" * 64, "0644", 10), FileEntry("b.py", "2" * 64, "0644", 20))
        b = (FileEntry("b.py", "2" * 64, "0644", 20), FileEntry("a.py", "1" * 64, "0644", 10))
        self.assertNotEqual(compute_bundle_sha256(a), compute_bundle_sha256(b))


class ParseDescriptorDictTests(SimpleTestCase):
    def test_valid_descriptor_parses(self):
        descriptor = parse_descriptor_dict(_valid_descriptor_dict())
        self.assertEqual(descriptor.generation, 1)
        self.assertEqual(descriptor.entrypoint, "updaterd.py")
        self.assertEqual(len(descriptor.files), 3)

    def test_unknown_top_level_field_rejected(self):
        data = _valid_descriptor_dict()
        data["extra_field"] = "nope"
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_missing_top_level_field_rejected(self):
        data = _valid_descriptor_dict()
        del data["entrypoint"]
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_unsupported_schema_version_rejected(self):
        data = _valid_descriptor_dict(schema_version=2)
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_generation_zero_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(generation=0))

    def test_generation_negative_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(generation=-1))

    def test_generation_over_bound_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(generation=99_999_999))

    def test_wire_protocols_must_be_sorted(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(supported_wire_protocols=[4, 3]))

    def test_wire_protocols_duplicate_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(supported_wire_protocols=[3, 3]))

    def test_wire_protocols_empty_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(supported_wire_protocols=[]))

    def test_wire_protocols_too_many_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(supported_wire_protocols=list(range(1, 20))))

    def test_files_empty_rejected(self):
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(_valid_descriptor_dict(files=[]))

    def test_files_unsorted_rejected(self):
        data = _valid_descriptor_dict()
        data["files"] = list(reversed(data["files"]))
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_duplicate_path_rejected(self):
        data = _valid_descriptor_dict()
        data["files"].append(dict(data["files"][0]))
        data["files"].sort(key=lambda e: e["path"])
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_bad_sha256_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["sha256"] = "not-hex"
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_uppercase_sha256_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["sha256"] = data["files"][0]["sha256"].upper()
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_disallowed_mode_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["mode"] = "0777"
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_negative_size_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["size_bytes"] = -1
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_oversized_file_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["size_bytes"] = 999_999_999
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_entrypoint_must_be_in_files(self):
        data = _valid_descriptor_dict(entrypoint="missing.py")
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_entrypoint_must_be_mode_0755(self):
        data = _valid_descriptor_dict()
        for entry in data["files"]:
            if entry["path"] == "updaterd.py":
                entry["mode"] = "0644"
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_bundle_sha256_mismatch_rejected(self):
        data = _valid_descriptor_dict(bundle_sha256="f" * 64)
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_unknown_file_entry_field_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["extra"] = "nope"
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)

    def test_symlink_style_path_still_just_a_string_but_traversal_rejected(self):
        data = _valid_descriptor_dict()
        data["files"][0]["path"] = "../outside.py"
        with self.assertRaises(DescriptorError):
            parse_descriptor_dict(data)


class GenerationAdvancesTests(SimpleTestCase):
    def test_first_generation_must_be_one(self):
        self.assertTrue(generation_advances(1, None))
        self.assertFalse(generation_advances(2, None))

    def test_strictly_increasing_required(self):
        self.assertTrue(generation_advances(4, 3))
        self.assertFalse(generation_advances(3, 3))
        self.assertFalse(generation_advances(2, 3))

    def test_skip_is_allowed(self):
        self.assertTrue(generation_advances(7, 3))


class VerifyDescriptorAgainstDirectoryTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _write(self, relative, content: bytes, mode=0o644):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def _descriptor_for(self, files: dict[str, tuple[bytes, str]], entrypoint: str):
        entries = []
        for path, (content, mode) in files.items():
            entries.append({
                "path": path, "sha256": hash_file(self._write(path, content, int(mode, 8))),
                "mode": mode, "size_bytes": len(content),
            })
        entries.sort(key=lambda e: e["path"])
        entry_objs = tuple(FileEntry(e["path"], e["sha256"], e["mode"], e["size_bytes"]) for e in entries)
        data = {
            "schema_version": 1, "generation": 1, "runtime_version": 1,
            "manifest_protocol_version": 1, "supported_wire_protocols": [3],
            "entrypoint": entrypoint, "files": entries,
            "bundle_sha256": compute_bundle_sha256(entry_objs),
        }
        return parse_descriptor_dict(data)

    def test_exact_match_no_mismatches(self):
        descriptor = self._descriptor_for(
            {"updaterd.py": (b"print(1)\n", "0755"), "lib.py": (b"x = 1\n", "0644")},
            entrypoint="updaterd.py",
        )
        self.assertEqual(verify_descriptor_against_directory(descriptor, self.root), ())

    def test_missing_file_reported(self):
        descriptor = self._descriptor_for({"updaterd.py": (b"x\n", "0755")}, entrypoint="updaterd.py")
        (self.root / "updaterd.py").unlink()
        reasons = verify_descriptor_against_directory(descriptor, self.root)
        self.assertTrue(any("missing" in r for r in reasons))

    def test_extra_file_reported(self):
        descriptor = self._descriptor_for({"updaterd.py": (b"x\n", "0755")}, entrypoint="updaterd.py")
        (self.root / "sneaky.py").write_text("evil")
        reasons = verify_descriptor_against_directory(descriptor, self.root)
        self.assertTrue(any("not declared" in r for r in reasons))

    def test_hash_mismatch_same_size_reported(self):
        # Same byte LENGTH as the original ("x\n") so this exercises the
        # hash-comparison branch specifically, not the size-mismatch one.
        descriptor = self._descriptor_for({"updaterd.py": (b"x\n", "0755")}, entrypoint="updaterd.py")
        (self.root / "updaterd.py").write_bytes(b"y\n")
        reasons = verify_descriptor_against_directory(descriptor, self.root)
        self.assertTrue(any("sha256" in r for r in reasons))

    def test_mode_mismatch_reported(self):
        descriptor = self._descriptor_for({"updaterd.py": (b"x\n", "0755")}, entrypoint="updaterd.py")
        (self.root / "updaterd.py").chmod(0o644)
        reasons = verify_descriptor_against_directory(descriptor, self.root)
        self.assertTrue(any("mode" in r for r in reasons))

    def test_symlink_reported_not_followed(self):
        descriptor = self._descriptor_for({"updaterd.py": (b"x\n", "0755")}, entrypoint="updaterd.py")
        (self.root / "updaterd.py").unlink()
        (self.root / "real.py").write_text("x")
        (self.root / "updaterd.py").symlink_to(self.root / "real.py")
        reasons = verify_descriptor_against_directory(descriptor, self.root)
        self.assertTrue(any("symlink" in r for r in reasons))
