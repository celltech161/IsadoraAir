from django.apps import AppConfig


class SpokenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "spoken"
    verbose_name = "iPortal spoken content"

    def ready(self):
        from . import recipes
        recipes.register_all()
