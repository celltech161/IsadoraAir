"""Read-only observation of spoken library projections (no locks, no writes).

Repairs belong to ``spoken.delivery.reconcile`` (``manage.py spoken_projection
reconcile``); this only reports what reconcile would act on, for the admin
and ``manage.py spoken_projection status``.
"""
from __future__ import annotations

from datetime import timedelta

from django.db.models import Count
from django.utils import timezone

from . import delivery, library_files
from .models import AssetState, DeliveryMode, EditionState, ItemState, SpokenAsset, SpokenAssetEdition, SpokenItem


def asset_file_status(asset) -> str:
    """'ok' | 'missing' | 'wrong_bytes' | 'unsafe' | 'absent' (nothing expected) | 'unexpected'."""
    expected = delivery._expected_file(asset)
    if expected is None:
        return "unexpected" if library_files.exists(asset.library_relpath) else "absent"
    sha, _size = library_files.current(expected[0])
    if sha is None:
        return "missing"
    if sha == "unsafe":
        return "unsafe"
    return "ok" if sha == expected[1].sha256 else "wrong_bytes"


def observe() -> dict:
    issues = []
    now = timezone.now()
    counts = dict(SpokenAsset.objects.values_list("state").annotate(n=Count("pk")).values_list("state", "n"))
    for asset in SpokenAsset.objects.exclude(state=AssetState.RETIRED).select_related("track"):
        status = asset_file_status(asset)
        if status not in ("ok", "absent"):
            issues.append({"asset": str(asset.pk), "issue": f"file_{status}"})
        if asset.state == AssetState.LIVE and asset.track_id is None:
            issues.append({"asset": str(asset.pk), "issue": "live_without_track"})
        if asset.state != AssetState.LIVE and asset.track is not None and asset.track.ready2air:
            issues.append({"asset": str(asset.pk), "issue": "track_ready_without_content"})
    doubled = (SpokenAssetEdition.objects.filter(state=EditionState.ACTIVE).values("asset")
               .annotate(n=Count("pk")).filter(n__gt=1))
    for row in doubled:                                      # impossible: a database constraint forbids it
        issues.append({"asset": str(row["asset"]), "issue": "multiple_active_editions"})
    stale = SpokenAssetEdition.objects.filter(
        state=EditionState.PENDING, created_at__lte=now - timedelta(seconds=delivery.STALE_PENDING_SECONDS))
    for edition in stale:
        issues.append({"asset": str(edition.asset_id), "issue": "stale_pending_edition", "edition": str(edition.pk)})
    projected_modes = (DeliveryMode.CURRENT_EDITION, DeliveryMode.ACCUMULATING)
    for item in SpokenItem.objects.filter(state=ItemState.DELIVERED, content_type__delivery_mode__in=projected_modes):
        if not SpokenAsset.objects.filter(current_item=item, state__in=(AssetState.LIVE, AssetState.DETACHED)).exists():
            issues.append({"item": str(item.pk), "issue": "delivered_without_projection"})
    return {"healthy": not issues, "assets": counts, "detached_ignored": counts.get(AssetState.DETACHED, 0),
            "issues": issues}
