"""Runtime Foundation E6 -- TTS scratch-surface evidence tests
(/run/isadoraair/tts). Uses only real, resolvable, non-hardcoded
fixture identities (the test process's own real uid/gid via pwd) --
never a hardcoded development username."""

from __future__ import annotations

import os
import pwd
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.runtime_scratch import (
    SCRATCH_DIRECTORY_MODE,
    STATE_ABSENT,
    STATE_HEALTHY,
    STATE_SYMLINK,
    STATE_UNRESOLVED_IDENTITY,
    STATE_UNSAFE_ANCESTRY,
    STATE_UNSAFE_PERMISSIONS,
    STATE_WRONG_OWNER,
    STATE_WRONG_TYPE,
    evaluate_scratch_surface,
    resolve_expected_identity,
)


class ScratchSurfaceFixture(SimpleTestCase):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory(prefix="isadoraair-e6-scratch-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.me = pwd.getpwuid(os.getuid()).pw_name


class ServiceIdentityResolutionTests(ScratchSurfaceFixture):
    def test_none_isa_user_resolves_to_none(self):
        self.assertIsNone(resolve_expected_identity(None))

    def test_empty_string_isa_user_resolves_to_none(self):
        self.assertIsNone(resolve_expected_identity(""))

    def test_unknown_username_resolves_to_none_not_a_crash(self):
        self.assertIsNone(resolve_expected_identity("definitely-not-a-real-user-e6"))

    def test_real_username_resolves_to_real_uid_gid(self):
        identity = resolve_expected_identity(self.me)
        self.assertIsNotNone(identity)
        self.assertEqual(identity[0], os.getuid())

    def test_offline_identity_is_resolved_from_target_passwd_not_host(self):
        target_uid = os.getuid() + 10000
        target_gid = os.getgid() + 10000
        etc = self.root / "etc"
        etc.mkdir()
        (etc / "passwd").write_text(
            f"{self.me}:x:{target_uid}:{target_gid}:Target:/nonexistent:/usr/sbin/nologin\n",
            encoding="utf-8",
        )
        self.assertEqual(
            resolve_expected_identity(self.me, target_root=self.root),
            (target_uid, target_gid),
        )

    def test_offline_identity_refuses_symlinked_target_etc_ancestry(self):
        outside = self.root / "outside-etc"
        outside.mkdir()
        (outside / "passwd").write_text(
            f"{self.me}:x:12345:12346:Outside:/nonexistent:/usr/sbin/nologin\n",
            encoding="utf-8",
        )
        (self.root / "etc").symlink_to(outside)
        self.assertIsNone(resolve_expected_identity(self.me, target_root=self.root))


class ScratchSurfaceEvidenceTests(ScratchSurfaceFixture):
    def test_no_isa_user_is_unresolved_identity_never_guessed_healthy(self):
        scratch = self.root / "tts"
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o700)
        evidence = evaluate_scratch_surface(isa_user=None, path=scratch)
        self.assertEqual(evidence.state, STATE_UNRESOLVED_IDENTITY)
        self.assertFalse(evidence.healthy)

    def test_absent_directory(self):
        evidence = evaluate_scratch_surface(isa_user=self.me, path=self.root / "does-not-exist")
        self.assertEqual(evidence.state, STATE_ABSENT)

    def test_correct_service_owned_0700_is_healthy(self):
        scratch = self.root / "tts"
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o700)
        evidence = evaluate_scratch_surface(isa_user=self.me, path=scratch)
        self.assertEqual(evidence.state, STATE_HEALTHY)
        self.assertTrue(evidence.healthy)

    def test_wrong_owner(self):
        scratch = self.root / "tts"
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o700)
        evidence = evaluate_scratch_surface(isa_user="root", path=scratch)
        # A non-root test process cannot own a root-owned directory --
        # this proves the mismatch is detected, not that root exists.
        self.assertIn(evidence.state, (STATE_WRONG_OWNER, STATE_HEALTHY))
        if os.getuid() != 0:
            self.assertEqual(evidence.state, STATE_WRONG_OWNER)

    def test_unsafe_permissions(self):
        scratch = self.root / "tts"
        scratch.mkdir(mode=0o700)
        os.chmod(scratch, 0o755)
        evidence = evaluate_scratch_surface(isa_user=self.me, path=scratch)
        self.assertEqual(evidence.state, STATE_UNSAFE_PERMISSIONS)

    def test_symlink_is_flagged(self):
        real = self.root / "real-dir"
        real.mkdir(mode=0o700)
        link = self.root / "tts"
        link.symlink_to(real)
        evidence = evaluate_scratch_surface(isa_user=self.me, path=link)
        self.assertEqual(evidence.state, STATE_SYMLINK)

    def test_symlink_parent_is_unsafe_ancestry(self):
        real_parent = self.root / "real-isadoraair"
        real_parent.mkdir()
        (real_parent / "tts").mkdir(mode=0o700)
        (self.root / "run").mkdir()
        (self.root / "run" / "isadoraair").symlink_to(real_parent)
        evidence = evaluate_scratch_surface(
            isa_user="station",
            target_root=self.root,
            expected_uid=os.getuid(),
            expected_gid=os.getgid(),
        )
        self.assertEqual(evidence.state, STATE_UNSAFE_ANCESTRY)
        self.assertFalse(evidence.healthy)

    def test_target_identity_not_same_named_host_identity_controls_ownership(self):
        target_uid = os.getuid() + 10000
        target_gid = os.getgid() + 10000
        (self.root / "etc").mkdir()
        (self.root / "etc" / "passwd").write_text(
            f"{self.me}:x:{target_uid}:{target_gid}:Target:/nonexistent:/usr/sbin/nologin\n",
            encoding="utf-8",
        )
        scratch = self.root / "run" / "isadoraair" / "tts"
        scratch.mkdir(parents=True, mode=0o700)
        scratch.chmod(0o700)
        evidence = evaluate_scratch_surface(
            isa_user=self.me, target_root=self.root
        )
        self.assertEqual(evidence.expected["owner"], target_uid)
        self.assertEqual(evidence.state, STATE_WRONG_OWNER)

    def test_wrong_type_is_flagged(self):
        plain_file = self.root / "tts"
        plain_file.write_text("not a directory", encoding="utf-8")
        evidence = evaluate_scratch_surface(isa_user=self.me, path=plain_file)
        self.assertEqual(evidence.state, STATE_WRONG_TYPE)

    def test_expected_mode_is_0700(self):
        self.assertEqual(SCRATCH_DIRECTORY_MODE, 0o700)

    def test_evidence_never_mutates_the_filesystem(self):
        scratch = self.root / "tts"
        scratch.mkdir(mode=0o755)
        before = scratch.stat().st_mode
        evaluate_scratch_surface(isa_user=self.me, path=scratch)
        self.assertEqual(scratch.stat().st_mode, before)
