"""Regression coverage for the test-run isolation boundary (isadoraair/test_isolation.py).

These tests run INSIDE an engaged process (settings were configured by the test
invocation), so every write they provoke is aimed at a *simulated* station
directory registered with ``protect_for_test`` -- nothing here writes under
/srv or /var/lib. The subprocess controls start fresh interpreters with
deliberately unsafe configuration (the stale-.env scenario) and prove they are
neutralised or refused before any test could write; they never connect to a
database and never run a test.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from isadoraair import test_isolation as iso
from isadoraair import test_safety

BASE = Path(settings.BASE_DIR)


class _Scratch(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))
        self.station = self.tmp / "station"
        self.station.mkdir()
        self.addCleanup(iso.clear_blocked_writes)

    def protected(self):
        return iso.protect_for_test(self.station)


class ProtectedPathClassificationTests(_Scratch):
    def test_fixed_roots_and_the_live_checkout_are_protected(self):
        for path in ("/srv/isadoraair/waveforms/1.json", "/srv/isadoraair", "/var/lib/isadoraair/reports/x",
                     "/opt/isadoraair/manage.py", "/opt/isadoraair-runtime/bin/python"):
            self.assertTrue(iso.protected_match(path), path)
        live = os.path.realpath("/opt/isadoraair")
        if live != "/opt/isadoraair":
            self.assertTrue(iso.protected_match(os.path.join(live, "library", "models.py")))

    def test_lookalikes_and_this_worktree_are_not_protected(self):
        for path in ("/srv/isadoraair-other/x", "/srv/station/weather", "/var/lib/isadoraair2/x", "/tmp/x",
                     str(BASE / "library" / "models.py")):
            self.assertIsNone(iso.protected_match(path), path)

    def test_ancestors_only_count_when_asked(self):
        self.assertIsNone(iso.protected_match("/srv"))
        self.assertTrue(iso.protected_match("/srv", ancestors=True))
        self.assertTrue(iso.protected_match("/var/lib", ancestors=True))

    def test_traversal_and_symlink_aliases_resolve_to_the_protected_root(self):
        (self.station / "waveforms").mkdir()           # created before the directory is declared protected
        with self.protected():
            alias = self.tmp / "alias"
            alias.symlink_to(self.station, target_is_directory=True)
            chain = self.tmp / "chain"
            chain.symlink_to(alias, target_is_directory=True)
            for path in (self.tmp / "x" / ".." / "station" / "waveforms" / "1.json", alias / "waveforms" / "1.json",
                         chain / "waveforms" / "1.json", alias / "not-yet-created" / "1.json"):
                self.assertEqual(iso.protected_match(path), str(self.station), path)
        self.assertIsNone(iso.protected_match(alias / "waveforms" / "1.json"))   # no longer registered


class WriteBoundaryTests(_Scratch):
    def setUp(self):
        super().setUp()
        self.file = self.station / "1.json"
        self.file.write_bytes(b"station data")          # planted before protection is registered

    def test_every_mutation_of_protected_storage_is_refused_and_recorded(self):
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "src").write_bytes(b"x")
        with self.protected():
            attempts = {
                "open w": lambda: open(self.file, "w"),
                "open a": lambda: open(self.file, "ab"),
                "open x": lambda: open(self.station / "new.json", "x"),
                "open r+": lambda: open(self.file, "r+b"),
                "os.open WRONLY": lambda: os.open(self.file, os.O_WRONLY),
                "Path.write_text": lambda: self.file.write_text("overwrite"),
                "os.remove": lambda: os.remove(self.file),
                "os.rename out": lambda: os.rename(self.file, elsewhere / "stolen"),
                "os.rename in": lambda: os.rename(elsewhere / "src", self.station / "dst"),
                "os.chmod": lambda: os.chmod(self.file, 0o777),
                "os.mkdir": lambda: os.mkdir(self.station / "sub"),
                "os.symlink": lambda: os.symlink(elsewhere, self.station / "link"),
                "shutil.copyfile": lambda: shutil.copyfile(elsewhere / "src", self.station / "copy"),
                "shutil.move": lambda: shutil.move(str(elsewhere / "src"), str(self.station / "moved")),
                "shutil.rmtree": lambda: shutil.rmtree(self.station),
                "shutil.rmtree ancestor": lambda: shutil.rmtree(self.tmp),
            }
            for label, attempt in attempts.items():
                with self.subTest(label), self.assertRaises(iso.ProtectedPathWriteBlocked):
                    attempt()
            self.assertEqual(len(iso.blocked_writes()), len(attempts))
        self.assertEqual(self.file.read_bytes(), b"station data")
        self.assertEqual(sorted(p.name for p in self.station.iterdir()), ["1.json"])
        self.assertEqual((elsewhere / "src").read_bytes(), b"x")

    def test_reads_and_unrelated_writes_are_not_refused(self):
        with self.protected():
            self.assertEqual(self.file.read_bytes(), b"station data")
            self.assertEqual(open(self.file).read(), "station data")
            os.stat(self.file)
            os.listdir(self.station)
            (self.tmp / "free.txt").write_text("fine")
            shutil.copyfile(self.file, self.tmp / "copied")            # reading a protected source is allowed
            (self.tmp / "dir").mkdir()
        self.assertEqual(iso.blocked_writes(), [])

    def test_a_symlink_into_protected_storage_cannot_be_written_through_but_can_be_removed(self):
        alias = self.tmp / "alias"
        alias.symlink_to(self.station, target_is_directory=True)
        with self.protected():
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                (alias / "1.json").write_text("through the link")
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                os.rename(alias / "1.json", self.tmp / "x")
            os.unlink(alias)               # removing the link itself never touches the target
        self.assertEqual(self.file.read_bytes(), b"station data")
        self.assertFalse(alias.exists())

    def test_the_refusal_cannot_be_swallowed_by_a_broad_except(self):
        swallowed = []
        with self.protected():
            try:
                try:
                    self.file.write_text("x")
                except Exception as exc:                                  # noqa: BLE001 -- the shape analysis uses
                    swallowed.append(exc)
            except iso.ProtectedPathWriteBlocked:
                pass
        self.assertEqual(swallowed, [])
        self.assertEqual(len(iso.blocked_writes()), 1)                    # ...and it is recorded regardless

    def test_a_swallowed_refusal_still_fails_the_run(self):
        with self.protected():
            try:
                self.file.write_text("x")
            except BaseException:                                          # noqa: BLE001 -- swallow it entirely
                pass
        stream = StringIO()
        self.assertEqual(iso.fail_on_blocked(0, stream), 1)
        self.assertIn("ISOLATION FAILURE", stream.getvalue())
        self.assertIn(str(self.file), stream.getvalue())
        iso.clear_blocked_writes()
        self.assertEqual(iso.fail_on_blocked(0, StringIO()), 0)

    def test_a_per_test_override_pointing_at_scratch_works_and_one_pointing_at_the_station_does_not(self):
        scratch = self.tmp / "scratch"
        scratch.mkdir()
        with self.protected():
            with override_settings(WAVEFORMS_DIR=str(scratch)):
                (Path(settings.WAVEFORMS_DIR) / "1.json").write_text("ok")
            with override_settings(WAVEFORMS_DIR=str(self.station)):
                with self.assertRaises(iso.ProtectedPathWriteBlocked):
                    (Path(settings.WAVEFORMS_DIR) / "1.json").write_text("bypass attempt")
        self.assertEqual((scratch / "1.json").read_text(), "ok")
        self.assertEqual(self.file.read_bytes(), b"station data")


class EffectiveSettingsTests(_Scratch):
    def setUp(self):
        super().setUp()
        self.namespace = settings.ISADORAAIR_TEST_ISOLATION["namespace"]
        self.safe = {name: os.path.join(self.namespace, sub) for name, sub in iso.PATH_SETTINGS.items()}
        self.safe.update({"WEB_REQUESTS_INGEST_URL": "", "WEB_REQUESTS_INGEST_API_KEY": "",
                          "HOUR_REFILL_INSTALL_ARMED": False,
                          "PRODUCTION_VALIDATION_SOCKET": os.path.join(self.namespace, "validator.sock"),
                          "PRODUCTION_RENDER_SOCKET": os.path.join(self.namespace, "render.sock")})
        self.dev_db = {"default": {"NAME": "isadoraair_dev", "USER": "isadoraair_dev"}}

    def problems(self, **changes):
        values = dict(self.safe, **changes)
        return iso.effective_problems(values.get, self.namespace, self.dev_db, {})

    def test_the_running_configuration_is_isolated(self):
        self.assertEqual(iso.effective_problems(lambda n: getattr(settings, n, None), self.namespace,
                                                settings.DATABASES), [])
        for name in iso.PATH_SETTINGS:
            self.assertTrue(getattr(settings, name).startswith(self.namespace + os.sep), name)

    def test_a_safe_configuration_has_no_problems(self):
        self.assertEqual(self.problems(), [])

    def test_the_production_waveform_path_is_rejected(self):
        found = self.problems(WAVEFORMS_DIR="/srv/isadoraair/waveforms")
        self.assertEqual(len(found), 1)
        self.assertIn("WAVEFORMS_DIR", found[0])
        self.assertIn("protected", found[0])

    def test_symlink_traversal_and_ancestor_variants_are_rejected(self):
        alias = self.tmp / "waveform-alias"
        alias.symlink_to("/srv/isadoraair/waveforms", target_is_directory=True)
        for label, value in (("symlink alias", str(alias)),
                             ("alias + subdir", str(alias / "sub")),
                             ("traversal", self.namespace + "/waveforms/../../../../srv/isadoraair/waveforms"),
                             ("ancestor of station storage", "/srv"),
                             ("var/lib ancestor", "/var/lib"),
                             ("the live checkout", "/opt/isadoraair/media")):
            with self.subTest(label):
                found = self.problems(WAVEFORMS_DIR=value)
                self.assertEqual(len(found), 1, found)
                self.assertIn("WAVEFORMS_DIR", found[0])

    def test_a_directory_that_is_merely_elsewhere_is_rejected_too(self):
        # An "arbitrary alternative override": safe-looking but outside the per-run namespace.
        found = self.problems(LIBRARY_ROOT=str(self.tmp))
        self.assertEqual(len(found), 1)
        self.assertIn("not inside the test namespace", found[0])

    def test_every_writable_root_is_checked(self):
        for name in iso.PATH_SETTINGS:
            with self.subTest(name):
                self.assertTrue(self.problems(**{name: "/srv/isadoraair/music"}))
                self.assertTrue(self.problems(**{name: None}))

    def test_network_ingest_hour_refill_and_production_sockets_are_rejected(self):
        self.assertTrue(self.problems(WEB_REQUESTS_INGEST_URL="https://radio.example.org"))
        self.assertTrue(self.problems(WEB_REQUESTS_INGEST_API_KEY="secret"))
        self.assertTrue(self.problems(HOUR_REFILL_INSTALL_ARMED=True))
        self.assertTrue(self.problems(PRODUCTION_VALIDATION_SOCKET="/run/isadoraair-validation/validator.sock"))
        self.assertTrue(self.problems(PRODUCTION_RENDER_SOCKET="/run/isadoraair-render/render.sock"))

    def test_database_identity_requires_the_development_pair(self):
        for database in ({"NAME": "isadoraair", "USER": "isadoraair"},
                         {"NAME": "isadoraair_dev", "USER": "isadoraair"},
                         {"NAME": "isadoraair", "USER": "isadoraair_dev"},
                         {"NAME": "test_isadoraair", "USER": "isadoraair_dev"},
                         {"NAME": "test_isadoraair_dev", "USER": "isadoraair"}, {}):
            with self.subTest(database):
                values = dict(self.safe)
                self.assertTrue(iso.effective_problems(values.get, self.namespace, {"default": database}, {}))

    def test_the_runtime_name_of_the_disposable_test_database_is_accepted(self):
        # Django renames the live connection to test_<NAME> when it creates the test database.
        values = dict(self.safe)
        for name in ("isadoraair_dev", "test_isadoraair_dev"):
            self.assertEqual(iso.effective_problems(values.get, self.namespace,
                                                    {"default": {"NAME": name, "USER": "isadoraair_dev"}}, {}), [])

    def test_the_isolated_ci_escape_is_refused_on_a_station_host(self):
        other = {"default": {"NAME": "ci_db", "USER": "ci_user"}}
        values = dict(self.safe)
        with mock.patch.object(iso, "_station_markers", return_value=["/etc/isadoraair/station.json"]):
            self.assertTrue(iso.effective_problems(values.get, self.namespace, other, {iso.ISOLATED_CI_ENV: "1"}))
        with mock.patch.object(iso, "_station_markers", return_value=[]):
            self.assertEqual(iso.effective_problems(values.get, self.namespace, other, {iso.ISOLATED_CI_ENV: "1"}), [])
            self.assertTrue(iso.effective_problems(values.get, self.namespace, other, {}))


class NamespaceValidationTests(_Scratch):
    def make(self, name=iso.NAMESPACE_PREFIX + "abc", mode=0o700, parent=None):
        path = (parent or self.tmp) / name
        path.mkdir()
        os.chmod(path, mode)
        return str(path)

    def refused(self, path):
        with self.assertRaises(iso.UnsafeTestEnvironmentError) as raised:
            iso.validate_namespace(path)
        self.assertEqual(raised.exception.category, iso.CATEGORY_NAMESPACE)
        return str(raised.exception)

    def test_a_proper_namespace_is_accepted(self):
        with mock.patch.object(iso, "approved_bases", return_value=frozenset({str(self.tmp)})):
            self.assertEqual(iso.validate_namespace(self.make()), str(self.tmp / (iso.NAMESPACE_PREFIX + "abc")))

    def test_unsafe_namespaces_are_refused(self):
        with mock.patch.object(iso, "approved_bases", return_value=frozenset({str(self.tmp)})):
            self.refused("relative/path")
            self.refused(str(self.tmp / "missing"))
            self.assertIn("mode 0700", self.refused(self.make(name=iso.NAMESPACE_PREFIX + "open", mode=0o755)))
            self.assertIn("name must start", self.refused(self.make(name="not-the-prefix")))
            real = self.make(name=iso.NAMESPACE_PREFIX + "real")
            link = self.tmp / (iso.NAMESPACE_PREFIX + "link")
            link.symlink_to(real, target_is_directory=True)
            self.assertIn("canonical", self.refused(str(link)))
            f = self.tmp / (iso.NAMESPACE_PREFIX + "file")
            f.write_text("x")
            self.assertIn("not a directory", self.refused(str(f)))
            self.assertIn("canonical", self.refused(str(self.tmp / "x" / ".." / (iso.NAMESPACE_PREFIX + "real"))))

    def test_a_namespace_outside_an_approved_base_is_refused(self):
        # self.tmp is a sub-directory of the temp base, not the base itself.
        self.assertIn("approved temporary base", self.refused(self.make(name=iso.NAMESPACE_PREFIX + "unapproved")))

    def test_a_namespace_inside_station_storage_is_refused(self):
        under_station = self.station / (iso.NAMESPACE_PREFIX + "x")
        under_station.mkdir()
        os.chmod(under_station, 0o700)
        with mock.patch.object(iso, "approved_bases", return_value=frozenset({str(self.station)})), self.protected():
            self.assertIn("overlaps protected", self.refused(str(under_station)))


class EngagementTests(SimpleTestCase):
    def test_assert_engaged_passes_in_this_process(self):
        state = iso.assert_engaged()
        self.assertTrue(iso.guard_installed())
        self.assertEqual(state["database"], {"NAME": "isadoraair_dev", "USER": "isadoraair_dev"})

    def test_it_fails_closed_when_isolation_was_never_configured(self):
        with override_settings(ISADORAAIR_TEST_ISOLATION=None):
            with self.assertRaises(iso.UnsafeTestEnvironmentError) as raised:
                iso.assert_engaged()
            self.assertEqual(raised.exception.category, iso.CATEGORY_NOT_ENGAGED)
            # ...and so does every test package, via test_safety.install().
            with self.assertRaises(iso.UnsafeTestEnvironmentError):
                test_safety.install()

    def test_it_fails_closed_when_the_write_boundary_is_missing(self):
        with mock.patch.object(iso, "_guard_installed", False):
            with self.assertRaises(iso.UnsafeTestEnvironmentError):
                iso.assert_engaged()

    def test_it_fails_closed_if_the_namespace_disappears_or_an_effective_root_reappears_as_protected(self):
        state = dict(settings.ISADORAAIR_TEST_ISOLATION, namespace="/tmp/isadoraair-test-data.gone")
        with override_settings(ISADORAAIR_TEST_ISOLATION=state), self.assertRaises(iso.UnsafeTestEnvironmentError):
            iso.assert_engaged()
        with override_settings(WAVEFORMS_DIR="/srv/isadoraair/waveforms"):
            with self.assertRaises(iso.UnsafeTestEnvironmentError) as raised:
                iso.assert_engaged()
            self.assertEqual(raised.exception.category, iso.CATEGORY_SETTINGS)

    def test_the_description_names_the_namespace_and_every_ignored_stale_value(self):
        state = {"namespace": "/tmp/ns", "roots": {"WAVEFORMS_DIR": "/tmp/ns/waveforms"},
                 "database": {"NAME": "isadoraair_dev", "USER": "isadoraair_dev"},
                 "stale_configuration": {"WAVEFORMS_DIR": "/srv/isadoraair/waveforms"}}
        text = iso.describe(state)
        self.assertIn("ENGAGED", text)
        self.assertIn("configured WAVEFORMS_DIR=/srv/isadoraair/waveforms was NOT honoured", text)

    def test_the_guarded_runner_sandboxes_every_process_and_runs_the_preflight_first(self):
        script = (BASE / "production" / "tests" / "run_with_validation_service.sh").read_text()
        self.assertIn('export ISADORAAIR_TEST_SANDBOX="$run" ISADORAAIR_TEST_CONTROL="$ctl" ISADORAAIR_TEST_DATA_ROOT="$data"',
                      script)
        self.assertIn('"$sandbox" service -- "$python" "$here/manage.py" "$4"', script)       # validation + render units
        self.assertIn('-p Delegate=yes "$sandbox" scope -- "$here/production/tests/run_delegated.sh" "$@"', script)
        self.assertIn('"$sandbox" plain -- "$python" "$here/manage.py" test_isolation_preflight', script)
        self.assertIn('"$sandbox" plain -- "$pg_bin/postgres" -D', script)
        self.assertLess(script.index("test_isolation_preflight >"), script.index("systemd-run --user --quiet"))
        self.assertEqual(script.count("verify_unit \"$1\""), 1)                            # every service is checked
        self.assertNotIn("systemd-run --user --scope --quiet -p Delegate=yes \"$here/production/tests/run_delegated.sh\"",
                         script)                                                           # never unsandboxed


class FreshProcessControlTests(SimpleTestCase):
    """Negative controls in real interpreters: unsafe configuration is neutralised or refused
    before any test could run. No database connection is made; no test is run."""

    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="isolation-control.")))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        # A namespace must sit directly in the approved temp base, so make it there.
        self.namespace = Path(os.path.realpath(tempfile.mkdtemp(prefix=iso.NAMESPACE_PREFIX)))
        self.addCleanup(shutil.rmtree, str(self.namespace), True)
        self.addCleanup(self.remove_links)
        self.links = []

    def remove_links(self):
        for link in self.links:
            with_name = Path(link)
            if with_name.is_symlink():
                with_name.unlink()

    def run_preflight(self, *, namespace="default", **env_changes):
        env = {k: v for k, v in os.environ.items() if k != iso.NAMESPACE_ENV}
        if namespace == "default":
            env[iso.NAMESPACE_ENV] = str(self.namespace)
        elif namespace is not None:
            env[iso.NAMESPACE_ENV] = namespace
        env.update(env_changes)
        return subprocess.run([sys.executable, "manage.py", "test_isolation_preflight"], cwd=BASE, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_a_fresh_process_with_a_valid_namespace_is_engaged_and_every_probe_is_refused(self):
        result = self.run_preflight()
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        report = json.loads(result.stdout)
        self.assertTrue(report["isolated"])
        self.assertTrue(all(r["outcome"] == "refused" for r in report["probes"]), report["probes"])

    def test_a_stale_env_naming_the_production_waveform_directory_is_neutralised_before_any_write(self):
        result = self.run_preflight(WAVEFORMS_DIR="/srv/isadoraair/waveforms", LIBRARY_ROOT="/srv/isadoraair/music")
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        report = json.loads(result.stdout)
        self.assertEqual(report["roots"]["WAVEFORMS_DIR"], str(self.namespace / "waveforms"))
        self.assertEqual(report["stale_configuration_not_honoured"]["WAVEFORMS_DIR"], "/srv/isadoraair/waveforms")

    def test_a_symlink_alias_of_the_production_waveform_directory_is_neutralised(self):
        alias = self.tmp / "waveforms-alias"
        alias.symlink_to("/srv/isadoraair/waveforms", target_is_directory=True)
        result = self.run_preflight(WAVEFORMS_DIR=str(alias), REPORTS_ROOT="/var/lib")
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        report = json.loads(result.stdout)
        self.assertEqual(report["roots"]["WAVEFORMS_DIR"], str(self.namespace / "waveforms"))
        self.assertEqual(report["roots"]["REPORTS_ROOT"], str(self.namespace / "reports"))
        self.assertIn("WAVEFORMS_DIR", report["stale_configuration_not_honoured"])

    def test_network_ingest_and_hour_refill_are_forced_off(self):
        result = self.run_preflight(WEB_REQUESTS_INGEST_URL="https://radio.example.org",
                                    WEB_REQUESTS_INGEST_API_KEY="secret", HOUR_REFILL_INSTALL_ARMED="true")
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        report = json.loads(result.stdout)
        self.assertTrue(report["ingest_url_blank"] and report["ingest_key_blank"])
        self.assertFalse(report["hour_refill_install_armed"])
        self.assertNotIn("secret", result.stdout + result.stderr)

    def test_a_production_service_socket_is_replaced(self):
        result = self.run_preflight(PRODUCTION_VALIDATION_SOCKET="/run/isadoraair-validation/validator.sock")
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        self.assertEqual(json.loads(result.stdout)["validation_socket"], str(self.namespace / "validator.sock"))

    def refused(self, result, category):
        self.assertNotEqual(result.returncode, 0, result.stdout[-800:])
        self.assertIn(category, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_no_namespace_means_the_guard_is_not_engaged_and_the_command_refuses(self):
        self.refused(self.run_preflight(namespace=None), iso.CATEGORY_NOT_ENGAGED)

    def test_a_namespace_inside_station_storage_is_refused(self):
        self.refused(self.run_preflight(namespace="/srv/isadoraair"), iso.CATEGORY_NAMESPACE)

    def test_a_namespace_that_is_a_symlink_to_station_storage_is_refused(self):
        link = Path(tempfile.gettempdir()) / f"{iso.NAMESPACE_PREFIX}link-{os.getpid()}"
        link.symlink_to("/srv/isadoraair/waveforms", target_is_directory=True)
        self.links.append(link)
        self.refused(self.run_preflight(namespace=str(link)), iso.CATEGORY_NAMESPACE)

    def test_a_missing_or_open_namespace_is_refused(self):
        absent = Path(tempfile.gettempdir()) / f"{iso.NAMESPACE_PREFIX}absent-{os.getpid()}"
        self.refused(self.run_preflight(namespace=str(absent)), iso.CATEGORY_NAMESPACE)
        os.chmod(self.namespace, 0o755)
        self.refused(self.run_preflight(), iso.CATEGORY_NAMESPACE)

    def test_production_database_credentials_are_refused_before_any_connection(self):
        self.refused(self.run_preflight(DB_NAME="isadoraair", DB_USER="isadoraair", DB_PASSWORD="not-a-real-password"),
                     iso.CATEGORY_DATABASE)
        self.refused(self.run_preflight(DB_USER="isadoraair"), iso.CATEGORY_DATABASE)


class DescriptorRelativeWriteTests(_Scratch):
    """P1-A: mutations relative to a directory descriptor, or through a descriptor, are judged
    by the directory or file they really reach. Only the synthetic station directory is aimed at."""

    def setUp(self):
        super().setUp()
        self.file = self.station / "1.json"
        self.file.write_bytes(b"station data")
        self.scratch = self.tmp / "scratch"
        self.scratch.mkdir()
        (self.scratch / "src").write_bytes(b"scratch")
        self.station_fd = os.open(self.station, os.O_RDONLY | os.O_DIRECTORY)
        self.scratch_fd = os.open(self.scratch, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.station_fd)
        self.addCleanup(os.close, self.scratch_fd)

    def assertStationUntouched(self):
        self.assertEqual(self.file.read_bytes(), b"station data")
        self.assertEqual(sorted(p.name for p in self.station.iterdir()), ["1.json"])

    def test_descriptor_relative_mutations_of_protected_storage_are_refused(self):
        with self.protected():
            attempts = {
                "os.open create": lambda: os.open("new.json", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=self.station_fd),
                "os.open truncate": lambda: os.open("1.json", os.O_WRONLY | os.O_TRUNC, dir_fd=self.station_fd),
                "os.open via '..' from scratch": lambda: os.open("../station/1.json", os.O_RDWR, dir_fd=self.scratch_fd),
                "os.rename in": lambda: os.rename("src", "1.json", src_dir_fd=self.scratch_fd, dst_dir_fd=self.station_fd),
                "os.rename out": lambda: os.rename("1.json", "stolen", src_dir_fd=self.station_fd,
                                                   dst_dir_fd=self.scratch_fd),
                "os.replace in": lambda: os.replace("src", "1.json", src_dir_fd=self.scratch_fd,
                                                    dst_dir_fd=self.station_fd),
                "os.unlink": lambda: os.unlink("1.json", dir_fd=self.station_fd),
                "os.rmdir via '..'": lambda: os.rmdir("../station", dir_fd=self.scratch_fd),
                "os.mkdir": lambda: os.mkdir("sub", dir_fd=self.station_fd),
                "os.symlink": lambda: os.symlink("/etc/hostname", "link", dir_fd=self.station_fd),
                "os.link out (a second name for the protected inode)":
                    lambda: os.link("1.json", "hardlink", src_dir_fd=self.station_fd, dst_dir_fd=self.scratch_fd),
                "os.chmod": lambda: os.chmod("1.json", 0o600, dir_fd=self.station_fd),
                "opener with dir_fd": lambda: open("1.json", "w", opener=lambda name, flags: os.open(
                    name, flags, 0o644, dir_fd=self.station_fd)),
                "shutil.rmtree with dir_fd": lambda: shutil.rmtree("station", dir_fd=os.open(self.tmp, os.O_RDONLY)),
            }
            for label, attempt in attempts.items():
                with self.subTest(label), self.assertRaises(iso.ProtectedPathWriteBlocked) as raised:
                    attempt()
                self.assertEqual(raised.exception.root, str(self.station), label)
        self.assertStationUntouched()
        self.assertEqual((self.scratch / "src").read_bytes(), b"scratch")

    def test_a_directory_descriptor_opened_through_a_symlink_alias_is_resolved(self):
        alias = self.tmp / "alias"
        alias.symlink_to(self.station, target_is_directory=True)
        alias_fd = os.open(alias, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, alias_fd)
        with self.protected():
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                os.open("1.json", os.O_WRONLY | os.O_TRUNC, dir_fd=alias_fd)
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                os.unlink("1.json", dir_fd=alias_fd)
        self.assertStationUntouched()

    def test_descriptor_based_mutations_are_judged_by_the_file_they_change(self):
        with self.protected():
            fd = os.open(self.file, os.O_RDONLY)              # reading is allowed
            self.addCleanup(os.close, fd)
            for label, attempt in {"fchmod": lambda: os.fchmod(fd, 0o600), "ftruncate": lambda: os.ftruncate(fd, 0),
                                   "fdopen for writing": lambda: os.fdopen(os.dup(fd), "w")}.items():
                with self.subTest(label), self.assertRaises(iso.ProtectedPathWriteBlocked):
                    attempt()
        self.assertStationUntouched()

    def test_physical_dotdot_after_a_symlink_is_resolved_like_the_kernel_does(self):
        (self.station / "inner").mkdir()                      # created before protection is registered
        link = self.scratch / "link"
        link.symlink_to(self.station / "inner", target_is_directory=True)
        tricky = f"{link}/../1.json"                           # the kernel reaches station/1.json
        self.assertEqual(os.path.realpath(tricky), str(self.file))
        with self.protected():
            self.assertEqual(iso.protected_match(tricky), str(self.station))
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                open(tricky, "w")
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                os.open("link/../1.json", os.O_WRONLY, dir_fd=self.scratch_fd)
        self.assertEqual(self.file.read_bytes(), b"station data")
        self.assertEqual(sorted(p.name for p in self.station.iterdir()), ["1.json", "inner"])

    def test_an_unresolvable_directory_descriptor_is_refused_not_skipped(self):
        gone = self.scratch / "gone"
        gone.mkdir()
        gone_fd = os.open(gone, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, gone_fd)
        gone.rmdir()                                          # the descriptor now names no path at all
        with self.protected():
            with self.assertRaises(iso.ProtectedPathWriteBlocked) as raised:
                os.open("x", os.O_WRONLY | os.O_CREAT, dir_fd=gone_fd)
        self.assertEqual(raised.exception.root, iso.UNRESOLVABLE)

    def test_a_relative_os_open_that_bypassed_the_wrapper_is_refused(self):
        import posix                                          # the unwrapped function, as a stale capture would be
        with self.assertRaises(iso.ProtectedPathWriteBlocked) as raised:
            posix.open("never-created", os.O_WRONLY | os.O_CREAT, 0o600, dir_fd=self.scratch_fd)
        self.assertEqual(raised.exception.root, iso.UNRESOLVABLE)
        self.assertFalse((self.scratch / "never-created").exists())

    def test_descriptor_relative_writes_in_scratch_still_work(self):
        with self.protected():
            fd = os.open("made.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self.scratch_fd)
            os.write(fd, b"ok")
            os.fchmod(fd, 0o644)
            os.ftruncate(fd, 2)
            os.close(fd)
            os.rename("made.json", "moved.json", src_dir_fd=self.scratch_fd, dst_dir_fd=self.scratch_fd)
            os.mkdir("sub", dir_fd=self.scratch_fd)
            os.symlink("moved.json", "link", dir_fd=self.scratch_fd)
            os.link("moved.json", "second", src_dir_fd=self.scratch_fd, dst_dir_fd=self.scratch_fd)
            os.unlink("second", dir_fd=self.scratch_fd)
            os.unlink("link", dir_fd=self.scratch_fd)
            os.rmdir("sub", dir_fd=self.scratch_fd)
            with open("opened.txt", "w", opener=lambda n, f: os.open(n, f, 0o644, dir_fd=self.scratch_fd)) as handle:
                handle.write("ok")
            with os.fdopen(os.open(self.scratch / "fdopen.txt", os.O_WRONLY | os.O_CREAT, 0o644), "w") as handle:
                handle.write("ok")
            with tempfile.TemporaryFile(dir=self.scratch) as unnamed:      # O_TMPFILE: no name at all
                unnamed.write(b"abc")
                unnamed.truncate(1)
            subprocess.run([sys.executable, "-c", "print('pipe ok')"], check=True, capture_output=True)
        self.assertEqual(iso.blocked_writes(), [])
        self.assertEqual((self.scratch / "moved.json").read_bytes(), b"ok")
        self.assertEqual((self.scratch / "opened.txt").read_text(), "ok")

    def test_a_swallowed_descriptor_relative_refusal_still_fails_the_run(self):
        with self.protected():
            try:
                os.unlink("1.json", dir_fd=self.station_fd)
            except BaseException:                                          # noqa: BLE001 -- swallow it entirely
                pass
        self.assertEqual(iso.fail_on_blocked(0, StringIO()), 1)
        self.assertStationUntouched()

    def test_a_descriptor_opened_before_the_guard_saw_it_is_a_documented_residual(self):
        # os.write has no audit event: a descriptor that was ALREADY writable is beyond the
        # Python layer (the kernel sandbox is the boundary for it). Shown on a synthetic file.
        preopened = self.station / "preopened.bin"
        preopened.write_bytes(b"before")
        fd = os.open(preopened, os.O_WRONLY)
        self.addCleanup(os.close, fd)
        with self.protected():
            os.write(fd, b"AFTER!")
            with self.assertRaises(iso.ProtectedPathWriteBlocked):
                import mmap
                mmap.mmap(fd, 6, access=mmap.ACCESS_WRITE)               # a writable mapping is checked
        self.assertEqual(preopened.read_bytes(), b"AFTER!")


class KernelSandboxTests(SimpleTestCase):
    """P1-B: this process, and anything it starts, is inside production/tests/sandbox_exec.sh."""

    def test_this_process_is_inside_the_kernel_sandbox(self):
        self.assertEqual(iso.sandbox_problems(), [])
        self.assertIn("isadoraair-test-run.", settings.ISADORAAIR_TEST_ISOLATION["sandbox"])

    def test_station_storage_and_host_service_sockets_are_not_there(self):
        for path in iso.SANDBOX_HIDDEN_PATHS:
            with self.subTest(path):
                self.assertTrue(not os.path.lexists(path) or (os.path.isdir(path) and not os.listdir(path)))

    def test_missing_or_spoofed_sandbox_facts_are_reported(self):
        run = os.environ[iso.SANDBOX_ENV]
        self.assertIn("is not set", iso.sandbox_problems({})[0])
        self.assertTrue(any("canonical sandbox run directory" in p
                            for p in iso.sandbox_problems(dict(os.environ, **{iso.SANDBOX_ENV: "/tmp/elsewhere"}))))
        self.assertTrue(any("PGPASSWORD" in p for p in iso.sandbox_problems(dict(os.environ, PGPASSWORD="x"))))
        self.assertTrue(any("HOME" in p for p in iso.sandbox_problems(dict(os.environ, HOME="/home/someone"))))
        self.assertEqual(iso.sandbox_problems(dict(os.environ, **{iso.SANDBOX_ENV: run})), [])

    def test_inside_the_sandbox_the_database_must_be_the_disposable_server(self):
        run = os.environ[iso.SANDBOX_ENV]
        environ = {iso.SANDBOX_ENV: run, iso.ISOLATED_CI_ENV: "1"}
        dev = {"NAME": "isadoraair_dev", "USER": "isadoraair_dev"}
        self.assertIsNone(iso._database_problem({"default": dict(dev, HOST=f"{run}/pgsock")}, environ))
        for host in ("localhost", "127.0.0.1", "/run/postgresql", "/var/run/postgresql", ""):
            with self.subTest(host):
                self.assertIn("disposable", iso._database_problem({"default": dict(dev, HOST=host)}, environ))
        # The isolated-CI escape never applies inside the sandbox.
        self.assertTrue(iso._database_problem({"default": {"NAME": "ci", "USER": "ci", "HOST": f"{run}/pgsock"}},
                                              environ))

    def test_a_fresh_interpreter_without_the_sandbox_refuses_before_any_write(self):
        env = {k: v for k, v in os.environ.items() if k != iso.SANDBOX_ENV}
        for extra in ({}, {iso.ISOLATED_CI_ENV: "1"}):          # the CI exemption never applies in here
            with self.subTest(extra):
                result = subprocess.run([sys.executable, "manage.py", "test_isolation_preflight"], cwd=BASE,
                                        env=dict(env, **extra), capture_output=True, text=True, timeout=120)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(iso.CATEGORY_SANDBOX, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_shell_and_native_children_cannot_write_the_checkout(self):
        # The checkout is writable on the host; inside the sandbox it is a read-only mount.
        target = BASE / f".sandbox-child-probe-{os.getpid()}"
        self.addCleanup(lambda: target.unlink() if target.exists() else None)
        shell = subprocess.run(["/bin/sh", "-c", f"echo x > '{target}'"], capture_output=True, text=True)
        self.assertNotEqual(shell.returncode, 0)
        self.assertIn("Read-only file system", shell.stderr)
        if shutil.which("ffmpeg"):
            ffmpeg = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                                     "-i", "sine=frequency=440:duration=0.2", "-f", "mp3", str(target)],
                                    capture_output=True, text=True, timeout=60)
            self.assertNotEqual(ffmpeg.returncode, 0)
            self.assertIn("Read-only file system", ffmpeg.stderr)
            scratch = Path(settings.WAVEFORMS_DIR) / f"ffmpeg-{os.getpid()}.mp3"
            ok = subprocess.run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                                 "-i", "sine=frequency=440:duration=0.2", "-f", "mp3", str(scratch)],
                                capture_output=True, text=True, timeout=60)
            self.assertEqual(ok.returncode, 0, ok.stderr)
            scratch.unlink()
        self.assertFalse(target.exists())

    def test_children_cannot_reach_the_service_manager_or_a_host_database(self):
        result = subprocess.run(["systemd-run", "--user", "--quiet", "/bin/true"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        for path in ("/run/user", "/run/postgresql", "/var/run/postgresql", "/run/dbus"):
            self.assertFalse(os.path.lexists(path), path)
        with open("/proc/self/net/dev", encoding="ascii") as stream:
            self.assertEqual([line.split(":")[0].strip() for line in stream.readlines()[2:]], ["lo"])
