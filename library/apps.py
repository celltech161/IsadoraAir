from django.apps import AppConfig


class LibraryConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'library'

    def ready(self):
        # Wire the group-access cache invalidation signals now that
        # the app registry is fully loaded. Safe to call twice --
        # library/middleware.py also tries at import time (for the
        # case where it's imported before AppConfig.ready runs, e.g.
        # inline test setup), and post_save.connect is idempotent
        # with weak=False plus the same receiver identity.
        from library.middleware import _wire_signals
        try:
            _wire_signals()
        except Exception:
            pass
