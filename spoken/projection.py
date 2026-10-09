"""Pure projection planning -- what C2 will write to the library, computed
without touching it.

C1 deliberately creates, changes and deletes NO library Track and writes NO
library file: these helpers only derive the deterministic identity and
metadata a projection would use. Identity never comes from a submitted
filename: the library path is ``<category code>/spoken-<asset uuid>.flac``.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from django.utils import timezone

from .models import LIBRARY_RELPATH_PATTERN, AssetMode, DeliveryMode, TitlePolicy

_CATEGORY_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,19}$")
_RELPATH_RE = re.compile(LIBRARY_RELPATH_PATTERN)


class ProjectionPlanError(ValueError):
    pass


def library_relpath(category_code: str, asset_id) -> str:
    """``<category>/spoken-<uuid>.flac`` -- never derived from user input."""
    if not isinstance(category_code, str) or not _CATEGORY_CODE_RE.fullmatch(category_code):
        raise ProjectionPlanError(f"category code {category_code!r} cannot form a library path")
    relpath = f"{category_code}/spoken-{uuid.UUID(str(asset_id)).hex}.flac"
    assert _RELPATH_RE.fullmatch(relpath)
    return relpath


def render_title(policy: str, *, item_title: str, submitter: str, when) -> str:
    """A fixed-enum title policy -- never a format string."""
    local = timezone.localtime(when) if timezone.is_aware(when) else when
    date = local.strftime("%m/%d/%Y")
    item_title = (item_title or "").strip()
    if policy == TitlePolicy.SUBMITTED_DATE:
        title = date
    elif policy == TitlePolicy.ITEM_TITLE:
        title = item_title or date
    elif policy == TitlePolicy.ITEM_TITLE_AND_DATE:
        title = f"{item_title} - {date}" if item_title else date
    elif policy == TitlePolicy.SUBMITTER_AND_DATE:
        title = f"{submitter or 'unknown'} - {date}"
    else:
        raise ProjectionPlanError(f"unknown title policy {policy!r}")
    return title[:200]


@dataclass(frozen=True)
class ProjectionPlan:
    mode: str
    slot_key: str
    category_id: int
    category_code: str
    title: str
    artist: str
    ready2air: bool


def plan_projection(item, *, slot_key: str = "") -> ProjectionPlan:
    """How ``item`` WOULD project under its type's policy (no writes)."""
    ctype = item.content_type
    if ctype.delivery_mode == DeliveryMode.RETAIN_ONLY:
        raise ProjectionPlanError("retain-only content is never projected to the library")
    if ctype.target_category_id is None:
        raise ProjectionPlanError("the content type has no target category")
    mode = AssetMode.CURRENT_EDITION if ctype.delivery_mode == DeliveryMode.CURRENT_EDITION else AssetMode.ACCUMULATING
    if mode == AssetMode.ACCUMULATING and slot_key:
        raise ProjectionPlanError("accumulating assets have no slot key")
    when = item.submitted_at or item.created_at
    return ProjectionPlan(
        mode=mode, slot_key=slot_key, category_id=ctype.target_category_id,
        category_code=ctype.target_category.code,
        title=render_title(ctype.title_policy, item_title=item.title, submitter=item.submitted_by_username,
                           when=when),
        artist=ctype.artist_label[:200], ready2air=bool(ctype.deliver_ready_to_air),
    )
