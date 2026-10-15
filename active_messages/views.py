"""The producer's Active Messages workspace (iPortal Phase D3B).

``/iportal/messages/`` -- "My Messages": the producer's enabled urgent and
public-address content types and, per type, their exact ACTIVE message and
PREPARED drafts. Every change is a CSRF-protected POST naming one exact
message UUID (or, to start a draft, one exact content type) and goes through
``active_messages.services`` -- the services authorize again, so a hidden or
shown button is never the authorization. Nothing here is inferred from a
producer/type pair: no "my current message".

* start     ``services.prepare_message`` (then the shared recorder opens it);
* publish   ``services.activate_message`` -- an explicit act, separate from
            saving audio (Save ≠ Publish); offered only where the type allows
            producer self-publish (otherwise the draft awaits operator
            approval -- PREPARED is the review state);
* recall    ``services.clear_message(expected_state=active)`` after a
            confirmation naming the type and title;
* discard   ``services.clear_message(expected_state=prepared)``, likewise.

Another producer's message is a 404 here (non-disclosure). The global
operator board, Replay Now and expiry controls are not part of this page.
"""
from __future__ import annotations

from collections import defaultdict

from django.contrib import messages as flash
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from produced_content import authority
from produced_content.models import ACTIVE_MESSAGE_MODES, ProducedContentType
from production.recorder.views import url_name

from . import services
from .iportal import ADAPTER_KEY, NATIVE_AIRING_CLOSED, native_airing_open
from .models import ActiveBroadcastMessage, MessageState

M = MessageState
UNFINISHED = (M.PREPARED, M.ACTIVE)


def _require_workspace(user):
    control = services.can(user, services.CONTROL)
    if not (control or services.can(user, services.PRODUCE)):
        raise PermissionDenied("Active messages are not available to your account.")
    return control


def _producer_key(user) -> str:
    return f"user:{user.pk}"


def _recorder_url(message_id) -> str:
    page = reverse("active_messages:" + url_name(ADAPTER_KEY, "page"))
    return f"{page}?message={message_id}"


def _own_message(user, message_id, control):
    """The exact message, visible to its producer or control; else 404."""
    message = (ActiveBroadcastMessage.objects.select_related("content_type", "message_media")
               .filter(pk=message_id).first())
    if message is None or not (control or message.producer_key == _producer_key(user)):
        raise Http404("no such message")
    return message


def _types_for(user, control):
    """Enabled urgent/public-address types: all of them for control, the
    assigned ones for a producer. Regular and test types never appear."""
    rows = (authority.assigned_types(user) if not control
            else ProducedContentType.objects.filter(enabled=True))
    return list(rows.filter(delivery_mode__in=ACTIVE_MESSAGE_MODES).order_by("sort_order", "name", "key"))


@require_GET
def my_messages(request):
    control = _require_workspace(request.user)
    policy = services.current_policy()
    types = _types_for(request.user, control)
    mine = list(ActiveBroadcastMessage.objects.select_related("content_type", "message_media")
                .filter(producer_key=_producer_key(request.user), state__in=UNFINISHED)
                .order_by("created_at", "id"))
    by_type = defaultdict(lambda: {"active": [], "drafts": []})
    for message in mine:
        by_type[message.content_type_id]["active" if message.state == M.ACTIVE else "drafts"].append(message)

    def message_row(message, *, publishable, recallable):
        bound = message.message_media
        return {
            "message": message, "recorder_url": _recorder_url(message.pk),
            "duration": bound.decoded_duration_seconds if bound is not None else None,
            "has_audio": bound is not None,
            "can_publish": publishable and bound is not None,
            "self_publish": publishable,
            "can_recall": recallable,
        }

    rows, listed = [], set()
    for content_type in types:
        listed.add(content_type.pk)
        found = by_type.get(content_type.pk, {"active": [], "drafts": []})
        self_publish = control or bool(content_type.producer_self_publish)
        recallable = control or bool(content_type.recallable)
        rows.append({
            "type": content_type,
            "replay_seconds": content_type.replay_interval_seconds or policy.default_replay_interval_seconds,
            "replay_is_default": not content_type.replay_interval_seconds,
            "active": [message_row(m, publishable=False, recallable=recallable) for m in found["active"]],
            # Every PREPARED row is shown: several drafts are never hidden or guessed between.
            "drafts": [message_row(m, publishable=self_publish, recallable=False) for m in found["drafts"]],
        })
    # The producer's own unfinished messages of types no longer offered here
    # (assignment removed, type disabled): visible, so they can still be
    # withdrawn -- recall needs no assignment.
    others = [message_row(m, publishable=False,
                          recallable=m.state == M.ACTIVE and (control or bool(m.content_type.recallable)))
              for m in mine if m.content_type_id not in listed]
    return render(request, "active_messages/my_messages.html", {
        "rows": rows, "others": others, "control": control,
        "gate_open": native_airing_open(), "gate_message": NATIVE_AIRING_CLOSED,
        "max_message_seconds": policy.max_message_seconds,
    })


def _fail(request, exc):
    flash.error(request, str(exc))
    return redirect("active_messages:my-messages")


@require_POST
def start_draft(request):
    """A new PREPARED message of one exact type, via the services; then the
    shared recorder opens that exact message."""
    _require_workspace(request.user)
    title = (request.POST.get("title") or "").strip()
    try:
        message = services.prepare_message(actor=request.user, content_type=request.POST.get("content_type", ""),
                                           title=title)
    except services.ActiveMessageError as exc:
        return _fail(request, exc)
    return redirect(_recorder_url(message.pk))


@require_POST
def publish(request, message_id):
    control = _require_workspace(request.user)
    message = _own_message(request.user, message_id, control)
    try:
        message = services.activate_message(message.pk, actor=request.user)
    except services.ActiveMessageError as exc:
        return _fail(request, exc)
    flash.success(request, f"Published: {message.content_type.name} — {message.title or message.content_type.name}.")
    return redirect("active_messages:my-messages")


ACTIONS = {
    "recall": {"state": M.ACTIVE, "verb": "Recall", "reason": "recalled by its producer",
               "explain": "It will not air again. An airing already on air finishes."},
    "discard": {"state": M.PREPARED, "verb": "Discard", "reason": "draft discarded by its producer",
                "explain": "The draft is closed; its saved audio stays in the station's media history."},
}


def _lifecycle(request, message_id, action):
    control = _require_workspace(request.user)
    message = _own_message(request.user, message_id, control)
    spec = ACTIONS[action]
    if request.method == "GET":
        if message.state != spec["state"]:
            flash.info(request, f"That message is {message.get_state_display().lower()} now.")
            return redirect("active_messages:my-messages")
        return render(request, "active_messages/confirm.html", {"message": message, "action": action, **spec})
    try:
        services.clear_message(message.pk, actor=request.user, reason=spec["reason"],
                               expected_state=spec["state"])
    except services.ActiveMessageError as exc:
        return _fail(request, exc)
    flash.success(request, f"{spec['verb']}ed: {message.content_type.name} — "
                           f"{message.title or message.content_type.name}.")
    return redirect("active_messages:my-messages")


@require_http_methods(["GET", "POST"])
def recall(request, message_id):
    return _lifecycle(request, message_id, "recall")


@require_http_methods(["GET", "POST"])
def discard(request, message_id):
    return _lifecycle(request, message_id, "discard")
