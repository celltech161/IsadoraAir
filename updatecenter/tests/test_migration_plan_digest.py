"""Reviewed migration approval -- canonical plan digest.

Proves compute_migration_plan_digest is deterministic and sensitive to
every dimension the workorder named: target commit, migration file
content, added/removed/reordered/reclassified operations. Pure-function
tests (no DB, no subprocess) against the exact canonicalization the
protected executor and the approval-lookup both rely on.
"""
from django.test import SimpleTestCase

from updatecenter.management.commands.updatecenter_probe import (
    compute_migration_plan_digest, extract_manual_operations,
)


def make_plan(*, target_commit_seed="a", extra_op=None, reorder=False, reclassify=False):
    op_a = {"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}
    op_b = {"operation": "AddField", "classification": "manual", "detail": "non-null AddField uses relational field"}
    if reclassify:
        op_b = dict(op_b, classification="additive", detail="nullable field")
    ops_first = [op_a, op_b]
    if reorder:
        ops_first = [op_b, op_a]
    plan = [
        {
            "ref": "sample.0001_initial",
            "migration_file_sha256": target_commit_seed * 64,
            "operations": ops_first,
        },
    ]
    if extra_op is not None:
        plan.append({
            "ref": "sample.0002_extra",
            "migration_file_sha256": "e" * 64,
            "operations": [extra_op],
        })
    return plan


class DigestDeterminismTests(SimpleTestCase):
    def _digest(self, **kwargs):
        plan = make_plan(**kwargs)
        return compute_migration_plan_digest(
            release_id="r0100", target_commit="b" * 40, manifest_sha256="c" * 64, plan=plan,
        )

    def test_identical_inputs_produce_identical_digest(self):
        self.assertEqual(self._digest(), self._digest())

    def test_changed_target_commit_changes_digest(self):
        plan = make_plan()
        d1 = compute_migration_plan_digest(release_id="r0100", target_commit="b" * 40, manifest_sha256="c" * 64, plan=plan)
        d2 = compute_migration_plan_digest(release_id="r0100", target_commit="d" * 40, manifest_sha256="c" * 64, plan=plan)
        self.assertNotEqual(d1, d2)

    def test_changed_release_id_changes_digest(self):
        plan = make_plan()
        d1 = compute_migration_plan_digest(release_id="r0100", target_commit="b" * 40, manifest_sha256="c" * 64, plan=plan)
        d2 = compute_migration_plan_digest(release_id="r0101", target_commit="b" * 40, manifest_sha256="c" * 64, plan=plan)
        self.assertNotEqual(d1, d2)

    def test_changed_manifest_hash_changes_digest(self):
        plan = make_plan()
        d1 = compute_migration_plan_digest(release_id="r0100", target_commit="b" * 40, manifest_sha256="c" * 64, plan=plan)
        d2 = compute_migration_plan_digest(release_id="r0100", target_commit="b" * 40, manifest_sha256="f" * 64, plan=plan)
        self.assertNotEqual(d1, d2)

    def test_changed_migration_file_content_changes_digest(self):
        # Different migration_file_sha256, same operations otherwise --
        # simulates "the ref/operations look the same but the actual
        # file content differs."
        d1 = self._digest(target_commit_seed="a")
        d2 = self._digest(target_commit_seed="9")
        self.assertNotEqual(d1, d2)

    def test_added_operation_changes_digest(self):
        d1 = self._digest()
        d2 = self._digest(extra_op={"operation": "AddField", "classification": "additive", "detail": "nullable field"})
        self.assertNotEqual(d1, d2)

    def test_reordered_operations_change_digest(self):
        d1 = self._digest()
        d2 = self._digest(reorder=True)
        self.assertNotEqual(d1, d2)

    def test_reclassified_operation_changes_digest(self):
        d1 = self._digest()
        d2 = self._digest(reclassify=True)
        self.assertNotEqual(d1, d2)

    def test_digest_is_a_64_char_hex_string(self):
        digest = self._digest()
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises ValueError if not valid hex


class ExtractManualOperationsTests(SimpleTestCase):
    def test_only_manual_operations_are_extracted_in_plan_order(self):
        plan = [
            {
                "ref": "sample.0001_initial",
                "migration_file_sha256": "a" * 64,
                "operations": [
                    {"operation": "CreateModel", "classification": "additive", "detail": "new table/model"},
                    {"operation": "AddField", "classification": "manual", "detail": "relational"},
                ],
            },
            {
                "ref": "sample.0002_next",
                "migration_file_sha256": "b" * 64,
                "operations": [
                    {"operation": "RunPython", "classification": "manual", "detail": "outside allowlist"},
                ],
            },
        ]
        manual = extract_manual_operations(plan)
        self.assertEqual(len(manual), 2)
        self.assertEqual(manual[0], {
            "ref": "sample.0001_initial", "operation_index": 1,
            "operation": "AddField", "classification": "manual", "detail": "relational",
        })
        self.assertEqual(manual[1], {
            "ref": "sample.0002_next", "operation_index": 0,
            "operation": "RunPython", "classification": "manual", "detail": "outside allowlist",
        })

    def test_no_manual_operations_returns_empty_list(self):
        plan = [{
            "ref": "sample.0001_initial", "migration_file_sha256": "a" * 64,
            "operations": [{"operation": "CreateModel", "classification": "additive", "detail": "new table/model"}],
        }]
        self.assertEqual(extract_manual_operations(plan), [])
