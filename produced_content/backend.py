"""THE bridge from a regular ProducedContentType to its Phase-C backend (D3C).

A regular produced-content type is processed and delivered by the accepted
Phase-C ``spoken`` domain -- its validation, leased render worker, the five
code-backed recipes, lifecycle/history and library projection -- through ONE
explicitly linked ``spoken.SpokenContentType``:

    ProducedContentType (station-facing configuration authority)
        --spoken_content_type (OneToOne, PROTECT)--> SpokenContentType (managed backend)

There is one source of configuration truth: the ProducedContentType. The
backing row is created here and its managed fields are re-derived from the
produced type on every synchronization (``MANAGED_FIELDS``); Django Admin shows
a linked backing row read-only. Backend identity is a generated, durable
``produced-<uuid hex>`` code -- never the producer-facing key, and ownership
is never inferred from names: only the explicit link counts.

``ensure_regular_backend`` is the only writer of the link. It locks the exact
produced type first (lock order: ProducedContentType -> SpokenContentType ->
SpokenItem -> ProductionMedia, i.e. this row ahead of Phase C's own order),
so concurrent first synchronizations create exactly one backing row. Every
regular producer operation goes through it, so production always runs on a
re-proven configuration.

A regular type is RUNNABLE only with one of the five D3C recipes (registered in
production's render registry); a blank or unknown recipe fails closed with a
configuration error. Nothing here executes a path, command, import or script:
the recipe key is a lookup in a fixed set.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from .models import DeliveryMode, OutputMode, ProducedContentType

# The five accepted Phase-C recipes (spoken.recipes) -- reused, never reimplemented.
D3C_RECIPES = ("cal_events", "usd239min", "news_item", "3min_ctr", "lcl_psa")
BACKEND_CODE_PREFIX = "produced-"
# The backing SpokenContentType fields the produced type owns (see managed_values).
MANAGED_FIELDS = ("enabled", "name", "description", "delivery_mode", "recipe_key", "recipe_params", "target_category_id",
                  "artist_label", "requires_review", "deliver_ready_to_air", "on_current_recall", "recall_policy",
                  "title_policy", "default_lifetime")


class BackendError(Exception):
    code = "backend_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotRegular(BackendError):
    code = "not_regular"


class NotRunnable(BackendError):
    """The regular type's configuration cannot be produced (fail closed)."""
    code = "not_runnable"


def runnable_problem(content_type) -> str:
    """Why this REGULAR type cannot produce now ("" when it can). Configuration
    only -- ``enabled`` and authorization are the caller's concern."""
    from production.services import render
    if content_type.delivery_mode != DeliveryMode.REGULAR:
        return "only a regular type is processed into library content"
    if content_type.target_category_id is None:
        return "no target library category is configured"
    if content_type.output_mode not in OutputMode.values:
        return "no output mode (single or accumulating) is configured"
    recipe = content_type.recipe_key
    if not recipe:
        return f"no processing recipe is configured (one of: {', '.join(D3C_RECIPES)})"
    if recipe not in D3C_RECIPES:
        return f"{recipe!r} is not a D3C processing recipe (one of: {', '.join(D3C_RECIPES)})"
    try:
        render.get(recipe)
    except render.UnknownRecipe:
        return f"the {recipe!r} recipe is not registered on this installation"
    return ""


def managed_values(content_type) -> dict:
    """The exact backing configuration a regular produced type implies."""
    from spoken.models import CurrentRecallPolicy, RecallPolicy, TitlePolicy
    from spoken.models import DeliveryMode as SpokenDeliveryMode
    mode = {OutputMode.SINGLE: SpokenDeliveryMode.CURRENT_EDITION,
            OutputMode.ACCUMULATING: SpokenDeliveryMode.ACCUMULATING}[content_type.output_mode]
    return {
        "enabled": bool(content_type.enabled),
        "name": content_type.name,
        "description": content_type.description,
        "delivery_mode": mode,
        "recipe_key": content_type.recipe_key,
        "recipe_params": {},                         # the registered recipe's own defaults
        "target_category_id": content_type.target_category_id,
        "artist_label": (content_type.artist_tag or content_type.name)[:200],
        "requires_review": False,
        "deliver_ready_to_air": True,
        "on_current_recall": CurrentRecallPolicy.RETIRE_ASSET,
        "recall_policy": RecallPolicy.OWNER_ANYTIME,
        "title_policy": TitlePolicy.ITEM_TITLE,
        "default_lifetime": None,
    }


def drift(content_type, backing) -> list[str]:
    """The managed fields where ``backing`` differs from ``content_type``."""
    if content_type.delivery_mode != DeliveryMode.REGULAR or runnable_problem(content_type):
        return []
    return sorted(name for name, value in managed_values(content_type).items() if getattr(backing, name) != value)


def _new_code() -> str:
    return f"{BACKEND_CODE_PREFIX}{uuid.uuid4().hex}"


def _config_error(exc: ValidationError) -> NotRunnable:
    return NotRunnable(f"the spoken backend refused this configuration: {'; '.join(exc.messages)}",
                       code=getattr(exc, "code", None) or "backend_invalid")


def ensure_regular_backend(content_type):
    """Lock the exact produced type, require it regular and runnable, create
    or synchronize its backing SpokenContentType, persist the explicit link
    and return the backing row (locked until the caller's transaction ends,
    when called inside one)."""
    from spoken.models import SpokenContentType
    with transaction.atomic():
        row = (ProducedContentType.objects.select_for_update(of=("self",))
               .filter(pk=getattr(content_type, "pk", content_type)).first())
        if row is None:
            raise NotRegular("no such produced content type", code="unknown_type")
        if row.delivery_mode != DeliveryMode.REGULAR:
            raise NotRegular(f"{row.key} is not a regular content type")
        problem = runnable_problem(row)
        if problem:
            raise NotRunnable(f"{row.name} cannot be produced: {problem}")
        values = managed_values(row)
        try:
            if row.spoken_content_type_id is None:
                backing = SpokenContentType(code=_new_code(), **values)
                backing.save()
                ProducedContentType.objects.filter(pk=row.pk).update(spoken_content_type=backing)
            else:
                backing = SpokenContentType.objects.select_for_update().get(pk=row.spoken_content_type_id)
                changed = [name for name, value in values.items() if getattr(backing, name) != value]
                if changed:
                    for name in changed:
                        setattr(backing, name, values[name])
                    backing.save()                   # Phase C's own validated, row-locked configuration save
        except ValidationError as exc:
            raise _config_error(exc) from exc
        return SpokenContentType.objects.get(pk=backing.pk)


@dataclass(frozen=True)
class Health:
    ok: bool
    text: str


def health(content_type) -> Health:
    """A read-only verdict for Admin and the producer page."""
    if content_type.delivery_mode != DeliveryMode.REGULAR:
        return Health(True, "not a regular type (no spoken backend)")
    problem = runnable_problem(content_type)
    if problem:
        return Health(False, f"not runnable: {problem}")
    backing = content_type.spoken_content_type
    if backing is None:
        return Health(True, "runnable; the backend is created on the first submission")
    changed = drift(content_type, backing)
    if changed:
        return Health(False, f"backend out of date ({', '.join(changed)}); synchronized on the next submission "
                             f"or save")
    return Health(True, f"healthy (backend {backing.code})")


def synchronize(content_type) -> Health:
    """After a station edit (Admin save): bring an existing or creatable
    backend in line. A type that is not runnable keeps NO enabled backend:
    an existing backing row is disabled (fail closed) and nothing is
    created. Never raises for a configuration problem -- the verdict says it."""
    from spoken.models import SpokenContentType
    row = ProducedContentType.objects.filter(pk=getattr(content_type, "pk", content_type)).first()
    if row is None or row.delivery_mode != DeliveryMode.REGULAR:
        return Health(True, "not a regular type (no spoken backend)")
    if runnable_problem(row):
        if row.spoken_content_type_id is not None:
            with transaction.atomic():
                ProducedContentType.objects.select_for_update(of=("self",)).filter(pk=row.pk).first()
                backing = SpokenContentType.objects.select_for_update().get(pk=row.spoken_content_type_id)
                if backing.enabled:
                    backing.enabled = False
                    backing.save()
        return health(row)
    try:
        ensure_regular_backend(row)
    except BackendError as exc:
        return Health(False, f"not runnable: {exc}")
    return health(ProducedContentType.objects.get(pk=row.pk))
