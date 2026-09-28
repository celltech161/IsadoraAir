"""Reviewed migration approval -- r0089 principal acceptance fixture.

r0089 (roadmap 2.5's authz app + library.0085) is the real-world release
that originally motivated this workorder: its migration set contains six
operations the mechanical classifier cannot prove automatic (one
ManyToManyField-through AddField, five RunPython data-seed operations).
This file proves the mechanism discovers exactly that set NATURALLY, by
rolling this test database back to its pre-r0089 state and running the
real build_probe_payload() against the real migration graph -- nothing
about r0089, authz, or these specific migration names is hard-coded
anywhere in the implementation itself (see updatecenter_probe.py and
executor.py: neither imports or references "r0089"/"authz" at all).

Uses the same real-migration-rollback technique already established in
test_phase_b_probe_integration.py's R0011/R0075 classes.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TransactionTestCase, override_settings

from updatecenter.management.commands.updatecenter_probe import build_probe_payload
from updatecenter.models import MigrationPlanApproval, UpdateJob, UpdateJobState

User = get_user_model()

R0089_TARGET_COMMIT = "0" * 40  # placeholder -- this test never touches git, only DB state


@override_settings(SECURE_SSL_REDIRECT=False)
class R0089AcceptanceTests(TransactionTestCase):
    def setUp(self):
        self.executor = MigrationExecutor(connection)
        self.pre_r0089_targets = [
            ("authz", None),
            ("library", "0084_alter_uitheme_logo_alter_uitheme_station_logo"),
        ]
        self.leaf_targets = self.executor.loader.graph.leaf_nodes()
        self.executor.migrate(self.pre_r0089_targets)

    def tearDown(self):
        MigrationExecutor(connection).migrate(self.leaf_targets)

    def test_discovers_exactly_six_manual_operations_naturally(self):
        payload = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        self.assertEqual(len(payload["manual_operations"]), 6)
        refs = sorted({entry["ref"] for entry in payload["manual_operations"]})
        self.assertEqual(refs, sorted([
            "authz.0001_initial",
            "authz.0002_seed_capabilities_and_roles",
            "authz.0004_seed_schedule_access_config",
            "authz.0006_correct_remote_host_playout_capability",
            "authz.0007_authorization_closeout_capabilities",
            "library.0085_remote_dj_queue_set_next_access",
        ]))
        # The one AddField (Role.capabilities, M2M-through) plus five
        # RunPython -- exactly the shape investigated in the original
        # r0089 report, discovered here mechanically, not asserted by fiat.
        by_ref = {entry["ref"]: entry for entry in payload["manual_operations"]}
        self.assertEqual(by_ref["authz.0001_initial"]["operation"], "AddField")
        for ref in (
            "authz.0002_seed_capabilities_and_roles", "authz.0004_seed_schedule_access_config",
            "authz.0006_correct_remote_host_playout_capability",
            "authz.0007_authorization_closeout_capabilities",
            "library.0085_remote_dj_queue_set_next_access",
        ):
            self.assertEqual(by_ref[ref]["operation"], "RunPython")

    def test_produces_a_stable_digest_across_repeated_probes(self):
        first = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        second = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        self.assertEqual(first["migration_plan_digest"], second["migration_plan_digest"])
        self.assertIsNotNone(first["migration_plan_digest"])

    def test_without_approval_reports_not_found_and_no_wildcard_matches(self):
        payload = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        self.assertEqual(payload["approval"], {"found": False})

        # An approval for a DIFFERENT release must never match.
        MigrationPlanApproval.objects.create(
            target_release_id="r0090", target_commit=R0089_TARGET_COMMIT,
            migration_plan_digest=payload["migration_plan_digest"],
            approved_by_username="operator", reason="wrong release",
        )
        payload_again = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        self.assertEqual(payload_again["approval"], {"found": False})

        # An approval for r0089 with a WRONG (stale/forged) digest must
        # never match either -- this is the "no wildcard approval" and
        # "browser/forged digest cannot substitute" proof: only an
        # approval whose digest is EXACTLY what this fresh, independent
        # recomputation produced is ever honored.
        MigrationPlanApproval.objects.create(
            target_release_id="r0089", target_commit=R0089_TARGET_COMMIT,
            migration_plan_digest="f" * 64,
            approved_by_username="operator", reason="forged/stale digest",
        )
        payload_yet_again = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        self.assertEqual(payload_yet_again["approval"], {"found": False})

    def test_exact_approval_through_the_real_view_is_then_found(self):
        payload = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        digest = payload["migration_plan_digest"]

        superuser = User.objects.create_superuser("r0089_su", "su@example.invalid", "pw")
        job = UpdateJob.objects.create(
            initiated_by=superuser, initiated_by_username="r0089_su",
            installed_release_id="r0088", target_release_id="r0089",
            installed_commit="1" * 40, target_commit=R0089_TARGET_COMMIT,
            state=UpdateJobState.MANUAL_INTERVENTION_REQUIRED,
            failure_classification="MIGRATION_OPERATION_MANUAL",
            migration_plan_review={
                "release_id": "r0089", "target_commit": R0089_TARGET_COMMIT,
                "manifest_sha256": "a" * 64, "migration_plan_digest": digest,
                "manual_operations": payload["manual_operations"],
            },
        )
        client = Client()
        client.force_login(superuser)
        response = client.post(
            f"/updates/jobs/{job.id}/migration-review/approve/",
            {"confirmed_migration_plan_digest": digest, "reason": "Reviewed all six operations; safe."},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            MigrationPlanApproval.objects.filter(target_release_id="r0089", migration_plan_digest=digest).exists()
        )

        # Fresh, independent recomputation now finds it.
        final_payload = build_probe_payload(release_id="r0089", target_commit=R0089_TARGET_COMMIT)
        self.assertTrue(final_payload["approval"]["found"])
        self.assertEqual(final_payload["approval"]["approved_by"], "r0089_su")

        # The ORIGINAL stopped job is untouched -- still exactly the
        # historical record it was, never resumed/mutated by the approval.
        job.refresh_from_db()
        self.assertEqual(job.state, UpdateJobState.MANUAL_INTERVENTION_REQUIRED)
