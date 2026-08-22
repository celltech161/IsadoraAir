from subprocess import CompletedProcess, TimeoutExpired
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from django.test import TestCase

from hardware.admin import AudioInputAdmin, AudioOutputAdmin
from hardware.devices import (
    AlsaCardIdentity,
    list_alsa_card_identities,
    parse_alsa_cards,
)
from hardware.models import AudioInput, AudioOutput


ASOUND_CARDS = """\
 0 [Loopback       ]: Loopback - Loopback
                      Loopback 1
 1 [CODEC          ]: USB-Audio - USB Audio CODEC
                      Burr-Brown from TI USB Audio CODEC at usb-0000:00:14.0-10, full speed
 2 [D10s           ]: USB-Audio - D10s
                      Topping D10s at usb-0000:00:14.0-9, high speed
 5 [CODEC_1        ]: USB-Audio - USB Audio CODEC
                      Burr-Brown from TI USB Audio CODEC at usb-0000:00:14.0-2, full speed
 6 [PCH            ]: HDA-Intel - HDA Intel PCH
                      HDA Intel PCH at 0x4000100000 irq 149
 7 [CaptureOnly    ]: USB-Audio - Capture Interface
                      Example Capture Interface at usb-0000:00:14.0-7, full speed
 8 [PlaybackDev1   ]: USB-Audio - Playback Device One Only
                      Example Playback Device at usb-0000:00:14.0-8, full speed
 9 [CaptureDev2    ]: USB-Audio - Capture Device Two Only
                      Example Capture Device at usb-0000:00:14.0-11, full speed
"""

APLAY_OUTPUT = """\
**** List of PLAYBACK Hardware Devices ****
card 0: Loopback [Loopback], device 0: Loopback PCM [Loopback PCM]
card 1: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
card 2: D10s [D10s], device 0: USB Audio [USB Audio]
card 5: CODEC_1 [USB Audio CODEC], device 0: USB Audio [USB Audio]
card 6: PCH [HDA Intel PCH], device 0: Analog [Analog]
card 8: PlaybackDev1 [Playback Device One Only], device 1: USB Audio [USB Audio]
"""

ARECORD_OUTPUT = """\
**** List of CAPTURE Hardware Devices ****
card 0: Loopback [Loopback], device 0: Loopback PCM [Loopback PCM]
card 1: CODEC [USB Audio CODEC], device 0: USB Audio [USB Audio]
card 5: CODEC_1 [USB Audio CODEC], device 0: USB Audio [USB Audio]
card 6: PCH [HDA Intel PCH], device 0: Analog [Analog]
card 7: CaptureOnly [Capture Interface], device 0: USB Audio [USB Audio]
card 9: CaptureDev2 [Capture Device Two Only], device 2: USB Audio [USB Audio]
"""


def _identity(card_id, *, card_index=1, direction="playback", location=None):
    return AlsaCardIdentity(
        card_index=card_index,
        card_id=card_id,
        driver="USB-Audio",
        name="USB Audio CODEC",
        description="Burr-Brown from TI USB Audio CODEC",
        usb_location=location,
        capabilities=frozenset({direction}),
    )


class AlsaCardIdentityDiscoveryTests(TestCase):
    def test_proc_cards_parser_returns_structured_identity_descriptions(self):
        cards = parse_alsa_cards(ASOUND_CARDS)
        by_id = {card.card_id: card for card in cards}

        self.assertEqual(by_id["CODEC"].card_index, 1)
        self.assertEqual(by_id["CODEC"].driver, "USB-Audio")
        self.assertEqual(by_id["CODEC"].name, "USB Audio CODEC")
        self.assertIn("Burr-Brown from TI", by_id["CODEC"].description)
        self.assertEqual(by_id["CODEC"].usb_location, "usb-0000:00:14.0-10")
        self.assertEqual(by_id["PCH"].driver, "HDA-Intel")
        self.assertEqual(by_id["Loopback"].description, "Loopback 1")

    def test_identical_usb_devices_remain_distinct_and_labels_show_location(self):
        by_id = {card.card_id: card for card in parse_alsa_cards(ASOUND_CARDS)}

        self.assertNotEqual(by_id["CODEC"].card_id, by_id["CODEC_1"].card_id)
        self.assertIn("usb-0000:00:14.0-10", by_id["CODEC"].label)
        self.assertIn("usb-0000:00:14.0-2", by_id["CODEC_1"].label)
        self.assertNotEqual(by_id["CODEC"].label, by_id["CODEC_1"].label)

    def _discover(self, direction, command_output):
        cards_path = Mock()
        cards_path.read_text.return_value = ASOUND_CARDS
        completed = CompletedProcess(
            args=["aplay" if direction == "playback" else "arecord", "-l"],
            returncode=0,
            stdout=command_output,
            stderr="",
        )
        with patch("hardware.devices._ASOUND_CARDS_PATH", cards_path), patch(
            "hardware.devices.subprocess.run", return_value=completed
        ) as run:
            identities = list_alsa_card_identities(direction)
        run.assert_called_once_with(
            ["aplay" if direction == "playback" else "arecord", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return identities

    def test_output_identities_include_only_playback_capable_cards(self):
        identities = self._discover("playback", APLAY_OUTPUT)

        self.assertEqual(
            [identity.card_id for identity in identities],
            ["Loopback", "CODEC", "D10s", "CODEC_1", "PCH"],
        )
        self.assertNotIn("CaptureOnly", {identity.card_id for identity in identities})
        self.assertNotIn("PlaybackDev1", {identity.card_id for identity in identities})
        self.assertTrue(all(identity.capabilities == {"playback"} for identity in identities))

    def test_input_identities_include_only_capture_capable_cards(self):
        identities = self._discover("capture", ARECORD_OUTPUT)

        self.assertEqual(
            [identity.card_id for identity in identities],
            ["Loopback", "CODEC", "CODEC_1", "PCH", "CaptureOnly"],
        )
        self.assertNotIn("D10s", {identity.card_id for identity in identities})
        self.assertNotIn("CaptureDev2", {identity.card_id for identity in identities})
        self.assertTrue(all(identity.capabilities == {"capture"} for identity in identities))

    def test_nonzero_only_pcm_is_excluded_to_match_production_dev_zero_resolver(self):
        playback = self._discover("playback", APLAY_OUTPUT)
        capture = self._discover("capture", ARECORD_OUTPUT)

        self.assertNotIn("PlaybackDev1", {card.card_id for card in playback})
        self.assertNotIn("CaptureDev2", {card.card_id for card in capture})

    def test_proc_or_command_failure_returns_no_live_identities(self):
        missing_cards = Mock()
        missing_cards.read_text.side_effect = OSError("not mounted")
        with patch("hardware.devices._ASOUND_CARDS_PATH", missing_cards):
            self.assertEqual(list_alsa_card_identities("playback"), [])

        cards_path = Mock()
        cards_path.read_text.return_value = ASOUND_CARDS
        with patch("hardware.devices._ASOUND_CARDS_PATH", cards_path), patch(
            "hardware.devices.subprocess.run",
            side_effect=TimeoutExpired("aplay", 5),
        ):
            self.assertEqual(list_alsa_card_identities("playback"), [])


class AudioIdentityAdminDropdownTests(TestCase):
    def _output_form(self, obj, identities, raw_devices=()):
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        with patch.object(
            admin_instance, "_enumerate", return_value=list(raw_devices)
        ), patch(
            "hardware.admin.list_alsa_card_identities", return_value=list(identities)
        ):
            return admin_instance.get_form(request=None, obj=obj)

    def _input_form(self, obj, identities, raw_devices=()):
        admin_instance = AudioInputAdmin(AudioInput, None)
        with patch.object(
            admin_instance, "_enumerate", return_value=list(raw_devices)
        ), patch(
            "hardware.admin.list_alsa_card_identities", return_value=list(identities)
        ):
            return admin_instance.get_form(request=None, obj=obj)

    def test_output_and_input_forms_use_direction_specific_discovery(self):
        output = AudioOutput(name="Dropdown Output", device="")
        input_ = AudioInput(name="Dropdown Input", device="")
        output_admin = AudioOutputAdmin(AudioOutput, None)
        input_admin = AudioInputAdmin(AudioInput, None)

        with patch.object(output_admin, "_enumerate", return_value=[]), patch(
            "hardware.admin.list_alsa_card_identities",
            return_value=[_identity("D10s")],
        ) as discover:
            output_admin.get_form(request=None, obj=output)
            discover.assert_called_once_with("playback")

        with patch.object(input_admin, "_enumerate", return_value=[]), patch(
            "hardware.admin.list_alsa_card_identities",
            return_value=[_identity("CODEC", direction="capture")],
        ) as discover:
            input_admin.get_form(request=None, obj=input_)
            discover.assert_called_once_with("capture")

    def test_configured_identity_is_preserved_when_currently_unavailable(self):
        for obj, build_form in (
            (
                AudioOutput(
                    name="Unavailable Output",
                    device="",
                    device_identity_kind="alsa_card_id",
                    device_identity="CODEC",
                ),
                self._output_form,
            ),
            (
                AudioInput(
                    name="Unavailable Input",
                    device="",
                    device_identity_kind="alsa_card_id",
                    device_identity="CODEC",
                ),
                self._input_form,
            ),
        ):
            form_class = build_form(obj, [_identity("PCH")])
            choices = list(form_class.base_fields["device_identity"].widget.choices)
            self.assertEqual(choices[0], ("", "— not configured —"))
            self.assertIn(("CODEC", "CODEC — currently unavailable"), choices)

    def test_selecting_codec_1_stores_only_the_short_card_id(self):
        obj = AudioOutput.objects.create(name="Identity Storage Output", device="")
        identities = [
            _identity("CODEC", location="usb-0000:00:14.0-10"),
            _identity("CODEC_1", location="usb-0000:00:14.0-2"),
        ]
        form_class = self._output_form(obj, identities)
        form = form_class(
            data={
                "name": obj.name,
                "device": "",
                "sort_order": obj.sort_order,
                "device_identity_kind": "alsa_card_id",
                "device_identity": "CODEC_1",
            },
            instance=obj,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.device_identity, "CODEC_1")
        saved.refresh_from_db()
        self.assertEqual(saved.device_identity, "CODEC_1")

    def test_blank_choice_preserves_legacy_mode(self):
        obj = AudioInput.objects.create(name="Legacy Dropdown Input", device="")
        form_class = self._input_form(
            obj, [_identity("CODEC", direction="capture")]
        )
        form = form_class(
            data={
                "name": obj.name,
                "device": "",
                "sort_order": obj.sort_order,
                "device_identity_kind": "",
                "device_identity": "",
                "gain_db": obj.gain_db,
            },
            instance=obj,
        )

        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.device_identity_kind, "")
        self.assertEqual(saved.device_identity, "")

    def test_discovery_exception_does_not_break_form_and_preserves_current(self):
        obj = AudioOutput(
            name="Discovery Failure Output",
            device="",
            device_identity_kind="alsa_card_id",
            device_identity="CODEC",
        )
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        with patch.object(admin_instance, "_enumerate", return_value=[]), patch(
            "hardware.admin.list_alsa_card_identities",
            side_effect=RuntimeError("unexpected enumeration"),
        ):
            form_class = admin_instance.get_form(request=None, obj=obj)

        choices = list(form_class.base_fields["device_identity"].widget.choices)
        self.assertEqual(
            choices,
            [
                ("", "— not configured —"),
                ("CODEC", "CODEC — currently unavailable"),
            ],
        )

    def test_existing_raw_device_dropdown_behavior_is_unchanged(self):
        obj = AudioOutput(name="Raw Device Output", device="plughw:9,0")
        form_class = self._output_form(
            obj,
            identities=[],
            raw_devices=[("plughw:1,0", "plughw:1,0 — USB Audio / USB Audio")],
        )

        choices = list(form_class.base_fields["device"].widget.choices)
        self.assertEqual(
            choices,
            [
                ("", "— not configured —"),
                ("plughw:9,0", "plughw:9,0 (UNAVAILABLE)"),
                ("plughw:1,0", "plughw:1,0 — USB Audio / USB Audio"),
            ],
        )

    def test_admin_help_no_longer_tells_operator_to_type_proc_value(self):
        for admin_instance in (
            AudioOutputAdmin(AudioOutput, None),
            AudioInputAdmin(AudioInput, None),
        ):
            descriptions = " ".join(
                options.get("description", "")
                for _name, options in admin_instance.get_fieldsets(None, None)
            )
            self.assertNotIn("cat /proc/asound/cards", descriptions)
            self.assertIn("dropdown lists currently detected", descriptions)

        output_form = self._output_form(
            AudioOutput(name="Help Output", device=""), identities=[]
        )
        input_form = self._input_form(
            AudioInput(name="Help Input", device=""), identities=[]
        )
        for form_class in (output_form, input_form):
            help_text = form_class.base_fields["device_identity"].help_text
            self.assertNotIn("cat /proc/asound/cards", help_text)
            self.assertIn("Only the stable short card ID is stored", help_text)


class EffectiveIdentityMixerControlsTests(TestCase):
    TARGET_CASES = (
        (AudioOutput, AudioOutputAdmin, "playback", "Master,0"),
        (AudioInput, AudioInputAdmin, "capture", "Capture,0"),
    )

    def _save_with_target_transition(
        self,
        model,
        admin_class,
        direction,
        control_id,
        *,
        before,
        after,
    ):
        obj = model.objects.create(
            name=f"Mixer transition {model.__name__} {model.objects.count()}",
            device=before[2],
            device_identity_kind=before[0],
            device_identity=before[1],
            mixer_control_values={},
        )
        initial = {
            "device_identity_kind": before[0],
            "device_identity": before[1],
            "device": before[2],
        }

        # Mirrors ModelAdmin's real lifecycle: the synthetic controls were
        # built from the initial target, then ModelForm._post_clean copied the
        # submitted model fields onto obj before save_model receives it.
        obj.device_identity_kind = after[0]
        obj.device_identity = after[1]
        obj.device = after[2]
        control = {
            "control_id": control_id,
            "label": control_id.split(",", 1)[0],
            "has_enum": False,
            "has_switch": False,
            "has_volume": True,
            "value_pct": 40,
        }
        cleaned_data = {
            "device_identity_kind": after[0],
            "device_identity": after[1],
            "device": after[2],
            "mixer_0": 55,
        }
        form = SimpleNamespace(
            initial=initial,
            cleaned_data=cleaned_data,
            changed_data=["mixer_0"],
            _mixer_control_map={"mixer_0": control},
        )
        identities = [
            _identity("PCH", card_index=6, direction=direction),
            _identity("CODEC", card_index=5, direction=direction),
        ]
        admin_instance = admin_class(model, None)
        request = object()
        with patch(
            "hardware.admin.list_alsa_card_identities", return_value=identities
        ), patch("hardware.admin.subprocess.run") as run, patch(
            "hardware.admin._alsa_store"
        ) as store, patch("hardware.admin.messages.warning") as warning:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            admin_instance.save_model(
                request=request, obj=obj, form=form, change=True
            )

        obj.refresh_from_db()
        return obj, control, run, store, warning

    def _assert_mixer_applied(self, *, before, after, expected_card):
        for model, admin_class, direction, control_id in self.TARGET_CASES:
            with self.subTest(model=model.__name__):
                obj, control, run, store, warning = self._save_with_target_transition(
                    model,
                    admin_class,
                    direction,
                    control_id,
                    before=before,
                    after=after,
                )
                self.assertIn(
                    call(
                        [
                            "amixer",
                            "-c",
                            str(expected_card),
                            "sset",
                            control_id,
                            "55%",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=5,
                        check=True,
                    ),
                    run.call_args_list,
                )
                store.assert_called_once()
                warning.assert_not_called()
                self.assertEqual(obj.device_identity_kind, after[0])
                self.assertEqual(obj.device_identity, after[1])
                self.assertEqual(obj.device, after[2])
                self.assertEqual(obj.mixer_control_values, {control["control_id"]: 55})

    def _assert_mixer_skipped(self, *, before, after):
        warning_text = (
            "Audio device changed. Hardware mixer controls were not applied "
            "because the controls shown belonged to the previous device. "
            "Reopen this page to configure the newly selected device."
        )
        for model, admin_class, direction, control_id in self.TARGET_CASES:
            with self.subTest(model=model.__name__):
                obj, _control, run, store, warning = self._save_with_target_transition(
                    model,
                    admin_class,
                    direction,
                    control_id,
                    before=before,
                    after=after,
                )
                run.assert_not_called()
                store.assert_not_called()
                warning.assert_called_once()
                self.assertEqual(warning.call_args.args[1], warning_text)
                self.assertEqual(obj.device_identity_kind, after[0])
                self.assertEqual(obj.device_identity, after[1])
                self.assertEqual(obj.device, after[2])
                self.assertEqual(obj.mixer_control_values, {})

    def test_rendered_mixer_controls_follow_stable_identity_card(self):
        cases = (
            (
                AudioOutput(
                    name="Stable Mixer Output",
                    device="plughw:9,0",
                    device_identity_kind="alsa_card_id",
                    device_identity="CODEC",
                ),
                AudioOutputAdmin(AudioOutput, None),
                "playback",
            ),
            (
                AudioInput(
                    name="Stable Mixer Input",
                    device="plughw:9,0",
                    device_identity_kind="alsa_card_id",
                    device_identity="CODEC",
                ),
                AudioInputAdmin(AudioInput, None),
                "capture",
            ),
        )
        for obj, admin_instance, direction in cases:
            with self.subTest(model=type(obj).__name__), patch(
                "hardware.admin.list_alsa_card_identities",
                return_value=[_identity("CODEC", card_index=5, direction=direction)],
            ), patch("hardware.admin.list_mixer_controls", return_value=[]) as controls:
                admin_instance.get_fieldsets(request=None, obj=obj)
            controls.assert_called_once_with(5)

    def test_unavailable_stable_identity_never_falls_back_to_raw_mixer_card(self):
        obj = AudioOutput(
            name="Unavailable Stable Mixer Output",
            device="plughw:9,0",
            device_identity_kind="alsa_card_id",
            device_identity="MISSING",
        )
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        with patch(
            "hardware.admin.list_alsa_card_identities", return_value=[]
        ), patch("hardware.admin.list_mixer_controls") as controls:
            admin_instance.get_fieldsets(request=None, obj=obj)
        controls.assert_not_called()

    def test_legacy_mode_mixer_controls_still_use_raw_device_card(self):
        obj = AudioOutput(
            name="Legacy Mixer Output",
            device="plughw:9,0",
            device_identity_kind="",
            device_identity="",
        )
        admin_instance = AudioOutputAdmin(AudioOutput, None)
        with patch("hardware.admin.list_mixer_controls", return_value=[]) as controls:
            admin_instance.get_fieldsets(request=None, obj=obj)
        controls.assert_called_once_with(9)

    def test_stable_identity_unchanged_applies_mixer_edits(self):
        self._assert_mixer_applied(
            before=("alsa_card_id", "PCH", "plughw:9,0"),
            after=("alsa_card_id", "PCH", "plughw:9,0"),
            expected_card=6,
        )

    def test_raw_fallback_edit_under_same_stable_identity_applies_mixer_edits(self):
        self._assert_mixer_applied(
            before=("alsa_card_id", "PCH", "plughw:9,0"),
            after=("alsa_card_id", "PCH", "plughw:8,0"),
            expected_card=6,
        )

    def test_legacy_raw_unchanged_applies_mixer_edits(self):
        self._assert_mixer_applied(
            before=("", "", "plughw:9,0"),
            after=("", "", "plughw:9,0"),
            expected_card=9,
        )

    def test_stable_identity_change_skips_old_mixer_edits(self):
        self._assert_mixer_skipped(
            before=("alsa_card_id", "PCH", "plughw:9,0"),
            after=("alsa_card_id", "CODEC", "plughw:9,0"),
        )

    def test_bound_model_form_does_not_redirect_old_controls_to_new_identity(self):
        for model, admin_class, direction, control_id in self.TARGET_CASES:
            with self.subTest(model=model.__name__):
                obj = model.objects.create(
                    name=f"Bound transition {model.__name__}",
                    device="plughw:9,0",
                    device_identity_kind="alsa_card_id",
                    device_identity="PCH",
                    mixer_control_values={},
                )
                old_control = {
                    "control_id": control_id,
                    "label": control_id.split(",", 1)[0],
                    "has_enum": False,
                    "has_switch": False,
                    "has_volume": True,
                    "value_pct": 40,
                }
                identities = [
                    _identity("PCH", card_index=6, direction=direction),
                    _identity("CODEC", card_index=5, direction=direction),
                ]
                admin_instance = admin_class(model, None)
                with patch.object(
                    admin_instance,
                    "_enumerate",
                    return_value=[("plughw:9,0", "Raw fallback")],
                ), patch(
                    "hardware.admin.list_alsa_card_identities",
                    return_value=identities,
                ), patch(
                    "hardware.admin.list_mixer_controls",
                    return_value=[old_control],
                ):
                    form_class = admin_instance.get_form(request=None, obj=obj)

                data = {
                    "name": obj.name,
                    "device": "plughw:9,0",
                    "sort_order": obj.sort_order,
                    "device_identity_kind": "alsa_card_id",
                    "device_identity": "CODEC",
                    "mixer_0": 55,
                }
                if model is AudioInput:
                    data["gain_db"] = obj.gain_db
                form = form_class(data=data, instance=obj)
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.initial["device_identity"], "PCH")
                self.assertEqual(form.cleaned_data["device_identity"], "CODEC")
                submitted_obj = admin_instance.save_form(
                    request=None, form=form, change=True
                )

                with patch("hardware.admin.subprocess.run") as run, patch(
                    "hardware.admin._alsa_store"
                ) as store, patch("hardware.admin.messages.warning") as warning:
                    admin_instance.save_model(
                        request=object(),
                        obj=submitted_obj,
                        form=form,
                        change=True,
                    )

                run.assert_not_called()
                store.assert_not_called()
                warning.assert_called_once()
                submitted_obj.refresh_from_db()
                self.assertEqual(submitted_obj.device_identity, "CODEC")
                self.assertEqual(submitted_obj.mixer_control_values, {})

    def test_stable_to_legacy_skips_old_mixer_edits(self):
        self._assert_mixer_skipped(
            before=("alsa_card_id", "PCH", "plughw:9,0"),
            after=("", "", "plughw:9,0"),
        )

    def test_legacy_raw_card_change_skips_old_mixer_edits(self):
        self._assert_mixer_skipped(
            before=("", "", "plughw:9,0"),
            after=("", "", "plughw:8,0"),
        )

    def test_legacy_to_stable_skips_old_mixer_edits(self):
        self._assert_mixer_skipped(
            before=("", "", "plughw:9,0"),
            after=("alsa_card_id", "CODEC", "plughw:9,0"),
        )

    def test_reload_after_target_change_enumerates_new_effective_card(self):
        for model, admin_class, direction, control_id in self.TARGET_CASES:
            with self.subTest(model=model.__name__):
                obj, _control, _run, _store, _warning = (
                    self._save_with_target_transition(
                        model,
                        admin_class,
                        direction,
                        control_id,
                        before=("alsa_card_id", "PCH", "plughw:9,0"),
                        after=("alsa_card_id", "CODEC", "plughw:9,0"),
                    )
                )
                new_control = {
                    "control_id": control_id,
                    "label": control_id.split(",", 1)[0],
                    "has_enum": False,
                    "has_switch": False,
                    "has_volume": True,
                    "value_pct": 25,
                }
                admin_instance = admin_class(model, None)
                with patch.object(
                    admin_instance, "_enumerate", return_value=[]
                ), patch(
                    "hardware.admin.list_alsa_card_identities",
                    return_value=[
                        _identity("CODEC", card_index=5, direction=direction)
                    ],
                ), patch(
                    "hardware.admin.list_mixer_controls",
                    return_value=[new_control],
                ) as controls:
                    form_class = admin_instance.get_form(request=None, obj=obj)

                self.assertTrue(controls.call_args_list)
                self.assertTrue(
                    all(invocation.args == (5,) for invocation in controls.call_args_list)
                )
                self.assertEqual(
                    form_class._mixer_control_map,
                    {"mixer_0": new_control},
                )
