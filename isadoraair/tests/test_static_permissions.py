"""r0108: collected static files stay servable by nginx, and no collectstatic
destination operation can be steered outside STATIC_ROOT.

r0107 on KOGR: Update Center ran collectstatic under the updater's
UMask=0077, the new ``production/iportal/`` directory was created 0700 and
nginx answered 403. The staticfiles backend (isadoraair.static_storage) now
creates 0755/0644 on opened descriptors and repairs the tree after each run.

Codex then showed the destination side must not trust ANY mutable pathname:
the reconciler's lstat-then-walk (5707522), Django's pathname ``_save``/
``exists``/``delete`` behind a symlinked intermediate, and a swapped
application-layout ancestor (35a77eb) each reached private files. The tests
below run the REAL collectstatic command, under umask 0077, against
adversarial trees -- with a deterministic interposed attacker that wins every
race window -- and require the private trees byte-for-byte and mode-for-mode
unchanged. A path-operation audit proves no destination pathname is used.
"""
import builtins
import contextlib
import hashlib
import os
import stat
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.exceptions import ImproperlyConfigured, SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage, default_storage, storages
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from isadoraair import static_storage

# Nested application static directories that exist in this release.
NESTED = ("production/iportal", "weather/css", "weather/js", "admin/js/admin")
ASSET = "production/iportal/workstation.js"
SOURCE_ASSET = Path(settings.BASE_DIR) / "production" / "static" / ASSET
# Any refusal counts as failing closed; the security assertion is always the
# untouched private tree.
REFUSED = (OSError, ImproperlyConfigured, SuspiciousFileOperation, CommandError)
CONFINEMENT = getattr(static_storage, "StaticConfinementError", OSError)


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


def snapshot(root: Path) -> dict:
    """Every entry under ``root`` (root included): type, mode, and content hash
    or link target -- never following a symlink."""
    result = {}
    stack = [root]
    while stack:
        path = stack.pop()
        info = os.lstat(path)
        key = str(path.relative_to(root.parent))
        if stat.S_ISLNK(info.st_mode):
            result[key] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            result[key] = ("dir", stat.S_IMODE(info.st_mode))
            stack.extend(sorted(path.iterdir()))
        else:
            result[key] = ("file", stat.S_IMODE(info.st_mode), hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def private_file(path: Path, content=b"SENTINEL private bytes\n", file_mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(file_mode)


def collect(*args):
    call_command("collectstatic", "--noinput", "--skip-checks", *args, stdout=StringIO(), stderr=StringIO())


def try_collect(*args):
    try:
        collect(*args)
    except REFUSED as exc:
        return exc
    return None


class Attacker:
    """Deterministically swaps ONE pathname between its real object (if any)
    and a symlink to an external target, around the code under test's own
    filesystem calls -- the attacker wins every race window that code leaves.
    A context manager: the interposition lasts for one operation.

    schedule
      "pre"     -- already a symlink before the operation starts;
      "sticky"  -- real for the first call naming it, a symlink ever after
                   (looked at -- or pinned -- then replaced);
      "flip"    -- real whenever it is CHECKED (lstat/stat/open/mkdir of that
                   name), a symlink immediately after every check -- so every
                   later USE of the pathname meets the symlink;
      "listing" -- replaced right after the first directory listing;
      "unflip"  -- a symlink at first, an ordinary attacker file after the
                   first call naming it.
    A real object displaced by the symlink is kept at ``<path>.real``."""

    CHECKS = ("lstat", "stat", "open", "mkdir")
    USES = ("chmod", "scandir", "listdir", "unlink", "remove", "rename", "replace", "rmdir", "utime")

    def __init__(self, target: Path, external: Path, schedule: str):
        self.target, self.external, self.schedule = target, external, schedule
        self.real = target.with_name(target.name + ".real")
        self.seen = self.listed = False
        self.patchers = []

    def __enter__(self):
        self._orig = {name: getattr(os, name) for name in self.CHECKS + self.USES + ("symlink", "readlink")}
        if self.schedule in ("pre", "unflip"):
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

    def _lstat(self, path):
        try:
            return self._orig["lstat"](path)
        except FileNotFoundError:
            return None

    def to_symlink(self):
        info = self._lstat(self.target)
        if info is not None and stat.S_ISLNK(info.st_mode):
            return
        if info is not None:
            self._orig["rename"](self.target, self.real)
        self._orig["symlink"](self.external, self.target)

    def to_real(self):
        info = self._lstat(self.target)
        if info is None or not stat.S_ISLNK(info.st_mode):
            return
        self._orig["unlink"](self.target)
        if self._lstat(self.real) is not None:
            self._orig["rename"](self.real, self.target)

    def to_attacker_file(self):
        info = self._lstat(self.target)
        if info is not None and stat.S_ISLNK(info.st_mode):
            self._orig["unlink"](self.target)
            fd = self._orig["open"](self.target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.write(fd, b"attacker\n")
            os.close(fd)

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
                if mine and self.schedule == "unflip" and not self.seen:
                    self.seen = True
                    self.to_attacker_file()
                if name in ("listdir", "scandir") and self.schedule == "listing" and not self.listed:
                    self.listed = True
                    self.to_symlink()
        return call

    @property
    def original(self) -> Path:
        """Where the real object is now."""
        return self.real if os.path.lexists(self.real) else self.target


class StaticPermissionTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="static-perms-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        # The application layout exists; STATIC_ROOT does not yet: a fresh install.
        (self.base / "app").mkdir()
        self.static_root = self.base / "app" / "staticfiles"
        override = override_settings(STATIC_ROOT=str(self.static_root))
        override.enable()
        self.addCleanup(override.disable)
        old = os.umask(0o077)                           # the updater's UMask
        self.addCleanup(os.umask, old)

    def assert_servable(self):
        self.assertEqual(nginx_unservable(self.static_root), [])
        for nested in NESTED:
            with self.subTest(nested=nested):
                self.assertTrue((self.static_root / nested).is_dir(), nested)

    # -- configuration scope ---------------------------------------------------------
    def test_only_the_staticfiles_backend_is_changed(self):
        storage = storages["staticfiles"]
        self.assertIsInstance(storage, static_storage.StaticRootStorage)
        # Not a FileSystemStorage: it offers no destination path, so collectstatic
        # runs none of its own pathname code (islink/unlink/makedirs) against it.
        self.assertNotIsInstance(storage, FileSystemStorage)
        with self.assertRaises(NotImplementedError):
            storage.path(ASSET)
        self.assertEqual(staticfiles_storage.url(ASSET), f"/static/{ASSET}")
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
        self.assertFalse(self.static_root.exists())
        collect()
        self.assertEqual(mode(self.static_root), 0o755)
        self.assertEqual(mode(self.static_root / "production" / "iportal"), 0o755)
        self.assertEqual(mode(self.static_root / ASSET), 0o644)
        self.assertEqual((self.static_root / ASSET).read_bytes(), SOURCE_ASSET.read_bytes())
        self.assertEqual([p.name for p in self.static_root.rglob(static_storage.TEMP_PREFIX + "*")], [])
        self.assert_servable()

    def test_nothing_outside_static_root_is_broadened(self):
        private = self.base / "private"                  # e.g. .env, sockets, backup material
        private_file(private / "secret")
        private.chmod(0o700)
        outside = self.base / "outside"
        private_file(outside / "file")
        outside.chmod(0o700)
        before = (snapshot(private), snapshot(outside))
        app_mode = mode(self.base / "app")
        collect()
        (self.static_root / "link-out").symlink_to(outside)               # a planted symlink is not followed
        collect()
        self.assertEqual((snapshot(private), snapshot(outside)), before)
        self.assertEqual(mode(self.base), 0o700)                         # the temp base itself
        self.assertEqual(mode(self.base / "app"), app_mode)              # an ancestor is never chmodded
        self.assert_servable()

    def test_production_media_directories_keep_their_own_restrictive_mode(self):
        from production.services import layout
        root = self.base / "production-media"
        with override_settings(PRODUCTION_MEDIA_ROOT=str(root)):
            collect()
            layout.ensure_layout()
        self.assertEqual(mode(root), 0o750)
        for name in ("media", "incoming", "work", "locks"):
            self.assertEqual(mode(root / name), 0o750, name)

    # -- upgrade repair --------------------------------------------------------------------
    def test_an_existing_tree_with_0700_directories_is_repaired_on_the_next_run(self):
        """The KOGR r0107 shape: files already collected (so collectstatic skips
        them as unmodified), their directories left 0700 / files 0600."""
        collect()
        broken = ["production", "production/iportal", "weather", "weather/css", "weather/js", "admin"]
        for name in broken:
            (self.static_root / name).chmod(0o700)
        (self.static_root / "weather" / "css").joinpath("stale-only-here").mkdir(mode=0o700)
        damaged_files = [self.static_root / ASSET] + list((self.static_root / "weather").rglob("*.*"))
        for path in damaged_files:
            path.chmod(0o600)
        self.static_root.chmod(0o700)
        self.assertNotEqual(nginx_unservable(self.static_root), [])
        collect()
        self.assertEqual(mode(self.static_root), 0o755)
        for name in broken:
            self.assertEqual(mode(self.static_root / name), 0o755, name)
        self.assertEqual(mode(self.static_root / "weather" / "css" / "stale-only-here"), 0o755)
        for path in damaged_files:
            self.assertEqual(mode(path), 0o644, path)
        self.assert_servable()

    def test_a_modified_source_replaces_the_destination_in_place(self):
        collect()
        target = self.static_root / ASSET
        target.write_bytes(b"stale")
        os.utime(target, (0, 0))                          # older than the source: delete + save
        collect()
        self.assertEqual(target.read_bytes(), SOURCE_ASSET.read_bytes())
        self.assertEqual(mode(target), 0o644)

    def test_repair_only_adds_bits(self):
        collect()
        wider_dir = self.static_root / "weather"
        wider_dir.chmod(0o775)
        wider_file = self.static_root / "weather" / "css" / os.listdir(self.static_root / "weather" / "css")[0]
        wider_file.chmod(0o664)
        collect()
        self.assertEqual(mode(wider_dir), 0o775)
        self.assertEqual(mode(wider_file), 0o664)

    def test_collectstatic_link_mode_is_refused(self):
        """--link would make collectstatic create directories and symlinks BY
        PATH itself; this storage is non-local, so collectstatic refuses it."""
        with self.assertRaises(CommandError):
            collect("--link")
        self.assertFalse(self.static_root.exists())

    def test_clear_deletes_files_through_descriptors_and_recollects(self):
        collect()
        (self.static_root / "obsolete.js").write_text("old")
        collect("--clear")
        self.assertFalse((self.static_root / "obsolete.js").exists())
        self.assert_servable()

    def test_a_dry_run_changes_nothing(self):
        collect()
        (self.static_root / "weather").chmod(0o700)
        collect("--dry-run")
        self.assertEqual(mode(self.static_root / "weather"), 0o700)

    # -- storage contract -----------------------------------------------------------------------
    def test_destination_names_are_confined(self):
        collect()
        storage = storages["staticfiles"]
        for bad in ("/etc/passwd", "../outside.js", "production/../../outside.js", "a\x00b", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(SuspiciousFileOperation):
                    static_storage.destination_components(bad)
                with self.assertRaises((SuspiciousFileOperation, ValueError)):
                    storage.save(bad, ContentFile(b"x"))
        self.assertFalse((self.base / "outside.js").exists())
        self.assertEqual(static_storage.destination_components("a//./b/c.js"), ["a", "b", "c.js"])

    def test_storage_metadata_never_reports_a_symlink_target(self):
        collect()
        storage = storages["staticfiles"]
        outside = self.base / "outside.js"
        private_file(outside)
        os.utime(outside, (4_000_000_000, 4_000_000_000))                  # "newer" than any source
        link = self.static_root / "linked.js"
        link.symlink_to(outside)
        self.assertTrue(storage.exists("linked.js"))                        # the entry exists ...
        with self.assertRaises(static_storage.StaticConfinementError):     # ... but has no usable time
            storage.get_modified_time("linked.js")
        with self.assertRaises(static_storage.StaticConfinementError):
            storage.size("linked.js")
        with self.assertRaises(static_storage.StaticConfinementError):
            storage.open("linked.js")
        storage.delete("linked.js")                                         # removes the ENTRY only
        self.assertFalse(os.path.lexists(link))
        self.assertEqual(outside.read_bytes(), b"SENTINEL private bytes\n")
        link.symlink_to(outside)
        storage.save("linked.js", ContentFile(b"real"))                     # replaces the entry, never writes through
        self.assertEqual((link.is_symlink(), link.read_bytes(), mode(link)), (False, b"real", 0o644))
        self.assertEqual((outside.read_bytes(), mode(outside)), (b"SENTINEL private bytes\n", 0o600))
        directories, files = storage.listdir("")
        self.assertIn("production", directories)
        self.assertIn("linked.js", files)

    # -- non-regular destination leaves (Codex on 737d11d) ------------------------------------
    NEWER = 4_000_000_000                                         # 2096: newer than any source

    def make_leaf(self, kind):
        """Replace the collected asset with a non-regular entry whose own mtime
        is NEWER than the source -- what used to be skipped as "unmodified"."""
        leaf = self.static_root / ASSET
        if leaf.is_dir() and not leaf.is_symlink():
            leaf.rmdir()                                          # (left over when not replaced)
        else:
            leaf.unlink()
        if kind == "directory":
            leaf.mkdir()
        elif kind == "symlink":
            outside = self.base / "outside.js"
            if not outside.exists():
                private_file(outside)
            leaf.symlink_to(outside)
        elif kind == "fifo":
            os.mkfifo(leaf)
        elif kind == "socket":
            import socket
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(leaf))
            listener.close()                                      # the socket inode stays
        os.utime(leaf, (self.NEWER, self.NEWER), follow_symlinks=False)
        self.assertGreater(os.lstat(leaf).st_mtime, SOURCE_ASSET.stat().st_mtime)
        return leaf

    def test_a_newer_directory_where_an_asset_belongs_is_replaced_not_skipped(self):
        """Codex's reproduction through the real release command: an empty
        directory at production/iportal/workstation.js with a newer mtime was
        accepted as current file metadata -- "0 static files copied, 152
        unmodified" -- and the asset stayed missing with a successful exit."""
        collect()
        leaf = self.make_leaf("directory")
        out = StringIO()
        call_command("collectstatic", "--noinput", "--skip-checks", stdout=out, stderr=StringIO())
        self.assertTrue(stat.S_ISREG(os.lstat(leaf).st_mode), "the asset is still not a regular file")
        self.assertEqual(leaf.read_bytes(), SOURCE_ASSET.read_bytes())
        self.assertEqual(mode(leaf), 0o644)
        self.assertIn("1 static file copied", out.getvalue())             # not "unmodified"
        self.assert_servable()

    def test_no_non_regular_leaf_is_reported_as_a_current_static_file(self):
        storage = storages["staticfiles"]
        for kind in ("directory", "symlink", "fifo", "socket"):
            with self.subTest(kind=kind):
                collect()
                leaf = self.make_leaf(kind)
                self.assertTrue(storage.exists(ASSET))                     # the entry exists ...
                for method in (storage.get_modified_time, storage.size, storage.open):
                    with self.assertRaises(static_storage.StaticConfinementError):   # ... but is no static file
                        method(ASSET)
                collect()                                                   # and is replaced, not skipped
                self.assertTrue(stat.S_ISREG(os.lstat(leaf).st_mode), kind)
                self.assertEqual(leaf.read_bytes(), SOURCE_ASSET.read_bytes())
                self.assertEqual(mode(leaf), 0o644)
        outside = self.base / "outside.js"
        self.assertEqual((outside.read_bytes(), mode(outside)), (b"SENTINEL private bytes\n", 0o600))

    def test_a_non_empty_directory_where_an_asset_belongs_fails_loudly(self):
        collect()
        leaf = self.make_leaf("directory")
        (leaf / "keep").write_text("k")
        os.utime(leaf, (self.NEWER, self.NEWER))
        with self.assertRaises(OSError):                                    # never a silent "unmodified"
            collect()
        self.assertEqual((leaf / "keep").read_text(), "k")                 # never deleted recursively

    def test_a_directory_where_a_file_is_expected_fails_safely(self):
        collect()
        storage = storages["staticfiles"]
        (self.static_root / "dir.js").mkdir()
        (self.static_root / "dir.js" / "keep").write_text("k")
        with self.assertRaises(OSError):
            storage.save("dir.js", ContentFile(b"x"))
        self.assertEqual((self.static_root / "dir.js" / "keep").read_text(), "k")
        self.assertEqual([p.name for p in self.static_root.glob(static_storage.TEMP_PREFIX + "*")], [])

    # -- refusals ----------------------------------------------------------------------------
    def test_an_entry_that_cannot_be_repaired_fails_the_run_loudly(self):
        collect()
        (self.static_root / "weather").chmod(0o700)
        with mock.patch.object(static_storage.os, "fchmod", side_effect=PermissionError(1, "Operation not permitted")):
            with self.assertRaises(static_storage.StaticPermissionError) as raised:
                collect()
        self.assertIn("weather", str(raised.exception))

    def test_a_symlinked_static_root_is_refused(self):
        real = self.base / "real-static"
        real.mkdir(mode=0o700)
        self.static_root.symlink_to(real)
        with self.assertRaises(static_storage.StaticConfinementError):
            static_storage.reconcile_static_permissions(self.static_root)
        with self.assertRaises(static_storage.StaticConfinementError):
            collect()
        self.assertEqual(snapshot(real), {"real-static": ("dir", 0o700)})

    def test_a_missing_ancestor_fails_closed_and_is_never_created(self):
        with override_settings(STATIC_ROOT=str(self.base / "missing" / "staticfiles")):
            with self.assertRaises(static_storage.StaticPermissionError):
                collect()
        self.assertFalse((self.base / "missing").exists())

    def test_reconcile_of_a_missing_root_is_a_no_op(self):
        self.assertEqual(static_storage.reconcile_static_permissions(self.static_root), [])
        self.assertFalse(self.static_root.exists())



# -- reconciliation races (r0108 corrective 1, Codex on 5707522) -------------------------

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
            except (ImproperlyConfigured, CONFINEMENT):
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




class CollectstaticAttackTests(SimpleTestCase):
    """The REAL release command (collectstatic --noinput --skip-checks, umask
    0077) against adversarial destination trees. Required in every case: the
    private trees unchanged -- modes, bytes, entries -- whatever the outcome."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="static-attack-")
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.app = self.base / "apptree"                 # the writable application layout (checkout)
        self.app.mkdir()
        self.static_root = self.app / "staticfiles"
        override = override_settings(STATIC_ROOT=str(self.static_root))
        override.enable()
        self.addCleanup(override.disable)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        # Private material elsewhere (.env, ProductionMedia, reports, recovery,
        # backups are all of this shape): 0700 directories, 0600 files.
        self.external = self.base / "external"
        private_file(self.external / "iportal" / "workstation.js")         # Codex's sentinel
        private_file(self.external / "secret")
        private_file(self.external / "workstation.js")
        for directory in (self.external / "iportal", self.external):
            directory.chmod(0o700)
        self.external_file = self.base / "external-file"
        private_file(self.external_file)
        # An outside application layout, with a staticfiles tree of its own.
        self.evil = self.base / "evil"
        private_file(self.evil / "staticfiles" / "secret")
        private_file(self.evil / "staticfiles" / ASSET)
        for directory in sorted((self.evil).rglob("*"), reverse=True):
            if directory.is_dir():
                directory.chmod(0o700)
        self.evil.chmod(0o700)

    def private(self):
        return {name: snapshot(path) for name, path in
                (("external", self.external), ("external-file", self.external_file), ("evil", self.evil))}

    def clean_tree(self):
        collect()
        self.assertEqual(nginx_unservable(self.static_root), [])

    def age(self, *relative):
        for name in relative:
            os.utime(self.static_root / name, (0, 0))      # older than the source: delete + save

    def damage(self):
        for name in ("production", "production/iportal", "weather"):
            (self.static_root / name).chmod(0o700)
        (self.static_root / ASSET).chmod(0o600)

    # 15. a symlinked intermediate ancestor of a destination
    def test_a_symlinked_intermediate_directory_is_never_written_through(self):
        self.static_root.mkdir()
        (self.static_root / "production").symlink_to(self.external)
        before = self.private()
        outcome = try_collect()
        self.assertEqual(self.private(), before)
        self.assertIsInstance(outcome, CONFINEMENT)                        # refused, never followed
        self.assertFalse((self.external / "iportal" / "capture_worklet.js").exists())

    # 16. Codex's exact reproduction: an external 0600 file behind STATIC_ROOT/production
    def test_an_external_file_behind_a_symlinked_directory_is_never_replaced_or_deleted(self):
        self.clean_tree()
        os.rename(self.static_root / "production", self.base / "production.orig")
        (self.static_root / "production").symlink_to(self.external)
        sentinel = (self.external / "iportal" / "workstation.js").read_bytes()
        before = self.private()
        for args in ((), ("--clear",)):
            with self.subTest(args=args):
                outcome = try_collect(*args)
                self.assertEqual(self.private(), before)
                if args:
                    # --clear deletes the planted symlink as an ENTRY (never its
                    # referent) and collects a real directory in its place.
                    self.assertIsNone(outcome)
                    self.assertFalse((self.static_root / "production").is_symlink())
                    self.assertEqual(nginx_unservable(self.static_root), [])
                else:
                    self.assertIsInstance(outcome, CONFINEMENT)
                self.assertEqual(self.private(), before)
                self.assertEqual(mode(self.external / "iportal" / "workstation.js"), 0o600)
                self.assertEqual((self.external / "iportal" / "workstation.js").read_bytes(), sentinel)

    # 17. the application layout (an ancestor of STATIC_ROOT) substituted
    def test_application_layout_ancestor_substitution(self):
        for schedule in ("pre", "listing", "sticky", "flip"):
            with self.subTest(schedule=schedule):
                self.setUp()
                self.clean_tree()
                self.damage()
                self.age(ASSET)
                before = self.private()
                with Attacker(self.app, self.evil, schedule) as attacker:
                    outcome = try_collect()
                self.assertEqual(self.private(), before)                    # the outside layout untouched
                if outcome is None:                                          # ran: only on the pinned original
                    pinned = attacker.original / "staticfiles"
                    self.assertEqual(nginx_unservable(pinned), [])
                    self.assertEqual((pinned / ASSET).read_bytes(), SOURCE_ASSET.read_bytes())
                else:
                    self.assertIsInstance(outcome, CONFINEMENT)
                if schedule == "pre":
                    self.assertIsInstance(outcome, CONFINEMENT)

    # 18. ... while STATIC_ROOT does not exist yet
    def test_absent_root_ancestor_substitution_never_creates_outside(self):
        (self.evil / "staticfiles" / "secret").unlink()
        for path in sorted((self.evil / "staticfiles").rglob("*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        (self.evil / "staticfiles").rmdir()
        for schedule in ("pre", "listing", "sticky", "flip"):
            with self.subTest(schedule=schedule):
                if os.path.lexists(self.app) and self.app.is_symlink():
                    self.app.unlink()
                for leftover in (self.app, self.app.with_name("apptree.real")):
                    if leftover.exists():
                        import shutil
                        shutil.rmtree(leftover)
                self.app.mkdir()
                before = self.private()
                with Attacker(self.app, self.evil, schedule) as attacker:
                    outcome = try_collect()
                self.assertEqual(self.private(), before)
                self.assertFalse(os.path.lexists(self.evil / "staticfiles"))
                if outcome is None:
                    self.assertEqual(nginx_unservable(attacker.original / "staticfiles"), [])
                else:
                    self.assertIsInstance(outcome, CONFINEMENT)

    # 19. the destination leaf raced during the save
    def test_leaf_substitution_during_save(self):
        scenarios = {
            "absent -> symlink": ("absent", ("sticky", "flip")),
            "file -> symlink": ("file", ("sticky", "flip")),
            "symlink -> file": ("symlink", ("unflip",)),
            "symlink throughout": ("symlink", ("pre",)),
        }
        for label, (start, schedules) in scenarios.items():
            for schedule in schedules:
                with self.subTest(label, schedule=schedule):
                    self.setUp()
                    self.clean_tree()
                    leaf = self.static_root / ASSET
                    if start == "absent":
                        leaf.unlink()
                    else:
                        self.age(ASSET)
                    before = self.private()
                    with Attacker(leaf, self.external_file, schedule):
                        outcome = try_collect()
                    self.assertEqual(self.private(), before)
                    if outcome is None and not leaf.is_symlink():
                        self.assertIn(leaf.read_bytes(), (SOURCE_ASSET.read_bytes(), b"attacker\n"))

    # 20. deletion/replacement raced: the intermediate directory or the leaf
    def test_deletion_substitution_is_confined(self):
        targets = {"intermediate": (lambda: self.static_root / "production" / "iportal", lambda: self.external),
                   "leaf": (lambda: self.static_root / ASSET, lambda: self.external_file)}
        for label, (target, external) in targets.items():
            for schedule in ("sticky", "flip"):
                with self.subTest(label, schedule=schedule):
                    self.setUp()
                    self.clean_tree()
                    self.age(ASSET, "production/iportal/recorder.js")           # stale: collectstatic deletes them
                    before = self.private()
                    with Attacker(target(), external(), schedule):
                        outcome = try_collect()
                    self.assertEqual(self.private(), before)
                    self.assertTrue((self.external / "workstation.js").exists())
                    self.assertTrue((self.external / "iportal" / "workstation.js").exists())
                    if outcome is not None:
                        self.assertIsInstance(outcome, CONFINEMENT)


class PathOperationAuditTests(SimpleTestCase):
    """22. Every filesystem call collectstatic makes, recorded: none may name
    STATIC_ROOT, anything below it, or any ancestor of it other than ``/`` by
    pathname -- not a mutation, and not an existence/metadata check either.
    Source-side reads (the apps' static directories) are not destination."""

    FUNCTIONS = ("open", "stat", "lstat", "access", "mkdir", "makedirs", "chmod", "chown", "lchown", "unlink",
                 "remove", "rename", "replace", "rmdir", "removedirs", "listdir", "scandir", "utime", "symlink",
                 "link", "readlink", "truncate")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="static-audit-")
        self.addCleanup(tmp.cleanup)
        self.app = Path(tmp.name) / "apptree"
        self.app.mkdir()
        self.static_root = self.app / "staticfiles"
        override = override_settings(STATIC_ROOT=str(self.static_root))
        override.enable()
        self.addCleanup(override.disable)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def destination(self, value):
        if isinstance(value, int) or value is None:
            return False
        try:
            path = os.path.abspath(os.fsdecode(os.fspath(value)))
        except TypeError:
            return False
        if path == "/":
            return False                                              # the one trusted anchor
        root = str(self.static_root)
        return path == root or path.startswith(root + "/") or root.startswith(path.rstrip("/") + "/")

    @contextlib.contextmanager
    def audited(self):
        calls, relative = [], []
        originals, wrappers = {}, {}
        for name in self.FUNCTIONS:
            original = getattr(os, name, None)
            if original is None:
                continue

            def wrapper(*args, _name=name, _original=original, **kwargs):
                by_fd = kwargs.get("dir_fd") is not None or kwargs.get("src_dir_fd") is not None
                named = [arg for arg in args[:2] if self.destination(arg)]
                if named and not by_fd:
                    calls.append((_name, named))
                if by_fd:
                    relative.append(_name)
                return _original(*args, **kwargs)
            originals[name], wrappers[name] = original, wrapper
        real_open = builtins.open

        def opener(file, *args, **kwargs):
            if self.destination(file):
                calls.append(("builtins.open", [file]))
            return real_open(file, *args, **kwargs)
        patchers = [mock.patch.object(os, name, wrapper) for name, wrapper in wrappers.items()]
        patchers.append(mock.patch.object(builtins, "open", opener))
        # The wrappers stand in for the real functions' descriptor support.
        for attr in ("supports_dir_fd", "supports_fd", "supports_follow_symlinks"):
            current = getattr(os, attr)
            patchers.append(mock.patch.object(
                os, attr, current | {wrappers[name] for name in wrappers if originals[name] in current}))
        for patcher in patchers:
            patcher.start()
        try:
            yield calls, relative
        finally:
            for patcher in reversed(patchers):
                patcher.stop()

    def test_collectstatic_never_names_a_destination_path(self):
        runs = {
            "fresh root": lambda: None,
            "repair of a damaged tree": lambda: [os.chmod(self.static_root / n, 0o700)
                                                 for n in ("production", "weather/css")],
            "stale asset replaced": lambda: os.utime(self.static_root / ASSET, (0, 0)),
            "--clear": lambda: None,
        }
        for label, prepare in runs.items():
            with self.subTest(label):
                prepare()
                with self.audited() as (calls, relative):
                    collect(*(("--clear",) if label == "--clear" else ()))
                self.assertEqual(calls, [])
                self.assertGreater(len(relative), 100)                 # it did work -- through descriptors
                self.assertEqual(nginx_unservable(self.static_root), [])


class StaticCreationTests(SimpleTestCase):
    """What collectstatic creates is servable even without the post-process
    step, and the caller's umask is never touched."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="static-create-")
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / "app").mkdir()
        self.static_root = Path(tmp.name) / "app" / "staticfiles"
        override = override_settings(STATIC_ROOT=str(self.static_root))
        override.enable()
        self.addCleanup(override.disable)
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)

    def test_created_entries_are_servable_even_without_post_process(self):
        with mock.patch.object(os, "umask", wraps=os.umask) as umask:
            collect("--no-post-process")
        self.assertEqual([call for call in umask.call_args_list], [])     # no process-wide umask games
        self.assertEqual(nginx_unservable(self.static_root), [])
        self.assertEqual(mode(self.static_root / "production" / "iportal"), 0o755)
        self.assertEqual(mode(self.static_root / ASSET), 0o644)
