"""Produced content as an iPortal recorder consumer (2.22D3C).

ONE registered adapter (``produced-content``) for the two workflows of the
"My Content" workspace; the recorder core never imports this app. Every
subject is server-resolved to exactly one object -- never a model name, path,
filename, category, script or "my current item":

* ``?item=<uuid>``      REGULAR: one exact SpokenItem of a D3C-managed backend
                        (produced_content.regular). Only its own producer sees
                        it (anyone else: 404). A DRAFT is writable by its
                        producer while they may still produce the exact enabled
                        type; its recorder "save" is the explicit SUBMISSION
                        (``regular.submit``: revision under the row lock, then
                        submit + validation in one transaction). Submitted and
                        later items are read-only previews.
* ``?test_type=<key>``  TEST / NO AIR: the AUTHENTICATED user's own binding for
                        one exact test type (``resolve_for`` -- the identity
                        comes from the session). Save binds a take
                        (produced_content.no_air), never anything airable.

Authorization order for every operation (the recorder enforces it): broad
capability (``authorize``), exact subject resolution, then the exact
type/assignment/ownership check (``authorize_subject``) -- before any intake,
preview, source or commit work.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from production.policy import PLATFORM_MAX_BYTES, PLATFORM_MAX_DURATION_SECONDS, MediaPolicy
from production.recorder.contracts import (
    Conflict, CurrentAudio, Forbidden, RecorderError, RecordingAdapter, RecordingContext, SubjectNotFound,
)
from production.recorder.views import url_name
from spoken.models import ItemState

from . import authority, backend, no_air, regular
from .models import TEST_TAKE_RETENTION_DAYS, ProducedContentType

ADAPTER_KEY = "produced-content"
WRITE_OPERATIONS = frozenset({"record", "import", "edit", "save"})
READ_OPERATIONS = frozenset({"open", "export"})
NOTHING_AIRS = "Nothing in this workspace can air."
SUBMITTED_MESSAGE = "Submitted. Processing and library delivery continue automatically; follow it on My Content."
S = ItemState


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        raise SubjectNotFound("bad_subject", "no such item") from None


@dataclass(frozen=True)
class RegularSubject:
    item: object                 # spoken.SpokenItem
    produced: ProducedContentType

    @property
    def pk(self):
        return self.item.pk


@dataclass(frozen=True)
class TestSubject:
    user_id: int
    content_type: ProducedContentType
    take: object | None          # ProducedContentTestTake


def regular_write_refusal(user, subject) -> str:
    """Why this exact regular item cannot be recorded onto / submitted now."""
    item, produced = subject.item, subject.produced
    if item.state != S.DRAFT:
        return f"This item is {item.get_state_display().lower()}; it is read-only here."
    refusal = authority.production_refusal(user, produced)
    if refusal:
        return f"{refusal} This draft cannot be submitted (you can still abandon it on My Content)."
    problem = backend.runnable_problem(produced)
    if problem:
        return f"{produced.name} cannot be produced right now: {problem}."
    return ""


class ProducedContentAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__(
            key=ADAPTER_KEY, label="My Content",
            description="Record or import your produced segments (news, calendars, PSAs ...) and submit them for "
                        "processing into the station library -- or practice in a test / no-air workspace.",
            entry_url="/iportal/content/",
        )

    # -- authorization ----------------------------------------------------------
    def authorize(self, user, operation):
        return authority.has_workspace(user)

    def show_in_hub(self, user):
        if authority.can(user, authority.CONTROL):
            return True
        return authority.can(user, authority.PRODUCE) and authority.content_types_for(user).exists()

    def authorize_subject(self, user, subject, operation):
        if isinstance(subject, RegularSubject):
            if not regular.is_owner(user, subject.item):
                raise SubjectNotFound("bad_subject", "no such item")          # never disclose another's item
            if operation in READ_OPERATIONS:
                return True
            if operation == "save" and subject.item.state != S.DRAFT:
                # An editor opened on the draft, saving after the item moved on
                # (another tab submitted it): a conflict, never a replacement.
                raise Conflict("stale_revision", "this item was already submitted",
                               current_revision=regular.item_revision(subject.item))
            refusal = regular_write_refusal(user, subject) if operation in WRITE_OPERATIONS else \
                "this workspace cannot do that"
            if refusal:
                raise Forbidden("read_only", refusal)
            return True
        refusal = authority.production_refusal(user, subject.content_type)
        if refusal and subject.take is None:
            # Not theirs to produce, and nothing of theirs to see or remove.
            raise SubjectNotFound("bad_subject", "no such test workspace")
        if operation in READ_OPERATIONS:
            return True
        if operation in WRITE_OPERATIONS:
            if refusal:
                raise Forbidden("read_only", refusal)
            return True
        if operation == "remove":                                          # discarding one's own take
            return True
        raise Forbidden("forbidden", "this workspace cannot do that")

    # -- subject ------------------------------------------------------------------
    def resolve(self, params):
        if params.get("test_type"):
            raise SubjectNotFound("bad_subject", "a test workspace belongs to the signed-in user")
        return self._regular(params)

    def resolve_for(self, user, params):
        if params.get("item") and params.get("test_type"):
            raise SubjectNotFound("bad_subject", "name one subject")
        if params.get("test_type"):
            try:
                content_type = no_air.resolve_test_type(str(params.get("test_type")))
            except no_air.NotFound:
                raise SubjectNotFound("bad_subject", "no such test workspace") from None
            return TestSubject(user.pk, content_type, no_air.current_take(user, content_type))
        return self._regular(params)

    def _regular(self, params):
        if not params.get("item"):
            raise SubjectNotFound("bad_subject", "item is required")
        found = regular.managed_item(_uuid(params.get("item")))
        if found is None:
            raise SubjectNotFound("bad_subject", "no such item")
        return RegularSubject(*found)

    def subject_params(self, subject):
        if isinstance(subject, RegularSubject):
            return {"item": str(subject.pk)}
        return {"test_type": subject.content_type.key}

    def media_policy(self, subject):
        if isinstance(subject, RegularSubject):
            from spoken.configuration import media_policy_for
            return media_policy_for(subject.item.content_type)
        return MediaPolicy(max_bytes=PLATFORM_MAX_BYTES, max_duration_seconds=float(PLATFORM_MAX_DURATION_SECONDS),
                           require_engine_decode=True)

    def _media_url(self, subject, media_id):
        base = reverse(f"produced_content:{url_name(ADAPTER_KEY, 'media')}", kwargs={"media_id": media_id})
        key, value = next(iter(self.subject_params(subject).items()))
        return f"{base}?{key}={value}"

    def _current(self, subject, media, label):
        duration = media.decoded_duration_seconds
        return CurrentAudio(origin="production_media", label=label,
                            duration_seconds=float(duration) if duration is not None else None,
                            media_id=str(media.pk), preview_url=self._media_url(subject, media.pk))

    def context(self, request, subject):
        if isinstance(subject, RegularSubject):
            return self._regular_context(request, subject)
        return self._test_context(request, subject)

    def _regular_context(self, request, subject):
        item, produced = subject.item, subject.produced
        refusal = regular_write_refusal(request.user, subject)
        draft = item.state == S.DRAFT
        current = None
        if item.rendition_media_id is not None:
            current = self._current(subject, item.rendition_media, "Processed audio")
        elif item.submission_media_id is not None:
            current = self._current(subject, item.submission_media, "Submitted audio")
        operations = ["open", "export"]
        if draft and not refusal:
            operations += ["record", "import", "save"]
        category = produced.target_category
        display = [
            ("Content type", f"{produced.name} ({produced.key})"),
            ("Library category", f"{category.name} ({category.code})" if category else "none"),
            ("Output", produced.get_output_mode_display()),
            ("Processing recipe", produced.recipe_key or "none"),
            ("Artist", produced.artist_tag or produced.name),
            ("Title", item.title or "the submission date"),
            ("State", item.get_state_display()),
        ]
        if item.failure_code:
            display.append(("Problem", item.failure_code))
        if draft:
            purpose = ("Record or import your audio, then submit it. Submitting sends it for automatic processing "
                       "and delivery into the station library; nothing is submitted or on air until you submit.")
            terms = {
                "badge": "SUBMITTED", "save": "Submit for processing", "current": "submitted audio",
                "current_exists": "This item already has submitted audio",
                "none": "Nothing submitted yet.",
                "changed": "This item has changed since then.",
                "saved": SUBMITTED_MESSAGE,
                "editor_hint": "browser only; nothing is submitted or on air until you submit",
                "restored": "Draft restored. Nothing is submitted until you submit it.",
            }
            air_label = "Submitted for processing"
        else:
            purpose = (f"This item is {item.get_state_display().lower()}. Its audio can no longer change here; "
                       f"processing and delivery continue automatically. Start a new submission on My Content to "
                       f"replace it.")
            # "saved" too: the workstation words its post-submit confirmation from
            # the context the commit returns, which is this read-only one.
            terms = {"badge": item.get_state_display().upper(), "current": "item's audio",
                     "saved": SUBMITTED_MESSAGE,
                     "current_exists": "This item has audio", "none": "This item has no audio.",
                     "editor_hint": "browser only; this item is read-only",
                     "restored": "Draft restored for preview only; this item is read-only."}
            air_label = item.get_state_display()
        return RecordingContext(
            adapter=ADAPTER_KEY, subject=self.subject_params(subject),
            title=f"{produced.name} — {item.title or 'new submission'}",
            purpose=purpose, allowed_operations=tuple(operations),
            max_duration_seconds=float(self.media_policy(subject).max_duration_seconds
                                       or PLATFORM_MAX_DURATION_SECONDS),
            max_bytes=self.media_policy(subject).max_bytes,
            revision=regular.item_revision(item), current=current, display=tuple(display),
            return_url=reverse("produced_content:my-content"),
            blocked_reason=refusal if draft else "",
            air_label=air_label, terms=terms,
        )

    def _test_context(self, request, subject):
        content_type, take = subject.content_type, subject.take
        refusal = authority.production_refusal(request.user, content_type)
        current = self._current(subject, take.media, "Saved test take (never airs)") if take is not None else None
        operations = ["open", "export"]
        if not refusal:
            operations += ["record", "import", "save"] + (["edit"] if current is not None else [])
        if take is not None:
            operations.append("remove")
        display = [
            ("Content type", f"{content_type.name} ({content_type.key})"),
            ("Mode", "TEST / NO AIR"),
            ("Saved test take", "yes" if take is not None else "none yet"),
            ("Kept until", timezone.localtime(take.expires_at).strftime("%Y-%m-%d %H:%M %Z")
             if take is not None else "—"),
            ("Retention", f"{TEST_TAKE_RETENTION_DAYS} days after your most recent save"),
        ]
        terms = {
            "badge": "TEST / NO AIR", "save": "Save test take", "current": "saved test take",
            "current_exists": "You already have a saved test take here",
            "none": "No saved test take yet.",
            "changed": "Your saved test take has changed since then.",
            "saved": f"Test take saved. {NOTHING_AIRS}",
            "editor_hint": f"browser only; {NOTHING_AIRS.lower()}",
            "restored": f"Draft restored. {NOTHING_AIRS}",
        }
        return RecordingContext(
            adapter=ADAPTER_KEY, subject=self.subject_params(subject),
            title=f"{content_type.name} — TEST / NO AIR",
            purpose=f"TEST / NO AIR. Practice recording, importing and editing here. {NOTHING_AIRS}",
            allowed_operations=tuple(operations),
            max_duration_seconds=float(PLATFORM_MAX_DURATION_SECONDS), max_bytes=PLATFORM_MAX_BYTES,
            revision=no_air.take_revision(take), current=current, display=tuple(display),
            return_url=reverse("produced_content:my-content"),
            blocked_reason=refusal, air_label="Saved test take (never airs)", terms=terms,
        )

    # -- media --------------------------------------------------------------------
    def can_access_media(self, user, subject, media):
        if media.owner_id is not None and media.owner_id == getattr(user, "pk", None):
            return True                                                    # one's own takes
        if isinstance(subject, RegularSubject):                            # this exact item's audio
            return media.pk in (subject.item.submission_media_id, subject.item.rendition_media_id)
        return subject.take is not None and media.pk == subject.take.media_id

    def source(self, subject):
        if isinstance(subject, TestSubject) and subject.take is not None:
            return ("production_media", subject.take.media)
        return None

    # -- lifecycle ----------------------------------------------------------------
    def commit(self, user, subject, media, expected_revision):
        if isinstance(subject, RegularSubject):
            try:
                regular.submit(user, subject.item, media, expected_revision=expected_revision)
            except regular.RevisionConflict as exc:
                raise Conflict("stale_revision", "this item changed since the editor opened it",
                               current_revision=exc.detail.get("current_revision", "")) from exc
            except regular.StateConflict as exc:
                raise Conflict(exc.code, str(exc)) from exc
            except regular.NotFound as exc:
                raise SubjectNotFound("bad_subject", "no such item") from exc
            except regular.NotPermitted as exc:
                raise Forbidden(exc.code, str(exc)) from exc
            except regular.RegularError as exc:
                raise RecorderError(exc.code, str(exc), status=422) from exc
            return
        try:
            no_air.save_take(user, subject.content_type, media, expected_revision=expected_revision)
        except no_air.RevisionConflict as exc:
            raise Conflict("stale_revision", "your test take changed since this editor opened it",
                           current_revision=exc.detail.get("current_revision", "")) from exc
        except no_air.NotPermitted as exc:
            raise Forbidden(exc.code, str(exc)) from exc
        except no_air.TestTakeError as exc:
            raise RecorderError(exc.code, str(exc), status=422) from exc

    def remove(self, user, subject, expected_revision):
        if not isinstance(subject, TestSubject):
            raise RecorderError("not_supported", "abandon or recall an item on My Content")
        try:
            no_air.discard_take(user, subject.content_type, expected_revision=expected_revision)
        except no_air.RevisionConflict as exc:
            raise Conflict("stale_revision", "your test take changed since this editor opened it",
                           current_revision=exc.detail.get("current_revision", "")) from exc


ADAPTER = ProducedContentAdapter()
