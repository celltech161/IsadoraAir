from django.apps import AppConfig


class AuthzConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'authz'

    def ready(self):
        # Same try/except-at-import-plus-ready() belt-and-suspenders
        # pattern as library/apps.py -- safe to call twice.
        from authz.evaluator import _wire_signals
        try:
            _wire_signals()
        except Exception:
            pass
