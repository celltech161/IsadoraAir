from django.apps import AppConfig


class HardwareConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "hardware"
    verbose_name = "Hardware Config"

    def ready(self):
        from . import signals  # noqa: F401  # registers post_save handlers
