from django.db import migrations


def _extract_cause_categories(raw_payload):
    """Duplicated (not imported) from road_conditions.services.
    extract_cause_categories() -- migrations must not import live app
    code, which can change shape later and break this migration when
    run from zero on a fresh install. Same logic, frozen here; see
    that function's own docstring for the full rationale."""
    if not isinstance(raw_payload, dict):
        return []
    categories = set()
    for detail in raw_payload.get("details") or []:
        if not isinstance(detail, dict):
            continue
        descriptions = detail.get("descriptions")
        if not isinstance(descriptions, list):
            continue
        for desc in descriptions:
            if not isinstance(desc, dict):
                continue
            if desc.get("description-type") != "PhraseDescription":
                continue
            if not desc.get("is-cause"):
                continue
            kind = desc.get("kind")
            if isinstance(kind, dict):
                category = kind.get("category")
                if isinstance(category, str) and category:
                    categories.add(category)
    return sorted(categories)


def backfill(apps, schema_editor):
    """One-time backfill for rows synced before cause_categories
    existed. sync_events() only re-normalizes an existing row when its
    payload_checksum changes (see services.sync_events) -- without
    this backfill, a long-lived, unchanged event (e.g. a months-long
    construction advisory KDOT hasn't touched since before this
    field existed) would keep an empty cause_categories indefinitely,
    silently defeating the whole point of this field for exactly the
    events it matters most for (this migration exists BECAUSE of two
    real live events found in exactly that state). raw_payload already
    has everything needed -- descriptions arrays are small (single
    digits) and are never truncated by sanitize_payload_for_storage()'s
    list-length limit, so no re-fetch from the live API is required."""
    RoadEvent = apps.get_model("road_conditions", "RoadEvent")
    for event in RoadEvent.objects.all().iterator():
        categories = _extract_cause_categories(event.raw_payload)
        if categories:
            event.cause_categories = categories
            event.save(update_fields=["cause_categories"])


def reverse_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("road_conditions", "0008_roadevent_cause_categories"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_noop),
    ]
