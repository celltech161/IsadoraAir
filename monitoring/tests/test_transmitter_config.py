from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from monitoring.admin import TransmitterConfigAdminForm
from monitoring.models import TransmitterConfig
from monitoring.services.transmitters import transmitter_type_choices


class TransmitterConfigModelTests(TestCase):
    def test_default_preserves_existing_cobalt_behavior(self):
        config = TransmitterConfig.load()
        self.assertEqual(
            config.transmitter_type, TransmitterConfig.TYPE_COBALT_C300
        )
        self.assertEqual(config.password, "")

    def test_stable_type_choices_include_disabled_cobalt_and_bw(self):
        self.assertEqual(
            {value for value, _label in TransmitterConfig.TYPE_CHOICES},
            {"none", "cobalt_c300", "bw_tx300v3"},
        )
        self.assertEqual(
            TransmitterConfig.TYPE_CHOICES, transmitter_type_choices()
        )


class TransmitterConfigAdminFormTests(TestCase):
    def setUp(self):
        self.config = TransmitterConfig.load()
        self.config.transmitter_type = TransmitterConfig.TYPE_BW_TX300V3
        self.config.host = "203.0.113.10"
        self.config.password = "test-only-placeholder"
        self.config.save()

    def _form_data(self, **overrides):
        data = {
            "transmitter_type": "bw_tx300v3",
            "host": "203.0.113.10",
            "port": 23,
            "password": "",
            "timeout_seconds": 3.0,
            "poll_interval_seconds": 30,
            "full_power_watts": 265.0,
        }
        data.update(overrides)
        return data

    def test_blank_password_preserves_saved_value(self):
        form = TransmitterConfigAdminForm(
            data=self._form_data(host="203.0.113.11"),
            instance=self.config,
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.config.refresh_from_db()
        self.assertEqual(self.config.host, "203.0.113.11")
        self.assertEqual(self.config.password, "test-only-placeholder")

    def test_new_password_replaces_saved_value(self):
        form = TransmitterConfigAdminForm(
            data=self._form_data(password="replacement-test-password"),
            instance=self.config,
        )

        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.config.refresh_from_db()
        self.assertEqual(self.config.password, "replacement-test-password")

    def test_bw_requires_a_password_when_none_is_saved(self):
        self.config.password = ""
        self.config.save()
        form = TransmitterConfigAdminForm(
            data=self._form_data(password=""),
            instance=self.config,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("password", form.errors)


@override_settings(SECURE_SSL_REDIRECT=False)
class TransmitterConfigAdminRenderingTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_superuser(
            "transmitter-admin", "tx-admin@example.invalid", "test-password"
        )
        self.client.force_login(self.staff)
        self.config = TransmitterConfig.load()
        self.config.transmitter_type = TransmitterConfig.TYPE_BW_TX300V3
        self.config.host = "203.0.113.10"
        self.config.password = "test-only-placeholder"
        self.config.save()

    def test_saved_password_is_masked_and_not_rendered(self):
        response = self.client.get(
            reverse(
                "admin:monitoring_transmitterconfig_change",
                args=[self.config.pk],
            )
        )

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn('type="password"', html)
        self.assertNotIn("test-only-placeholder", html)
