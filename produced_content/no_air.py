"""TEST / NO AIR: a producer's current saved test take (D3C).

    (user, exact test type) -> ProducedContentTestTake -> one validated ProductionMedia

The ONLY durable effect of the TEST workspace. It calls no spoken transition,
renderer or projection, creates no Track, log item, active message or queue
entry and sends no engine command. And the media itself can never air: every
take made in the TEST recorder is NO-AIR ProductionMedia from intake on
(``airing_prohibited``, immutable, inherited by every derivative), which the
canonical air-capable binding refuses -- while it is a test take, after it is
replaced, discarded or expires, and if it was never saved at all.

* save      lock + re-read + authorize the exact type (still TEST, enabled,
            the user's); then lock the user's binding; compare its optimistic
            revision under that lock (a stale tab gets a conflict, never a
            silent overwrite); bind the user's own VALID NO-AIR take under
            ``lock_for_no_air_binding`` (ordinary media is refused:
            not_test_media); the retention clock restarts
            (TEST_TAKE_RETENTION_DAYS from the most recent save);
* discard   the producer removes their binding (same revision rule);
* expire    a bounded sweep (``expire_due``) removes bindings past their
            expiry -- run from the existing spoken render worker loop.

When a binding is replaced, discarded or expires, the previously bound media
gets an after-commit attempt at the ordinary safe purge
(production.services.retention.purge_media); if anything else references it,
the purge refuses and ordinary retention decides -- a harmless outcome.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from production.errors import (
    MediaAiringNotProhibited, MediaInconsistent, MediaNotValidated, MediaPurged, PurgeRefused,
)
from production.models import ProductionMedia
from production.services import retention

from . import authority
from .models import TEST_TAKE_RETENTION_DAYS, DeliveryMode, ProducedContentTestTake, ProducedContentType

log = logging.getLogger(__name__)
RETENTION = timedelta(days=TEST_TAKE_RETENTION_DAYS)
NO_TAKE_REVISION = "none"
TAKE_KINDS = (ProductionMedia.KIND_RECORDING, ProductionMedia.KIND_UPLOAD, ProductionMedia.KIND_EDIT)
EXPIRY_BATCH = 100


class TestTakeError(Exception):
    code = "test_take_error"

    def __init__(self, message: str, *, code: str | None = None, **detail):
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail


class NotFound(TestTakeError):
    code = "not_found"


class NotPermitted(TestTakeError):
    code = "not_permitted"


class MediaRefused(TestTakeError):
    code = "media_refused"


class RevisionConflict(TestTakeError):
    code = "stale_revision"


def resolve_test_type(content_type) -> ProducedContentType:
    try:
        row = authority.resolve(content_type)
    except authority.UnknownContentType:
        raise NotFound("no such test content type") from None
    if row.delivery_mode != DeliveryMode.TEST:
        raise NotFound("no such test content type")
    return row


def current_take(user, content_type) -> ProducedContentTestTake | None:
    if getattr(user, "pk", None) is None:
        return None
    return (ProducedContentTestTake.objects.select_related("media", "content_type")
            .filter(user_id=user.pk, content_type_id=getattr(content_type, "pk", content_type)).first())


def take_revision(take) -> str:
    if take is None:
        return NO_TAKE_REVISION
    return hashlib.sha256(f"{take.pk}|{take.revision}|{take.media_id}".encode()).hexdigest()[:32]


def _purge_quietly(media_id) -> None:
    try:
        retention.purge_media(media_id)
    except PurgeRefused:
        pass                                    # still referenced elsewhere: ordinary retention decides
    except Exception:                           # noqa: BLE001 -- a purge problem never undoes the change
        log.exception("produced_content: could not purge unbound test media %s", media_id)


def _locked_take(user, content_type, expected_revision):
    take = (ProducedContentTestTake.objects.select_for_update()
            .filter(user_id=user.pk, content_type_id=content_type.pk).first())
    current = take_revision(take)
    if not expected_revision or expected_revision != current:
        raise RevisionConflict("your test take changed since this editor opened it", current_revision=current)
    return take


def save_take(user, content_type, media, *, expected_revision) -> ProducedContentTestTake:
    """Bind the user's own valid take as their current test take of this exact type.

    Lock order: ProducedContentType -> ProducedContentTestTake -> ProductionMedia.
    The exact type is locked, re-read and proven TEST, enabled and the user's
    (CONTROL, or PRODUCE + assignment) BEFORE the binding is locked or created,
    so a binding always names a type that is still TEST at the commit point: a
    concurrent mode change (ProducedContentType.save takes the same row lock)
    either commits first -- and this save is refused -- or waits and then sees
    the committed take and is refused itself."""
    with transaction.atomic():
        try:
            ctype = authority.lock_for_production(user, content_type, mode=DeliveryMode.TEST)
        except authority.ProductionRefused as exc:
            raise (NotFound if exc.code == "unknown_type" else NotPermitted)(str(exc), code=exc.code) from None
        take = _locked_take(user, ctype, expected_revision)
        candidate = ProductionMedia.objects.filter(pk=getattr(media, "pk", media)).first()
        if candidate is None or candidate.owner_id != user.pk:
            raise MediaRefused("only your own takes can be saved here", code="not_your_media")
        if candidate.kind not in TAKE_KINDS:
            raise MediaRefused("a test take is a recording, upload or edit", code="media_kind")
        try:
            # The NO-AIR binding lock: only media made under a TEST / NO AIR
            # subject (immutable, from intake) -- never ordinary media.
            bound = retention.lock_for_no_air_binding(candidate)
        except MediaAiringNotProhibited as exc:
            raise MediaRefused("only a take made in this TEST / NO AIR workspace can be saved here",
                               code="not_test_media") from exc
        except (MediaPurged, MediaNotValidated, MediaInconsistent) as exc:
            raise MediaRefused("the take is missing or not validated as usable audio",
                               code="media_not_valid") from exc
        now = timezone.now()
        previous = None
        if take is None:
            try:
                with transaction.atomic():
                    take = ProducedContentTestTake.objects.create(user=user, content_type=ctype, media=bound,
                                                                  expires_at=now + RETENTION)
            except IntegrityError:
                # Another tab saved the first take meanwhile: this editor is stale.
                raise RevisionConflict("your test take changed since this editor opened it",
                                       current_revision=take_revision(current_take(user, ctype))) from None
        else:
            previous = take.media_id if take.media_id != bound.pk else None
            take.media = bound
            take.revision = take.revision + 1
            take.expires_at = now + RETENTION
            take.save(update_fields=["media", "revision", "expires_at", "updated_at"])
        if previous is not None:
            transaction.on_commit(lambda: _purge_quietly(previous))
        return take


def discard_take(user, content_type, *, expected_revision) -> None:
    """The producer removes their current test take (no assignment needed:
    withdrawing one's own temporary media is always allowed)."""
    ctype = resolve_test_type(content_type)
    with transaction.atomic():
        take = _locked_take(user, ctype, expected_revision)
        if take is None:
            return
        media_id = take.media_id
        take.delete()
        transaction.on_commit(lambda: _purge_quietly(media_id))


def expire_due(*, limit=EXPIRY_BATCH, now=None) -> int:
    """Remove at most ``limit`` expired bindings, oldest first, each in its own
    transaction (a binding saved again meanwhile is no longer due and stays).
    Returns how many were removed."""
    now = now or timezone.now()
    due = list(ProducedContentTestTake.objects.filter(expires_at__lte=now).order_by("expires_at", "pk")
               .values_list("pk", flat=True)[:max(0, int(limit))])
    removed = 0
    for pk in due:
        with transaction.atomic():
            take = (ProducedContentTestTake.objects.select_for_update(skip_locked=True)
                    .filter(pk=pk, expires_at__lte=now).first())
            if take is None:
                continue
            media_id = take.media_id
            take.delete()
            transaction.on_commit(lambda media_id=media_id: _purge_quietly(media_id))
            removed += 1
    return removed
