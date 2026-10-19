from django.apps import AppConfig


class ActiveMessagesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "active_messages"
    verbose_name = "iPortal active messages"

    def ready(self):
        # 2.22D3B: the producer workspace is an iPortal recorder consumer (the
        # shared recorder/editor; subject = one exact ActiveBroadcastMessage).
        from production.recorder import registry as recorder_registry

        from .iportal import ADAPTER as active_message_recorder_adapter
        recorder_registry.register(active_message_recorder_adapter)
        # D3E-C review 3: a Track may own a reserved native publication path
        # only under that path's publication destination lock.
        from library.models import register_track_write_guard

        from . import track_guard
        register_track_write_guard(track_guard.check_track_save, track_guard.check_track_update,
                                   track_guard.check_track_bulk_update)
