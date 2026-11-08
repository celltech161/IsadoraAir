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
* **Preview and token.**  The preview writes nothing to the library.  It reports the resolved count, what
  each field would change, the Tracks a write guard would refuse, warnings and a sample.  It records, in the
  operator's server-side session, each selected Track's *edit state* (a fingerprint of every column and
  membership a bulk edit reads or writes, plus the file path) and what the preview decided for it, and
  returns a token signed with ``django.core.signing`` that names that record, the operator, the resolved
  Track ids and the normalized change set.  The token cannot be made without the server's key, expires
  after ``TOKEN_MAX_AGE_SECONDS``, is good for one apply only and only in the session and for the operator
  it was issued to.  Issuing and claiming lock the session's database row (``SessionPreviews``), so
  concurrent applies of one token -- in any worker -- cannot both claim it.
* **Apply.**  Refused (409) when the token is expired or used, or the selection or the change set no longer
  matches the preview.  Otherwise each Track is applied in its own short transaction under its row lock: its
  edit state is recomputed under that lock, and a Track that changed since the preview is reported
  ``stale`` and left alone, never overwritten.  One refused, stale or failing Track neither blocks nor rolls
  back the others; the selection as a whole is therefore *not* all-or-nothing, and the result reports
  every Track as updated, unchanged, refused, stale, missing or failed.  Changes that a bulk edit neither
  reads nor writes (play counts, logs, PlayEvents, cue markers, waveforms) do not make a Track stale.
  Shared lookup rows (a new artist, album or genre) are created once, before the Tracks are written.

Deliberately not done here: audio-file tags are never written (that stays a separate Track Detail
action), cue markers are never written (they are not editable fields), waveform caches, source audio,
logs and PlayEvents are untouched and nothing is reanalyzed.  A primary-category change is database-only
by default -- the file is not relocated; the preview and the result count the files that will stay in
their old category folder.

**File-location policy (3.11).**  ``file_location`` chooses what a *Set primary category* does to the
files: ``metadata_only`` (the default, and what every request that omits it gets: the r0125 behaviour
above) or ``move`` -- each affected file moves to ``LIBRARY_ROOT/<new Category code>/<its name>`` through
the shared guarded relocation service (``media_relocation``), in the same per-Track transaction as the
Track's other changes, so the Category and the path commit together or not at all and a failed move never
falls back to a metadata-only change.  Relocation authority is never inferred from a ``category:set``
alone; ``move`` needs a ``category:set`` (a ``category:clear`` has no folder and is refused with it).  The
preview checks every file to be moved (the strict source rules, the destination, the filesystem, and the
playout engine: a Track on air, queued, claimed or in the current or next log hour is refused) and binds the
policy, each file's path and witness and its destination to the token; changing the policy invalidates the
preview.  Applying the move policy needs ``confirm_move: true``, checked before the preview is spent (a missing or
false confirmation is refused and the preview stays valid).  At most ``MOVE_LIMIT`` files move per bulk edit (the
1,000-Track selection limit is unchanged); their lock waits are bounded per Track and per request.  Every outcome
that needs the operator (``media_relocation.ATTENTION``, a staged entry that could not be removed, an unexpected
failure) stops the remaining Tracks, which are reported as not attempted.  Every other edit -- and every Track whose file is
already in the new Category's folder -- is applied exactly as before, with no new file or engine checks.
"""
from __future__ import annotations

import hashlib
import json
import os
import logging
from dataclasses import dataclass, field
from pathlib import Path

import secrets
import time
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.core import signing
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import DatabaseError, IntegrityError, transaction
from django.utils import timezone

log = logging.getLogger(__name__)

#: Most Tracks one bulk edit may touch.  Each Track is one short transaction; this keeps a request
#: comfortably inside the web worker's timeout without a background service.
MAX_TRACKS = 1000
#: Tracks shown with before/after values in a preview.
SAMPLE_SIZE = 10
#: How long a preview's token may be applied.
TOKEN_MAX_AGE_SECONDS = 15 * 60
#: Previews kept per session (the oldest unused one is dropped); each holds one entry per selected Track.
PENDING_PREVIEWS = 3
SESSION_KEY = "library_bulk_edit_previews"
_TOKEN_SALT = "library.track_bulk_edit.preview.v2"

LEAVE, SET, CLEAR = "leave", "set", "clear"
#: The file-location policy of a primary-category change (3.11).
METADATA_ONLY, MOVE_FILES = "metadata_only", "move"
FILE_LOCATIONS = (METADATA_ONLY, MOVE_FILES)
#: Most files one bulk edit may move (each one is its own guarded transaction).
MOVE_LIMIT = 25
#: Lock waits of one moving Track's transaction, and of all of a request's moves together.
MOVE_LOCK_BUDGET_SECONDS = 2.0
MOVE_REQUEST_LOCK_BUDGET_SECONDS = 10.0
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


def parse_file_location(raw, ops) -> str:
    """The file-location policy of a request: ``metadata_only`` when omitted (r0125 compatibility).  Raises
    BulkEditError for anything else than the two policies, or ``move`` without a primary-category ``set``."""
    if raw is None:
        return METADATA_ONLY
    if raw not in FILE_LOCATIONS:
        raise BulkEditError("file_location must be 'move' or 'metadata_only'")
    if raw == MOVE_FILES:
        category = [o for o in ops if o.field == "category"]
        if not category:
            raise BulkEditError("Moving files needs a primary-category change (Set primary category)")
        if category[0].op != SET:
            raise BulkEditError("Clearing the primary category has no folder to move files to; choose "
                                "'metadata_only'")
    return raw


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


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def ops_digest(ops) -> str:
    return _digest([[o.field, o.op, o.value] for o in ops])


def ids_digest(ids) -> str:
    return _digest(list(ids))


def edit_state(track, additional, holidays) -> str:
    """Fingerprint of everything a bulk edit reads or writes for this Track (and its file path): a change to
    any of it between preview and apply makes the Track stale.  Play counts, timestamps, cue markers and
    other columns are deliberately excluded."""
    return _digest([
        track.filepath, track.artist_id, track.album_id, track.album.album_artist if track.album_id else None,
        track.genre_id, track.year, track.composer, track.publisher, track.record_label, track.energy,
        track.vocal_type, track.end_type, track.rotation_weight, track.ready2air, track.category_id,
        sorted(additional), sorted(holidays),
    ])[:32]


class SessionPreviews:
    """Pending preview records in the operator's own database session row (r0125).

    Every read-modify-write runs in one transaction holding that row's lock (``SELECT ... FOR UPDATE``), so
    two requests of the same session -- in different Gunicorn workers, on different connections -- cannot
    both claim one preview: the second waits for the first to commit, then finds the record gone.  The
    request's in-memory session is brought in line and left unmodified, so SessionMiddleware does not write
    an older copy back at the end of the request.  Needs the database session backend (the configured
    default); anything else is refused rather than silently racing.

    The session row alone cannot make a claim permanent: any other request of the session that loaded it
    before the claim saves its whole copy afterwards, record included (Django sessions are last-writer-wins).
    So a claim also inserts a *claim marker*: a row of its own in the same ``django_session`` table, keyed
    by the preview's nonce under a prefix Django never generates (its keys are 32 lowercase letters and
    digits; ``:`` cannot occur), carrying no user and expiring after the token can no longer be used.  The
    insert succeeds exactly once (primary-key uniqueness), commits with the claim, and no ordinary session
    save can touch it: a session only ever writes its own row.  A record that a stale save put back is
    therefore refused."""

    ENGINE = "django.contrib.sessions.backends.db"
    CLAIM_PREFIX = "bulkedit:"

    def __init__(self, session):
        if getattr(settings, "SESSION_ENGINE", self.ENGINE) != self.ENGINE:
            raise ImproperlyConfigured("bulk-edit previews need the database session backend")
        self.session = session

    @contextmanager
    def _locked(self):
        from django.contrib.sessions.models import Session
        key = self.session.session_key
        with transaction.atomic():
            row = Session.objects.select_for_update().filter(session_key=key).first() if key else None
            if row is None:
                raise BulkEditError("Your session has ended. Sign in again, then preview again.", status=409)
            data = self.session.decode(row.session_data)
            pending = dict(data.get(SESSION_KEY) or {})
            yield pending
            data[SESSION_KEY] = pending
            row.session_data = self.session.encode(data)
            row.save(update_fields=["session_data"])
        self.session[SESSION_KEY] = pending
        self.session.modified = False

    def issue(self, nonce, record):
        with self._locked() as pending:
            now = time.time()
            for key in [k for k, r in pending.items() if now - r.get("issued", 0) > TOKEN_MAX_AGE_SECONDS]:
                del pending[key]
            pending[nonce] = record
            for key in sorted(pending, key=lambda k: pending[k]["issued"])[:-PENDING_PREVIEWS]:
                del pending[key]

    def claim(self, nonce):
        """The record, removed and durably marked claimed (exactly one caller ever gets it), or None."""
        with self._locked() as pending:
            record = pending.pop(nonce, None)
            if record is None or not self._mark_claimed(nonce):
                return None
            return record

    def _claim_key(self, nonce):
        digest = hashlib.sha256(str(nonce).encode()).hexdigest()
        return self.CLAIM_PREFIX + digest[:40 - len(self.CLAIM_PREFIX)]

    def _mark_claimed(self, nonce) -> bool:
        from django.contrib.sessions.models import Session
        now = timezone.now()
        Session.objects.filter(session_key__startswith=self.CLAIM_PREFIX, expire_date__lt=now).delete()
        try:
            with transaction.atomic():
                Session.objects.create(
                    session_key=self._claim_key(nonce), session_data=self.session.encode({"bulk_edit_claimed": True}),
                    expire_date=now + timedelta(seconds=TOKEN_MAX_AGE_SECONDS + 300))
        except IntegrityError:
            return False
        return True


def _issue_token(previews, user, selection, ops, tracks: dict, mode=METADATA_ONLY) -> str:
    """Record a preview for the operator and return the signed token naming it (and its file-location policy)."""
    nonce = secrets.token_urlsafe(16)
    previews.issue(nonce, {"user": user.pk, "ids": ids_digest(selection.ids), "ops": ops_digest(ops),
                           "issued": int(time.time()), "tracks": tracks, "mode": mode})
    return signing.dumps({"n": nonce, "u": user.pk, "s": ids_digest(selection.ids), "o": ops_digest(ops),
                          "m": mode}, salt=_TOKEN_SALT)


def _redeem_token(previews, user, token, selection, ops, mode=METADATA_ONLY) -> dict:
    """The preview a token names, removed from the session (one use).  Raises BulkEditError."""
    if not isinstance(token, str) or not token:
        raise BulkEditError("Preview the edit first: apply needs the preview's token")
    try:
        claims = signing.loads(token, salt=_TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise BulkEditError("This preview has expired. Preview again before applying.", status=409) from None
    except signing.BadSignature:
        raise BulkEditError("This is not a valid preview token. Preview the edit first.") from None
    if not isinstance(claims, dict) or claims.get("u") != user.pk:
        raise BulkEditError("This preview was made by another operator.", status=403)
    record = previews.claim(claims.get("n"))
    if record is None or record.get("user") != user.pk:
        raise BulkEditError("This preview was already applied or is no longer available. Preview again.",
                            status=409)
    if time.time() - record["issued"] > TOKEN_MAX_AGE_SECONDS:
        raise BulkEditError("This preview has expired. Preview again before applying.", status=409)
    if claims.get("s") != ids_digest(selection.ids) or record["ids"] != claims.get("s"):
        raise BulkEditError(
            "The selection differs from the preview (Tracks were added or removed since it was made). "
            "Preview again before applying.", status=409, count=len(selection.ids), missing=selection.missing)
    if claims.get("o") != ops_digest(ops) or record["ops"] != claims.get("o"):
        raise BulkEditError("The changes differ from the preview. Preview again before applying.", status=409)
    if claims.get("m", METADATA_ONLY) != mode or record.get("mode", METADATA_ONLY) != mode:
        raise BulkEditError("The file-location choice (move files, or update the Category only) differs from the "
                            "preview. Preview again before applying.", status=409)
    return record


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


class _Mover:
    """Move-policy planning (3.11): which files a primary-category change relocates, and which it cannot."""

    def __init__(self, targets):
        from library.services import library_reconciliation, media_relocation
        from library.services.library_duplicate_resolution import _engine_state
        try:
            self.root = media_relocation.library_root()
        except media_relocation.Stop as stop:
            raise BulkEditError(stop.message, status=409) from None
        self.targets = targets
        self.protection = library_reconciliation._protection_predicates()
        self.now = timezone.now()
        self.engine, self.engine_problem = _engine_state()
        self.destinations = {}
        self.moves = []
        self.counts = {"no_move_needed": 0, "collisions": 0, "scheduling_conflicts": 0, "file_refusals": 0,
                       "later_log_warnings": 0}

    def plan(self, track, plan):
        """(move, refusal): the file move this Track's change needs (None when its file is already in the new
        Category's folder), or why it cannot be moved now."""
        from library.services import media_relocation as m
        code = self.targets.category(plan.columns["category_id"]).code
        try:
            m.validate_category_storage_component(code)
        except ValidationError:
            return None, f"File not moved: the Category code {code!r} cannot be a Library folder name"
        if m.category_folder(track.filepath, self.root) == code:
            self.counts["no_move_needed"] += 1
            return None, None
        try:
            witness = m.check_source(track.filepath, root=self.root, strict=True, protection=self.protection,
                                     track=track)
            destination = m.destination_for(track.filepath, code, self.root)
            m.check_destination(track.filepath, destination, root=self.root, track_id=track.pk, allow_copy=False)
        except m.Stop as stop:
            self.counts["collisions" if stop.outcome == m.COLLISION else "file_refusals"] += 1
            return None, "File not moved: " + stop.message
        if destination in self.destinations:
            self.counts["collisions"] += 1
            return None, (f"File not moved: Track #{self.destinations[destination]} of this selection moves a file "
                          "with the same name to the same folder")
        blockers, warnings = m.playout_conflicts(track, now=self.now, engine=self.engine,
                                                 engine_problem=self.engine_problem)
        if blockers:
            self.counts["scheduling_conflicts"] += 1
            return None, "Not moved now: " + "; ".join(blockers)
        self.counts["later_log_warnings"] += bool(warnings)
        self.destinations[destination] = track.pk
        move = {"from": track.filepath, "to": destination, "witness": witness}
        self.moves.append({"id": track.pk, "title": track.title, "artist": track.artist.name if track.artist_id else "",
                           "from": os.path.relpath(track.filepath, self.root),
                           "to": os.path.relpath(destination, self.root)})
        return move, None


def _write_plan(track, plan, *, extra_fields=()) -> None:
    """Write one Track's planned columns and memberships (inside the caller's transaction, under its row lock)."""
    from library.models import Track
    through_categories = Track.additional_categories.through
    through_holidays = Track.holidays.through
    track_id = track.pk
    if plan.columns or extra_fields:
        for attname, value in plan.columns.items():
            setattr(track, attname, value)
        track.save(update_fields=sorted({*plan.update_fields, *extra_fields, "updated_at"}))
    if plan.remove_categories:
        through_categories.objects.filter(track_id=track_id, category_id__in=plan.remove_categories).delete()
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


def _apply_move(track_id, entry, ops, targets, *, user, root, protection, budget_seconds, category_from=None):
    """One previewed move: the Track's whole planned change and its file move as one guarded operation
    (``media_relocation.execute``).  Returns its ``media_relocation.Result``."""
    from library.models import Track
    from library.services import media_relocation as m
    from library.services.library_duplicate_resolution import _engine_state

    state_then, _disposition, source, destination, witness = entry[:5]
    engine, engine_problem = _engine_state()
    held = {}
    target = next((o.value for o in ops if o.field == "category" and o.op == SET), None)
    # Both Categories for the audit: the original from the row as read before the move and, once it is locked, as
    # it is under the lock (never from the Track after the change); the destination from the change set.
    audit = {"operation": "bulk_category_move", "witness": witness, "category_from": category_from,
             "category_to": targets.category(target).code if target is not None else None}

    def check(locked):
        track = Track.objects.select_related("artist", "album", "genre", "category").get(pk=locked.pk)
        additional, holidays = _memberships([track_id])
        current_additional, current_holidays = additional.get(track_id, set()), holidays.get(track_id, set())
        audit["category_from"] = track.category.code if track.category_id else None
        if edit_state(track, current_additional, current_holidays) != state_then:
            raise m.Stop(m.STALE, "changed since the preview; left as it is")
        plan = _plan(track, ops, targets, current_additional=current_additional, current_holidays=current_holidays)
        target = plan.columns.get("category_id")
        if target is None or m.destination_for(track.filepath, targets.category(target).code, root) != destination:
            raise m.Stop(m.STALE, "its planned destination changed since the preview; left as it is")
        blockers, _warnings = m.playout_conflicts(track, now=timezone.now(), engine=engine,
                                                  engine_problem=engine_problem)
        if blockers:
            raise m.Stop(m.REFUSED, "not moved now: " + "; ".join(blockers))
        held.update(track=track, plan=plan)

    def write(_locked, filepath):
        track, plan = held["track"], held["plan"]
        track.filepath = filepath
        _write_plan(track, plan, extra_fields=("filepath",))
        return {"category_to": targets.category(plan.columns["category_id"]).code}

    return m.execute(track_id=track_id, source=source, destination=destination, origin="bulk_edit", user=user,
                     write=write, check=check, expected_witness=witness, strict=True, allow_copy=False,
                     lock_budget_seconds=budget_seconds, protection=protection, audit=audit)


def preview(selection: Selection, ops, *, user, previews, file_location=METADATA_ONLY) -> dict:
    """What applying ``ops`` to ``selection`` would do, and the token to apply exactly that.  Writes nothing to
    the library; ``previews`` (normally ``SessionPreviews``) records the preview.  ``file_location``: the
    policy for files of a primary-category change (``move`` plans and checks every move)."""
    targets = _Targets(create=False)
    mover = _Mover(targets) if file_location == MOVE_FILES else None
    additional, holidays = _memberships(selection.ids)
    summary = {}
    for op in ops:
        summary.setdefault(op.field, {"label": FIELDS[op.field].label if op.field in FIELDS
                                      else MEMBERSHIPS[op.field], "change": 0, "unchanged": 0})
    changing, unchanged, folder_stays = 0, 0, 0
    refusals, samples, tracks = [], [], {}
    for track in _track_rows(selection.ids):
        state = edit_state(track, additional.get(track.pk, set()), holidays.get(track.pk, set()))
        plan = _plan(track, ops, targets, current_additional=additional.get(track.pk, set()),
                     current_holidays=holidays.get(track.pk, set()))
        changed_fields = {c["field"] for c in plan.changes}
        for name, counts in summary.items():
            counts["change" if name in changed_fields else "unchanged"] += 1
        if plan.empty:
            unchanged += 1
            tracks[str(track.pk)] = [state, "unchanged"]
            continue
        title, artist = track.title, track.artist.name if track.artist_id else ""
        refusal = _guard_refusal(track, plan)
        move = None
        if not refusal and mover is not None and "category_id" in plan.columns:
            move, refusal = mover.plan(track, plan)
        if refusal:
            refusals.append({"id": track.pk, "title": title, "artist": artist, "reason": refusal})
            tracks[str(track.pk)] = [state, "refused", refusal[:300]]
            continue
        tracks[str(track.pk)] = [state, "move", move["from"], move["to"], move["witness"]] if move else [state, "change"]
        changing += 1
        folder_stays += plan.folder_stays if mover is None else 0
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
    moving = {}
    if mover is not None:
        if len(mover.moves) > MOVE_LIMIT:
            raise BulkEditError(
                f"{len(mover.moves)} files would be moved; at most {MOVE_LIMIT} files can be moved in one bulk edit. "
                "Narrow the selection, or choose to update the Category only.", count=len(mover.moves))
        if mover.moves:
            warnings.append(
                f"{len(mover.moves)} file(s) will be moved to the new Category's folder. A moved file's waveform shows "
                "as unverified until you run Reanalyze Track on it (Repick and Reset Cue Points wait until then); "
                "cue points, approvals, play history, logs and the audio itself are unchanged.")
        if mover.counts["later_log_warnings"]:
            warnings.append(f"{mover.counts['later_log_warnings']} Track(s) to be moved are in later approved log "
                            "hours; those entries are kept as they are and will play the file from its new place.")
        moving = {"moves": mover.moves, "moves_count": len(mover.moves), "move_limit": MOVE_LIMIT, **mover.counts}
    return {
        "file_location": file_location,
        **moving,
        "selection": {"mode": selection.mode, "description": selection.description,
                      "count": len(selection.ids), "missing": selection.missing},
        "operations": _describe_ops(ops, targets),
        "summary": summary,
        "tracks_changing": changing,
        "tracks_unchanged": unchanged,
        "refusals": refusals,
        "warnings": warnings,
        "samples": samples,
        "token": _issue_token(previews, user, selection, ops, tracks, file_location),
        "token_expires_in": TOKEN_MAX_AGE_SECONDS,
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


def apply(selection: Selection, ops, token, *, user, previews, file_location=METADATA_ONLY, confirm_move=False) -> dict:
    """Apply exactly the previewed edit (see the module docstring).  Each Track is its own transaction under its
    row lock; every outcome is reported.  ``confirm_move``: the move policy needs the operator's explicit
    confirmation with the apply request, checked before the preview is spent."""
    from library.models import Track, TrackWriteRefused

    from library.services import library_audit, media_relocation

    if file_location == MOVE_FILES and confirm_move is not True:
        raise BulkEditError("Confirm that the previewed files will be moved (confirm_move) before applying. The "
                            "preview is still valid.")
    operations = [f"{o.field}:{o.op}" for o in ops]
    try:
        record = _redeem_token(previews, user, token, selection, ops, file_location)
    except BulkEditError as exc:
        # Nothing was applied; worth keeping for troubleshooting (an expired, replayed, foreign or drifted preview).
        library_audit.record(
            "bulk_edit.apply", user=user, outcome="refused", level="warning",
            title=f"Library bulk edit refused: {exc}"[:200],
            detail={"reason": str(exc), "status": exc.status, "selection_mode": selection.mode,
                    "requested": len(selection.ids) + len(selection.missing), "operations": operations})
        raise
    previewed = record["tracks"]
    movers = [k for k, v in previewed.items() if v[1] == "move"]
    if movers and (file_location != MOVE_FILES or len(movers) > MOVE_LIMIT):
        raise BulkEditError("This preview cannot be applied. Preview again.", status=409)
    with transaction.atomic():
        targets = _prepare_targets(ops, selection.ids)
    root = protection = None
    if movers:
        from library.services import library_reconciliation
        try:
            root = media_relocation.library_root()
        except media_relocation.Stop as stop:
            raise BulkEditError(stop.message, status=409) from None
        protection = library_reconciliation._protection_predicates()
    move_deadline = time.monotonic() + MOVE_REQUEST_LOCK_BUDGET_SECONDS
    updated, unchanged, folder_stays = [], 0, 0
    refused, stale, failed, missing = [], [], [], list(selection.missing)
    moved, cleanup_required, uncertain, not_attempted = [], [], [], []
    halted, attempted = False, 0
    for track_id in selection.ids:
        title = artist = ""
        state_then, disposition, *reason = previewed.get(str(track_id), [None, None])
        if halted:
            not_attempted.append({"id": track_id, "title": "", "artist": "",
                                  "reason": "not attempted: processing stopped at a Track that needs attention"})
            continue
        attempted += 1
        if disposition == "move":
            row = Track.objects.filter(pk=track_id).values("title", "artist__name", "category__code").first()
            if row is None:
                missing.append(track_id)
                continue
            info = {"id": track_id, "title": row["title"], "artist": row["artist__name"] or ""}
            budget = max(0.001, min(MOVE_LOCK_BUDGET_SECONDS, move_deadline - time.monotonic()))
            try:
                result = _apply_move(track_id, [state_then, disposition, *reason], ops, targets, user=user,
                                     root=root, protection=protection, budget_seconds=budget,
                                     category_from=row["category__code"])
            except Exception as exc:              # an unresolved result: stop, never guess
                log.exception("bulk_edit track_id=%s move: unexpected failure", track_id)
                uncertain.append({**info, "reason": f"unexpected error ({type(exc).__name__}); its outcome is not "
                                                    "known. Check this Track before doing anything else."})
                halted = True
                continue
            m = media_relocation
            if result.outcome in (m.MOVED, m.COMMITTED_CLEANUP_REQUIRED):
                updated.append(track_id)
                moved.append({**info, "from": os.path.relpath(result.source, root),
                              "to": os.path.relpath(result.destination, root)})
                if result.outcome == m.COMMITTED_CLEANUP_REQUIRED:
                    cleanup_required.append({**info, "reason": result.message,
                                             "location": result.as_dict().get("cleanup_location")})
            elif result.outcome == m.STALE:
                stale.append({**info, "reason": result.message})
            elif result.outcome == m.UNCHANGED:
                unchanged += 1
            elif result.outcome in (m.REFUSED, m.COLLISION):
                refused.append({**info, "reason": result.message})
            elif result.outcome == m.UNCERTAIN:
                uncertain.append({**info, "reason": result.message})
                halted = True
            else:
                failed.append({**info, "reason": result.message + (
                    f" {result.cleanup_message}" if result.cleanup_message else "")})
            # Every outcome that needs the operator (media_relocation.ATTENTION, or a staged entry that could not be
            # removed) stops the remaining Tracks.
            halted = halted or result.attention
            continue
        try:
            with transaction.atomic():
                track = (Track.objects.select_related("artist", "album", "genre", "category")
                         .select_for_update(of=("self",)).filter(pk=track_id).first())
                if track is None:
                    missing.append(track_id)
                    continue
                title, artist = track.title, track.artist.name if track.artist_id else ""
                additional, holidays = _memberships([track_id])
                current_additional, current_holidays = additional.get(track_id, set()), holidays.get(track_id, set())
                if state_then is None or edit_state(track, current_additional, current_holidays) != state_then:
                    stale.append({"id": track_id, "title": title, "artist": artist,
                                  "reason": "changed since the preview; left as it is"})
                    continue
                if disposition == "refused":
                    refused.append({"id": track_id, "title": title, "artist": artist,
                                    "reason": reason[0] if reason else "refused at preview"})
                    continue
                plan = _plan(track, ops, targets, current_additional=current_additional,
                             current_holidays=current_holidays)
                if plan.empty:
                    unchanged += 1
                    continue
                if file_location == MOVE_FILES and "category_id" in plan.columns and _needs_move(track, plan, targets):
                    # Previewed as needing no move, but its file is no longer in the new Category's folder.
                    stale.append({"id": track_id, "title": title, "artist": artist,
                                  "reason": "its file location changed since the preview; left as it is"})
                    continue
                _write_plan(track, plan)
                updated.append(track_id)
                folder_stays += plan.folder_stays
        except TrackWriteRefused as exc:
            refused.append({"id": track_id, "title": title, "artist": artist, "reason": str(exc)})
        except (ValidationError, DatabaseError) as exc:
            failed.append({"id": track_id, "title": title, "artist": artist,
                           "reason": f"not saved ({type(exc).__name__}); this Track was not changed"})
            log.warning("bulk_edit track_id=%s failed: %s", track_id, type(exc).__name__)
        except Exception as exc:                      # report it, keep going: the other Tracks are independent
            failed.append({"id": track_id, "title": title, "artist": artist,
                           "reason": f"unexpected error ({type(exc).__name__}); this Track was not changed"})
            log.exception("bulk_edit track_id=%s unexpected failure", track_id)
    log.info("bulk_edit user=%s selection=%s requested=%d updated=%d unchanged=%d refused=%d stale=%d "
             "failed=%d missing=%d", getattr(user, "pk", None), selection.mode,
             len(selection.ids) + len(selection.missing), len(updated), unchanged, len(refused), len(stale),
             len(failed), len(missing))
    # Each Track above committed on its own; the audit summary is written after the last of them.  Its outcome
    # is never "completed" while any Track was refused, stale, failed or missing.
    problems = [t["id"] for t in (*refused, *stale, *failed, *uncertain, *not_attempted, *cleanup_required)] \
        + sorted(set(missing))
    requested = len(selection.ids) + len(selection.missing)
    outcome = ("completed" if updated and not problems else "partial" if updated
               else "no_change" if not problems else "nothing_applied")
    action_id = library_audit.record(
        "bulk_edit.apply", user=user, outcome=outcome, level="info" if outcome in ("completed", "no_change") else "warning",
        title=f"Library bulk edit {outcome.replace('_', ' ')}: {len(updated)} of {requested} Track(s) updated",
        detail={"selection_mode": selection.mode, "requested": requested, "operations": operations,
                "updated": len(updated), "unchanged": unchanged, "refused": len(refused), "stale": len(stale),
                "failed": len(failed), "missing": len(set(missing)), "file_location": file_location,
                "attempted": attempted, "moved": len(moved), "cleanup_required": len(cleanup_required),
                "uncertain": len(uncertain),
                "not_attempted": len(not_attempted),
                "updated_tracks": library_audit.id_sample(updated),
                "problem_tracks": library_audit.id_sample(problems)})
    return {
        "ok": True,
        "outcome": outcome,
        "audit_action_id": action_id,
        "requested": len(selection.ids) + len(selection.missing),
        "updated": len(updated),
        "updated_ids": updated,
        "unchanged": unchanged,
        "refused": refused,
        "stale": stale,
        "failed": failed,
        "missing": sorted(set(missing)),
        "files_left_in_previous_category_folder": folder_stays,
        "file_location": file_location,
        "attempted": attempted,
        "moved": len(moved),
        "moved_tracks": moved,
        "cleanup_required": cleanup_required,
        "uncertain": uncertain,
        "not_attempted": not_attempted,
    }


def _needs_move(track, plan, targets) -> bool:
    """Move policy: does this Track's file (now) sit outside the new primary Category's folder?"""
    from library.services import media_relocation
    try:
        root = media_relocation.library_root()
    except media_relocation.Stop:
        return True
    return media_relocation.category_folder(track.filepath, root) != targets.category(plan.columns["category_id"]).code
