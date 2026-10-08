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
        self.assertEqual((staticfiles_storage.file_permissions_mode, staticfiles_storage.directory_permissions_mode),
                         (0o644, 0o755))
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
        with mock.patch.object(static_storage.os, "chmod", side_effect=PermissionError(1, "Operation not permitted")):
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
