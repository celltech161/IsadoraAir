from django.apps import AppConfig


class SpokenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "spoken"
    verbose_name = "iPortal spoken content"

    def ready(self):
        from . import recipes
        recipes.register_all()
        # C2B: the projection owns some columns of projected Tracks, and a
        # projected Track's deletion detaches its asset (spoken.delivery).
        from django.db.models.signals import pre_delete

        from library.models import Track, register_track_write_guard

        from . import delivery, guard
        register_track_write_guard(guard.check_track_save, guard.check_track_update, guard.check_track_bulk_update)
        pre_delete.connect(delivery.track_deleted, sender=Track, dispatch_uid="spoken_projection_track_deleted",
                           weak=False)
