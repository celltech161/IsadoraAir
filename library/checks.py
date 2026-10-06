"""System checks for the evergreen VoiceTrack <-> ProductionMedia binding (2.22B B22).

The canonical binding (production.services.retention.lock_for_binding) is a
row lock plus a reference written in ONE transaction. That is only meaningful
if VoiceTrack and ProductionMedia live in the same database: there is no
cross-database binding and no compensation protocol. A router that splits
them is a configuration error, reported at startup.
"""
from django.core import checks
from django.db import router


@checks.register(checks.Tags.database, checks.Tags.models)
def voicetrack_media_share_one_database(app_configs=None, **kwargs):
    from library.models import VoiceTrack
    from production.models import ProductionMedia

    errors = []
    pairs = (("write", router.db_for_write), ("read", router.db_for_read))
    for kind, resolve in pairs:
        left, right = resolve(VoiceTrack), resolve(ProductionMedia)
        if (left or "default") != (right or "default"):
            errors.append(checks.Error(
                f"VoiceTrack ({kind}: {left or 'default'}) and ProductionMedia ({kind}: {right or 'default'}) "
                "are routed to different databases.",
                hint="The VoiceTrack media binding is a same-transaction lock + reference; cross-database "
                     "binding is not supported. Route both models to the same database.",
                id="library.E900",
            ))
    if router.allow_relation(VoiceTrack(), ProductionMedia()) is False:
        errors.append(checks.Error(
            "A database router forbids the VoiceTrack -> ProductionMedia relation.",
            hint="Remove the router rule; the evergreen VoiceTrack binding requires it.",
            id="library.E901",
        ))
    return errors
