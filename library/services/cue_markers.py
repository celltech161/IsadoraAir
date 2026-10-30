"""The single authority for a Track's eight cue markers (roadmap 1.21, r0123).

Every change of a marker value, or of its protection, goes through this module:

* ``operator_edit``       -- an operator sets, approves (locks unchanged) or unlocks markers;
                             revision-checked against the browser's snapshot.
* ``apply_calculation``   -- analysis (automatic or operator-requested) or a writer that has
                             just published new media; checked against the revision the
                             calculation started from, and it never replaces a protected marker
                             unless the media itself was replaced or an operator confirmed it.
* ``copy_for_equivalent_media`` -- markers carried to a new Track holding byte-equivalent audio.

The model enforces it: ``Track.save()`` of an existing Track never writes marker columns outside
this authority (a stale instance cannot overwrite newer markers, and a save that changes one is
refused), and ``TrackQuerySet.update``/``bulk_update`` refuse them. A *new* Track may be created
with marker values (nothing exists to overwrite); without state they count as historical and are
protected. Writers that know their markers' provenance set them through this module instead. The
spoken projection's own grant (base QuerySet primitives) stays the owner of the columns it
projects; its guard refuses this module's writes to them, reported as a structured error.

Operating contract, including the analysis/cache rules Package B builds on: docs/CUE_MARKERS.md.

Protection of one marker, from ``cue_marker_state[name]``:

* ``locked``    -- an operator set or approved it. Analysis never replaces it; an operator may edit
                   it (current revision), and an operator-requested recalculation may replace it
                   only with an explicit, confirmed override.
* ``unlocked``  -- written by analysis or a generator, or deliberately unlocked: eligible for
                   recalculation.
* no entry      -- a value of unknown historical provenance ("legacy"): protected exactly like a
                   locked marker. Exceptions, which need no guess about authorship: an empty
                   marker (nothing to destroy), and Cue In at the column default 0 on a Track that
                   has never been analyzed (no waveform recorded).

* ``review``    -- an operator approval of a marker whose audio has since been replaced
                   (r0123 Package B): protected like ``locked`` but *not* an approval of the
                   current audio; the old approval is kept in ``previous_approval``.

Audio revisions (Package B): an entry records ``audio_revision``, the decoded-audio revision
(library.services.audio_identity) of the file it was set or approved for, when a trusted
waveform cache established it -- otherwise None ("unknown", never assumed to match anything).

New media (``media_replaced=True``) rebinds the markers it supplies: protection that applied to the
replaced audio is superseded, and that is audited. An operator approval is never silently
dropped or carried over: it is retired into the marker's ``previous_approval`` (with its value,
approver, time and audio revision). A locked marker the new media does not supply keeps its
value but becomes ``review``. Only an approval recorded for exactly the new audio revision (the
"new" media is the same decoded sound, e.g. a tag-only change) stays locked. Operator actions on a
marker (edit, approve, unlock) resolve its review and drop ``previous_approval``.
"""
from __future__ import annotations

import contextlib
import contextvars
import copy
import math
from dataclasses import dataclass, field

from django.db import transaction
from django.utils import timezone

MARKERS = (
    "cue_in_seconds", "cue_out_seconds", "next_start_seconds", "intro_until_seconds",
    "sweep_start_seconds", "outro_starts_seconds", "hook_in_seconds", "hook_out_seconds",
)
STATE_COLUMNS = ("cue_marker_state", "cue_marker_revision")
PROTECTED_COLUMNS = MARKERS + STATE_COLUMNS
DEFAULTS = {name: None for name in MARKERS} | {"cue_in_seconds": 0.0}

LOCKED, UNLOCKED, REVIEW = "locked", "unlocked", "review"
LEGACY, UNSET = "legacy", "unset"            # effective protection of a marker without an entry
PROTECTED = (LOCKED, REVIEW, LEGACY)          # never replaced by automatic analysis
#: Entry keys an operator action on the marker resolves.
_REVIEW_KEYS = ("previous_approval", "review_reason", "audio_changed_at")
SOURCE_OPERATOR, SOURCE_ANALYSIS, SOURCE_GENERATED, SOURCE_UNKNOWN = "operator", "analysis", "generated", "unknown"
SOURCES = (SOURCE_OPERATOR, SOURCE_ANALYSIS, SOURCE_GENERATED, SOURCE_UNKNOWN)

#: A marker may sit this far past the stored duration (analysis and probe durations differ slightly).
DURATION_TOLERANCE_SECONDS = 0.05
#: (earlier, later, strictly): ordering wherever both markers are set and either one changes.
ORDER = (
    ("cue_in_seconds", "cue_out_seconds", False),
    ("cue_in_seconds", "next_start_seconds", False),
    ("next_start_seconds", "cue_out_seconds", False),
    ("cue_in_seconds", "intro_until_seconds", False),
    ("outro_starts_seconds", "cue_out_seconds", False),
    ("hook_in_seconds", "hook_out_seconds", True),
)
LABELS = {
    "cue_in_seconds": "Cue In", "cue_out_seconds": "Cue Out", "next_start_seconds": "Next Start",
    "intro_until_seconds": "Intro", "sweep_start_seconds": "Sweep", "outro_starts_seconds": "Outro",
    "hook_in_seconds": "Hook In", "hook_out_seconds": "Hook Out",
}

_authority = contextvars.ContextVar("cue_marker_authority", default=False)


@contextlib.contextmanager
def _authorized():
    token = _authority.set(True)
    try:
        yield
    finally:
        _authority.reset(token)


def _refused(detail):
    from library.models import TrackWriteRefused
    return TrackWriteRefused(f"cue markers change only through library.services.cue_markers: {detail}")


# ------------------------------------------------------------------ model-level guards

def guard_columns(columns) -> None:
    """``QuerySet.update``/``bulk_update`` may not name a marker column outside the authority."""
    named = sorted(set(columns) & set(PROTECTED_COLUMNS))
    if named and not _authority.get():
        raise _refused(f"{named} in a bulk update")


def guard_new_track(track) -> None:
    """A new Track may carry marker values (nothing exists to overwrite yet); without a state entry
    they are of unknown provenance and therefore protected. It may not arrive with protection
    state or a revision of its own: those are only ever written by the authority."""
    if _authority.get():
        return
    if track.__dict__.get("cue_marker_state") or track.__dict__.get("cue_marker_revision"):
        raise _refused("a new Track cannot be created with cue-marker state or revision")


def remember_loaded(track) -> None:
    track.__dict__["_cue_marker_loaded"] = {
        name: copy.deepcopy(track.__dict__[name]) for name in PROTECTED_COLUMNS if name in track.__dict__}


def guard_track_save(track, kwargs: dict) -> dict:
    """``Track.save()`` outside the authority: refuse a marker change, and leave the marker columns
    out of the UPDATE so an instance loaded earlier cannot overwrite newer markers."""
    if _authority.get():
        return kwargs
    if track._state.adding or track.pk is None:
        guard_new_track(track)
        return kwargs
    present = [name for name in PROTECTED_COLUMNS if name in track.__dict__]
    loaded = track.__dict__.get("_cue_marker_loaded")
    if loaded is None:
        loaded = type(track)._base_manager.filter(pk=track.pk).values(*present).first() or {}
    changed = [name for name in present if name in loaded and not _same(track.__dict__[name], loaded[name])]
    if changed:
        raise _refused(f"Track {track.pk} save changes {changed}")
    update_fields = kwargs.get("update_fields")
    if update_fields is None:
        update_fields = [f.name for f in track._meta.concrete_fields
                         if not f.primary_key and f.attname in track.__dict__]
    kwargs = dict(kwargs)
    kwargs["update_fields"] = [name for name in update_fields if name not in PROTECTED_COLUMNS]
    return kwargs


# ------------------------------------------------------------------ protection and validation

def _same(a, b) -> bool:
    if a is None or b is None or isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a == b
    try:
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-6)
    except (TypeError, ValueError):
        return a == b


def never_analyzed(row) -> bool:
    return not (row.get("waveform_path") or "")


def protection_of(name, row) -> str:
    """``locked``, ``unlocked``, ``review``, ``legacy`` or ``unset`` for one marker of a Track row (dict)."""
    entry = (row.get("cue_marker_state") or {}).get(name)
    if isinstance(entry, dict) and entry.get("protection") in (LOCKED, UNLOCKED, REVIEW):
        return entry["protection"]
    value = row.get(name)
    if value is None:
        return UNSET
    if name == "cue_in_seconds" and _same(value, 0.0) and never_analyzed(row):
        return UNSET                      # the column default on a Track no analysis has touched
    return LEGACY


def coerce(name, value):
    """A submitted marker value as a float (or None); ValueError if it is not a number."""
    if name not in MARKERS:
        raise ValueError(f"{name!r} is not a cue marker")
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{LABELS[name]} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{LABELS[name]} must be a number") from None
    return number


def validate(final: dict, changed, duration) -> list[str]:
    """Problems with the changed markers of the resulting marker set (empty if valid)."""
    problems = []
    limit = float(duration) if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0 \
        else None
    for name in changed:
        value = final[name]
        if value is None:
            if name == "cue_in_seconds":
                problems.append("Cue In cannot be empty")
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            problems.append(f"{LABELS[name]} must be a finite number")
        elif value < 0:
            problems.append(f"{LABELS[name]} cannot be negative")
        elif limit is not None and value > limit + DURATION_TOLERANCE_SECONDS:
            problems.append(f"{LABELS[name]} ({value:.3f}s) is past the end of the track ({limit:.3f}s)")
    if problems:
        return problems
    for earlier, later, strict in ORDER:
        if earlier not in changed and later not in changed:
            continue
        a, b = final[earlier], final[later]
        if a is None or b is None:
            continue
        if a > b or (strict and a == b):
            relation = "before" if strict else "at or before"
            problems.append(f"{LABELS[earlier]} ({a:.3f}s) must be {relation} {LABELS[later]} ({b:.3f}s)")
    return problems


# ------------------------------------------------------------------ results

@dataclass
class Result:
    track_id: int
    ok: bool = True
    revision: int | None = None
    changed: dict = field(default_factory=dict)       # name -> [old, new]
    approved: list = field(default_factory=list)
    unlocked: list = field(default_factory=list)
    preserved: dict = field(default_factory=dict)     # name -> protection that kept it
    superseded: dict = field(default_factory=dict)    # name -> protection replaced with new media
    review: list = field(default_factory=list)        # locked markers kept for review after new media
    overridden: list = field(default_factory=list)
    skipped: dict = field(default_factory=dict)       # name -> reason (e.g. unchanged)
    conflict: str | None = None                       # stale revision
    error: str | None = None                          # refused or invalid

    def to_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items()}


def _now_iso(now):
    return now.isoformat()


def _user_id(user):
    return getattr(user, "pk", None) if user is not None and getattr(user, "is_authenticated", False) else None


def _resolved(entry) -> dict:
    """An entry with its review (and retired approval) resolved by an operator action."""
    return {key: value for key, value in (entry or {}).items() if key not in _REVIEW_KEYS}


def _approved_for(entry, audio_revision) -> bool:
    """A locked operator approval recorded for exactly this (known) decoded-audio revision."""
    return (audio_revision is not None and isinstance(entry, dict) and entry.get("protection") == LOCKED
            and entry.get("audio_revision") == audio_revision)


def _retired_approval(entry, value, now, reason, new_audio_revision) -> dict | None:
    """The durable record of an operator approval that no longer applies (None if there was none)."""
    if not isinstance(entry, dict):
        return None
    if entry.get("protection") == REVIEW:
        return entry.get("previous_approval")          # already retired; keep the original record
    if entry.get("protection") != LOCKED:
        return entry.get("previous_approval")          # carried until an operator resolves it
    record = {key: entry.get(key) for key in ("source", "set_at", "set_by", "approved_at", "approved_by",
                                               "audio_revision")}
    return {**record, "value": value, "retired_at": _now_iso(now), "retired_reason": reason,
            "retired_for_audio_revision": new_audio_revision}


def review(row, current_audio_revision=None) -> dict:
    """Per marker, for review screens (Package C): its protection and, for an approval, whether it
    was given for the current decoded audio (``current``), other audio (``other``), audio replaced
    since (``previous``) or audio never identified (``unknown``), plus any retired approval."""
    state = row.get("cue_marker_state") or {}
    out = {}
    for name in MARKERS:
        entry = state.get(name) if isinstance(state.get(name), dict) else {}
        protection = protection_of(name, row)
        info = {"protection": protection, "value": row.get(name)}
        if protection == LOCKED:
            recorded = entry.get("audio_revision")
            info["audio"] = ("current" if recorded and recorded == current_audio_revision
                             else "other" if recorded and current_audio_revision else "unknown")
        elif protection == REVIEW:
            info["audio"] = "previous"
        if entry.get("previous_approval"):
            info["previous_approval"] = entry["previous_approval"]
        if entry.get("audio_changed_at"):
            info["audio_changed_at"] = entry["audio_changed_at"]
        out[name] = info
    return out


def _emit(title, detail, level="info"):
    from monitoring.models import emit_event
    emit_event(category="library", level=level, title=title, detail=detail,
               dedupe_key=f"library|cue-markers|{detail.get('track_id')}|{detail.get('revision')}|{title}")


def _locked_row(track_id):
    from library.models import Track
    return (Track.objects.select_for_update().filter(pk=track_id)
            .values(*MARKERS, *STATE_COLUMNS, "duration_seconds", "waveform_path").first())


def _write(track_id, values: dict, state: dict, revision: int):
    from library.models import Track
    with _authorized():
        Track.objects.filter(pk=track_id).update(**values, cue_marker_state=state, cue_marker_revision=revision)


def _commit(result: Result, row, values: dict, state: dict, audit=None) -> Result:
    """Write a changed marker set (one revision step), or nothing when nothing changed."""
    from library.models import TrackWriteRefused
    current_state = row["cue_marker_state"] or {}
    if not values and state == current_state:
        result.revision = row["cue_marker_revision"]
        return result
    revision = row["cue_marker_revision"] + 1
    try:
        _write(result.track_id, values, state, revision)
    except TrackWriteRefused as exc:          # e.g. a column the spoken projection owns
        result.ok, result.error = False, str(exc)
        result.revision = row["cue_marker_revision"]
        result.changed = {}
        return result
    result.revision = revision
    if audit:
        title, level = audit
        detail = {"track_id": result.track_id, "revision": revision, **{
            key: value for key, value in result.to_dict().items()
            if key in ("changed", "approved", "unlocked", "superseded", "review", "overridden", "preserved")
            and value}}
        transaction.on_commit(lambda: _emit(title, detail, level))
    return result


# ------------------------------------------------------------------ operator edits

def operator_edit(track_id, *, expected_revision, user, values=None, approve=(), unlock=(),
                  audio_revision=None) -> Result:
    """Set (and lock) markers, approve unchanged ones (lock without editing) or unlock them, for
    the operator who saw ``expected_revision``. Only the named markers are touched.
    ``audio_revision``: the decoded-audio revision the operator is working on, when a trusted
    waveform cache establishes it (``waveform_cache.current_audio_revision``); else None."""
    result = Result(track_id=track_id)
    values = dict(values or {})
    approve, unlock = tuple(approve), tuple(unlock)
    unknown = sorted((set(values) | set(approve) | set(unlock)) - set(MARKERS))
    if unknown:
        result.ok, result.error = False, f"not cue markers: {unknown}"
        return result
    overlap = (set(values) & set(unlock)) | (set(approve) & set(unlock))
    if overlap:
        result.ok, result.error = False, f"cannot both set or approve and unlock {sorted(overlap)}"
        return result
    if not isinstance(expected_revision, int) or isinstance(expected_revision, bool):
        result.ok, result.error = False, "cue_marker_revision is required to change cue markers"
        return result
    try:
        values = {name: coerce(name, value) for name, value in values.items()}
    except ValueError as exc:
        result.ok, result.error = False, str(exc)
        return result
    now, who = timezone.now(), _user_id(user)
    with transaction.atomic():
        row = _locked_row(track_id)
        if row is None:
            result.ok, result.error = False, "Track not found"
            return result
        if row["cue_marker_revision"] != expected_revision:
            result.ok = False
            result.revision = row["cue_marker_revision"]
            result.conflict = (f"the cue markers changed since this page loaded (revision "
                               f"{expected_revision} -> {row['cue_marker_revision']}); reload before editing")
            return result
        state = copy.deepcopy(row["cue_marker_state"] or {})
        new_values = {}
        for name, value in values.items():
            entry = {"protection": LOCKED, "source": SOURCE_OPERATOR, "set_at": _now_iso(now), "set_by": who,
                     "approved_at": _now_iso(now), "approved_by": who, "audio_revision": audio_revision}
            if _same(row[name], value):
                if (state.get(name) or {}).get("protection") == LOCKED:
                    result.skipped[name] = "unchanged and already locked"
                    continue
                previous = _resolved(state.get(name)) or {"source": SOURCE_UNKNOWN}
                state[name] = {**previous, "protection": LOCKED, "approved_at": _now_iso(now), "approved_by": who,
                               "audio_revision": audio_revision}
                result.approved.append(name)
                continue
            new_values[name] = value
            state[name] = entry
            result.changed[name] = [row[name], value]
        for name in approve:
            previous = state.get(name) or {"source": SOURCE_UNKNOWN}
            if previous.get("protection") == LOCKED and previous.get("approved_by") is not None \
                    and (audio_revision is None or previous.get("audio_revision") == audio_revision):
                result.skipped[name] = "already approved and locked"
                continue
            state[name] = {**_resolved(previous), "protection": LOCKED, "approved_at": _now_iso(now),
                           "approved_by": who, "audio_revision": audio_revision}
            result.approved.append(name)
        for name in unlock:
            previous = state.get(name) or {"source": SOURCE_UNKNOWN}
            if previous.get("protection") == UNLOCKED:
                result.skipped[name] = "already unlocked"
                continue
            state[name] = {**_resolved(previous), "protection": UNLOCKED, "unlocked_at": _now_iso(now),
                           "unlocked_by": who}
            result.unlocked.append(name)
        final = {name: new_values.get(name, row[name]) for name in MARKERS}
        problems = validate(final, set(new_values), row["duration_seconds"])
        if problems:
            result.ok, result.error = False, "; ".join(problems)
            result.changed = {}
            result.approved, result.unlocked = [], []
            result.revision = row["cue_marker_revision"]
            return result
        return _commit(result, row, new_values, state, audit=("Cue markers changed by operator", "info"))


# ------------------------------------------------------------------ calculations

def apply_calculation(track_id, values: dict, *, basis_revision, source=SOURCE_ANALYSIS, requested_by=None,
                      override=(), override_confirmed=False, media_replaced=False, audio_revision=None,
                      duration=None) -> Result:
    """Apply calculated markers (analysis, or a writer that knows its own media's markers).

    ``basis_revision`` is the marker revision read before the calculation started; if the markers
    changed since (an operator edit, another calculation), nothing is applied (stale). Without
    ``media_replaced`` a locked, review or legacy marker is preserved, unless ``requested_by`` (an
    operator) names it in ``override`` with ``override_confirmed``. ``duration`` (the media's own
    duration) bounds the values; default: the Track's stored duration. ``audio_revision``: the
    decoded-audio revision the values were calculated from (None if unknown); with
    ``media_replaced`` it decides which approvals still apply (see the module docstring)."""
    result = Result(track_id=track_id)
    unknown = sorted(set(values) - set(MARKERS))
    if unknown or source not in SOURCES or source == SOURCE_OPERATOR:
        result.ok, result.error = False, f"invalid calculation (markers {unknown}, source {source!r})"
        return result
    override = tuple(override)
    if override and (requested_by is None or not override_confirmed or set(override) - set(values)):
        result.ok, result.error = False, ("an override of protected markers needs an operator request, the "
                                          "markers to override among the calculated ones, and explicit confirmation")
        return result
    now, who = timezone.now(), _user_id(requested_by)
    with transaction.atomic():
        row = _locked_row(track_id)
        if row is None:
            result.ok, result.error = False, "Track not found"
            return result
        if row["cue_marker_revision"] != basis_revision:
            result.ok = False
            result.revision = row["cue_marker_revision"]
            result.conflict = (f"stale calculation: markers changed from revision {basis_revision} to "
                               f"{row['cue_marker_revision']} while it ran; nothing applied")
            transaction.on_commit(lambda: _emit("Stale cue-marker calculation discarded", {
                "track_id": track_id, "revision": row["cue_marker_revision"], "basis_revision": basis_revision,
                "source": source}))
            return result
        state = copy.deepcopy(row["cue_marker_state"] or {})
        new_values = {}
        for name, value in values.items():
            protection = protection_of(name, row)
            previous = state.get(name) if isinstance(state.get(name), dict) else None
            replacing = media_replaced and not _approved_for(previous, audio_revision)
            if replacing:
                if protection in PROTECTED and not _same(row[name], value):
                    result.superseded[name] = protection
            elif protection in PROTECTED:
                if name in override:
                    result.overridden.append(name)
                else:
                    result.preserved[name] = protection
                    continue
            if _same(row[name], value) and not replacing and name not in override:
                result.skipped[name] = "unchanged"
                continue
            entry = {"protection": UNLOCKED, "source": source, "set_at": _now_iso(now), "set_by": who,
                     "audio_revision": audio_revision}
            if name in override:
                entry["overridden_from"] = state.get(name) or {"source": SOURCE_UNKNOWN}
            retired = _retired_approval(previous, row[name], now, "new_media" if replacing else "recalculated",
                                        audio_revision)
            if retired:
                entry["previous_approval"] = retired
            if replacing and protection == LEGACY and not _same(row[name], value):
                entry["replaced_historical_value"] = {"value": row[name], "replaced_at": _now_iso(now)}
            state[name] = entry
            if not _same(row[name], value):
                new_values[name] = value
                result.changed[name] = [row[name], value]
        if media_replaced:
            # Markers the new media does not supply keep their values, but an approval of the old
            # audio is not an approval of this one, and every carried value is marked as older.
            for name in MARKERS:
                previous = state.get(name) if isinstance(state.get(name), dict) else None
                if name in values or row[name] is None or _approved_for(previous, audio_revision):
                    continue
                protection = protection_of(name, row)
                if protection == LOCKED:
                    state[name] = {"protection": REVIEW, "source": previous.get("source", SOURCE_UNKNOWN),
                                   "set_at": previous.get("set_at"), "set_by": previous.get("set_by"),
                                   "previous_approval": _retired_approval(previous, row[name], now, "new_media",
                                                                          audio_revision),
                                   "review_reason": "the audio was replaced after this marker was approved",
                                   "audio_changed_at": _now_iso(now)}
                    result.review.append(name)
                else:
                    state[name] = {**(previous or {}), "audio_changed_at": _now_iso(now)}
        final = {name: new_values.get(name, row[name]) for name in MARKERS}
        problems = validate(final, set(new_values), duration if duration is not None else row["duration_seconds"])
        if problems:
            result.ok, result.error = False, "; ".join(problems)
            result.changed, result.superseded, result.overridden = {}, {}, []
            result.revision = row["cue_marker_revision"]
            return result
        audit = None
        if result.superseded or result.review:
            audit = ("Cue-marker protection superseded by new media", "warning")
        elif result.overridden:
            audit = ("Protected cue markers recalculated on operator request", "info")
        return _commit(result, row, new_values, state, audit=audit)


def copy_for_equivalent_media(source_id, target_id, names, *, source_identity=None, target_identity=None) -> Result:
    """Carry markers to a newly created Track whose audio should be the source's (a lossless
    transcode). The target must be untouched, and equivalence is never inferred from paths or
    Track ids: only decoded-audio identities (library.services.audio_identity) of both files prove it.

    * proven equal: values with their protection and provenance, recorded as verified;
    * proven different: refused -- this is not equivalent media;
    * not provable (an identity is unavailable): values are carried as historical (protected, of
      unknown provenance) and any operator approval only as ``previous_approval``, never as an
      approval of the new file."""
    from library.services import audio_identity
    result = Result(track_id=target_id)
    names = tuple(names)
    if set(names) - set(MARKERS):
        result.ok, result.error = False, f"not cue markers: {sorted(set(names) - set(MARKERS))}"
        return result
    if source_id == target_id:
        result.ok, result.error = False, "a Track is not equivalent media of itself"
        return result
    verified = audio_identity.same_audio(source_identity, target_identity)
    if source_identity is not None and target_identity is not None and not verified:
        result.ok, result.error = False, "the two files' decoded audio differs: not equivalent media"
        return result
    revision = audio_identity.revision(target_identity) if verified else None
    from library.models import Track
    now = timezone.now()
    with transaction.atomic():
        source = Track.objects.filter(pk=source_id).values(*MARKERS, "cue_marker_state").first()
        row = _locked_row(target_id)
        if source is None or row is None:
            result.ok, result.error = False, "Track not found"
            return result
        if row["cue_marker_revision"] != 0:
            result.ok, result.error = False, "the target Track's markers were already set"
            return result
        state = copy.deepcopy(row["cue_marker_state"] or {})
        values = {}
        for name in names:
            if source[name] is None and name != "cue_in_seconds":
                continue
            values[name] = source[name]
            entry = (source["cue_marker_state"] or {}).get(name)
            entry = entry if isinstance(entry, dict) else None
            if verified:
                # Without an entry the value stays historical (protected, unknown provenance) on the copy.
                state[name] = copy.deepcopy(entry) if entry else {"source": SOURCE_UNKNOWN}
                state[name]["equivalence"] = {"verified": True, "audio_revision": revision}
            else:
                state[name] = {"source": SOURCE_UNKNOWN, "equivalence": {"verified": False}}
                retired = _retired_approval(entry, source[name], now, "unverified_copy", None)
                if retired:
                    state[name]["previous_approval"] = retired
            state[name]["copied_from_track"] = source_id
            if not _same(row[name], values[name]):
                result.changed[name] = [row[name], values[name]]
        return _commit(result, row, values, state)


# ------------------------------------------------------------------ analysis requests

def request_analysis(queryset) -> int:
    """Ask the analyzer to (re)analyze these Tracks. Markers are not touched."""
    return queryset.update(analysis_requested_at=timezone.now())


def snapshot(track_id) -> dict | None:
    """The current markers, their state and revision (no lock)."""
    from library.models import Track
    return Track.objects.filter(pk=track_id).values(*MARKERS, *STATE_COLUMNS, "duration_seconds",
                                                    "waveform_path", "analysis_requested_at").first()
