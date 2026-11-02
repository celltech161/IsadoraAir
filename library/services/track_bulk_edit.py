"""Bounded bulk metadata editing for /library/ (roadmap 3.11, r0125 Phase A).

The operator picks a selection (explicitly checked rows, or every result of a filter) and a change set,
previews it, then applies exactly what was previewed:

* **Change set.**  Every editable field is *leave* (the default: an omitted field never changes anything),
  *set* (a validated value) or *clear* (only where the field may be empty).  Additional categories and
  holidays are only ever *added* or *removed* by name; existing memberships that were not named are
  never touched.  Values are validated and normalized before anything is written, with the same lookups
  single-Track editing uses (``Artist.get_or_create_ci``, ``Album`` by title within the Track's
  album artist, ``Genre`` by name).
* **Selection.**  ``ids`` is the rows the operator ticked (on any page); ``filter`` is every Track matching
  the Library filter (minus ``exclude_ids``), which the operator chooses explicitly.  Both are resolved on
  the server and capped at ``MAX_TRACKS``.  Tracks removed since the list was loaded are reported as
  missing, never guessed.
* **Preview and token.**  The preview writes nothing.  It reports the resolved count, what each field would
  change, the Tracks a write guard would refuse, warnings and a sample, plus a token over the resolved
  Track ids and the normalized change set.  Apply recomputes the token and refuses (409) when the selection
  or the change set no longer matches what was previewed.
* **Apply.**  Each Track is applied in its own short transaction under its row lock, so one refused or
  failing Track neither blocks nor rolls back the others, and every outcome is reported.  Shared lookup
  rows (a new artist, album or genre) are created once, before the Tracks are written.

Deliberately not done here: audio-file tags are never written (that stays a separate Track Detail
action), cue markers are never written (they are not editable fields), waveform caches, source audio,
logs and PlayEvents are untouched, nothing is reanalyzed, and a primary-category change is database-only
-- the file is not relocated (Track Detail's relocation remains the one way to move a file); the preview
and the result count the files that will stay in their old category folder.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DatabaseError, transaction

log = logging.getLogger(__name__)

#: Most Tracks one bulk edit may touch.  Each Track is one short transaction; this keeps a request
#: comfortably inside the web worker's timeout without a background service.
MAX_TRACKS = 1000
#: Tracks shown with before/after values in a preview.
SAMPLE_SIZE = 10

LEAVE, SET, CLEAR = "leave", "set", "clear"
SELECTION_IDS, SELECTION_FILTER = "ids", "filter"


class BulkEditError(Exception):
    """A request that cannot be previewed or applied; nothing was written."""

    def __init__(self, message, *, status=400, **extra):
        super().__init__(message)
        self.status = status
        self.extra = extra


@dataclass(frozen=True)
class FieldSpec:
    label: str
    kind: str                 # text | choice | int | bool | artist | album | genre | category
    clearable: bool
    empty: object = ""        # the stored value of a cleared field


#: The editable fields, in display order.  Per-recording identity (title, ISRC, track and disc numbers,
#: related artists, file facts) and cue markers are deliberately absent.
FIELDS = {
    "artist": FieldSpec("Artist", "artist", clearable=False),
    "album": FieldSpec("Album", "album", clearable=True, empty=None),
    "genre": FieldSpec("Genre", "genre", clearable=True, empty=None),
    "year": FieldSpec("Year", "int", clearable=True, empty=None),
    "composer": FieldSpec("Composer", "text", clearable=True),
    "publisher": FieldSpec("Publisher", "text", clearable=True),
    "record_label": FieldSpec("Record label", "text", clearable=True),
    "energy": FieldSpec("Energy", "choice", clearable=True),
    "vocal_type": FieldSpec("Vocal type", "choice", clearable=True),
    "end_type": FieldSpec("End type", "choice", clearable=False),
    "rotation_weight": FieldSpec("Rotation weight", "int", clearable=False),
    "ready2air": FieldSpec("Ready to Air", "bool", clearable=False),
    "category": FieldSpec("Primary category", "category", clearable=True, empty=None),
}
MEMBERSHIPS = {
    "additional_categories": "Additional categories",
    "holidays": "Holidays",
}
_INT_BOUNDS = {"year": (1000, 9999), "rotation_weight": (0, 5)}


@dataclass(frozen=True)
class Operation:
    field: str
    op: str                   # set | clear | add | remove
    value: object             # normalized, JSON-serializable (names, ids, codes or literals)


@dataclass
class Selection:
    mode: str
    ids: list[int]
    missing: list[int]
    description: str


# --------------------------------------------------------------------------- parsing and validation

def _track_field(name):
    from library.models import Track
    return Track._meta.get_field(name)


def _text(name, value, max_length):
    if not isinstance(value, str):
        raise BulkEditError(f"{FIELDS[name].label if name in FIELDS else name}: expected text")
    text = value.replace("\x00", "").strip()
    if not text:
        label = FIELDS[name].label if name in FIELDS else name
        raise BulkEditError(f"{label}: an empty value is not a value -- choose Clear to empty the field")
    if len(text) > max_length:
        raise BulkEditError(f"{FIELDS[name].label}: longer than {max_length} characters")
    return text


def _normalize_set(name, value):
    from library.models import Album, Artist, Category, Genre
    spec = FIELDS[name]
    if spec.kind == "text":
        return _text(name, value, _track_field(name).max_length)
    if spec.kind == "choice":
        choices = {key for key, _label in _track_field(name).choices}
        if value not in choices:
            raise BulkEditError(f"{spec.label}: must be one of {', '.join(sorted(choices))}")
        return value
    if spec.kind == "int":
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise BulkEditError(f"{spec.label}: expected a whole number")
        try:
            number = _track_field(name).clean(value, None)
        except ValidationError:
            raise BulkEditError(f"{spec.label}: expected a whole number") from None
        low, high = _INT_BOUNDS[name]
        if not low <= number <= high:
            raise BulkEditError(f"{spec.label}: must be between {low} and {high}")
        return number
    if spec.kind == "bool":
        if not isinstance(value, bool):
            raise BulkEditError(f"{spec.label}: expected true or false")
        return value
    if spec.kind == "artist":
        return _text(name, value, Artist._meta.get_field("name").max_length)
    if spec.kind == "album":
        return _text(name, value, Album._meta.get_field("title").max_length)
    if spec.kind == "genre":
        return _text(name, value, Genre._meta.get_field("name").max_length)
    if spec.kind == "category":
        if isinstance(value, bool) or not isinstance(value, int):
            raise BulkEditError(f"{spec.label}: expected a category id")
        if not Category.objects.filter(pk=value).exists():
            raise BulkEditError(f"{spec.label}: category {value} not found", status=404)
        return value
    raise AssertionError(spec.kind)


def _membership_values(name, values, verb):
    from library.models import Category, Holiday
    if not isinstance(values, list):
        raise BulkEditError(f"{MEMBERSHIPS[name]}: '{verb}' must be a list")
    if name == "additional_categories":
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            raise BulkEditError(f"{MEMBERSHIPS[name]}: '{verb}' must list category ids")
        found = set(Category.objects.filter(pk__in=values).values_list("pk", flat=True))
    else:
        if not all(isinstance(v, str) and v for v in values):
            raise BulkEditError(f"{MEMBERSHIPS[name]}: '{verb}' must list holiday codes")
        found = set(Holiday.objects.filter(code__in=values).values_list("code", flat=True))
    unknown = sorted(set(values) - found, key=str)
    if unknown:
        raise BulkEditError(f"{MEMBERSHIPS[name]}: not found: {', '.join(map(str, unknown))}", status=404)
    return sorted(set(values), key=str)


def parse_changes(raw) -> tuple[Operation, ...]:
    """Validate and normalize a change set.  Raises BulkEditError; never writes."""
    if not isinstance(raw, dict):
        raise BulkEditError("changes must be an object")
    unknown = sorted(set(raw) - set(FIELDS) - set(MEMBERSHIPS))
    if unknown:
        raise BulkEditError(f"Not editable in bulk: {', '.join(unknown)}")
    ops = []
    for name, spec in FIELDS.items():
        if name not in raw:
            continue
        entry = raw[name]
        if not isinstance(entry, dict) or entry.get("op", LEAVE) not in (LEAVE, SET, CLEAR):
            raise BulkEditError(f"{spec.label}: expected {{\"op\": \"leave\" | \"set\" | \"clear\", \"value\": ...}}")
        op = entry.get("op", LEAVE)
        if op == LEAVE:
            continue
        if op == CLEAR:
            if not spec.clearable:
                raise BulkEditError(f"{spec.label} cannot be cleared")
            ops.append(Operation(name, CLEAR, None))
        else:
            if "value" not in entry:
                raise BulkEditError(f"{spec.label}: 'set' needs a value")
            ops.append(Operation(name, SET, _normalize_set(name, entry["value"])))
    for name, label in MEMBERSHIPS.items():
        if name not in raw:
            continue
        entry = raw[name]
        if not isinstance(entry, dict) or set(entry) - {"add", "remove"}:
            raise BulkEditError(f"{label}: expected {{\"add\": [...], \"remove\": [...]}}")
        add = _membership_values(name, entry.get("add", []), "add")
        remove = _membership_values(name, entry.get("remove", []), "remove")
        if set(add) & set(remove):
            raise BulkEditError(f"{label}: the same entry cannot be both added and removed")
        if add:
            ops.append(Operation(name, "add", add))
        if remove:
            ops.append(Operation(name, "remove", remove))
    if not ops:
        raise BulkEditError("Nothing to change: every field is set to Leave unchanged")
    return tuple(ops)


def resolve_selection(raw) -> Selection:
    """The Track ids a selection names now (sorted), and the requested ids that no longer exist."""
    from library.models import Track
    from library.services.track_filters import filter_tracks

    if not isinstance(raw, dict):
        raise BulkEditError("selection must be an object")
    mode = raw.get("mode")
    if mode == SELECTION_IDS:
        ids = raw.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
            raise BulkEditError("selection.ids must be a list of Track ids")
        requested = sorted(set(ids))
        if not requested:
            raise BulkEditError("No Tracks are selected")
        if len(requested) > MAX_TRACKS:
            raise BulkEditError(f"{len(requested)} Tracks are selected; a bulk edit is limited to {MAX_TRACKS}")
        present = sorted(Track.objects.filter(pk__in=requested).values_list("pk", flat=True))
        missing = sorted(set(requested) - set(present))
        return Selection(SELECTION_IDS, present, missing, f"{len(requested)} selected Track(s)")
    if mode == SELECTION_FILTER:
        spec = raw.get("filter") or {}
        exclude = raw.get("exclude_ids") or []
        if not isinstance(spec, dict) or not isinstance(exclude, list) \
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in exclude):
            raise BulkEditError("selection.filter must be an object and exclude_ids a list of Track ids")
        q = spec.get("q") or ""
        category = spec.get("category") or None
        ready_raw = spec.get("ready2air") or ""
        if not isinstance(q, str) or ready_raw not in ("", "true", "false") \
                or not (category is None or isinstance(category, (int, str))):
            raise BulkEditError("selection.filter has an invalid value")
        if category is not None and not str(category).isdigit():
            raise BulkEditError("selection.filter.category must be a category id")
        ready2air = {"true": True, "false": False}.get(ready_raw)
        qs = filter_tracks(Track.objects.all(), q=q.strip(), category_id=category, ready2air=ready2air)
        qs = qs.exclude(pk__in=exclude)
        ids = sorted(set(qs.values_list("pk", flat=True)[:MAX_TRACKS + 1]))
        if not ids:
            raise BulkEditError("No Tracks match this filter")
        if len(ids) > MAX_TRACKS:
            total = qs.count()
            raise BulkEditError(
                f"{total} Tracks match this filter; a bulk edit is limited to {MAX_TRACKS}. Narrow the filter.",
                count=total)
        parts = [f'search "{q.strip()}"' if q.strip() else "", f"category {category}" if category else "",
                 {"true": "ready to air", "false": "not ready"}.get(ready_raw, "")]
        described = ", ".join(p for p in parts if p) or "no filter"
        excluded = f", {len(set(exclude))} unticked" if exclude else ""
        return Selection(SELECTION_FILTER, ids, [], f"every Track matching the filter ({described}{excluded})")
    raise BulkEditError("selection.mode must be 'ids' or 'filter'")


def token_for(ids, ops) -> str:
    """Binds an apply to exactly the Tracks and the normalized changes that were previewed."""
    payload = json.dumps({"v": 1, "ids": list(ids), "ops": [[o.field, o.op, o.value] for o in ops]},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# --------------------------------------------------------------------------- per-Track planning

@dataclass
class _Targets:
    """Lookup rows a change set refers to.  ``create`` resolves (and creates) them; a preview only looks."""
    create: bool
    #: After the shared rows are prepared, a row first needed inside one Track's transaction is not cached:
    #: if that transaction rolls back, the next Track must not reuse a row that no longer exists.
    frozen: bool = False
    artists: dict = field(default_factory=dict)
    albums: dict = field(default_factory=dict)
    genres: dict = field(default_factory=dict)
    categories: dict = field(default_factory=dict)
    holidays: dict = field(default_factory=dict)

    def artist(self, name):
        from library.models import Artist
        if name not in self.artists:
            self.artists[name] = (Artist.get_or_create_ci(name)[0] if self.create
                                  else Artist.objects.filter(name__iexact=name).first())
        return self.artists[name]

    def album(self, title, album_artist):
        from library.models import Album
        key = (title, album_artist)
        if key not in self.albums:
            row = (Album.objects.get_or_create(title=title, album_artist=album_artist)[0] if self.create
                   else Album.objects.filter(title=title, album_artist=album_artist).first())
            if self.frozen:
                return row
            self.albums[key] = row
        return self.albums[key]

    def genre(self, name):
        from library.models import Genre
        if name not in self.genres:
            self.genres[name] = (Genre.objects.get_or_create(name=name)[0] if self.create
                                 else Genre.objects.filter(name=name).first())
        return self.genres[name]

    def category(self, pk):
        from library.models import Category
        if pk not in self.categories:
            self.categories[pk] = Category.objects.get(pk=pk)
        return self.categories[pk]

    def holiday(self, code):
        from library.models import Holiday
        if code not in self.holidays:
            self.holidays[code] = Holiday.objects.get(code=code)
        return self.holidays[code]


@dataclass
class _Plan:
    """What one Track would change: column values (by attname) and memberships, plus display labels."""
    columns: dict = field(default_factory=dict)       # attname -> new value
    update_fields: set = field(default_factory=set)
    add_categories: list = field(default_factory=list)
    remove_categories: list = field(default_factory=list)
    add_holidays: list = field(default_factory=list)
    remove_holidays: list = field(default_factory=list)
    changes: list = field(default_factory=list)       # [{"field", "label", "before", "after"}]
    unchanged: set = field(default_factory=set)       # field names already at their target
    folder_stays: bool = False

    @property
    def empty(self):
        return not (self.columns or self.add_categories or self.remove_categories
                    or self.add_holidays or self.remove_holidays)


def _display(spec_kind, value):
    if value is None or value == "":
        return "(empty)"
    if spec_kind == "category":
        return f"{value.code} — {value.name}"
    if spec_kind == "bool":
        return "Yes" if value else "No"
    if spec_kind in ("artist", "genre"):
        return value.name
    if spec_kind == "album":
        return value.title
    return str(value)


def _category_folder(track):
    """The category-code folder the Track's file sits in (LIBRARY_ROOT/<code>/...), or None."""
    root = Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music"))
    try:
        parts = Path(track.filepath).relative_to(root).parts
    except ValueError:
        return None
    return parts[0] if len(parts) > 1 else None


def _plan(track, ops, targets: _Targets, *, current_additional, current_holidays) -> _Plan:
    plan = _Plan()

    def change(name, attname, new, before, after):
        plan.columns[attname] = new
        plan.update_fields.add(name)
        plan.changes.append({"field": name, "label": FIELDS[name].label, "before": before, "after": after})

    def lookup_label(kind, row, requested):
        # A row a preview has not created yet is shown by the requested name.
        return _display(kind, row) if row is not None else f"{requested} (new)"

    new_primary = track.category_id
    for op in ops:
        if op.field not in FIELDS:
            continue
        spec = FIELDS[op.field]
        if spec.kind in ("artist", "album", "genre"):
            if op.op == CLEAR:
                target = None
            elif spec.kind == "artist":
                target = targets.artist(op.value)
            elif spec.kind == "album":
                target = targets.album(op.value, track.album.album_artist if track.album_id else "")
            else:
                target = targets.genre(op.value)
            current_id = getattr(track, f"{op.field}_id")
            unchanged = current_id is None if op.op == CLEAR else (target is not None and target.pk == current_id)
            if unchanged:
                plan.unchanged.add(op.field)
                continue
            after = "(empty)" if op.op == CLEAR else lookup_label(spec.kind, target, op.value)
            change(op.field, f"{op.field}_id", target.pk if target is not None else None,
                   _display(spec.kind, getattr(track, op.field)), after)
        elif spec.kind == "category":
            target = None if op.op == CLEAR else targets.category(op.value)
            new_primary = target.pk if target is not None else None
            if new_primary == track.category_id:
                plan.unchanged.add(op.field)
                continue
            change(op.field, "category_id", new_primary, _display("category", track.category),
                   _display("category", target))
            folder = _category_folder(track)
            plan.folder_stays = folder is not None and (target is None or folder != target.code)
        else:
            new = spec.empty if op.op == CLEAR else op.value
            current = getattr(track, op.field)
            if current == new:
                plan.unchanged.add(op.field)
                continue
            change(op.field, op.field, new, _display(spec.kind, current), _display(spec.kind, new))

    # Memberships: only the named entries move.  A Track never lists its own primary category as an
    # additional one (single-Track editing drops it the same way).
    category_ops = {o.op: o.value for o in ops if o.field == "additional_categories"}
    holiday_ops = {o.op: o.value for o in ops if o.field == "holidays"}
    for pk in category_ops.get("add", []):
        if pk != new_primary and pk not in current_additional:
            plan.add_categories.append(pk)
    for pk in category_ops.get("remove", []):
        if pk in current_additional:
            plan.remove_categories.append(pk)
    if new_primary is not None and new_primary in current_additional and new_primary not in plan.remove_categories:
        plan.remove_categories.append(new_primary)
    for code in holiday_ops.get("add", []):
        if code not in current_holidays:
            plan.add_holidays.append(code)
    for code in holiday_ops.get("remove", []):
        if code in current_holidays:
            plan.remove_holidays.append(code)
    if plan.add_categories or plan.remove_categories:
        after = (set(current_additional) - set(plan.remove_categories)) | set(plan.add_categories)
        plan.changes.append({
            "field": "additional_categories", "label": MEMBERSHIPS["additional_categories"],
            "before": ", ".join(sorted(targets.category(pk).code for pk in current_additional)) or "(none)",
            "after": ", ".join(sorted(targets.category(pk).code for pk in after)) or "(none)",
        })
    if plan.add_holidays or plan.remove_holidays:
        after = (set(current_holidays) - set(plan.remove_holidays)) | set(plan.add_holidays)
        plan.changes.append({
            "field": "holidays", "label": MEMBERSHIPS["holidays"],
            "before": ", ".join(sorted(current_holidays)) or "(none)",
            "after": ", ".join(sorted(after)) or "(none)",
        })
    return plan


def _memberships(ids):
    from library.models import Track
    additional, holidays = {}, {}
    for track_id, category_id in Track.additional_categories.through.objects.filter(
            track_id__in=ids).values_list("track_id", "category_id"):
        additional.setdefault(track_id, set()).add(category_id)
    for track_id, code in Track.holidays.through.objects.filter(track_id__in=ids).values_list(
            "track_id", "holiday_id"):
        holidays.setdefault(track_id, set()).add(code)
    return additional, holidays


def _guard_refusal(track, plan) -> str | None:
    """Would a registered Track write guard (e.g. a spoken projection) refuse these column changes?"""
    from library.models import TrackWriteRefused
    if not plan.columns:
        return None
    for attname, value in plan.columns.items():
        # A lookup row a preview has not created yet gets a placeholder id: any id differs from the
        # stored one, which is all a guard compares.
        setattr(track, attname, 0 if value is None and attname == "artist_id" else value)
    try:
        track.check_write_guards(sorted(plan.update_fields))
    except TrackWriteRefused as exc:
        return str(exc)
    return None


def _describe_ops(ops, targets):
    out = []
    for op in ops:
        if op.field in FIELDS:
            spec = FIELDS[op.field]
            if op.op == CLEAR:
                value = "(clear)"
            elif spec.kind == "category":
                value = _display("category", targets.category(op.value))
            elif spec.kind == "bool":
                value = _display("bool", op.value)
            else:
                value = str(op.value)
            out.append({"field": op.field, "label": spec.label, "op": op.op, "value": value})
        else:
            names = [targets.category(v).code if op.field == "additional_categories" else targets.holiday(v).name
                     for v in op.value]
            out.append({"field": op.field, "label": MEMBERSHIPS[op.field], "op": op.op, "value": ", ".join(names)})
    return out


def _track_rows(ids):
    from library.models import Track
    return Track.objects.select_related("artist", "album", "genre", "category").filter(pk__in=ids).order_by("pk")


def preview(selection: Selection, ops) -> dict:
    """What applying ``ops`` to ``selection`` would do.  Reads only."""
    targets = _Targets(create=False)
    additional, holidays = _memberships(selection.ids)
    summary = {}
    for op in ops:
        summary.setdefault(op.field, {"label": FIELDS[op.field].label if op.field in FIELDS
                                      else MEMBERSHIPS[op.field], "change": 0, "unchanged": 0})
    changing, unchanged, folder_stays = 0, 0, 0
    refusals, samples = [], []
    for track in _track_rows(selection.ids):
        plan = _plan(track, ops, targets, current_additional=additional.get(track.pk, set()),
                     current_holidays=holidays.get(track.pk, set()))
        changed_fields = {c["field"] for c in plan.changes}
        for name, counts in summary.items():
            counts["change" if name in changed_fields else "unchanged"] += 1
        if plan.empty:
            unchanged += 1
            continue
        title, artist = track.title, track.artist.name if track.artist_id else ""
        refusal = _guard_refusal(track, plan)
        if refusal:
            refusals.append({"id": track.pk, "title": title, "artist": artist, "reason": refusal})
            continue
        changing += 1
        folder_stays += plan.folder_stays
        if len(samples) < SAMPLE_SIZE:
            samples.append({"id": track.pk, "title": title, "artist": artist, "changes": plan.changes})
    warnings = []
    if folder_stays:
        warnings.append(
            f"{folder_stays} Track file(s) will stay in their current category folder: a bulk primary-category "
            "change updates the database only. Use Track Detail to relocate a file.")
    new_rows = [f"artist \"{name}\"" for name, row in targets.artists.items() if row is None]
    new_rows += [f"genre \"{name}\"" for name, row in targets.genres.items() if row is None]
    new_rows += sorted({f"album \"{title}\"" for (title, _aa), row in targets.albums.items() if row is None})
    if new_rows:
        warnings.append("Will be created: " + ", ".join(new_rows) + ".")
    return {
        "selection": {"mode": selection.mode, "description": selection.description,
                      "count": len(selection.ids), "missing": selection.missing},
        "operations": _describe_ops(ops, targets),
        "summary": summary,
        "tracks_changing": changing,
        "tracks_unchanged": unchanged,
        "refusals": refusals,
        "warnings": warnings,
        "samples": samples,
        "token": token_for(selection.ids, ops),
        "max_tracks": MAX_TRACKS,
    }


def _prepare_targets(ops, ids) -> _Targets:
    """Create the lookup rows the change set names, once, before any Track is written."""
    from library.models import Track
    targets = _Targets(create=True)
    for op in ops:
        if op.op != SET:
            continue
        if op.field == "artist":
            targets.artist(op.value)
        elif op.field == "genre":
            targets.genre(op.value)
        elif op.field == "album":
            album_artists = set(Track.objects.filter(pk__in=ids).values_list("album__album_artist", flat=True))
            for album_artist in sorted({aa or "" for aa in album_artists}):
                targets.album(op.value, album_artist)
    targets.frozen = True
    return targets


def apply(selection: Selection, ops, token, *, user=None) -> dict:
    """Apply exactly the previewed edit.  Each Track is its own transaction; every outcome is reported."""
    from library.models import Track, TrackWriteRefused

    current = token_for(selection.ids, ops)
    if token != current:
        raise BulkEditError(
            "The selection or the changes differ from the preview (Tracks were added, removed or changed "
            "since it was made). Preview again before applying.",
            status=409, count=len(selection.ids), missing=selection.missing)
    with transaction.atomic():
        targets = _prepare_targets(ops, selection.ids)
    through_categories = Track.additional_categories.through
    through_holidays = Track.holidays.through
    updated, unchanged, folder_stays = [], 0, 0
    failed, missing = [], list(selection.missing)
    for track_id in selection.ids:
        title = artist = ""
        try:
            with transaction.atomic():
                track = (Track.objects.select_related("artist", "album", "genre", "category")
                         .select_for_update(of=("self",)).filter(pk=track_id).first())
                if track is None:
                    missing.append(track_id)
                    continue
                title, artist = track.title, track.artist.name if track.artist_id else ""
                additional, holidays = _memberships([track_id])
                plan = _plan(track, ops, targets, current_additional=additional.get(track_id, set()),
                             current_holidays=holidays.get(track_id, set()))
                if plan.empty:
                    unchanged += 1
                    continue
                if plan.columns:
                    for attname, value in plan.columns.items():
                        setattr(track, attname, value)
                    track.save(update_fields=sorted({*plan.update_fields, "updated_at"}))
                if plan.remove_categories:
                    through_categories.objects.filter(track_id=track_id,
                                                      category_id__in=plan.remove_categories).delete()
                if plan.add_categories:
                    through_categories.objects.bulk_create(
                        [through_categories(track_id=track_id, category_id=pk) for pk in plan.add_categories],
                        ignore_conflicts=True)
                if plan.remove_holidays:
                    through_holidays.objects.filter(track_id=track_id, holiday_id__in=plan.remove_holidays).delete()
                if plan.add_holidays:
                    through_holidays.objects.bulk_create(
                        [through_holidays(track_id=track_id, holiday_id=code) for code in plan.add_holidays],
                        ignore_conflicts=True)
                updated.append(track_id)
                folder_stays += plan.folder_stays
        except TrackWriteRefused as exc:
            failed.append({"id": track_id, "title": title, "artist": artist, "reason": str(exc)})
        except (ValidationError, DatabaseError) as exc:
            failed.append({"id": track_id, "title": title, "artist": artist,
                           "reason": f"not saved ({type(exc).__name__})"})
            log.warning("bulk_edit track_id=%s failed: %s", track_id, type(exc).__name__)
        except Exception as exc:                      # report it, keep going: the other Tracks are independent
            failed.append({"id": track_id, "title": title, "artist": artist,
                           "reason": f"unexpected error ({type(exc).__name__}); this Track was not changed"})
            log.exception("bulk_edit track_id=%s unexpected failure", track_id)
    log.info("bulk_edit user=%s selection=%s requested=%d updated=%d unchanged=%d failed=%d missing=%d",
             getattr(user, "pk", None), selection.mode, len(selection.ids) + len(selection.missing),
             len(updated), unchanged, len(failed), len(missing))
    return {
        "ok": True,
        "requested": len(selection.ids) + len(selection.missing),
        "updated": len(updated),
        "updated_ids": updated,
        "unchanged": unchanged,
        "failed": failed,
        "missing": sorted(set(missing)),
        "files_left_in_previous_category_folder": folder_stays,
    }
