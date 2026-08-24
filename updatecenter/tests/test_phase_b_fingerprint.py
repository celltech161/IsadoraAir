from django.test import SimpleTestCase

from .phase_b_helpers import RUNTIME_ROOT  # installs standalone runtime on sys.path
from updatecenter.execution_contract import execution_fingerprint, execution_fingerprint_payload
from isadoraair_updater.release import fingerprint as root_fingerprint
from isadoraair_updater.release import execution_fingerprint_payload as root_payload


class IndependentFingerprintParityTests(SimpleTestCase):
    def test_application_and_protected_runtime_specs_match_exactly(self):
        values = dict(
            installed_release_id="r0002", installed_commit="a" * 40,
            target_release_id="r0004", target_commit="b" * 40,
            releases_in_plan=("r0003", "r0004"),
            migrations_required=("sample.0002_add",), migration_compatibility="additive",
            python_requirements_changed=False, apt_packages_new=(),
            systemd_units_changed=("isadoraair-engine.service",),
            systemd_units_new_required=(), systemd_units_new_optional=("isadoraair-updater.service",),
            systemd_units_removed_or_renamed=(), collectstatic_required=False,
            services_requiring_restart=("isadoraair-engine",), nginx_changed=False,
            runtime_components_changed=False, minimum_updater_protocol_version=1,
            manual_bootstrap_required=False,
        )
        self.assertEqual(execution_fingerprint_payload(**values), root_payload(**values))
        self.assertEqual(execution_fingerprint(**values), root_fingerprint(root_payload(**values)))
