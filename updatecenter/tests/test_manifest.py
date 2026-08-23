"""Structural manifest validation tests -- [P0] 1.1 Phase A."""
import json
from pathlib import Path

from django.test import SimpleTestCase

from updatecenter import manifest as m


def _valid_bootstrap(**overrides):
    data = {
        "schema_version": 1,
        "release_id": "r0001",
        "previous_release_id": None,
        "bootstrap_commit": "5a0cb0e707ac5e7fd364fca5f8ca801a1cc4c969",
        "minimum_updater_protocol_version": 1,
        "summary": "bootstrap",
        "migrations_required": [],
        "migration_compatibility": None,
        "python_requirements_changed": False,
        "requirements_sha256": None,
        "apt_packages_new": [],
        "systemd_units_changed": [],
        "systemd_units_new_required": [],
        "systemd_units_new_optional": [],
        "systemd_units_removed_or_renamed": [],
        "collectstatic_required": False,
        "services_requiring_restart": [],
        "nginx_changed": False,
        "runtime_components_changed": False,
    }
    data.update(overrides)
    return data


def _valid_followup(**overrides):
    data = {
        "schema_version": 1,
        "release_id": "r0002",
        "previous_release_id": "r0001",
        "minimum_updater_protocol_version": 1,
        "summary": "second release",
        "migrations_required": ["library.0079_mediaplaybackincident"],
        "migration_compatibility": "additive",
        "python_requirements_changed": True,
        "requirements_sha256": "a" * 64,
        "apt_packages_new": [],
        "systemd_units_changed": [],
        "systemd_units_new_required": [],
        "systemd_units_new_optional": [],
        "systemd_units_removed_or_renamed": [],
        "collectstatic_required": False,
        "services_requiring_restart": ["isadoraair-engine"],
        "nginx_changed": False,
        "runtime_components_changed": False,
    }
    data.update(overrides)
    return data


class ValidManifestsTests(SimpleTestCase):
    def test_valid_bootstrap_parses(self):
        parsed = m.validate_manifest_dict(_valid_bootstrap())
        self.assertEqual(parsed.release_id, "r0001")
        self.assertTrue(parsed.is_bootstrap)

    def test_actual_repo_bootstrap_manifest_is_valid(self):
        """The real deploy/releases/r0001.json this session wrote --
        proves the shipped file, not just a synthetic fixture, is
        valid. [P0] 1.1 correction: r0001 now correctly DECLARES its
        four schema-required migrations (see
        updatecenter/tests/test_planner.py's
        BootstrapSchemaExpectationTests for the full evidence-based
        reasoning) -- it does not claim zero, that was the bug."""
        path = Path(__file__).resolve().parents[2] / "deploy" / "releases" / "r0001.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        parsed = m.validate_manifest_dict(data, source_label="r0001.json")
        self.assertEqual(parsed.release_id, "r0001")
        self.assertTrue(parsed.is_bootstrap)
        self.assertEqual(
            set(parsed.migrations_required),
            {"tts.0001_initial", "road_conditions.0010_roadconditionsconfiguration_tts",
             "weather.0007_weathervoicepersona", "webrequests.0008_webrequestconfig_dedication_tts"},
        )

    def test_actual_phase_a_release_manifest_is_valid_and_non_bootstrap(self):
        path = Path(__file__).resolve().parents[2] / "deploy" / "releases" / "r0002.json"
        parsed = m.validate_manifest_dict(
            json.loads(path.read_text(encoding="utf-8")), source_label="r0002.json",
        )
        self.assertEqual(parsed.previous_release_id, "r0001")
        self.assertIsNone(parsed.bootstrap_commit)
        self.assertEqual(parsed.migrations_required, ("updatecenter.0001_initial",))
        self.assertEqual(parsed.services_requiring_restart, ("isadoraair-gunicorn",))
        self.assertFalse(parsed.collectstatic_required)

    def test_valid_followup_parses(self):
        parsed = m.validate_manifest_dict(_valid_followup())
        self.assertFalse(parsed.is_bootstrap)
        self.assertEqual(parsed.migrations_required, ("library.0079_mediaplaybackincident",))


class RejectionTests(SimpleTestCase):
    def test_unsupported_schema_version_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "schema_version"):
            m.validate_manifest_dict(_valid_bootstrap(schema_version=99))

    def test_unknown_field_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "unrecognized field"):
            m.validate_manifest_dict(_valid_bootstrap(totally_made_up_field=True))

    def test_pre_update_hooks_rejected_with_specific_message(self):
        with self.assertRaisesMessage(m.ManifestError, "hooks are rejected by design"):
            m.validate_manifest_dict(_valid_bootstrap(pre_update_hooks=["rm -rf /"]))

    def test_post_update_hooks_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(post_update_hooks=[]))

    def test_arbitrary_shell_field_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(shell="echo hi"))

    def test_arbitrary_commands_field_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(commands=[["ls"]]))

    def test_self_referential_release_commit_field_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "self-referential"):
            m.validate_manifest_dict(_valid_followup(release_commit="deadbeef" * 5))

    def test_bare_commit_field_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(commit="deadbeef" * 5))

    def test_bare_sha_field_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(sha="deadbeef" * 5))

    def test_git_sha_field_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(git_sha="deadbeef" * 5))

    def test_bootstrap_commit_forbidden_on_non_bootstrap_release(self):
        """A non-bootstrap release embedding a commit SHA is exactly
        the self-reference risk this schema forbids -- its commit
        identity must come only from git history."""
        with self.assertRaisesMessage(m.ManifestError, "only valid on the bootstrap"):
            m.validate_manifest_dict(_valid_followup(bootstrap_commit="a" * 40))

    def test_bootstrap_release_without_bootstrap_commit_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "no bootstrap_commit"):
            data = _valid_bootstrap()
            del data["bootstrap_commit"]
            m.validate_manifest_dict(data)

    def test_bootstrap_commit_must_be_full_sha(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(bootstrap_commit="not-a-sha"))

    def test_self_predecessor_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "self-predecessor"):
            m.validate_manifest_dict(_valid_followup(release_id="r0002", previous_release_id="r0002"))

    def test_missing_required_field_rejected(self):
        data = _valid_bootstrap()
        del data["collectstatic_required"]
        with self.assertRaisesMessage(m.ManifestError, "missing required"):
            m.validate_manifest_dict(data)

    def test_invalid_migration_ref_format_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(migrations_required=["not a valid ref"]))

    def test_duplicate_migration_ref_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "duplicate"):
            m.validate_manifest_dict(_valid_followup(
                migrations_required=["library.0079_mediaplaybackincident", "library.0079_mediaplaybackincident"]
            ))

    def test_migration_compatibility_required_when_migrations_present(self):
        with self.assertRaisesMessage(m.ManifestError, "migration_compatibility"):
            m.validate_manifest_dict(_valid_followup(migration_compatibility=None))

    def test_migration_compatibility_must_be_null_when_no_migrations(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(migration_compatibility="additive"))

    def test_invalid_migration_compatibility_value_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(migration_compatibility="maybe"))

    def test_requirements_sha_required_when_changed_true(self):
        with self.assertRaisesMessage(m.ManifestError, "requirements_sha256"):
            m.validate_manifest_dict(_valid_followup(requirements_sha256=None))

    def test_requirements_sha_must_be_null_when_unchanged(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(python_requirements_changed=False, requirements_sha256="a" * 64))

    def test_malformed_requirements_sha_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(requirements_sha256="not-hex"))

    def test_unknown_service_name_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "unknown service"):
            m.validate_manifest_dict(_valid_followup(services_requiring_restart=["some-random-service"]))

    def test_invalid_unit_name_shape_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(systemd_units_changed=["../../etc/passwd"]))

    def test_unit_name_without_service_or_timer_suffix_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(systemd_units_changed=["isadoraair-engine"]))

    def test_unit_in_two_lists_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "more than one systemd_units"):
            m.validate_manifest_dict(_valid_followup(
                systemd_units_changed=["isadoraair-engine.service"],
                systemd_units_new_required=["isadoraair-engine.service"],
            ))

    def test_invalid_apt_package_name_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(apt_packages_new=["Bad Name!"]))

    def test_duplicate_apt_package_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_followup(apt_packages_new=["ffmpeg", "ffmpeg"]))

    def test_summary_length_bounded(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(summary="x" * 501))

    def test_release_id_pattern_enforced(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(release_id="not-a-release-id"))

    def test_minimum_updater_protocol_version_too_new_rejected(self):
        with self.assertRaisesMessage(m.ManifestError, "requires updater protocol"):
            m.validate_manifest_dict(_valid_bootstrap(minimum_updater_protocol_version=999))

    def test_non_dict_manifest_rejected(self):
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(["not", "a", "dict"])

    def test_bool_rejected_where_int_expected(self):
        """Python's bool is a subclass of int -- a naive isinstance(x,
        int) check would silently accept True/False as schema_version.
        Confirms _require_type's explicit bool guard actually works."""
        with self.assertRaises(m.ManifestError):
            m.validate_manifest_dict(_valid_bootstrap(schema_version=True))
