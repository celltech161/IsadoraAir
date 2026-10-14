"""D2A fixtures: REAL audio through the real intake, validation service and
isadoraair-render service (the runner production/tests/run_with_validation_service.sh
provides both). Each input is a pure tone of its own frequency, so a
rendition can be analysed for order: which tone plays in which window."""
from __future__ import annotations

import io

from django.utils import timezone

from active_messages import services
from production.models import ProductionMedia, ReferenceAudio
from production.services import intake, reference_audio
from production.services.render import provenance
from production.tests.support import make_row
from spoken.tests import support as audio

from .support import Domain

STINGER_HZ, URGENT_HZ, URGENT2_HZ, PA_HZ = 1500.0, 440.0, 660.0, 880.0


def tone(seconds, frequency, *, channels=1) -> bytes:
    return audio.tone_wav([(seconds, frequency, 0.4)], channels=channels)


def ingest(data, *, owner=None, kind=ProductionMedia.KIND_RECORDING) -> ProductionMedia:
    return intake.ingest_stream(io.BytesIO(data), kind=kind, owner=owner, validate=True).media


def stinger(actor, *, key="stinger", seconds=1.0, frequency=STINGER_HZ) -> ReferenceAudio:
    bed = ingest(tone(seconds, frequency, channels=2), kind=ProductionMedia.KIND_BED)
    return reference_audio.register(key, bed, actor=actor)


def fake_rendition(snapshot, *, seconds="10.000000", digest=None, derived_from=None, **overrides):
    """A rendition ROW (no bytes; raw insert as the render executor's intake
    would make) claiming the given provenance -- for binding-proof tests."""
    from active_messages import rendering
    recipe = provenance.assembly(rendering.ASSEMBLY_KEY)
    values = dict(kind=ProductionMedia.KIND_RENDITION, container="flac", codec="flac", sample_rate=48000,
                  channels=2, decoded_duration_seconds=seconds, validation_state="valid", validation_code="ok",
                  validated_at=timezone.now(), recipe_key=recipe.key, recipe_version=recipe.version,
                  recipe_params_digest=digest or rendering.expected_digest(snapshot),
                  derived_from=derived_from or ProductionMedia.objects.get(pk=rendering.first_message_media(snapshot)))
    values.update(overrides)
    return make_row(**values)


class RealAudio(Domain):
    """Domain users plus real-audio message helpers."""

    def real_message(self, actor=None, *, kind="public_address", seconds=1.0, frequency=PA_HZ):
        actor = actor or self.admin
        message = services.prepare_message(actor=actor, kind=kind)
        return services.bind_message_media(message, ingest(tone(seconds, frequency), owner=actor), actor=actor)

    def real_active(self, actor=None, *, kind="public_address", seconds=1.0, frequency=PA_HZ, now=None):
        message = self.real_message(actor, kind=kind, seconds=seconds, frequency=frequency)
        return services.activate_message(message, actor=self.admin, now=now)

    def use_stinger(self, **kwargs):
        registration = stinger(self.admin, **kwargs)
        services.set_stinger_key(registration.key, actor=self.admin)
        return registration
