from django.db import migrations, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ModelState, ProjectState
from django.test import SimpleTestCase

from updatecenter.management.commands.updatecenter_probe import (
    _classify_operation,
)


class AddFieldClassificationTests(SimpleTestCase):
    def _classify(self, field):
        operation = migrations.AddField(
            model_name="item", name="added", field=field
        )
        return _classify_operation(operation)

    def test_nullable_field_is_additive(self):
        result = self._classify(models.TextField(null=True))
        self.assertEqual(result["classification"], "additive")

    def test_non_null_char_field_with_literal_default_is_additive(self):
        for default in ("", "cobalt_c300"):
            with self.subTest(default=default):
                result = self._classify(
                    models.CharField(max_length=24, default=default)
                )
                self.assertEqual(result["classification"], "additive")

    def test_non_null_field_without_default_is_manual(self):
        result = self._classify(models.CharField(max_length=24))
        self.assertEqual(result["classification"], "manual")

    def test_callable_or_non_scalar_default_is_manual(self):
        for default in (lambda: "value", {"value": "unsafe"}):
            with self.subTest(default=default):
                result = self._classify(
                    models.CharField(max_length=24, default=default)
                )
                self.assertEqual(result["classification"], "manual")

    def test_unique_primary_key_relational_and_indexed_fields_are_manual(self):
        fields = (
            models.CharField(max_length=24, default="value", unique=True),
            models.IntegerField(default=1, primary_key=True),
            models.ForeignKey(
                "sample.Parent", default=1, on_delete=models.CASCADE
            ),
            models.CharField(max_length=24, default="value", db_index=True),
        )
        for field in fields:
            with self.subTest(field=field):
                result = self._classify(field)
                self.assertEqual(result["classification"], "manual")

    def test_database_default_is_not_treated_as_simple_literal_default(self):
        result = self._classify(
            models.CharField(
                max_length=24, default="python", db_default="database"
            )
        )
        self.assertEqual(result["classification"], "manual")


class AlterFieldClassificationTests(SimpleTestCase):
    def _classify(self, old_field, new_field):
        state = ProjectState()
        state.add_model(
            ModelState(
                app_label="sample",
                name="Item",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("value", old_field),
                ],
            )
        )
        operation = migrations.AlterField(
            model_name="item", name="value", field=new_field
        )
        before_state = state.clone()
        operation.state_forwards("sample", state)
        return _classify_operation(
            operation,
            app_label="sample",
            before_state=before_state,
            after_state=state,
        )

    def test_help_text_only_change_is_additive(self):
        result = self._classify(
            models.CharField(max_length=24, help_text="old"),
            models.CharField(max_length=24, help_text="new"),
        )
        self.assertEqual(result["classification"], "additive")

    def test_nullability_change_is_manual(self):
        result = self._classify(
            models.CharField(max_length=24, null=True),
            models.CharField(max_length=24, null=False),
        )
        self.assertEqual(result["classification"], "manual")

    def test_max_length_and_type_changes_are_manual(self):
        cases = (
            (
                models.CharField(max_length=24),
                models.CharField(max_length=48),
            ),
            (models.CharField(max_length=24), models.TextField()),
        )
        for old_field, new_field in cases:
            with self.subTest(new_field=new_field):
                result = self._classify(old_field, new_field)
                self.assertEqual(result["classification"], "manual")

    def test_unique_index_column_and_database_default_changes_are_manual(self):
        cases = (
            models.CharField(max_length=24, unique=True),
            models.CharField(max_length=24, db_index=True),
            models.CharField(max_length=24, db_column="different_column"),
            models.CharField(max_length=24, db_default="database"),
        )
        for new_field in cases:
            with self.subTest(new_field=new_field):
                result = self._classify(
                    models.CharField(max_length=24), new_field
                )
                self.assertEqual(result["classification"], "manual")

    def test_primary_key_and_relation_changes_are_manual(self):
        cases = (
            (
                models.IntegerField(default=1),
                models.IntegerField(default=1, primary_key=True),
            ),
            (
                models.ForeignKey(
                    "sample.Parent", on_delete=models.CASCADE
                ),
                models.ForeignKey(
                    "sample.OtherParent", on_delete=models.CASCADE
                ),
            ),
            (
                models.ForeignKey(
                    "sample.Parent", on_delete=models.CASCADE
                ),
                models.ForeignKey(
                    "sample.Parent", on_delete=models.PROTECT
                ),
            ),
        )
        for old_field, new_field in cases:
            with self.subTest(new_field=new_field):
                result = self._classify(old_field, new_field)
                self.assertEqual(result["classification"], "manual")


class ActualR0011MigrationClassificationTests(SimpleTestCase):
    def test_every_actual_monitoring_0011_operation_is_additive(self):
        loader = MigrationLoader(None)
        migration_key = (
            "monitoring", "0011_transmitter_vendor_and_password"
        )
        migration = loader.disk_migrations[migration_key]
        state = loader.project_state(
            [("monitoring", "0010_alter_listenerpeak_tlh_since_at")]
        )
        classifications = []

        for operation in migration.operations:
            before_state = (
                state.clone()
                if operation.__class__.__name__ == "AlterField"
                else None
            )
            operation.state_forwards("monitoring", state)
            classifications.append(
                _classify_operation(
                    operation,
                    app_label="monitoring",
                    before_state=before_state,
                    after_state=state,
                )
            )

        self.assertEqual(len(classifications), 7)
        self.assertEqual(
            [item["operation"] for item in classifications],
            ["AddField", "AddField"] + ["AlterField"] * 5,
        )
        self.assertEqual(
            {item["classification"] for item in classifications},
            {"additive"},
        )
        self.assertEqual(
            [item["detail"] for item in classifications[:2]],
            ["non-null field with explicit simple literal default"] * 2,
        )
        self.assertEqual(
            [item["detail"] for item in classifications[2:]],
            [
                "field definition differs only in approved non-database metadata"
            ]
            * 5,
        )
