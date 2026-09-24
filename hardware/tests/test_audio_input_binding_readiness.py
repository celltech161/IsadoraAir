from io import StringIO
import json
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from hardware.management.commands.check_audio_input_bindings import (
    NOT_CONFIGURED,
    READY,
    REBIND_REQUIRED,
    input_binding_ready,
    inspect_audio_input_binding,
)
from hardware.models import AudioInput


class AudioInputBindingReadinessTests(TestCase):
    def _create_input(
        self,
        *,
        device="",
        identity_kind="",
        identity="",
    ):
        return AudioInput.objects.create(
            name="Studio Microphone 1",
            device=device,
            device_identity_kind=identity_kind,
            device_identity=identity,
        )

    def test_present_capture_capable_stable_identity_is_ready(self):
        self._create_input(
            device="plughw:99,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        result = inspect_audio_input_binding({"PCH"})

        self.assertEqual(result.status, READY)
        self.assertEqual(
            result.runtime_device,
            "plughw:CARD=PCH,DEV=0",
        )
        self.assertTrue(input_binding_ready(result))

    def test_identity_not_capture_capable_on_dev_zero_requires_rebind(self):
        self._create_input(
            device="plughw:6,2",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        result = inspect_audio_input_binding(set())

        self.assertEqual(result.status, REBIND_REQUIRED)
        self.assertIn(
            "not capture-capable on DEV=0",
            result.reason,
        )
        self.assertFalse(input_binding_ready(result))

    def test_raw_legacy_input_is_not_replacement_host_ready(self):
        self._create_input(
            device="plughw:2,0",
        )

        result = inspect_audio_input_binding({"PCH"})

        self.assertEqual(result.status, REBIND_REQUIRED)
        self.assertIn(
            "raw/legacy binding",
            result.reason,
        )
        self.assertFalse(input_binding_ready(result))

    def test_identity_kind_without_value_requires_rebind(self):
        self._create_input(
            device="plughw:6,0",
            identity_kind="alsa_card_id",
            identity="",
        )

        result = inspect_audio_input_binding({"PCH"})

        self.assertEqual(result.status, REBIND_REQUIRED)
        self.assertIn(
            "identity value is blank",
            result.reason,
        )

    def test_missing_studio_microphone_row_is_safe_disabled(self):
        result = inspect_audio_input_binding({"PCH"})

        self.assertEqual(result.status, NOT_CONFIGURED)
        self.assertIsNone(result.runtime_device)
        self.assertTrue(input_binding_ready(result))

    def test_empty_studio_microphone_row_is_safe_disabled(self):
        self._create_input()

        result = inspect_audio_input_binding({"PCH"})

        self.assertEqual(result.status, NOT_CONFIGURED)
        self.assertTrue(input_binding_ready(result))

    @patch(
        "hardware.management.commands.check_audio_input_bindings."
        "list_alsa_card_identities",
        return_value=[
            SimpleNamespace(card_id="PCH"),
        ],
    )
    def test_management_command_passes_for_capture_capable_identity(
        self,
        _discover,
    ):
        self._create_input(
            device="plughw:99,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        stdout = StringIO()

        call_command(
            "check_audio_input_bindings",
            stdout=stdout,
        )

        rendered = stdout.getvalue()
        self.assertIn(
            "READY: Studio Microphone 1",
            rendered,
        )
        self.assertIn(
            "Audio input binding readiness: PASS",
            rendered,
        )

    @patch(
        "hardware.management.commands.check_audio_input_bindings."
        "list_alsa_card_identities",
        return_value=[],
    )
    def test_management_command_fails_closed_for_missing_capture_capability(
        self,
        _discover,
    ):
        self._create_input(
            device="plughw:6,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                "check_audio_input_bindings",
                stdout=stdout,
            )

        self.assertIn(
            "REBIND_REQUIRED: Studio Microphone 1",
            stdout.getvalue(),
        )

    @patch(
        "hardware.management.commands.check_audio_input_bindings."
        "list_alsa_card_identities",
        return_value=[
            SimpleNamespace(card_id="PCH"),
        ],
    )
    def test_json_success_is_one_document(self, _discover):
        self._create_input(
            device="plughw:99,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        stdout = StringIO()

        call_command(
            "check_audio_input_bindings",
            stdout=stdout,
            as_json=True,
        )

        payload = json.loads(stdout.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(
            payload["capture_capable_card_ids"],
            ["PCH"],
        )
        self.assertEqual(
            payload["binding"]["status"],
            READY,
        )
        self.assertEqual(
            payload["binding"]["runtime_device"],
            "plughw:CARD=PCH,DEV=0",
        )
