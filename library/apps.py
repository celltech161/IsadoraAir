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
        # 2.22B: evergreen VoiceTrack is the first iPortal recorder consumer.
        from production.recorder import registry as recorder_registry
        from library.iportal import ADAPTER as voicetrack_recorder_adapter
        recorder_registry.register(voicetrack_recorder_adapter)

        from library.middleware import _wire_signals, _wire_station_tz_signals
        try:
            _wire_signals()
        except Exception:
            pass
        try:
            _wire_station_tz_signals()
        except Exception:
            pass
