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
