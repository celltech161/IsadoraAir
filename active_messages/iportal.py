"""Active messages as an iPortal recorder consumer (2.22D3B).

The ONLY place the shared recorder/editor (production.recorder) meets the
active-message domain; the recorder core never imports this app.

* subject      exactly one ActiveBroadcastMessage UUID (``?message=<uuid>``) --
               never "the producer's current message", a kind, a content-type
               name, a model name, a path or a generic foreign key;
* authorize    broad: control or ``active_message.produce``; per subject
               (``authorize_subject``, before any intake/preview/source/commit
               work): the message's own producer or control -- anyone else
               gets a 404 (non-disclosure) -- and only a PREPARED message may
               be recorded/imported/edited/saved, by control on an enabled type
               or by its producer while still assigned to that exact enabled
               type. ACTIVE and terminal messages are read-only (preview /
               export of their exact bound audio). ``remove`` is never offered:
               discarding a draft is a message lifecycle action on the
               producer's Messages page;
* commit       ``services.bind_message_media(expected_revision=...)`` -- the
               canonical locked binding; the revision is compared under the
               message row lock, a stale editor gets 409. Saving binds audio to
               the draft; it NEVER publishes (Save ≠ Publish);
* media access one's OWN takes, and the audio bound to this exact message.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.urls import reverse
from django.utils import timezone

from production.policy import PLATFORM_MAX_BYTES, MediaPolicy
from production.recorder.contracts import (
    Conflict, CurrentAudio, Forbidden, RecorderError, RecordingAdapter, RecordingContext, SubjectNotFound,
)
from production.recorder.views import url_name

from . import services
from .models import ActiveBroadcastMessage, MessageState

ADAPTER_KEY = "active-message"
WRITE_OPERATIONS = frozenset({"record", "import", "edit", "save"})
READ_OPERATIONS = frozenset({"open", "export"})
NATIVE_AIRING_CLOSED = "Native Active Message airing is not enabled on this installation."
M = MessageState


def native_airing_open() -> bool:
    return bool(services.NATIVE_AIRING_AVAILABLE and services.current_policy().native_airing_enabled)


def _seconds(value) -> str:
    value = int(value)
    return f"{value // 60} min {value % 60} s" if value % 60 else f"{value // 60} min"


@dataclass(frozen=True)
class Access:
    visible: bool          # may see the message at all (owner or control)
    writable: bool         # may record/import/edit/save onto it now
    reason: str = ""       # why it is not writable


def access(user, message) -> Access:
    """The exact-subject rules, shared by authorization and the context."""
    control = services.can(user, services.CONTROL)
    owner = getattr(user, "pk", None) is not None and message.producer_key == f"user:{user.pk}"
    if not (control or owner):
        return Access(False, False)
    if message.state == M.ACTIVE:
        return Access(True, False, "This message is active: its audio cannot change. To change what airs, "
                                   "prepare a replacement and publish it.")
    if message.state != M.PREPARED:
        return Access(True, False, f"This message is {message.get_state_display().lower()}; it is read-only.")
    content_type = message.content_type
    if not content_type.enabled:
        return Access(True, False, f"The content type {content_type.name} is disabled; this draft cannot change.")
    if content_type.delivery_mode != message.kind:
        return Access(True, False, "This draft's content type changed; it cannot change.")
    if not control:
        if not services.can(user, services.PRODUCE):
            return Access(True, False, "You can no longer produce active messages.")
        from produced_content import authority
        if not authority.is_assigned(user, content_type):
            return Access(True, False, f"You are no longer assigned to {content_type.name}; this draft is "
                                       f"read-only (you can still discard it).")
    return Access(True, True)


@dataclass(frozen=True)
class MessageSubject:
    message: ActiveBroadcastMessage

    @property
    def pk(self):
        return self.message.pk


class ActiveMessageAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__(
            key=ADAPTER_KEY, label="Urgent & Public Address Messages",
            description="Record your urgent and public-address messages, then publish them (or submit them "
                        "for operator approval).",
            entry_url="/iportal/messages/",
        )

    # -- authorization ----------------------------------------------------------
    def authorize(self, user, operation):
        if not getattr(user, "is_authenticated", False):
            return False
        return services.can(user, services.CONTROL) or services.can(user, services.PRODUCE)

    def show_in_hub(self, user):
        if not getattr(user, "is_authenticated", False):
            return False
        if services.can(user, services.CONTROL):
            return True
        from produced_content import authority
        from produced_content.models import ACTIVE_MESSAGE_MODES
        return services.can(user, services.PRODUCE) and authority.assigned_types(user).filter(
            delivery_mode__in=ACTIVE_MESSAGE_MODES).exists()

    def hub_links(self, user):
        # One hub entry for the whole subsystem: control also gets the board.
        if getattr(user, "is_authenticated", False) and services.can(user, services.CONTROL):
            return (("My messages", "/iportal/messages/"), ("Operator board", "/iportal/messages/board/"))
        return ()

    def authorize_subject(self, user, subject, operation):
        verdict = access(user, subject.message)
        if not verdict.visible:
            raise SubjectNotFound("bad_subject", "no such message")           # never disclose another's message
        if operation in READ_OPERATIONS:
            return True
        if operation in WRITE_OPERATIONS and verdict.writable:
            return True
        raise Forbidden("read_only", verdict.reason or "this message's audio cannot change here")

    # -- subject ------------------------------------------------------------------
    def resolve(self, params):
        try:
            message_id = services._message_id(params.get("message", ""))
        except services.InvalidRequest:
            raise SubjectNotFound("bad_subject", "message is required") from None
        message = (ActiveBroadcastMessage.objects.select_related("content_type", "message_media")
                   .filter(pk=message_id).first())
        if message is None:
            raise SubjectNotFound("bad_subject", "no such message")
        return MessageSubject(message)

    def subject_params(self, subject):
        return {"message": str(subject.pk)}

    def media_policy(self, subject):
        # The shared policy: engine-decodable, at most the CURRENT per-message
        # limit, within the platform byte ceiling. bind_message_media re-checks.
        return MediaPolicy(max_bytes=PLATFORM_MAX_BYTES,
                           max_duration_seconds=float(services.current_policy().max_message_seconds),
                           require_engine_decode=True)

    def _media_url(self, subject, media_id):
        base = reverse(f"active_messages:{url_name(ADAPTER_KEY, 'media')}", kwargs={"media_id": media_id})
        return f"{base}?message={subject.pk}"

    def context(self, request, subject):
        message = subject.message
        content_type = message.content_type
        policy = services.current_policy()
        verdict = access(request.user, message)
        prepared = message.state == M.PREPARED
        self_publish = bool(content_type.producer_self_publish)
        current = None
        if message.message_media_id is not None:
            label = {M.PREPARED: "Saved audio — not active until published",
                     M.ACTIVE: "Active message audio"}.get(message.state, "Audio (read-only)")
            duration = message.message_media.decoded_duration_seconds
            current = CurrentAudio(origin="production_media", label=label,
                                   duration_seconds=float(duration) if duration is not None else None,
                                   media_id=str(message.message_media_id),
                                   preview_url=self._media_url(subject, message.message_media_id))
        operations = ["open", "export"]
        if verdict.writable:
            operations += ["record", "import", "save"] + (["edit"] if current is not None else [])
        if message.state == M.ACTIVE:
            interval = f"{_seconds(message.replay_interval_seconds)} (frozen at activation)"
        elif content_type.replay_interval_seconds:
            interval = f"{_seconds(content_type.replay_interval_seconds)} (this content type)"
        else:
            interval = f"{_seconds(policy.default_replay_interval_seconds)} (station default)"
        display = [
            ("Content type", f"{content_type.name} ({content_type.key})"),
            ("Class", message.get_kind_display()),
            ("Producer", message.producer_username),
            ("Title", message.title or content_type.name),
            ("State", message.get_state_display()),
            ("Saved audio", current.label if current else "none yet"),
            ("Replay interval", interval),
            ("Producer self-publish", "yes" if self_publish else "no — publishing needs operator approval"),
            ("Producer recall", "yes" if content_type.recallable else "no — ask an operator to clear it"),
            ("Expires", timezone.localtime(message.expires_at).strftime("%Y-%m-%d %H:%M %Z")
             if message.expires_at else "no expiry"),
            ("Maximum length", _seconds(policy.max_message_seconds)),
        ]
        gate_open = native_airing_open()
        if not gate_open:
            display.append(("Native airing", NATIVE_AIRING_CLOSED))
        if prepared:
            next_step = ("Saving audio does not publish it: publish it from your Messages page."
                         if self_publish else
                         "Saving audio does not publish it: this content type needs operator approval, so a "
                         "saved draft waits on your Messages page until an operator publishes it.")
            purpose = f"Draft {message.get_kind_display().lower()} message. {next_step}"
            terms = {
                "badge": "DRAFT AUDIO", "save": "Save audio to this draft", "current": "draft's saved take",
                "current_exists": "This draft already has saved audio",
                "none": "No audio saved to this draft yet.",
                "changed": "This draft's saved audio has changed since then.",
                "saved": ("Saved to this draft. It is not active until you publish it from your Messages page."
                          if self_publish else
                          "Saved to this draft. It is not active: it now awaits operator approval."),
                "editor_hint": "browser only; saving updates this draft but does not publish it",
                "restored": "Draft restored. Saving updates this draft; publishing is a separate step.",
            }
            air_label = "Saved to this draft (not active)"
        else:
            purpose = {M.ACTIVE: "This message is active. Its audio is immutable; prepare a replacement to "
                                 "change it."}.get(message.state, "This message is no longer active (read-only).")
            terms = {"badge": message.get_state_display().upper(), "current": "message's audio",
                     "current_exists": "This message has audio", "none": "This message has no audio.",
                     "editor_hint": "browser only; this message is read-only",
                     "restored": "Draft restored for preview only; this message is read-only."}
            air_label = message.get_state_display()
        if not gate_open:
            purpose = f"{purpose} {NATIVE_AIRING_CLOSED}"
        return RecordingContext(
            adapter=ADAPTER_KEY, subject=self.subject_params(subject),
            title=f"{content_type.name} — {message.title or content_type.name}",
            purpose=purpose, allowed_operations=tuple(operations),
            max_duration_seconds=float(policy.max_message_seconds), max_bytes=PLATFORM_MAX_BYTES,
            revision=services.message_revision(message), current=current, display=tuple(display),
            # Opened from the operator board (D3D): Back returns there -- for control only.
            return_url=reverse("active_messages:board"
                               if request.GET.get("from") == "board" and services.can(request.user, services.CONTROL)
                               else "active_messages:my-messages"),
            blocked_reason="" if verdict.writable else verdict.reason,
            air_label=air_label, terms=terms,
        )

    # -- media --------------------------------------------------------------------
    def can_access_media(self, user, subject, media):
        if media.owner_id is not None and media.owner_id == getattr(user, "pk", None):
            return True                                        # one's own takes
        return media.pk == subject.message.message_media_id   # this exact message's bound audio

    def source(self, subject):
        message = subject.message
        if message.message_media_id is None:
            return None
        return ("production_media", message.message_media)

    # -- lifecycle ----------------------------------------------------------------
    def commit(self, user, subject, media, expected_revision):
        try:
            services.bind_message_media(subject.pk, media, actor=user, expected_revision=expected_revision)
        except services.RevisionConflict as exc:
            raise Conflict("stale_revision", "this message changed since the editor opened it",
                           current_revision=exc.current_revision) from exc
        except services.InvalidTransition as exc:
            raise Conflict(exc.code, str(exc)) from exc
        except services.NotPermitted as exc:
            raise Forbidden(exc.code, str(exc)) from exc
        except services.ActiveMessageError as exc:
            raise RecorderError(exc.code, str(exc), status=422) from exc


ADAPTER = ActiveMessageAdapter()
