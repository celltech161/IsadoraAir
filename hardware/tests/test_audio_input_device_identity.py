"""[P0] 1.3B2 -- AudioInput's additive device-identity fields
(device_identity_kind, device_identity). Rollback-safety and admin-form
coverage for the model change itself; resolver LOGIC (resolve_runtime_
device, alsa_card_identity_present) is covered exhaustively in
library/tests/test_audio_recovery.py -- this file only checks the model/
migration/admin surface those functions are fed from."""
from django.test import TestCase

from hardware.admin import AudioInputAdmin
from hardware.models import AudioInput


class AudioInputIdentityFieldDefaultsTests(TestCase):
    def test_new_fields_default_blank_existing_device_field_untouched(self):
        """A row created the OLD way (device only) must behave exactly
        as it did before this migration -- the whole point of keeping
        this additive and rollback-safe."""
        obj = AudioInput.objects.create(name="1.3B2 Test Input A", device="plughw:2,0")
        obj.refresh_from_db()
        self.assertEqual(obj.device, "plughw:2,0")
        self.assertEqual(obj.device_identity_kind, "")
        self.assertEqual(obj.device_identity, "")

    def test_identity_fields_round_trip(self):
        obj = AudioInput.objects.create(
            name="1.3B2 Test Input B", device="plughw:2,0",
            device_identity_kind="alsa_card_id", device_identity="PCH",
        )
        obj.refresh_from_db()
        self.assertEqual(obj.device_identity_kind, "alsa_card_id")
        self.assertEqual(obj.device_identity, "PCH")
        # Legacy field survives unchanged alongside the new ones.
        self.assertEqual(obj.device, "plughw:2,0")


class AudioInputAdminExposesIdentityFieldsTests(TestCase):
    def test_fieldsets_include_new_identity_fields(self):
        admin_instance = AudioInputAdmin(AudioInput, None)
        fieldsets = admin_instance.get_fieldsets(request=None, obj=None)
        all_fields = [f for _, opts in fieldsets for f in opts["fields"]]
        self.assertIn("device_identity_kind", all_fields)
        self.assertIn("device_identity", all_fields)
        # The legacy field is still there too -- not replaced.
        self.assertIn("device", all_fields)
