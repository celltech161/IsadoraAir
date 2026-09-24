from io import StringIO
import json
from pathlib import Path
import stat
import tempfile

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from hardware.management.commands.check_stereotool_bindings import (
    AcceptanceError,
    NOT_CONFIGURED,
    READY,
    REVIEW_REQUIRED,
    inspect_stereotool_bindings,
    record_stereotool_binding_acceptance,
)


class StereoToolBindingReadinessTests(SimpleTestCase):
    MACHINE_ID = "0123456789abcdef0123456789abcdef"
    CARDS = {
        "Loopback": 0,
        "D10s": 1,
        "Loopback_1": 3,
        "Loopback_2": 4,
        "PCH": 6,
    }

    def _write_rc(self, text, mode=0o600):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / ".stereo_tool.rc"
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    def _inspect(self, path, **kwargs):
        return inspect_stereotool_bindings(
            path,
            machine_id=kwargs.pop("machine_id", self.MACHINE_ID),
            cards=kwargs.pop("cards", self.CARDS),
            **kwargs,
        )

    def _accept(self, path, **kwargs):
        return record_stereotool_binding_acceptance(
            path,
            machine_id=kwargs.pop("machine_id", self.MACHINE_ID),
            cards=kwargs.pop("cards", self.CARDS),
            **kwargs,
        )

    def test_missing_rc_is_not_configured_and_fails_closed(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / "missing.rc"

        result = self._inspect(path)

        self.assertEqual(result.status, NOT_CONFIGURED)
        self.assertFalse(result.ready)
        self.assertFalse(result.rc_present)
        self.assertEqual(result.bindings, ())

    def test_present_rc_without_known_bindings_is_not_configured(self):
        path = self._write_rc("[Stereo]\nWidth = 1\n")

        result = self._inspect(path)

        self.assertEqual(result.status, NOT_CONFIGURED)
        self.assertFalse(result.ready)

    def test_enabled_embedded_numeric_binding_requires_review(self):
        path = self._write_rc(
            """
[Direct soundcard access]
Device ID = D10s: USB Audio (hw:1,0) (ALSA)
Enabled = 1
"""
        )

        result = self._inspect(path)

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.ready)
        binding = result.bindings[0]
        self.assertEqual(binding.addressing, "EMBEDDED_NUMERIC_HW")
        self.assertEqual(binding.card_index, 1)
        self.assertEqual(binding.device_number, 0)
        self.assertTrue(binding.acceptance_eligible)
        self.assertFalse(binding.accepted)

    def test_primary_input_without_enabled_fails_closed_but_is_acceptable(self):
        path = self._write_rc(
            """
[Soundcard - Input]
Device ID = Loopback: PCM (hw:0,1) (ALSA)
"""
        )

        result = self._inspect(path)

        self.assertEqual(result.status, REVIEW_REQUIRED)
        binding = result.bindings[0]
        self.assertEqual(binding.section, "Soundcard - Input")
        self.assertIsNone(binding.enabled)
        self.assertTrue(binding.acceptance_eligible)

    def test_explicitly_disabled_saved_binding_is_nonblocking(self):
        path = self._write_rc(
            """
[Low latency output]
Device ID = HDA Intel PCH: CX20632 Analog (hw:6,0) (ALSA)
Enabled = 0
"""
        )

        result = self._inspect(path)

        self.assertEqual(result.status, READY)
        self.assertTrue(result.ready)
        self.assertEqual(result.bindings[0].status, READY)
        self.assertFalse(result.bindings[0].enabled)
        self.assertFalse(result.bindings[0].acceptance_eligible)

    def test_bad_runtime_state_permissions_require_review_and_block_acceptance(self):
        path = self._write_rc(
            """
[Direct soundcard access]
Device ID = D10s: USB Audio (hw:1,0) (ALSA)
Enabled = 1
""",
            mode=0o644,
        )

        result = self._inspect(path)

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertEqual(result.rc_mode, "0644")
        self.assertIn("requires 0600", result.reason)

        with self.assertRaises(AcceptanceError):
            self._accept(path)

    def test_unknown_enabled_value_fails_closed_and_cannot_be_accepted(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:3,0) (ALSA)
Enabled = maybe
"""
        )

        result = self._inspect(path)

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.bindings[0].acceptance_eligible)

        with self.assertRaises(AcceptanceError):
            self._accept(path)

    def test_unknown_device_form_cannot_be_accepted(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = replacement device with unknown syntax
Enabled = 1
"""
        )

        result = self._inspect(path)
        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.bindings[0].acceptance_eligible)

        with self.assertRaises(AcceptanceError):
            self._accept(path)

    def test_unrelated_secret_and_dsp_fields_are_not_serialized(self):
        path = self._write_rc(
            """
[Some unrelated processor section]
License key = TOP-SECRET-LICENSE
Device ID = unrelated secret-ish value

[Soundcard - Input 2]
Device ID = Loopback: PCM (hw:4,1) (ALSA)
Enabled = 0
Password = SHOULD-NOT-APPEAR
"""
        )

        result = self._inspect(path)
        rendered = json.dumps(
            {
                "status": result.status,
                "reason": result.reason,
                "bindings": [
                    {
                        "section": binding.section,
                        "status": binding.status,
                        "device_id": binding.device_id,
                    }
                    for binding in result.bindings
                ],
            }
        )

        self.assertNotIn("TOP-SECRET-LICENSE", rendered)
        self.assertNotIn("SHOULD-NOT-APPEAR", rendered)
        self.assertNotIn("Some unrelated processor section", rendered)
        self.assertEqual(len(result.bindings), 1)

    def test_accept_current_transitions_exact_reviewed_binding_to_ready(self):
        path = self._write_rc(
            """
[Direct soundcard access]
Device ID = D10s: USB Audio (hw:1,0) (ALSA)
Enabled = 1

[Soundcard - Input]
Device ID = Loopback: PCM (hw:0,1) (ALSA)

[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:3,0) (ALSA)
Enabled = 1
"""
        )

        before = self._inspect(path)
        self.assertEqual(before.status, REVIEW_REQUIRED)

        accepted = self._accept(path)

        self.assertTrue(accepted.ready)
        self.assertTrue(accepted.acceptance_valid)
        self.assertTrue(
            all(
                binding.accepted
                for binding in accepted.bindings
                if binding.acceptance_eligible
            )
        )

        marker = Path(accepted.acceptance_path)
        self.assertTrue(marker.exists())
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)

        payload = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(
            set(payload),
            {
                "schema_version",
                "machine_id",
                "binding_sha256",
                "alsa_inventory_sha256",
            },
        )
        serialized = marker.read_text(encoding="utf-8")
        self.assertNotIn("D10s", serialized)
        self.assertNotIn("Loopback", serialized)
        self.assertNotIn("Device ID", serialized)

        recheck = self._inspect(path)
        self.assertTrue(recheck.ready)
        self.assertTrue(recheck.acceptance_valid)

    def test_acceptance_invalidates_when_binding_changes(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:3,0) (ALSA)
Enabled = 1
"""
        )
        accepted = self._accept(path)
        self.assertTrue(accepted.ready)

        path.write_text(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:4,0) (ALSA)
Enabled = 1
""",
            encoding="utf-8",
        )
        path.chmod(0o600)

        result = self._inspect(path)
        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.acceptance_valid)
        self.assertIn("changed after acceptance", result.reason)

    def test_acceptance_invalidates_on_different_machine_id(self):
        path = self._write_rc(
            """
[Direct soundcard access]
Device ID = D10s: USB Audio (hw:1,0) (ALSA)
Enabled = 1
"""
        )
        accepted = self._accept(path)
        self.assertTrue(accepted.ready)

        result = self._inspect(
            path,
            machine_id="fedcba9876543210fedcba9876543210",
        )
        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.acceptance_valid)
        self.assertIn("different machine", result.reason)

    def test_acceptance_invalidates_when_alsa_index_identity_map_changes(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:3,0) (ALSA)
Enabled = 1
"""
        )
        accepted = self._accept(path)
        self.assertTrue(accepted.ready)

        changed_cards = dict(self.CARDS)
        changed_cards["Loopback_1"] = 7

        result = self._inspect(path, cards=changed_cards)
        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.acceptance_valid)
        self.assertIn("inventory changed", result.reason)

    def test_bad_acceptance_marker_permissions_fail_closed(self):
        path = self._write_rc(
            """
[Direct soundcard access]
Device ID = D10s: USB Audio (hw:1,0) (ALSA)
Enabled = 1
"""
        )
        accepted = self._accept(path)
        marker = Path(accepted.acceptance_path)
        marker.chmod(0o644)

        result = self._inspect(path)

        self.assertEqual(result.status, REVIEW_REQUIRED)
        self.assertFalse(result.acceptance_valid)
        self.assertIn("marker mode is 0644", result.reason)

    def test_acceptance_refuses_missing_saved_card_index(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:99,0) (ALSA)
Enabled = 1
"""
        )

        with self.assertRaises(AcceptanceError) as ctx:
            self._accept(path)

        self.assertIn("card index is absent", str(ctx.exception))

    def test_normal_inspection_does_not_modify_runtime_or_marker(self):
        path = self._write_rc(
            """
[Direct soundcard access]
Device ID = D10s: USB Audio (hw:1,0) (ALSA)
Enabled = 1
"""
        )
        before = path.read_bytes()
        before_mode = path.stat().st_mode

        result = self._inspect(path)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_mode, before_mode)
        self.assertFalse(Path(result.acceptance_path).exists())

    def test_management_command_accept_current_then_normal_check_passes(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:3,0) (ALSA)
Enabled = 1
"""
        )
        stdout = StringIO()

        from unittest.mock import patch

        with patch(
            "hardware.management.commands.check_stereotool_bindings."
            "read_alsa_cards_present",
            return_value=self.CARDS,
        ), patch(
            "hardware.management.commands.check_stereotool_bindings."
            "_read_machine_id",
            return_value=self.MACHINE_ID,
        ):
            call_command(
                "check_stereotool_bindings",
                stdout=stdout,
                rc_path=str(path),
                accept_current=True,
            )

            recheck_out = StringIO()
            call_command(
                "check_stereotool_bindings",
                stdout=recheck_out,
                rc_path=str(path),
            )

        self.assertIn("acceptance recorded", stdout.getvalue())
        self.assertIn(
            "Stereo Tool hardware binding readiness: PASS",
            recheck_out.getvalue(),
        )

    def test_management_command_json_acceptance_success_is_one_document(self):
        path = self._write_rc(
            """
[Soundcard - Normal output]
Device ID = Loopback: PCM (hw:3,0) (ALSA)
Enabled = 1
"""
        )
        stdout = StringIO()

        from unittest.mock import patch

        with patch(
            "hardware.management.commands.check_stereotool_bindings."
            "read_alsa_cards_present",
            return_value=self.CARDS,
        ), patch(
            "hardware.management.commands.check_stereotool_bindings."
            "_read_machine_id",
            return_value=self.MACHINE_ID,
        ):
            call_command(
                "check_stereotool_bindings",
                stdout=stdout,
                rc_path=str(path),
                as_json=True,
                accept_current=True,
            )

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["acceptance_written"])
        self.assertTrue(payload["acceptance_valid"])

    def test_management_command_missing_rc_fails_closed(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                "check_stereotool_bindings",
                stdout=stdout,
                rc_path=str(Path(tempdir.name) / "missing.rc"),
            )

        self.assertIn("NOT_CONFIGURED", stdout.getvalue())
