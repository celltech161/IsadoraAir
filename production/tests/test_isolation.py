"""Meta-tests: this suite can never touch production state."""
import importlib
import inspect
import pkgutil
import unittest
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from isadoraair import test_safety

import production.tests as tests_package

from .support import PRODUCTION_DEFAULT_ROOT, IsolatedMediaRootMixin

# Pure-function / static-analysis classes that never read or write a media root.
EXEMPT = {
    "production.tests.test_formats", "production.tests.test_migration", "production.tests.test_isolation",
    # Policy/restore tests: pure checks or private temp trees; never a media root.
    "production.tests.test_root_policy",
    # 2.22B resource confinement: synthetic child processes and private temp
    # files only; validation is run on a private temp WAV, never a media root.
    "production.tests.test_confinement",
}
# Individually exempt classes (module.Class): GStreamer child/GError-table tests
# that only read private temp fixture files.
EXEMPT_CLASSES = {
    "production.tests.test_gst_taxonomy.ProbeChildTaxonomyTests",
    "production.tests.test_gst_taxonomy.ClassifyErrorTableTests",
    # 2.22B validation service: private temp WAVs, fake tools and transient
    # user-level services only; no database, never a media root.
    "production.tests.test_validation_service.ProtocolTests",
    "production.tests.test_validation_service.LifecycleTests",
    "production.tests.test_validation_service.AdmissionTests",
}


def test_classes():
    for module_info in pkgutil.iter_modules(tests_package.__path__):
        module = importlib.import_module(f"{tests_package.__name__}.{module_info.name}")
        if not module_info.name.startswith("test_"):
            continue
        for _name, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, unittest.TestCase) and cls.__module__ == module.__name__:
                yield module.__name__, cls


class IsolationTests(SimpleTestCase):
    def test_every_filesystem_or_database_test_class_uses_the_isolated_root(self):
        offenders = [f"{module}.{cls.__name__}" for module, cls in test_classes()
                     if module not in EXEMPT and f"{module}.{cls.__name__}" not in EXEMPT_CLASSES
                     and not issubclass(cls, IsolatedMediaRootMixin)]
        self.assertEqual(offenders, [])

    def test_the_default_setting_is_the_documented_production_path_and_is_overridden_in_tests(self):
        source = (Path(__file__).resolve().parents[2] / "isadoraair" / "settings.py").read_text()
        self.assertIn(f"default='{PRODUCTION_DEFAULT_ROOT}'", source)

    def test_the_destructive_test_guard_is_installed_for_this_package(self):
        self.assertTrue(getattr(TransactionTestCase, test_safety._INSTALLED_MARK, False))
        init = (Path(tests_package.__file__)).read_text()
        self.assertIn("_test_safety.install()", init)

    def test_the_media_root_is_not_exposed_by_any_nginx_configuration(self):
        repo = Path(__file__).resolve().parents[2]
        for path in list((repo / "deploy").glob("*nginx*")) + list((repo / "deploy").glob("*locations*")):
            self.assertNotIn("production-media", path.read_text(), path.name)

    def test_it_is_not_registered_as_an_admin_editable_setting(self):
        from isadoraair import env_config
        self.assertNotIn("PRODUCTION_MEDIA_ROOT", env_config.MANAGED_SETTINGS)


class RealRootGuardTests(IsolatedMediaRootMixin, TestCase):
    def test_the_active_root_is_private_and_never_the_production_root(self):
        self.assertNotEqual(settings.PRODUCTION_MEDIA_ROOT, PRODUCTION_DEFAULT_ROOT)
        self.assertTrue(settings.PRODUCTION_MEDIA_ROOT.startswith("/tmp"))

    def test_a_test_that_wrote_to_the_real_root_would_be_caught(self):
        from unittest import mock
        with mock.patch("production.tests.support._snapshot_real_root", return_value=[("/x", 1, 1)]), \
                self.assertRaises(AssertionError):
            self.assert_real_root_untouched()
