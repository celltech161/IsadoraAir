"""The Active Messages Operator Board (iPortal Phase D3D).

``/iportal/messages/board/`` -- the station operator's view of the whole
native active-message domain, across producers: what is ACTIVE (in the
domain -- which is NOT the same as on air), what awaits review (PREPARED is
the review state; there is no other approval lifecycle), the history and the
airings. CONTROL only (``active_message.control``; staff/superuser through
authorize()'s compatibility bypass); producers keep My Messages.

Every action names one exact message UUID and goes through the existing
services, which authorize again and keep the established lock order and
transactions:

* publish     ``activate_message(expected_revision=...)`` -- only the exact
              draft (and audio) the operator reviewed;
* clear       ``clear_message(expected_state=active)`` after a confirmation
              naming the message and its producer;
* decline     ``clear_message(expected_state=prepared)``, likewise;
* expiry      ``set_message_expiry(expected_expires_at=...)``;
* Replay Now  ``request_manual_replay(request_token=...)`` -- the token is
              issued with the page, so a repeated submission resolves to the
              same airing. While native airing is closed the board refuses
              BEFORE the service: no dormant execution is ever created as if
              it were a live replay.

Reads take no lock: the board never waits on, or holds, the policy lock the
engine's bounded admission needs. What a page shows is never authority --
every POST is decided again by the services under their locks.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib import messages as flash
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Case, Count, F, IntegerField, Q, Value, When, Window
from django.db.models.functions import RowNumber
from django.http import Http404, JsonResponse, QueryDict
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.http import urlencode
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from production.models import ProductionMedia, ReferenceAudio
from production.recorder.views import url_name

from . import services
from .iportal import ADAPTER_KEY, NATIVE_AIRING_CLOSED, native_airing_open
from .models import (
    TERMINAL_MESSAGE_STATES, UNFINISHED_EXECUTION_STATES, ActiveBroadcastExecution, ActiveBroadcastMessage,
    ActiveBroadcastMessageEvent, ExecutionState, MessageKind, MessageState,
)

M = MessageState
X = ExecutionState
FILTERS = {"current": "Current", "active": "Active", "review": "Review", "history": "History"}
PAGE_SIZE = 25
SEARCH_LIMIT = 100
ROW_EVENTS = 5
DETAIL_EVENTS = 200
RECENT_AIRINGS = 6
AIRINGS_PAGE = 25
# Newest first, deterministic on equal timestamps: the active_execution_recent
# index (created_at, id), scanned backwards.
EXECUTION_ORDER = ("-created_at", "-id")
SOON = timedelta(hours=1)
REASON_LIMIT = 200
EXECUTION_ATTENTION = (X.FAILED, X.INTERRUPTED)

EVENT_LABELS = {
    "prepared": "Draft started", "media_bound": "Audio saved", "activated": "Published", "superseded": "Replaced",
    "cleared": "Cleared", "discarded": "Discarded / declined", "expired": "Expired", "expiry_set": "Expiry changed",
    "execution_materialized": "Airing created (due)", "replay_requested": "Replay Now requested",
    "replay_coalesced": "Replay Now absorbed", "execution_reconciled": "Airing created (reconcile)",
    "reconciles": "Reconciles an airing", "reconciled": "Reconciled", "suppressed": "Suppressed",
    "mix_bound": "Aggregate ready", "render_retry": "Render retry", "failed": "Failed",
    "publication_retry": "Publication retry",
}
DETAIL_KEYS = ("reason", "expires_at", "previous", "supersedes", "superseded_by", "content_type", "duration_seconds",
               "code", "detail", "state", "outcome", "producer_recall", "self_published")


# -- access and view state ----------------------------------------------------------------------

def _require_control(user):
    if not services.can(user, services.CONTROL):
        raise PermissionDenied("The operator board needs active-message control.")


def _state(params) -> dict:
    """The board's filter / search / page, from a query (re-validated every time)."""
    view = params.get("filter") or "current"
    if view not in FILTERS:
        view = "current"
    q = (params.get("q") or "").strip()[:SEARCH_LIMIT]
    try:
        page = max(1, int(params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    return {"filter": view, "q": q, "page": page}


def _query(state, **extra) -> str:
    items = {}
    if state["filter"] != "current":
        items["filter"] = state["filter"]
    if state["q"]:
        items["q"] = state["q"]
    if state["page"] > 1:
        items["page"] = state["page"]
    items.update({key: value for key, value in extra.items() if value})
    return urlencode(items)


def _board_url(state, *, open_id=None) -> str:
    """Back to the operator's own view (filter, search, page), with the acted-on
    message expanded. The path is fixed: never a caller-supplied URL."""
    query = _query(state, open=str(open_id) if open_id else "")
    url = reverse("active_messages:board") + (f"?{query}" if query else "")
    return url + (f"#m-{open_id}" if open_id else "")


def _return_state(request) -> dict:
    """The board view a POST came from (its ``return`` field: a query string)."""
    return _state(QueryDict((request.POST.get("return") or "")[:400]))


def _message_or_404(message_id) -> ActiveBroadcastMessage:
    message = (ActiveBroadcastMessage.objects.select_related("content_type", "message_media")
               .filter(pk=message_id).first())
    if message is None:
        raise Http404("no such message")
    return message


def _name(message) -> str:
    return message.title or message.content_type.name


def _who(message) -> str:
    return f"“{_name(message)}” ({message.content_type.name}, {message.producer_username or message.producer_key})"


# -- read model (no locks) ------------------------------------------------------------------------

def _seconds(value) -> str:
    return "—" if value is None else f"{float(value):.1f} s"


def _audio(media, policy) -> dict:
    """What the operator needs to know about the bound audio (advisory: the
    services re-check everything under their locks)."""
    if media is None:
        return {"ok": False, "code": "missing", "label": "No audio", "preview": False}
    if media.retention_state == ProductionMedia.RETENTION_PURGED:
        return {"ok": False, "code": "purged", "label": "Audio purged", "preview": False}
    if media.validation_state != ProductionMedia.VALIDATION_VALID:
        return {"ok": False, "code": "invalid", "label": "Audio not valid", "preview": False}
    seconds = media.decoded_duration_seconds
    if media.airing_prohibited:
        return {"ok": False, "code": "test_media", "label": "TEST audio — can never air", "preview": True,
                "seconds": seconds}
    if seconds is None or seconds <= 0:
        return {"ok": False, "code": "duration_unknown", "label": "Duration unknown", "preview": True}
    if seconds > policy.max_message_seconds:
        return {"ok": False, "code": "too_long", "preview": True, "seconds": seconds,
                "label": f"{_seconds(seconds)} — over the {policy.max_message_seconds} s limit"}
    return {"ok": True, "code": "ok", "label": _seconds(seconds), "preview": True, "seconds": seconds}


def _expiry(message, now) -> dict:
    if message.expires_at is None:
        return {"set": False, "passed": False, "soon": False, "label": "No expiry"}
    local = timezone.localtime(message.expires_at)
    when = local.strftime("%Y-%m-%d %H:%M %Z")
    if message.state == M.EXPIRED:
        return {"set": True, "passed": True, "soon": False, "label": f"Expired at {when}"}
    passed = message.expires_at <= now
    if passed and message.state == M.ACTIVE:
        label = f"Expiry passed {when} — not yet expired by the sweep"
    elif passed:
        label = f"Expiry passed {when}"
    elif message.expires_at - now <= SOON:
        label = f"Expires in {max(1, int((message.expires_at - now).total_seconds() // 60))} min ({local:%H:%M %Z})"
    else:
        label = f"Expires {when}"
    return {"set": True, "passed": passed, "soon": not passed and message.expires_at - now <= SOON,
            "label": label, "local_input": local.strftime("%Y-%m-%dT%H:%M")}


def _type_problem(message) -> str:
    content_type = message.content_type
    if not content_type.enabled:
        return f"Content type {content_type.name} is disabled"
    if content_type.delivery_mode != message.kind:
        return f"Content type {content_type.name} is no longer a {message.get_kind_display().lower()} type"
    return ""


def _recent_events(ids, limit) -> dict:
    """The latest ``limit`` events of each message, in one bounded query."""
    if not ids:
        return {}
    rows = (ActiveBroadcastMessageEvent.objects.filter(message_id__in=ids)
            .annotate(rank=Window(RowNumber(), partition_by=[F("message_id")], order_by=F("id").desc()))
            .filter(rank__lte=limit).order_by("message_id", "-id"))
    grouped = {}
    for event in rows:
        grouped.setdefault(event.message_id, []).append(_event_row(event))
    return grouped


def _event_row(event) -> dict:
    detail = event.detail if isinstance(event.detail, dict) else {}
    facts = [(key, detail[key]) for key in DETAIL_KEYS if detail.get(key) not in (None, "")]
    return {"event": event, "label": EVENT_LABELS.get(event.event, event.event.replace("_", " ")),
            "actor": event.actor_username or event.get_actor_kind_display(), "facts": facts}


def _components(execution) -> set:
    if execution is None:
        return set()
    return {component.get("message") for component in (execution.composition_snapshot or {}).get("components", [])}


def _unfinished_execution():
    """The one unfinished airing, if any -- an indexed single-row lookup: the
    database allows at most one (open_slot = 1, unique, exactly the unfinished
    states)."""
    return ActiveBroadcastExecution.objects.filter(open_slot=1).first()


def _on_air(unfinished) -> dict:
    """THE board's interpretation of "on air", for the page and for every
    status refresh alike: only a STARTED airing (the engine's first real
    program buffer -- ACCEPTED and CLAIMED are not air), and exactly the
    messages frozen in ITS composition -- never the current active set."""
    if unfinished is None or unfinished.state != X.STARTED:
        return {"execution": None, "messages": []}
    return {"execution": str(unfinished.pk), "messages": sorted(_components(unfinished))}


def _airing_chip(airing) -> dict:
    """The strip's airing indicator (text and class), for the page and the refresh."""
    if airing is None:
        return {"text": "No airings yet", "class": ""}
    if airing["on_air"]:
        return {"text": "● ON AIR", "class": "onair"}
    return {"text": _airing_text(airing), "class": "alert" if airing["attention"] else ""}


def _execution_page(rows, before=None):
    """One newest-first page of airings, keyset-paginated on (created_at, id):
    the index range starts at the cursor (``before``, the last airing of the
    previous page), so a deep page costs what the first one does. Returns
    (airings, cursor for the next page or None)."""
    rows = rows.order_by(*EXECUTION_ORDER)
    if before is not None:
        rows = rows.filter(created_at__lte=before.created_at).exclude(created_at=before.created_at,
                                                                        id__gte=before.pk)
    found = list(rows[:AIRINGS_PAGE + 1])
    return found[:AIRINGS_PAGE], (found[AIRINGS_PAGE - 1] if len(found) > AIRINGS_PAGE else None)


def _cursor(params):
    try:
        return ActiveBroadcastExecution.objects.filter(pk=uuid.UUID(params.get("before") or "")).first()
    except ValueError:
        return None


# Airings whose FROZEN composition includes one exact message, newest first.
# The containment runs first, alone, in a MATERIALIZED CTE: the jsonb_path_ops
# GIN index (active_execution_members) finds exactly this message's airings,
# and only those are sorted and paged. Left to itself the planner may instead
# walk the newest-first index and test every airing's snapshot -- work that
# grows with the whole history. Keyset on (created_at, id), like every list.
MEMBER_SQL = """
    WITH members AS MATERIALIZED (
        SELECT id, created_at FROM {table} WHERE composition_snapshot @> %s::jsonb)
    SELECT airing.* FROM {table} AS airing JOIN members ON members.id = airing.id
    {after}
    ORDER BY members.created_at DESC, members.id DESC
    LIMIT %s"""


def _member_page(message, before=None):
    """One page of the airings this exact message was part of; returns
    (airings, cursor for the next page or None), like ``_execution_page``."""
    params = [json.dumps({"components": [{"message": str(message.pk)}]})]
    after = ""
    if before is not None:
        after = "WHERE (members.created_at, members.id) < (%s, %s)"
        params += [before.created_at, before.pk]
    sql = MEMBER_SQL.format(table=ActiveBroadcastExecution._meta.db_table, after=after)
    found = list(ActiveBroadcastExecution.objects.raw(sql, params + [AIRINGS_PAGE + 1]))
    return found[:AIRINGS_PAGE], (found[AIRINGS_PAGE - 1] if len(found) > AIRINGS_PAGE else None)


def _row(message, *, policy, now, events, active_by_slot, on_air_ids, gate_open) -> dict:
    audio = _audio(message.message_media, policy)
    row = {
        "message": message, "audio": audio, "expiry": _expiry(message, now),
        "events": events.get(message.pk, []), "on_air": str(message.pk) in on_air_ids,
        "detail_url": reverse("active_messages:board-message", args=[message.pk]),
        "recorder_url": reverse("active_messages:" + url_name(ADAPTER_KEY, "page")) + f"?message={message.pk}&from=board",
        "preview_url": (reverse("active_messages:" + url_name(ADAPTER_KEY, "media"),
                                kwargs={"media_id": message.message_media_id}) + f"?message={message.pk}"
                        if audio["preview"] else ""),
    }
    if message.state == M.PREPARED:
        problem = _type_problem(message)
        replaces = active_by_slot.get((message.producer_key, message.content_type_id))
        row.update(
            time_label="Created", time=message.created_at, problem=problem, replaces=replaces,
            revision=services.message_revision(message),
            needs_operator=not message.content_type.producer_self_publish,
            publishable=audio["ok"] and not problem and not row["expiry"]["passed"],
            blocked=(problem or ("" if audio["ok"] else audio["label"])
                     or ("Its expiry has passed" if row["expiry"]["passed"] else "")),
        )
    elif message.state == M.ACTIVE:
        row.update(time_label="Active since", time=message.activated_at,
                   next_due=message.next_due_at if gate_open else None)
    else:
        row.update(time_label=message.get_state_display(), time=message.terminal_at)
    return row


def _rows(messages, *, policy, now, on_air_ids, gate_open, event_limit=ROW_EVENTS) -> list:
    events = _recent_events([message.pk for message in messages], event_limit)
    active_by_slot = {}
    if any(message.state == M.PREPARED for message in messages):
        # The ACTIVE message a publication would replace: same producer, same EXACT type.
        for active in (ActiveBroadcastMessage.objects.select_related("content_type")
                       .filter(state=M.ACTIVE).order_by("activated_at")):
            active_by_slot[(active.producer_key, active.content_type_id)] = active
    return [_row(message, policy=policy, now=now, events=events, active_by_slot=active_by_slot,
                 on_air_ids=on_air_ids, gate_open=gate_open) for message in messages]


def _messages(state):
    rows = ActiveBroadcastMessage.objects.select_related("content_type", "message_media")
    if state["q"]:
        q = state["q"]
        match = (Q(title__icontains=q) | Q(producer_username__icontains=q) | Q(content_type__name__icontains=q)
                 | Q(content_type__key__icontains=q))
        try:
            match |= Q(pk=uuid.UUID(q))
        except ValueError:
            pass
        rows = rows.filter(match)
    urgent_first = Case(When(kind=MessageKind.URGENT, then=Value(0)), default=Value(1), output_field=IntegerField())
    view = state["filter"]
    if view == "active":                                    # composition order: urgent, oldest activation
        return rows.filter(state=M.ACTIVE).order_by(urgent_first, "activated_at", "id")
    if view == "review":                                    # drafts with audio first, then work in progress
        no_audio_last = Case(When(message_media__isnull=True, then=Value(1)), default=Value(0),
                             output_field=IntegerField())
        return rows.filter(state=M.PREPARED).order_by(no_audio_last, urgent_first, "created_at", "id")
    if view == "history":
        return rows.filter(state__in=TERMINAL_MESSAGE_STATES).order_by("-terminal_at", "-id")
    # current: the review queue (drafts with audio) first, then the active set
    review_first = Case(When(state=M.PREPARED, then=Value(0)), default=Value(1), output_field=IntegerField())
    return (rows.filter(Q(state=M.ACTIVE) | Q(state=M.PREPARED, message_media__isnull=False))
            .order_by(review_first, urgent_first, F("activated_at").asc(nulls_first=True), "created_at", "id"))


def _airing(execution) -> dict | None:
    if execution is None:
        return None
    return {"execution": execution, "on_air": execution.state == X.STARTED,
            "attention": execution.state in EXECUTION_ATTENTION or bool(execution.failure_code),
            "unfinished": execution.state in UNFINISHED_EXECUTION_STATES,
            "url": reverse("active_messages:board-airing", args=[execution.pk])}


def _status(policy) -> dict:
    # Only unfinished messages are counted, so the poll's work never grows with history.
    counts = ActiveBroadcastMessage.objects.filter(state__in=(M.ACTIVE, M.PREPARED)).aggregate(
        urgent=Count("id", filter=Q(state=M.ACTIVE, kind=MessageKind.URGENT)),
        public_address=Count("id", filter=Q(state=M.ACTIVE, kind=MessageKind.PUBLIC_ADDRESS)),
        review=Count("id", filter=Q(state=M.PREPARED, message_media__isnull=False)),
        drafts=Count("id", filter=Q(state=M.PREPARED)),
    )
    unfinished = _unfinished_execution()
    latest = unfinished or ActiveBroadcastExecution.objects.order_by(*EXECUTION_ORDER).first()
    # Advisory composition load, computed without the policy lock (and without
    # hashing the stinger): activation, materialization and Replay Now decide.
    active = list(ActiveBroadcastMessage.objects.filter(state=M.ACTIVE)
                  .values_list("kind", "message_media__decoded_duration_seconds"))
    total = sum((seconds or 0 for _kind, seconds in active), 0)
    stinger = None
    if policy.stinger_key and any(kind == MessageKind.URGENT for kind, _seconds in active):
        stinger = (ReferenceAudio.objects.filter(key=policy.stinger_key, retired_at__isnull=True)
                   .values_list("media__decoded_duration_seconds", flat=True).first())
    over = []
    if len(active) > policy.max_active_messages:
        over.append(f"{len(active)} active, limit {policy.max_active_messages}")
    if any((seconds or 0) > policy.max_message_seconds for _kind, seconds in active):
        over.append(f"a message is over the {policy.max_message_seconds} s limit")
    if total + (stinger or 0) > policy.max_aggregate_seconds:
        over.append(f"{float(total + (stinger or 0)):.0f} s, aggregate limit {policy.max_aggregate_seconds} s")
    last_event = ActiveBroadcastMessageEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
    return {
        **counts, "active": counts["urgent"] + counts["public_address"],
        "airing": _airing(latest), "on_air": _on_air(unfinished), "gate_open": native_airing_open(),
        "load": {"count": len(active), "max_count": policy.max_active_messages, "seconds": float(total),
                 "stinger_seconds": None if stinger is None else float(stinger),
                 "max_seconds": policy.max_aggregate_seconds, "over": over},
        "version": f"{policy.generation}.{last_event}",
    }


def _airing_text(airing) -> str:
    if airing is None:
        return "No airings yet"
    execution = airing["execution"]
    label = execution.get_state_display()
    return label if airing["unfinished"] else f"Last: {label}"


# -- pages --------------------------------------------------------------------------------------

@require_GET
def board(request):
    _require_control(request.user)
    state = _state(request.GET)
    now = timezone.now()
    policy = services.current_policy()
    status = _status(policy)
    page = Paginator(_messages(state), PAGE_SIZE).get_page(state["page"])
    state["page"] = page.number
    rows = _rows(list(page.object_list), policy=policy, now=now, on_air_ids=set(status["on_air"]["messages"]),
                 gate_open=status["gate_open"])
    try:
        open_id = str(uuid.UUID(request.GET.get("open", "")))
    except ValueError:
        open_id = ""
    airings = [_airing(execution) for execution in
               ActiveBroadcastExecution.objects.order_by(*EXECUTION_ORDER)[:RECENT_AIRINGS]]
    return render(request, "active_messages/board.html", {
        "state": state, "filters": [{"key": key, "label": label, "url": _board_url({**state, "filter": key,
                                                                                       "page": 1})}
                                    for key, label in FILTERS.items()],
        "page": page, "rows": rows, "open_id": open_id, "status": status, "chip": _airing_chip(status["airing"]),
        "airing_text": _airing_text(status["airing"]),
        "airings": airings, "policy": policy, "gate_message": NATIVE_AIRING_CLOSED,
        "return_query": _query(state), "replay_token": uuid.uuid4(),
        "page_urls": {"previous": _board_url({**state, "page": page.number - 1}) if page.has_previous() else "",
                      "next": _board_url({**state, "page": page.number + 1}) if page.has_next() else ""},
        "station_tz": timezone.localtime(now).strftime("%Z"),
    })


@require_GET
def board_status(request):
    """The status strip, for the page's bounded, unobtrusive refresh. Display
    only: it never changes anything and is never authority."""
    _require_control(request.user)
    status = _status(services.current_policy())
    airing = status["airing"]
    latest = airing["execution"] if airing else None
    return JsonResponse({
        "urgent": status["urgent"], "public_address": status["public_address"], "review": status["review"],
        "gate_open": status["gate_open"], "chip": _airing_chip(airing), "on_air": status["on_air"],
        "latest": None if latest is None else {"execution": str(latest.pk), "state": latest.state,
                                               "label": latest.get_state_display()},
        "over": status["load"]["over"], "version": status["version"],
    }, headers={"Cache-Control": "no-store"})


@require_GET
def message_detail(request, message_id):
    _require_control(request.user)
    message = _message_or_404(message_id)
    state = _state(request.GET)
    now = timezone.now()
    policy = services.current_policy()
    gate_open = native_airing_open()
    status = _status(policy)
    on_air = status["on_air"]
    row = _rows([message], policy=policy, now=now, on_air_ids=set(on_air["messages"]), gate_open=gate_open,
                event_limit=1)[0]
    events = [_event_row(event) for event in
              ActiveBroadcastMessageEvent.objects.filter(message=message).order_by("-id")[:DETAIL_EVENTS]]
    found, more = _member_page(message, _cursor(request.GET))
    airings = [_airing(execution) for execution in found]
    successor = message.superseded_by
    predecessor = ActiveBroadcastMessage.objects.filter(superseded_by=message).first()
    return render(request, "active_messages/board_message.html", {
        "row": row, "message": message, "events": events, "events_capped": len(events) >= DETAIL_EVENTS,
        "airings": airings, "successor": successor, "predecessor": predecessor, "state": state,
        "return_query": _query(state), "back_url": _board_url(state, open_id=message.pk), "policy": policy,
        "gate_open": gate_open, "gate_message": NATIVE_AIRING_CLOSED,
        "station_tz": timezone.localtime(now).strftime("%Z"), "on_air": on_air,
        "older_url": (reverse("active_messages:board-message", args=[message.pk]) + "?"
                      + _query(state, before=str(more.pk))) if more else "",
        "status_version": status["version"],
    })


@require_GET
def airings(request):
    """Every airing, newest first, keyset-paginated (bounded work per page)."""
    _require_control(request.user)
    state = _state(request.GET)
    found, more = _execution_page(ActiveBroadcastExecution.objects.all(), _cursor(request.GET))
    status = _status(services.current_policy())
    return render(request, "active_messages/board_airings.html", {
        "airings": [_airing(execution) for execution in found], "back_url": _board_url(state),
        "older_url": (reverse("active_messages:board-airings") + "?" + _query(state, before=str(more.pk)))
                     if more else "",
        "first_url": reverse("active_messages:board-airings") + (f"?{_query(state)}" if _query(state) else ""),
        "paged": bool(request.GET.get("before")), "gate_open": status["gate_open"],
        "status_version": status["version"],
    })


@require_GET
def airing_detail(request, execution_id):
    _require_control(request.user)
    execution = ActiveBroadcastExecution.objects.filter(pk=execution_id).first()
    if execution is None:
        raise Http404("no such airing")
    state = _state(request.GET)
    snapshot = execution.composition_snapshot or {}
    ids = [component.get("message") for component in snapshot.get("components", [])]
    titles = {str(pk): title or name for pk, title, name in ActiveBroadcastMessage.objects.filter(pk__in=ids)
              .values_list("id", "title", "content_type__name")}
    components = [{**component, "title": titles.get(component.get("message"), ""),
                   "url": reverse("active_messages:board-message", args=[component["message"]])}
                  for component in snapshot.get("components", [])]
    events = [_event_row(event) for event in
              ActiveBroadcastMessageEvent.objects.filter(execution=execution).order_by("-id")[:DETAIL_EVENTS]]
    return render(request, "active_messages/board_airing.html", {
        "airing": _airing(execution), "execution": execution, "components": components,
        "stinger": snapshot.get("stinger"), "stinger_unavailable": snapshot.get("stinger_unavailable"),
        "accounted_seconds": snapshot.get("accounted_seconds"), "events": events,
        "events_capped": len(events) >= DETAIL_EVENTS, "back_url": _board_url(state),
        "gate_open": native_airing_open(), "gate_message": NATIVE_AIRING_CLOSED,
        "status_version": _status(services.current_policy())["version"],
    })


# -- actions (POST; each one exact UUID) --------------------------------------------------------

@require_POST
def publish(request, message_id):
    """Publish the exact reviewed draft (``revision``) -- or report why not."""
    _require_control(request.user)
    state = _return_state(request)
    message = _message_or_404(message_id)
    revision = (request.POST.get("revision") or "").strip()
    if not revision:
        flash.error(request, f"Not published: {_who(message)} — review it on the board first.")
        return redirect(_board_url(state, open_id=message.pk))
    started = timezone.now()
    try:
        row = services.activate_message(message.pk, actor=request.user, expected_revision=revision)
    except services.RevisionConflict:
        flash.error(request, f"Not published: {_who(message)} changed since you reviewed it (new audio was saved). "
                             f"Listen again, then publish.")
        return redirect(_board_url(state, open_id=message.pk))
    except services.ActiveMessageError as exc:
        flash.error(request, f"Not published: {_who(message)} — {exc}")
        return redirect(_board_url(state, open_id=message.pk))
    if row.activated_at is None or row.activated_at < started:
        flash.info(request, f"Already active: {_who(message)} (published by {row.activated_by_username or 'someone'}).")
        return redirect(_board_url(state, open_id=message.pk))
    replaced = ActiveBroadcastMessage.objects.filter(superseded_by=row).first()
    text = f"Published: {_who(message)}."
    if replaced is not None:
        text += f" It replaces “{_name(replaced)}”, which is cleared."
    if not native_airing_open():
        text += " It is active, but it does not air: native airing is closed."
    flash.success(request, text)
    return redirect(_board_url(state, open_id=row.pk))


ACTIONS = {
    "clear": {"state": M.ACTIVE, "verb": "Clear", "done": "Cleared", "default_reason": "cleared by operator",
              "explain": "It will not be part of any future airing. An airing already on air finishes; it is never "
                         "cut."},
    "decline": {"state": M.PREPARED, "verb": "Decline", "done": "Declined", "default_reason": "declined by operator",
                "explain": "The draft is closed and can never be published. Its producer can prepare a new one; "
                           "its saved audio stays in the station's media history."},
}


def _lifecycle(request, message_id, action):
    _require_control(request.user)
    spec = ACTIONS[action]
    message = _message_or_404(message_id)
    if request.method == "GET":
        state = _state(request.GET)
        if message.state != spec["state"]:
            flash.info(request, f"{_who(message)} is {message.get_state_display().lower()} now; nothing to "
                                f"{spec['verb'].lower()}.")
            return redirect(_board_url(state, open_id=message.pk))
        return render(request, "active_messages/board_confirm.html", {
            "message": message, "action": action, **spec, "return_query": _query(state),
            "cancel_url": _board_url(state, open_id=message.pk), "reason_limit": REASON_LIMIT,
            "post_url": reverse(f"active_messages:board-{action}", args=[message.pk]),
        })
    state = _return_state(request)
    reason = (request.POST.get("reason") or "").strip()[:REASON_LIMIT] or spec["default_reason"]
    started = timezone.now()
    try:
        row = services.clear_message(message.pk, actor=request.user, reason=reason, expected_state=spec["state"])
    except services.ActiveMessageError as exc:
        flash.error(request, f"Not {spec['done'].lower()}: {_who(message)} — {exc}")
        return redirect(_board_url(state, open_id=message.pk))
    if row.terminal_at is None or row.terminal_at < started:            # idempotent: it was already final
        flash.info(request, f"Already {row.get_state_display().lower()}: {_who(message)}.")
    else:
        flash.success(request, f"{spec['done']}: {_who(message)}.")
    return redirect(_board_url(state, open_id=message.pk))


@require_http_methods(["GET", "POST"])
def clear(request, message_id):
    return _lifecycle(request, message_id, "clear")


@require_http_methods(["GET", "POST"])
def decline(request, message_id):
    return _lifecycle(request, message_id, "decline")


class _BadTime(ValueError):
    pass


def _local_time(value: str):
    """A station-local ``datetime-local`` value -> an aware datetime. A time the
    station clock skips (spring forward) or repeats (fall back) is refused,
    never guessed."""
    value = (value or "").strip()
    for pattern in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            naive = datetime.strptime(value, pattern)
            break
        except ValueError:
            continue
    else:
        raise _BadTime("Enter a date and time.")
    zone = timezone.get_current_timezone()
    first, second = naive.replace(tzinfo=zone, fold=0), naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        if first.astimezone(dt_timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
            raise _BadTime(f"{value.replace('T', ' ')} does not exist in station time (the clocks skip it).")
        raise _BadTime(f"{value.replace('T', ' ')} happens twice in station time (the clocks fall back); "
                       f"choose another time.")
    return first


@require_POST
def set_expiry(request, message_id):
    _require_control(request.user)
    state = _return_state(request)
    message = _message_or_404(message_id)
    shown = (request.POST.get("expected") or "").strip()
    expected = None
    if shown:
        expected = parse_datetime(shown)
        if expected is None or timezone.is_naive(expected):
            flash.error(request, "The expiry form is malformed; reload the board.")
            return redirect(_board_url(state, open_id=message.pk))
    try:
        expires_at = None if request.POST.get("remove") else _local_time(request.POST.get("expires_at"))
        row = services.set_message_expiry(message.pk, expires_at, actor=request.user, expected_expires_at=expected)
    except _BadTime as exc:
        flash.error(request, f"Expiry not changed: {exc}")
    except services.ActiveMessageError as exc:
        text = ("someone changed it since this page was loaded; check the new expiry"
                if exc.code == "expiry_changed" else str(exc))
        flash.error(request, f"Expiry not changed for {_who(message)}: {text}.")
    else:
        when = (timezone.localtime(row.expires_at).strftime("%Y-%m-%d %H:%M %Z") if row.expires_at else "none")
        flash.success(request, f"Expiry for {_who(message)}: {when}.")
    return redirect(_board_url(state, open_id=message.pk))


@require_POST
def replay(request):
    """Replay Now, idempotent on the page's request token. Refused here, before
    the service, while native airing is closed: a dormant execution would be
    nothing but a misleading record."""
    _require_control(request.user)
    state = _return_state(request)
    if not native_airing_open():
        flash.error(request, f"Replay Now is unavailable: {NATIVE_AIRING_CLOSED} Nothing was requested.")
        return redirect(_board_url(state))
    try:
        token = uuid.UUID(request.POST.get("request_token") or "")
    except ValueError:
        flash.error(request, "Replay Now: the request is malformed; reload the board.")
        return redirect(_board_url(state))
    try:
        # The outcome is decided by the service, inside its serialized transaction.
        execution, outcome = services.request_manual_replay_outcome(actor=request.user, request_token=token)
    except services.ActiveMessageError as exc:
        flash.error(request, f"Replay Now refused: {exc}.")
        return redirect(_board_url(state))
    short, where = str(execution.pk)[:8], execution.get_state_display().lower()
    if outcome == services.REPLAY_REPEATED:
        flash.info(request, f"Replay Now was already requested from this page: airing {short} ({where}). "
                            f"Nothing new was requested.")
    elif outcome == services.REPLAY_COALESCED:
        flash.success(request, f"Replay Now absorbed by the airing already in progress: {short} ({where}).")
    else:
        flash.success(request, f"Replay Now requested: new airing {short} ({where}). It is not on air until the "
                               f"engine starts it.")
    return redirect(_board_url(state))
