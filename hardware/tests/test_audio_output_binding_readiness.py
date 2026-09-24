from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from hardware.management.commands.check_audio_output_bindings import (
    READY,
    REBIND_REQUIRED,
    UNCONFIGURED,
    inspect_audio_output_bindings,
    output_bindings_ready,
)
from hardware.models import AudioOutput


class AudioOutputBindingReadinessTests(TestCase):
    def _create_output(
        self,
        *,
        name,
        device="",
        identity_kind="",
        identity="",
    ):
        return AudioOutput.objects.create(
            name=name,
            device=device,
            device_identity_kind=identity_kind,
            device_identity=identity,
        )

    def test_present_stable_identities_are_ready(self):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )
        self._create_output(
            name="Stereotool Input",
            device="plughw:0,0",
            identity_kind="alsa_card_id",
            identity="Loopback",
        )

        results = inspect_audio_output_bindings(
            {
                "PCH": 6,
                "Loopback": 0,
            }
        )

        by_name = {
            item.name: item
            for item in results
        }

        self.assertEqual(
            {
                name: item.status
                for name, item in by_name.items()
            },
            {
                "Studio Monitor": READY,
                "Stereotool Input": READY,
            },
        )
        self.assertTrue(output_bindings_ready(results))

        self.assertEqual(
            by_name["Studio Monitor"].runtime_device,
            "plughw:CARD=PCH,DEV=0",
        )

    def test_missing_replacement_host_identity_requires_rebind(self):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        results = inspect_audio_output_bindings(
            {"ReplacementDAC": 2}
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].status,
            REBIND_REQUIRED,
        )
        self.assertIn(
            "not present on this host",
            results[0].reason,
        )
        self.assertFalse(output_bindings_ready(results))

    def test_raw_legacy_output_is_not_replacement_host_ready(self):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
        )

        results = inspect_audio_output_bindings(
            {"PCH": 6}
        )

        self.assertEqual(
            results[0].status,
            REBIND_REQUIRED,
        )
        self.assertIn(
            "raw/legacy binding",
            results[0].reason,
        )
        self.assertFalse(output_bindings_ready(results))

    def test_identity_kind_without_value_requires_rebind(self):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="",
        )

        results = inspect_audio_output_bindings(
            {"PCH": 6}
        )

        self.assertEqual(
            results[0].status,
            REBIND_REQUIRED,
        )
        self.assertIn(
            "identity value is blank",
            results[0].reason,
        )

    def test_missing_required_studio_monitor_fails_closed(self):
        results = inspect_audio_output_bindings(
            {"PCH": 6}
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].name,
            "Studio Monitor",
        )
        self.assertEqual(
            results[0].status,
            UNCONFIGURED,
        )
        self.assertFalse(output_bindings_ready(results))

    def test_empty_optional_output_row_does_not_create_false_failure(self):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )
        self._create_output(
            name="Unused Optional Output",
        )

        results = inspect_audio_output_bindings(
            {"PCH": 6}
        )

        self.assertEqual(
            [item.name for item in results],
            ["Studio Monitor"],
        )
        self.assertTrue(output_bindings_ready(results))

    def test_configured_unrelated_output_does_not_block_engine_gate(self):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )
        self._create_output(
            name="Unused Configured Output",
            device="plughw:99,0",
            identity_kind="alsa_card_id",
            identity="MissingUnrelatedCard",
        )

        results = inspect_audio_output_bindings(
            {"PCH": 6}
        )

        self.assertEqual(
            [item.name for item in results],
            ["Studio Monitor"],
        )
        self.assertTrue(output_bindings_ready(results))

    @patch(
        "hardware.management.commands.check_audio_output_bindings."
        "read_alsa_cards_present",
        return_value={"PCH": 6},
    )
    def test_management_command_passes_without_opening_pcm(
        self,
        _cards,
    ):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        stdout = StringIO()

        call_command(
            "check_audio_output_bindings",
            stdout=stdout,
        )

        rendered = stdout.getvalue()
        self.assertIn(
            "READY: Studio Monitor",
            rendered,
        )
        self.assertIn(
            "Audio output binding readiness: PASS",
            rendered,
        )

    @patch(
        "hardware.management.commands.check_audio_output_bindings."
        "read_alsa_cards_present",
        return_value={"ReplacementDAC": 1},
    )
    def test_management_command_fails_closed_for_missing_identity(
        self,
        _cards,
    ):
        self._create_output(
            name="Studio Monitor",
            device="plughw:2,0",
            identity_kind="alsa_card_id",
            identity="PCH",
        )

        stdout = StringIO()

        with self.assertRaises(CommandError):
            call_command(
                "check_audio_output_bindings",
                stdout=stdout,
            )

        self.assertIn(
            "REBIND_REQUIRED: Studio Monitor",
            stdout.getvalue(),
        )
