"""[P0] 1.3C -- AudioOutput's additive device-identity fields
(device_identity_kind, device_identity). Rollback-safety and admin-form
coverage for the model change itself -- mirrors
test_audio_input_device_identity.py exactly, same additive/rollback-safe
shape reused unmodified from [P0] 1.3B2. Resolver LOGIC (resolve_runtime_
device, alsa_card_identity_present) is shared, unmodified code, already
covered exhaustively in library/tests/test_audio_recovery.py -- this file
only checks the model/migration/admin surface those functions are fed
from, now for AudioOutput too."""
from django.test import TestCase

from hardware.admin import AudioOutputAdmin
from hardware.models import AudioOutput


class AudioOutputIdentityFieldDefaultsTests(TestCase):
    def test_new_fields_default_blank_existing_device_field_untouched(self):
        """A row created the OLD way (device only) must behave exactly
        as it did before this migration -- the whole point of keeping
        this additive and rollback-safe. This is also the "which output
        slots have automatic recovery enabled by default after
        migration" proof: NONE, until identity is explicitly set."""
        obj = AudioOutput.objects.create(name="1.3C Test Output A", device="plughw:2,0")
        obj.refresh_from_db()
        self.assertEqual(obj.device, "plughw:2,0")
        self.assertEqual(obj.device_identity_kind, "")
        self.assertEqual(obj.device_identity, "")

    def test_identity_fields_round_trip(self):
        obj = AudioOutput.objects.create(
            name="1.3C Test Output B", device="plughw:5,0",
            device_identity_kind="alsa_card_id", device_identity="CODEC",
        )
        obj.refresh_from_db()
        self.assertEqual(obj.device_identity_kind, "alsa_card_id")
        self.assertEqual(obj.device_identity, "CODEC")
        # Legacy field survives unchanged alongside the new ones.
        self.assertEqual(obj.device, "plughw:5,0")

    def test_existing_studio_monitor_and_stereotool_rows_unaffected(self):
        """Rows matching the two names the engine actually resolves by
        (STUDIO_MONITOR_NAME / STEREOTOOL_OUTPUT_NAME) round-trip the
        same as any other AudioOutput row -- no special-casing anywhere
        in the model/migration layer. get_or_create, not create -- a
        data migration (hardware/migrations/0006_program_gain_db.py)
        already seeds a "Studio Monitor" row, so this exercises the
        REAL migration-seeded row, not a fresh duplicate (confirmed
        live: a bare .create() here hits AudioOutput.name's unique
        constraint against that seeded row)."""
        sm, _ = AudioOutput.objects.get_or_create(name="Studio Monitor", defaults={"device": "plughw:2,0"})
        st, _ = AudioOutput.objects.get_or_create(name="Stereotool Input", defaults={"device": "plughw:0,0"})
        for obj in (sm, st):
            obj.refresh_from_db()
            self.assertEqual(obj.device_identity_kind, "")
            self.assertEqual(obj.device_identity, "")


class AudioOutputAdminExposesIdentityFieldsTests(TestCase):
    def test_fieldsets_include_new_identity_fields_for_new_object(self):
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        fieldsets = admin_instance.get_fieldsets(request=None, obj=None)
        all_fields = [f for _, opts in fieldsets for f in opts["fields"]]
        self.assertIn("device_identity_kind", all_fields)
        self.assertIn("device_identity", all_fields)
        # The legacy field is still there too -- not replaced.
        self.assertIn("device", all_fields)

    def test_fieldsets_include_identity_fields_for_every_named_output(self):
        """Unlike the AGC fieldset (Studio Monitor only), the identity
        section must show for EVERY AudioOutput row -- Stereotool Input
        gets automatic recovery too, and the task explicitly forbids
        gating recovery on which named output this happens to be."""
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        for name in ("Studio Monitor", "Stereotool Input", "Some Other Output"):
            obj = AudioOutput(name=name, device="plughw:9,0")
            fieldsets = admin_instance.get_fieldsets(request=None, obj=obj)
            all_fields = [f for _, opts in fieldsets for f in opts["fields"]]
            self.assertIn("device_identity_kind", all_fields, f"missing for {name!r}")
            self.assertIn("device_identity", all_fields, f"missing for {name!r}")

    def test_agc_fieldset_still_studio_monitor_only(self):
        """Regression guard: adding the identity fieldset must not have
        disturbed the existing Studio-Monitor-only AGC fieldset gating."""
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        sm = AudioOutput(name="Studio Monitor", device="plughw:2,0")
        other = AudioOutput(name="Stereotool Input", device="plughw:0,0")
        sm_fields = [f for _, opts in admin_instance.get_fieldsets(request=None, obj=sm) for f in opts["fields"]]
        other_fields = [f for _, opts in admin_instance.get_fieldsets(request=None, obj=other) for f in opts["fields"]]
        self.assertIn("agc_enabled", sm_fields)
        self.assertNotIn("agc_enabled", other_fields)
