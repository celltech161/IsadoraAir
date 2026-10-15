"""The producer's "My Content" workspace (iPortal Phase D3C).

``/iportal/content/`` -- the producer's enabled REGULAR and TEST / NO-AIR
produced-content types (all enabled ones for control; urgent and public
address stay on My Messages):

* regular  target category, single/accumulating, recipe, a configuration problem;
           the station's current edition (single); the producer's own recent
           items (station control: every producer's) with their exact Phase-C
           state; start a submission; continue a
           draft in the shared recorder; abandon / withdraw / recall one exact
           item of their own;
* test     the producer's saved test take (kept seven days after the most
           recent save) and the TEST / NO-AIR recorder. Nothing there airs.

Every change is a CSRF-protected POST naming one exact type key or one exact
item UUID, and goes through produced_content.regular, which authorizes again:
a shown or hidden button is never the authorization. Another producer's item
is a 404 here -- except for station control, which may withdraw/recall any
producer's item of a managed type (never a standalone Phase-C one).
"""
from __future__ import annotations

from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from production.recorder.views import url_name
from spoken.models import AssetMode, AssetState, ItemState, SpokenAsset, SpokenItem

from . import authority, backend, no_air, regular
from .iportal import ADAPTER_KEY, NOTHING_AIRS
from .models import TEST_TAKE_RETENTION_DAYS, DeliveryMode, OutputMode

S = ItemState
RECENT_ITEMS = 15
PAST = {"Abandon": "Abandoned", "Withdraw": "Withdrawn", "Recall": "Recalled"}


def _require_workspace(user):
    if not authority.has_workspace(user):
        raise PermissionDenied("Produced content is not available to your account.")


def recorder_url(**subject) -> str:
    page = reverse("produced_content:" + url_name(ADAPTER_KEY, "page"))
    key, value = next(iter(subject.items()))
    return f"{page}?{key}={value}"


def _withdraw_verb(item) -> str:
    if item.state == S.DRAFT:
        return "Abandon"
    if item.state in (S.DELIVERED, S.SUPERSEDED):
        return "Recall"
    return "Withdraw"


def _item_row(user, item, *, writable, current_item_id=None):
    mine = regular.is_owner(user, item)
    return {
        "item": item, "recorder_url": recorder_url(item=item.pk), "mine": mine,
        "can_continue": item.state == S.DRAFT and writable and mine,
        "can_withdraw": item.state not in regular.NOT_WITHDRAWABLE,
        "withdraw_verb": _withdraw_verb(item),
        "is_current": current_item_id is not None and item.pk == current_item_id,
    }


def _current_edition(backing):
    """The live current-edition item of a single type's stable asset (or None)."""
    asset = (SpokenAsset.objects.select_related("current_item")
             .filter(content_type=backing, mode=AssetMode.CURRENT_EDITION, state=AssetState.LIVE).first())
    return asset.current_item if asset is not None else None


@login_required
@require_GET
def my_content(request):
    user = request.user
    _require_workspace(user)
    control = authority.can(user, authority.CONTROL)
    # Station control (CONTROL / spoken manager) sees and can withdraw every
    # producer's items of the MANAGED types; a producer only their own.
    station = authority.is_station_control(user)
    scope = {} if station else {"submitted_by": user}
    regular_rows, test_rows, listed = [], [], set()
    for content_type in authority.content_types_for(user).select_related("target_category",
                                                                           "spoken_content_type"):
        if content_type.delivery_mode == DeliveryMode.TEST:
            take = no_air.current_take(user, content_type)
            test_rows.append({"type": content_type, "take": take,
                              "recorder_url": recorder_url(test_type=content_type.key)})
            continue
        # Only a configuration problem blocks a producer: a backend that merely
        # drifted is re-synchronized by the next submission (backend.ensure_regular_backend).
        problem = backend.runnable_problem(content_type)
        backing = content_type.spoken_content_type
        writable = not problem
        mine, current = [], None
        if backing is not None:
            listed.add(backing.pk)
            current = (_current_edition(backing) if content_type.output_mode == OutputMode.SINGLE else None)
            mine = [_item_row(user, item, writable=writable, current_item_id=getattr(current, "pk", None))
                    for item in SpokenItem.objects.select_related("submission_media")
                    .filter(content_type=backing, **scope).order_by("-created_at")[:RECENT_ITEMS]]
        regular_rows.append({
            "type": content_type, "problem": problem, "can_start": not problem,
            "items": mine, "single": content_type.output_mode == OutputMode.SINGLE,
            "current": current, "current_is_mine": current is not None and current.submitted_by_id == user.pk,
        })
    # Unfinished items of managed types no longer offered here (assignment
    # removed, type disabled) -- the producer's own, or for station control
    # everyone's: visible, so they can still be withdrawn.
    others = [_item_row(user, item, writable=False)
              for item in SpokenItem.objects.select_related("content_type")
              .filter(content_type__produced_content_type__isnull=False, **scope)
              .exclude(content_type_id__in=listed).exclude(state__in=regular.NOT_WITHDRAWABLE)
              .order_by("-created_at")[:RECENT_ITEMS]]
    for row in others:
        row["type_name"] = row["item"].content_type.produced_content_type.name
    return render(request, "produced_content/my_content.html", {
        "regular_rows": regular_rows, "test_rows": test_rows, "others": others, "control": control,
        "station": station,
        "nothing_airs": NOTHING_AIRS, "retention_days": TEST_TAKE_RETENTION_DAYS,
    })


def _back(request, text, level="error"):
    getattr(flash, level)(request, text)
    return redirect("produced_content:my-content")


@login_required
@require_POST
def start(request):
    """One new DRAFT of one exact regular type; then the shared recorder opens it."""
    _require_workspace(request.user)
    try:
        item = regular.start(request.user, request.POST.get("content_type", ""), title=request.POST.get("title", ""))
    except regular.RegularError as exc:
        return _back(request, str(exc))
    return redirect(recorder_url(item=item.pk))


@login_required
@require_http_methods(["GET", "POST"])
def withdraw(request, item_id):
    """Abandon a draft, withdraw a submission or recall delivered content --
    one exact item of the producer's own, after a confirmation."""
    _require_workspace(request.user)
    found = regular.managed_item(item_id)
    if found is None or not regular.may_see(request.user, found[0]):
        raise Http404("no such item")
    item, produced = found
    verb = _withdraw_verb(item)
    if request.method == "GET":
        if item.state in regular.NOT_WITHDRAWABLE:
            return _back(request, f"That item is {item.get_state_display().lower()} now.", "info")
        return render(request, "produced_content/confirm.html", {"item": item, "produced": produced, "verb": verb})
    try:
        regular.withdraw(request.user, item.pk, reason=(f"{verb.lower()} by its producer" if regular.is_owner(request.user, item)
                                            else f"{verb.lower()} by station control"),
                         expected_state=request.POST.get("expected_state") or None)
    except regular.NotFound:
        raise Http404("no such item") from None
    except regular.RegularError as exc:
        return _back(request, str(exc))
    return _back(request, f"{PAST[verb]}: {produced.name} — {item.title or 'untitled'}.", "success")
