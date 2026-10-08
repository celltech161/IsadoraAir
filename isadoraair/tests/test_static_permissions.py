"""r0108: collected static files stay servable by nginx whatever the umask.

r0107 on KOGR: Update Center ran collectstatic under the updater's
UMask=0077, the new ``production/iportal/`` directory was created 0700 and
nginx answered 403 (``weather/css`` and ``weather/js`` had been silently
broken the same way since an earlier release). The staticfiles backend
(isadoraair.static_storage.StaticRootStorage) now creates with explicit modes
and repairs the whole STATIC_ROOT tree after every collectstatic run.

Every test runs the REAL collectstatic command over the project's real static
sources, under umask 0077, and judges the result as nginx would: as an
unrelated account, through the "other" permission bits only.
"""
import contextlib
import os
import stat
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ImproperlyConfigured
from django.core.files.storage import default_storage, storages
from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

from isadoraair import static_storage

# Nested application static directories that exist in this release.
NESTED = ("production/iportal", "weather/css", "weather/js", "admin/js/admin")


def mode(path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def nginx_unservable(root: Path) -> list[str]:
    """Everything an unrelated account (nginx) could not reach or read, judged
    from STATIC_ROOT down: directories need r-x and files r-- for "other"."""
    problems = []
    if mode(root) & 0o005 != 0o005:
        problems.append(str(root))
    for directory, dirnames, filenames in os.walk(root):
        for name in dirnames:
            path = os.path.join(directory, name)
            if not os.path.islink(path) and mode(path) & 0o005 != 0o005:
                problems.append(path)
        for name in filenames:
            path = os.path.join(directory, name)
            if not os.path.islink(path) and mode(path) & 0o004 != 0o004:
                problems.append(path)
    return problems


class StaticPermissionTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="static-perms-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        # STATIC_ROOT and its parent do not exist yet: a fresh install.
        self.static_root = self.base / "app" / "staticfiles"
        override = override_settings(STATIC_ROOT=str(self.static_root))
        override.enable()
        self.addCleanup(override.disable)
        old = os.umask(0o077)                           # the updater's UMask
        self.addCleanup(os.umask, old)

    def collect(self, *args):
        call_command("collectstatic", "--noinput", *args, stdout=StringIO(), stderr=StringIO())

    def assert_servable(self):
        self.assertEqual(nginx_unservable(self.static_root), [])
        for nested in NESTED:
            with self.subTest(nested=nested):
                self.assertTrue((self.static_root / nested).is_dir(), nested)

    # -- configuration scope ---------------------------------------------------------
    def test_only_the_staticfiles_backend_is_changed(self):
        self.assertIsInstance(storages["staticfiles"], static_storage.StaticRootStorage)
        # No post-create chmod BY PATH: modes come from the creation umask and
        # the descriptor-based reconcile (r0108 corrective).
        self.assertEqual((staticfiles_storage.file_permissions_mode, staticfiles_storage.directory_permissions_mode),
                         (None, None))
        self.assertEqual(staticfiles_storage.location, str(self.static_root))
        self.assertEqual(settings.STORAGES["default"]["BACKEND"], "django.core.files.storage.FileSystemStorage")
        self.assertNotIn("OPTIONS", settings.STORAGES["default"])
        # The global upload modes stay Django's defaults: uploads, reports and
        # every other storage are untouched by the static fix.
        self.assertIsNone(settings.FILE_UPLOAD_DIRECTORY_PERMISSIONS)
        self.assertEqual(settings.FILE_UPLOAD_PERMISSIONS, 0o644)
        self.assertIsNone(default_storage.directory_permissions_mode)

    # -- fresh install -----------------------------------------------------------------
    def test_a_nonexistent_static_root_is_created_servable_under_umask_0077(self):
        self.assertFalse(self.static_root.parent.exists())
        self.collect()
        self.assertEqual(mode(self.static_root), 0o755)
        self.assertEqual(mode(self.static_root / "production" / "iportal"), 0o755)
        self.assertEqual(mode(self.static_root / "production" / "iportal" / "workstation.js"), 0o644)
        self.assert_servable()

    def test_nothing_outside_static_root_is_broadened(self):
        private = self.base / "private"                  # e.g. .env, sockets, backup material
        private.mkdir(mode=0o700)
        secret = private / "secret"
        secret.write_text("x")
        secret.chmod(0o600)
        outside_target = self.base / "outside"
        outside_target.mkdir(mode=0o700)
        (outside_target / "file").write_text("x")
        (outside_target / "file").chmod(0o600)
        self.collect()
        # STATIC_ROOT's own parent was created by makedirs for the storage
        # (an intermediate of the location); the operator's private tree,
        # by contrast, is never visited.
        (self.static_root / "link-out").symlink_to(outside_target)        # a planted symlink is not followed
        self.collect()
        self.assertEqual(mode(private), 0o700)
        self.assertEqual(mode(secret), 0o600)
        self.assertEqual(mode(outside_target), 0o700)
        self.assertEqual(mode(outside_target / "file"), 0o600)
        self.assertEqual(mode(self.base), 0o700)                         # the temp base itself
        self.assert_servable()

    def test_production_media_directories_keep_their_own_restrictive_mode(self):
        from production.services import layout
        root = self.base / "production-media"
        with override_settings(PRODUCTION_MEDIA_ROOT=str(root)):
            self.collect()
            layout.ensure_layout()
        self.assertEqual(mode(root), 0o750)
        for name in ("media", "incoming", "work", "locks"):
            self.assertEqual(mode(root / name), 0o750, name)

    # -- upgrade repair --------------------------------------------------------------------
    def test_an_existing_tree_with_0700_directories_is_repaired_on_the_next_run(self):
        """The KOGR r0107 shape: files already collected (so collectstatic skips
        them as unmodified), their directories left 0700 / files 0600."""
        self.collect()
        broken = ["production", "production/iportal", "weather", "weather/css", "weather/js"]
        for name in broken:
            (self.static_root / name).chmod(0o700)
        (self.static_root / "weather" / "css").joinpath("stale-only-here").mkdir(mode=0o700)
        js = self.static_root / "production" / "iportal" / "workstation.js"
        js.chmod(0o600)
        self.static_root.chmod(0o700)
        self.assertNotEqual(nginx_unservable(self.static_root), [])
        self.collect()
        self.assertEqual(mode(self.static_root), 0o755)
        for name in broken:
            self.assertEqual(mode(self.static_root / name), 0o755, name)
        self.assertEqual(mode(self.static_root / "weather" / "css" / "stale-only-here"), 0o755)
        self.assertEqual(mode(js), 0o644)
        self.assert_servable()

    def test_repair_only_adds_bits(self):
        self.collect()
        wider_dir = self.static_root / "weather"
        wider_dir.chmod(0o775)
        wider_file = self.static_root / "weather" / "css" / os.listdir(self.static_root / "weather" / "css")[0]
        wider_file.chmod(0o664)
        self.collect()
        self.assertEqual(mode(wider_dir), 0o775)
        self.assertEqual(mode(wider_file), 0o664)

    def test_collectstatic_link_mode_directories_are_repaired_too(self):
        """--link creates directories itself (bypassing the storage's modes)."""
        self.collect("--link")
        self.assertTrue((self.static_root / "production" / "iportal" / "workstation.js").is_symlink())
        self.assert_servable()

    def test_a_dry_run_changes_nothing(self):
        self.collect()
        (self.static_root / "weather").chmod(0o700)
        self.collect("--dry-run")
        self.assertEqual(mode(self.static_root / "weather"), 0o700)

    # -- refusals ----------------------------------------------------------------------------
    def test_an_entry_that_cannot_be_repaired_fails_the_run_loudly(self):
        self.collect()
        (self.static_root / "weather").chmod(0o700)
        with mock.patch.object(static_storage.os, "fchmod", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaises(static_storage.StaticPermissionError) as raised:
                self.collect()
        self.assertIn("weather", str(raised.exception))

    def test_a_symlinked_static_root_is_refused(self):
        real = self.base / "real-static"
        real.mkdir(mode=0o700)
        self.static_root.parent.mkdir(parents=True)
        self.static_root.symlink_to(real)
        with self.assertRaises(ImproperlyConfigured):
            static_storage.reconcile_static_permissions(self.static_root)
        self.assertEqual(mode(real), 0o700)

    def test_reconcile_of_a_missing_root_is_a_no_op(self):
        self.assertEqual(static_storage.reconcile_static_permissions(self.static_root), [])
        self.assertFalse(self.static_root.exists())


# -- r0108 corrective: path-substitution races (Codex) --------------------------------

class Attacker:
    """Deterministically swaps ONE pathname between its real object and a
    symlink to an external target, around the reconciler's own filesystem
    calls -- the attacker wins every race window the code under test leaves.
    A context manager: the interposition lasts for one reconcile only.

    schedule
      "pre"     -- already a symlink before the reconcile starts;
      "sticky"  -- real for the first call naming it, a symlink ever after
                   (looked at, then replaced);
      "flip"    -- real whenever it is CHECKED (lstat/stat/open), a symlink
                   immediately after every check -- so every later USE of the
                   pathname (chmod, a walk, a listing) meets the symlink;
      "listing" -- replaced right after the reconcile's first directory
                   listing (the root has been opened and enumerated).
    The real object is kept at ``<path>.real`` while the symlink stands."""

    CHECKS = ("lstat", "stat", "open")
    USES = ("chmod", "scandir", "listdir")

    def __init__(self, target: Path, external: Path, schedule: str):
        self.target, self.external, self.schedule = target, external, schedule
        self.real = target.with_name(target.name + ".real")
        self.seen = self.listed = False
        self.patchers = []

    def __enter__(self):
        self._orig = {name: getattr(os, name) for name in self.CHECKS + self.USES}
        if self.schedule == "pre":
            self.to_symlink()
        wrappers = {name: self._wrap(name) for name in self.CHECKS + self.USES}
        self.patchers = [mock.patch.object(os, name, wrapper) for name, wrapper in wrappers.items()]
        # The wrappers stand in for the real functions' descriptor support.
        for attr in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks"):
            current = getattr(os, attr)
            extra = {wrappers[name] for name in wrappers if self._orig[name] in current}
            self.patchers.append(mock.patch.object(os, attr, current | extra))
        for patcher in self.patchers:
            patcher.start()
        return self

    def __exit__(self, *exc):
        for patcher in reversed(self.patchers):
            patcher.stop()
        return False

    def _is_link(self):
        try:
            return stat.S_ISLNK(self._orig["lstat"](self.target).st_mode)
        except FileNotFoundError:
            return False

    def to_symlink(self):
        if not self._is_link():
            os.rename(self.target, self.real)
            os.symlink(self.external, self.target)

    def to_real(self):
        if self._is_link():
            os.unlink(self.target)
            os.rename(self.real, self.target)

    def _names_target(self, args):
        if not args or isinstance(args[0], int):
            return False
        try:
            return os.path.basename(os.fsdecode(os.fspath(args[0]))) == self.target.name
        except TypeError:
            return False

    def _wrap(self, name):
        original = self._orig[name]

        def call(*args, **kwargs):
            mine = self._names_target(args)
            if mine and self.schedule == "flip" and name in self.CHECKS:
                self.to_real()
            try:
                return original(*args, **kwargs)
            finally:
                if mine and self.schedule == "flip" and name in self.CHECKS:
                    self.to_symlink()
                if mine and self.schedule == "sticky" and not self.seen:
                    self.seen = True
                    self.to_symlink()
                if name in ("listdir", "scandir") and self.schedule == "listing" and not self.listed:
                    self.listed = True
                    self.to_symlink()
        return call

    @property
    def original(self) -> Path:
        """Where the real object is now."""
        return self.real if os.path.lexists(self.real) else self.target


def _chmod_without_nofollow():
    """os.chmod as on a platform with no no-follow chmod (the condition that
    sent 5707522 into its lstat()+chmod(path) fallback)."""
    real_chmod = os.chmod

    def chmod(path, mode_, *, dir_fd=None, follow_symlinks=True):
        if not follow_symlinks:
            raise NotImplementedError("chmod: follow_symlinks unavailable on this platform")
        return real_chmod(path, mode_, dir_fd=dir_fd)
    return mock.patch.object(os, "chmod", chmod)


class StaticSubstitutionRaceTests(SimpleTestCase):
    """Codex reproduced both escapes against 5707522: the root swapped for a
    symlink between its lstat() and os.walk(), and a leaf swapped between the
    fallback's lstat() and chmod(path) -- each widened an external 0600 file to
    0644. In every test the external tree must be untouched, whatever the
    attacker's schedule."""

    SCHEDULES = ("pre", "sticky", "flip", "listing")

    def build(self):
        tmp = tempfile.TemporaryDirectory(prefix="static-race-")
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.root = base / "app" / "staticroot"
        (self.root / "nested").mkdir(parents=True)
        for path, content in (("top.css", "a"), ("nested/deep.css", "b"), ("nested/leaf.css", "c")):
            (self.root / path).write_text(content)
            (self.root / path).chmod(0o600)
        (self.root / "nested").chmod(0o700)
        self.root.chmod(0o700)
        # Private material outside STATIC_ROOT (.env, sockets, backups, media).
        self.external = base / "external"
        (self.external / "sub").mkdir(parents=True)
        (self.external / "secret").write_text("s")
        (self.external / "secret").chmod(0o600)
        (self.external / "sub").chmod(0o700)
        self.external.chmod(0o700)
        self.external_file = base / "external-file"
        self.external_file.write_text("s")
        self.external_file.chmod(0o600)
        # An outside tree with the SAME names as the static tree: a path-based
        # walk through a swapped pathname would find (and widen) these.
        self.mirror = base / "mirror"
        (self.mirror / "nested").mkdir(parents=True)
        for name in ("top.css", "nested/deep.css", "nested/leaf.css"):
            (self.mirror / name).write_text("m")
            (self.mirror / name).chmod(0o600)
        (self.mirror / "nested").chmod(0o700)
        self.mirror.chmod(0o700)
        self.private = {self.external: 0o700, self.external / "secret": 0o600, self.external / "sub": 0o700,
                        self.external_file: 0o600, self.mirror: 0o700, self.mirror / "top.css": 0o600,
                        self.mirror / "nested": 0o700, self.mirror / "nested" / "deep.css": 0o600,
                        self.mirror / "nested" / "leaf.css": 0o600}

    def assert_external_untouched(self):
        self.assertEqual({path: mode(path) for path in self.private}, self.private)

    def attack(self, target, external, schedule):
        with Attacker(target, external, schedule) as attacker:
            try:
                static_storage.reconcile_static_permissions(self.root)
            except ImproperlyConfigured:
                return attacker, "refused"
        return attacker, "ran"

    # A. the root, replaced after it was first looked at
    def test_a_root_substitution_never_reaches_the_external_tree(self):
        for schedule in ("pre", "sticky", "flip"):
            with self.subTest(schedule=schedule):
                self.build()
                attacker, outcome = self.attack(self.root, self.mirror, schedule)
                self.assert_external_untouched()
                if schedule == "pre":
                    self.assertEqual(outcome, "refused")              # a symlinked root is never followed
                else:                                                 # only the pinned original was reconciled
                    self.assertEqual(outcome, "ran")
                    self.assertEqual((mode(attacker.original), mode(attacker.original / "top.css")),
                                     (0o755, 0o644))

    # B. a nested directory, replaced after its parent was enumerated / it was checked
    def test_b_nested_directory_substitution_never_reaches_the_external_tree(self):
        for schedule in self.SCHEDULES:
            with self.subTest(schedule=schedule):
                self.build()
                attacker, outcome = self.attack(self.root / "nested", self.mirror / "nested", schedule)
                self.assertEqual(outcome, "ran")
                self.assert_external_untouched()
                self.assertEqual((mode(self.root), mode(self.root / "top.css")), (0o755, 0o644))
                if schedule in ("sticky", "flip"):            # opened before the swap: the original is done
                    self.assertEqual((mode(attacker.original), mode(attacker.original / "deep.css")),
                                     (0o755, 0o644))

    # C. a leaf file, replaced between discovery and the permission change
    def test_c_leaf_substitution_never_reaches_the_external_file(self):
        for schedule in self.SCHEDULES:
            for nofollow_chmod in ("available", "unavailable"):
                with self.subTest(schedule=schedule, nofollow_chmod=nofollow_chmod):
                    self.build()
                    if nofollow_chmod == "unavailable":
                        with _chmod_without_nofollow():
                            attacker, _ = self.attack(self.root / "nested" / "leaf.css", self.external_file, schedule)
                    else:
                        attacker, _ = self.attack(self.root / "nested" / "leaf.css", self.external_file, schedule)
                    self.assert_external_untouched()
                    if schedule in ("sticky", "flip"):
                        self.assertEqual(mode(attacker.original), 0o644)   # the pinned file got the bits

    # D. the root pathname replaced AFTER the root descriptor is pinned
    def test_d_root_replaced_after_pinning_only_the_pinned_directory_changes(self):
        self.build()
        attacker, outcome = self.attack(self.root, self.mirror, "listing")
        self.assertEqual(outcome, "ran")
        self.assertTrue(self.root.is_symlink())                     # the swap really happened mid-run
        self.assert_external_untouched()
        pinned = attacker.original
        self.assertEqual((mode(pinned), mode(pinned / "nested"), mode(pinned / "nested" / "deep.css")),
                         (0o755, 0o755, 0o644))

    # E. no safe mechanism -> explicit failure, never a path chmod
    def test_e_unsupported_mechanisms_fail_closed_without_any_path_chmod(self):
        no_fchmod = mock.Mock(side_effect=NotImplementedError("fchmod unavailable"))
        cases = {
            "fchmod unsupported": [mock.patch.object(os, "fchmod", no_fchmod)],
            "fchmod and no-follow chmod unsupported": [mock.patch.object(os, "fchmod", no_fchmod),
                                                       _chmod_without_nofollow()],
            "no descriptor-relative open": [mock.patch.object(os, "supports_dir_fd", frozenset())],
            "no fd listing": [mock.patch.object(os, "supports_fd", frozenset())],
            "no O_NOFOLLOW": [mock.patch.dict(os.__dict__), "drop O_NOFOLLOW"],
        }
        for label, patches in cases.items():
            with self.subTest(label):
                self.build()
                (self.root / "link-out").symlink_to(self.external_file)
                tree = (self.root, self.root / "nested", self.root / "top.css")
                before = {path: mode(path) for path in tree}
                path_chmods = []
                real_chmod = os.chmod
                with contextlib.ExitStack() as stack:
                    for patch in patches:
                        if patch == "drop O_NOFOLLOW":
                            del os.O_NOFOLLOW
                        else:
                            stack.enter_context(patch)
                    current_chmod = os.chmod

                    def spy(path, mode_, *, dir_fd=None, follow_symlinks=True):
                        if follow_symlinks:
                            path_chmods.append(path)
                        return current_chmod(path, mode_, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
                    stack.enter_context(mock.patch.object(os, "chmod", spy))
                    with self.assertRaises(static_storage.StaticPermissionError):
                        static_storage.reconcile_static_permissions(self.root)
                self.assertIs(os.chmod, real_chmod)
                self.assertTrue(hasattr(os, "O_NOFOLLOW"))
                self.assertEqual(path_chmods, [])                          # never chmod(path)
                self.assert_external_untouched()
                if not label.startswith("fchmod"):                        # refused before touching anything
                    self.assertEqual({path: mode(path) for path in tree}, before)


class StaticCreationTests(SimpleTestCase):
    """The creation side: collectstatic itself never changes a mode, owner or
    group BY PATH (Django's post-create chmod/chown is a check/use race), yet
    what it creates is servable even without the post-process step."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="static-create-")
        self.addCleanup(tmp.cleanup)
        self.static_root = Path(tmp.name) / "app" / "staticfiles"
        override = override_settings(STATIC_ROOT=str(self.static_root))
        override.enable()
        self.addCleanup(override.disable)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def collect(self, *args):
        call_command("collectstatic", "--noinput", *args, stdout=StringIO(), stderr=StringIO())

    def test_collectstatic_makes_no_path_based_metadata_change(self):
        calls = []
        real = {name: getattr(os, name) for name in ("chmod", "chown", "lchown")}

        def spy(name):
            def call(*args, **kwargs):
                calls.append((name, args[0]))
                return real[name](*args, **kwargs)
            return call
        with mock.patch.object(os, "chmod", spy("chmod")), mock.patch.object(os, "chown", spy("chown")), \
                mock.patch.object(os, "lchown", spy("lchown")):
            self.collect()                                     # fresh tree
            for name in ("production", "weather/css"):
                real["chmod"](self.static_root / name, 0o700)          # damage (not counted)
            calls.clear()
            self.collect()                                     # repair of an existing 0700 tree
        self.assertEqual(calls, [])
        self.assertEqual(nginx_unservable(self.static_root), [])

    def test_created_entries_are_servable_even_without_post_process(self):
        self.collect("--no-post-process")
        self.assertEqual(nginx_unservable(self.static_root), [])
        self.assertEqual(mode(self.static_root / "production" / "iportal"), 0o755)
        self.assertEqual(mode(self.static_root / "production" / "iportal" / "workstation.js"), 0o644)
        self.assertEqual(os.umask(0o077), 0o077)                    # the caller's umask is restored
