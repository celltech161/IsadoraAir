from django.apps import AppConfig


class ProducedContentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "produced_content"
    verbose_name = "iPortal produced content"

    def ready(self):
        # 2.22D3C: the My Content workspace is an iPortal recorder consumer (the
        # shared recorder/editor; subject = one exact SpokenItem, or the signed-in
        # user's own test take of one exact test type).
        from production.recorder import registry as recorder_registry

        from .iportal import ADAPTER as produced_content_recorder_adapter
        recorder_registry.register(produced_content_recorder_adapter)
        # CONTROL withdraws/recalls the items of D3C-managed backends -- and only
        # those (spoken.transitions.may_recall_as_manager).
        from spoken import transitions as spoken_transitions

        from .authority import controls_managed_type
        spoken_transitions.register_recall_authority(controls_managed_type)
