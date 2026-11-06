"""Resolve exact duplicate Track pairs from the Library Reconciliation page (roadmap 3.12).

An operator reviews two registered Tracks whose files the latest scan found byte-identical, chooses which one
to keep, optionally carries the discarded Track's categories over to the survivor as additional categories,
previews the consequences and deletes the duplicate.  This is a small workflow over existing mechanisms, not a
duplicate-management subsystem:

* **Eligible findings only.**  A duplicate finding of the latest *complete* scan with ``exact`` confidence
  (identical bytes, hashed in that scan) and exactly two registered Tracks.  Groups of three or more Tracks,
  likely / audio-equivalent / uncertain findings and file-only groups stay review-only.
* **Decisions.**  Per pair: keep A and delete B, keep B and delete A, keep both (the existing non-destructive
  ``DuplicateCandidate`` ``keep_both`` record, unchanged), or nothing.  No ``keep_a`` / ``keep_b`` row is ever
  written, so ``apply_duplicate_resolutions`` has nothing new to act on; deleting a Track removes its legacy
  ``DuplicateCandidate`` rows (CASCADE), including a pending legacy decision.
* **Preview.**  Writes nothing.  For each selection it reports both Tracks, the survivor's suggested additional
  categories, the discarded Track's references and whether the deletion is eligible, needs an explicit
  acknowledgment (it has VoiceTracks, which the deletion would remove with it) or is blocked.  It returns a
  token signed with ``django.core.signing``, bound to the operator, valid ``TOKEN_MAX_AGE_SECONDS``, naming the
  scan, the pairs and what was previewed of both Tracks (columns, categories and file identity).
* **Apply.**  Each pair is independent and is its own transaction: lock both Track rows (in id order), recheck
  everything against the current state and the preview, add the chosen categories to the survivor, delete the
  discarded Track through ``library.services.track_deletion.delete_track`` (PROTECT references, publication
  ownership and commit-safe source cleanup unchanged).  The category additions and the deletion commit
  together or not at all.  One blocked, stale or failing pair neither blocks nor rolls back the others.
  Anything changed since the preview refuses that pair; nothing is guessed.

Blocked (never deleted here): a discarded Track in a playlist or a rotation slot (PROTECT), in the log of the
current or a later hour (or the engine's active log), on air or claimed by the engine, protected by another
subsystem (spoken projection, Active Messages), remote-hosted, or whose file -- or the survivor's -- no longer
matches what the scan hashed.  Logs are never modified or regenerated.

The duplicate evidence is the scan's: identical SHA-256 for both files at the size and modification time the
scan recorded.  Nothing is re-hashed in the web worker; at apply both files must still be regular files at the
same path, with the size and modification time the scan hashed, and with the same identity (device, inode,
size, mtime, ctime) the preview saw.  A rewrite that preserves size and modification time is not detected.
"""
from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass, field
from datetime import timedelta

from django.core import signing
from django.db import DatabaseError, OperationalError, connections, router, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from library.services import library_reconciliation

log = logging.getLogger(__name__)

TOKEN_SALT = "library.reconciliation.duplicate_resolution.v1"
TOKEN_MAX_AGE_SECONDS = 10 * 60
#: Most selections one preview or apply handles: one findings page (the page shows 25 findings).
MAX_SELECTIONS = 25
#: Bounded reference listings in a preview (the full counts are always given).
REFERENCE_SAMPLE = 10
#: A LogItem the engine claimed this recently, but has not yet played, may be loaded on a deck.
CLAIM_WINDOW = timedelta(hours=3)
#: A Track that started this long ago plus its duration may still be on air.
PLAY_MARGIN = timedelta(minutes=10)
#: Longest wait for a row another operation holds: the request answers "busy" instead of tying up a worker.
LOCK_TIMEOUT = "5s"

DELETE, KEEP_BOTH = "delete", "keep_both"
ELIGIBLE, NEEDS_ACK, BLOCKED = "eligible", "needs_acknowledgment", "blocked"
TRACK_COLUMNS = ("filepath", "filename", "format", "title", "artist_id", "album_id", "duration_seconds",
                 "category_id", "remote_url")


class ResolutionRefused(Exception):
    """The whole request is refused; nothing was changed."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- eligibility (from the report)

def finding_eligibility(finding) -> tuple[bool, str]:
    """Whether the scan's finding may be resolved by deletion here, and if not, why (review only)."""
    if finding.get("type") != "duplicate":
        return False, "not a duplicate finding"
    members = finding.get("members") or []
    confidence = finding.get("confidence")
    if confidence != "exact":
        return False, ("only identical-byte (exact) findings can be resolved here; this one is "
                       f"{str(confidence or 'unknown').replace('_', ' ')}: review it by hand")
    if len(members) < 2:
        return False, "file review only: fewer than two registered Tracks"
    if len(members) > 2 or finding.get("member_count", len(members)) != 2:
        return False, f"{finding.get('member_count', len(members))} Tracks share these bytes: resolve by hand"
    evidence = _evidence(finding)
    if evidence is None:
        return False, "the scan did not record identical-byte evidence for both Tracks"
    return True, ""


def _evidence(finding) -> dict | None:
    """{track_id: file entry} for the two members, when both carry the same scan-time SHA-256."""
    members = finding.get("members") or []
    by_track = {}
    for entry in finding.get("files") or []:
        track_id = entry.get("track_id")
        if track_id in members:
            if track_id in by_track:
                return None
            by_track[track_id] = entry
    if len(members) != 2 or set(by_track) != set(members):
        return None
    digests = {by_track[t].get("sha256") for t in members}
    if len(digests) != 1 or not all(isinstance(d, str) and len(d) == 64 for d in digests):
        return None
    for entry in by_track.values():
        if not isinstance(entry.get("path"), str) or not isinstance(entry.get("size"), int) \
                or not isinstance(entry.get("mtime_ns"), int):
            return None
    return by_track


# --------------------------------------------------------------------------- current state

def _file_identity(path):
    """(dev, ino, size, mtime_ns, ctime_ns) of exactly this regular file (no symlink followed), or a reason."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, "its file no longer exists"
    except OSError as exc:
        return None, f"its file cannot be checked ({exc.strerror or type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return None, "its path is no longer a regular file"
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns], ""


def _additional(track_ids) -> dict:
    from library.models import Track
    out = {t: [] for t in track_ids}
    for track_id, category_id in Track.additional_categories.through.objects.filter(
            track_id__in=list(track_ids)).values_list("track_id", "category_id"):
        out[track_id].append(category_id)
    return {t: sorted(v) for t, v in out.items()}


def _fingerprint(track, additional, identity) -> dict:
    """Everything this workflow relies on about one Track: compared between preview and apply."""
    return {"columns": [getattr(track, name) for name in TRACK_COLUMNS], "additional": list(additional),
            "file": identity}


def _categories(ids) -> dict:
    from library.models import Category
    return {c.pk: c for c in Category.objects.filter(pk__in=[i for i in ids if i is not None])}


def _now():
    return timezone.now()


def _local_hour(now):
    local = timezone.localtime(now)
    return local.date(), local.hour


def _engine_state():
    try:
        from webrequests.services import _read_engine_state
        return _read_engine_state()
    except Exception:                       # unreadable engine state: rely on the database checks alone
        return None


def _log_url(date, hour) -> str:
    return f"/logs/?date={date.isoformat()}&hour={hour}&return=reconciliation"


def _upcoming_logs(track_id, now, engine):
    """Logs of the current or a later hour (and the engine's active log) that schedule this Track."""
    from library.models import LogItem

    today, hour = _local_hour(now)
    window = Q(playlist_log__date__gt=today) | Q(playlist_log__date=today, playlist_log__hour__gte=hour)
    active_log = (engine or {}).get("log_id")
    if isinstance(active_log, int) and not isinstance(active_log, bool):
        window |= Q(playlist_log_id=active_log)
    rows = {}
    for log_id, date, log_hour, status in (LogItem.objects.filter(track_id=track_id).filter(window)
                                           .values_list("playlist_log_id", "playlist_log__date",
                                                        "playlist_log__hour", "playlist_log__status")):
        entry = rows.setdefault(log_id, {"date": date.isoformat(), "hour": log_hour, "status": status,
                                         "items": 0, "url": _log_url(date, log_hour)})
        entry["items"] += 1
    return sorted(rows.values(), key=lambda e: (e["date"], e["hour"]))


def _on_air(track, now, engine) -> list[str]:
    """Reasons to think the Track is on air, loaded on a deck or about to be (empty when none)."""
    from library.models import LogItem, PlayEvent

    reasons = []
    for deck in ((engine or {}).get("decks") or {}).values():
        if isinstance(deck, dict) and deck.get("track_id") == track.pk:
            reasons.append("it is loaded on a playback deck")
            break
    if any(isinstance(q, dict) and q.get("track_id") == track.pk for q in (engine or {}).get("queue") or []):
        reasons.append("it is in the engine's upcoming queue")
    span = timedelta(seconds=float(track.duration_seconds or 3600)) + PLAY_MARGIN
    if LogItem.objects.filter(track_id=track.pk, played_at__isnull=True, playback_claimed_at__gte=now - CLAIM_WINDOW).exists():
        reasons.append("the engine has claimed a log entry for it that has not played yet")
    if LogItem.objects.filter(track_id=track.pk, played_at__gte=now - span).exists() or PlayEvent.objects.filter(
            track_id=track.pk, ended_at__isnull=True, started_at__gte=now - span).exists():
        reasons.append("it started playing recently and may still be on air")
    return reasons


def _references(track_id) -> dict:
    from library.models import PlaylistItem, RotationSlot, VoiceTrack
    playlists = sorted(set(PlaylistItem.objects.filter(track_id=track_id).values_list("playlist__name", flat=True)))
    rotations = sorted(set(RotationSlot.objects.filter(track_id=track_id).values_list("rotation__name", flat=True)))
    return {"playlists": playlists[:REFERENCE_SAMPLE], "playlist_count": len(playlists),
            "rotations": rotations[:REFERENCE_SAMPLE], "rotation_count": len(rotations),
            "voicetracks": VoiceTrack.objects.filter(track_id=track_id).count()}


def _describe(track, additional, categories, identity, references, upcoming) -> dict:
    primary = categories.get(track.category_id)
    return {
        "id": track.pk, "url": f"/track/{track.pk}/", "title": track.title,
        "artist": track.artist.name if track.artist_id else "", "album": track.album.title if track.album_id else "",
        "filepath": track.filepath, "rel": _relative(track.filepath), "format": track.format,
        "duration": track.duration_seconds, "size": identity[2] if identity else None,
        "category": _cat(primary), "additional_categories": [_cat(categories.get(c)) for c in additional
                                                             if categories.get(c) is not None],
        "ready2air": track.ready2air, "play_count": track.play_count,
        "references": {**references, "upcoming_logs": upcoming[:REFERENCE_SAMPLE],
                       "upcoming_log_count": len(upcoming),
                       "upcoming_log_items": sum(e["items"] for e in upcoming)},
    }


def _cat(category):
    return None if category is None else {"id": category.pk, "code": category.code, "name": category.name}


def _relative(path) -> str:
    root = os.path.realpath(library_reconciliation.library_root())
    if isinstance(path, str) and path.startswith(root + os.sep):
        return os.path.relpath(path, root)
    return path or ""


def _protection():
    return library_reconciliation._protection_predicates()


def _owner(track, protection) -> str | None:
    path_owner, track_owner = protection
    owner = track_owner(track.pk)
    if owner:
        return owner
    if track.remote_url:
        return "remote-hosted Track"
    root = os.path.realpath(library_reconciliation.library_root())
    path = track.filepath or ""
    if not path.startswith(root + os.sep) or os.path.realpath(path) != path:
        return "its file is not a canonical path inside the Library"
    return path_owner(path, os.path.relpath(path, root), path)


@dataclass
class _Inspection:
    """The current state of one pair, and what stands in the way of deleting the discarded Track."""
    keep: object = None
    discard: object = None
    fingerprints: dict = field(default_factory=dict)
    suggestions: list = field(default_factory=list)
    blockers: list = field(default_factory=list)
    voicetracks: int = 0
    warnings: list = field(default_factory=list)
    described: dict = field(default_factory=dict)


def _inspect(finding, keep_id, discard_id, *, protection, now, engine, describe=True, tracks=None) -> _Inspection:
    """Everything a deletion depends on, read now.  ``tracks``: rows the caller has already locked."""
    from library.models import Track

    out = _Inspection()
    evidence = _evidence(finding) or {}
    if tracks is None:
        tracks = {t.pk: t for t in Track.objects.select_related("artist", "album").filter(pk__in=(keep_id, discard_id))}
    keep, discard = tracks.get(keep_id), tracks.get(discard_id)
    if keep is None or discard is None:
        out.blockers.append(("the kept" if keep is None else "the discarded") + " Track no longer exists")
        return out
    out.keep, out.discard = keep, discard
    additional = _additional((keep_id, discard_id))
    identities = {}
    for role, track in (("kept", keep), ("discarded", discard)):
        scanned = evidence.get(track.pk) or {}
        identity, problem = _file_identity(track.filepath) if track.filepath else (None, "it has no file path")
        identities[track.pk] = identity
        if track.filepath != scanned.get("path"):
            out.blockers.append(f"the {role} Track's file path changed since the scan; run a new scan")
        elif identity is None:
            out.blockers.append(f"the {role} Track: {problem}")
        elif (identity[2], identity[3]) != (scanned.get("size"), scanned.get("mtime_ns")):
            out.blockers.append(f"the {role} Track's file changed since the scan compared it; run a new scan")
        owner = _owner(track, protection)
        if owner:
            out.blockers.append(f"the {role} Track is protected: {owner}")
    if identities.get(keep_id) and identities.get(discard_id) and \
            identities[keep_id][:2] == identities[discard_id][:2]:
        out.blockers.append("both Tracks now name the same file")
    out.fingerprints = {str(t.pk): _fingerprint(t, additional[t.pk], identities[t.pk]) for t in (keep, discard)}

    have = set(additional[keep_id]) | {keep.category_id}
    offered = [c for c in [discard.category_id, *additional[discard_id]] if c is not None and c not in have]
    out.suggestions = sorted(set(offered))

    references = {t.pk: _references(t.pk) for t in (keep, discard)}
    upcoming = {t.pk: _upcoming_logs(t.pk, now, engine) for t in (keep, discard)}
    refs = references[discard_id]
    if refs["playlist_count"]:
        out.blockers.append("the discarded Track is in playlist(s) " + ", ".join(f"'{n}'" for n in refs["playlists"])
                            + (" and more" if refs["playlist_count"] > len(refs["playlists"]) else "")
                            + ": remove it from them first")
    if refs["rotation_count"]:
        out.blockers.append("the discarded Track is a direct insert in rotation(s) "
                            + ", ".join(f"'{n}'" for n in refs["rotations"])
                            + (" and more" if refs["rotation_count"] > len(refs["rotations"]) else "")
                            + ": remove it from them first")
    if upcoming[discard_id]:
        out.blockers.append(f"the discarded Track is scheduled in {len(upcoming[discard_id])} current or upcoming "
                            "log hour(s): edit those logs first (nothing here changes a log)")
    out.blockers.extend(f"the discarded Track may be on air: {reason}" for reason in _on_air(discard, now, engine))
    out.voicetracks = refs["voicetracks"]
    if discard.ready2air and not keep.ready2air:
        out.warnings.append("The discarded Track is Ready to Air and the kept one is not; the kept Track's "
                            "readiness is not changed.")
    if describe:
        categories = _categories({keep.category_id, discard.category_id, *additional[keep_id],
                                  *additional[discard_id], *out.suggestions})
        out.described = {
            "keep_track": _describe(keep, additional[keep_id], categories, identities[keep_id], references[keep_id],
                              upcoming[keep_id]),
            "discard_track": _describe(discard, additional[discard_id], categories, identities[discard_id],
                                 references[discard_id], upcoming[discard_id]),
            "suggested_categories": [_cat(categories[c]) for c in out.suggestions if c in categories],
        }
    return out


# --------------------------------------------------------------------------- requests

def _report(scan_id):
    report = library_reconciliation.load_report()
    if report is None or report.get("scan_id") != scan_id:
        raise ResolutionRefused("A newer scan replaced the one this page shows. Reload the page.")
    if report.get("status") != "complete":
        raise ResolutionRefused("The latest scan is not complete (the Library storage may have been unavailable); "
                                "run a new scan before resolving duplicates.")
    return report


def _parse_selections(raw):
    if not isinstance(raw, list) or not raw:
        raise ResolutionRefused("Select at least one duplicate pair.", status=400)
    if len(raw) > MAX_SELECTIONS:
        raise ResolutionRefused(f"At most {MAX_SELECTIONS} pairs at a time (one page of findings).", status=400)
    parsed = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("finding_id"), str):
            raise ResolutionRefused("Each selection needs a finding_id.", status=400)
        action = item.get("action")
        if action == KEEP_BOTH:
            parsed.append({"finding_id": item["finding_id"], "action": KEEP_BOTH})
            continue
        if action != DELETE:
            raise ResolutionRefused("Each selection's action is 'delete' or 'keep_both'.", status=400)
        keep, discard = item.get("keep"), item.get("discard")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (keep, discard)):
            raise ResolutionRefused("A deletion names the Track to keep and the Track to discard.", status=400)
        parsed.append({"finding_id": item["finding_id"], "action": DELETE, "keep": keep, "discard": discard})
    return parsed


def _conflicts(selections) -> set:
    """Indexes of selections that name a finding or a Track another selection of the batch also names."""
    seen_findings, seen_tracks, bad = {}, {}, set()
    for index, item in enumerate(selections):
        keys = [("finding", item["finding_id"])]
        if item["action"] == DELETE:
            keys += [("track", item["keep"]), ("track", item["discard"])]
        for key in keys:
            store = seen_findings if key[0] == "finding" else seen_tracks
            if key[1] in store:
                bad.update({index, store[key[1]]})
            else:
                store[key[1]] = index
    return bad


def _finding(report, finding_id):
    return next((f for f in report["findings"] if f.get("id") == finding_id and f.get("type") == "duplicate"), None)


def _legacy_decision(finding, a, b):
    low, high = sorted((a, b))
    decision = (finding.get("decisions") or {}).get(f"{low}-{high}")
    return decision if isinstance(decision, dict) else None


def preview(scan_id, raw_selections, *, user) -> dict:
    """What the selected resolutions would do now, and the token apply needs.  Writes nothing."""
    report = _report(scan_id)
    selections = _parse_selections(raw_selections)
    conflicts = _conflicts(selections)
    protection, now, engine = _protection(), _now(), _engine_state()
    items, planned = [], []
    for index, sel in enumerate(selections):
        finding = _finding(report, sel["finding_id"])
        item = {"finding_id": sel["finding_id"], "action": sel["action"]}
        items.append(item)
        if index in conflicts:
            item.update(status=BLOCKED, reasons=["another selection in this batch names the same finding or Track; "
                                                 "choose one resolution per Track"])
            continue
        if finding is None:
            item.update(status=BLOCKED, reasons=["this finding is not a duplicate group of the current scan"])
            continue
        members = finding.get("members") or []
        if sel["action"] == KEEP_BOTH:
            if len(members) != 2:
                item.update(status=BLOCKED, reasons=["Keep both is offered here only for a pair of two Tracks"])
                continue
            decision = _legacy_decision(finding, *members)
            item.update(status=ELIGIBLE, track_a=members[0], track_b=members[1], legacy_decision=decision)
            if decision and decision.get("resolution") in ("keep_a", "keep_b") and not decision.get("applied"):
                item["warnings"] = [f"A legacy decision ({decision['resolution']}) is recorded for this pair and is "
                                    "left unchanged; apply_duplicate_resolutions could still act on it."]
            planned.append({"f": sel["finding_id"], "a": KEEP_BOTH, "t": sorted(members)})
            continue
        eligible, why = finding_eligibility(finding)
        if not eligible:
            item.update(status=BLOCKED, reasons=[why])
            continue
        if {sel["keep"], sel["discard"]} != set(members) or sel["keep"] == sel["discard"]:
            item.update(status=BLOCKED, reasons=["those Tracks are not the two Tracks of this finding"])
            continue
        inspection = _inspect(finding, sel["keep"], sel["discard"], protection=protection, now=now, engine=engine)
        item.update(keep=sel["keep"], discard=sel["discard"], **inspection.described,
                    voicetracks=inspection.voicetracks, warnings=inspection.warnings,
                    legacy_decision=_legacy_decision(finding, sel["keep"], sel["discard"]))
        if inspection.blockers:
            item.update(status=BLOCKED, reasons=inspection.blockers)
            continue
        item.update(status=NEEDS_ACK if inspection.voicetracks else ELIGIBLE, reasons=[])
        planned.append({"f": sel["finding_id"], "a": DELETE, "k": sel["keep"], "d": sel["discard"],
                        "fp": inspection.fingerprints, "c": inspection.suggestions, "v": inspection.voicetracks})
    counts = {s: sum(1 for i in items if i["status"] == s) for s in (ELIGIBLE, NEEDS_ACK, BLOCKED)}
    token = signing.dumps({"u": user.pk, "s": scan_id, "p": planned}, salt=TOKEN_SALT, compress=True) if planned \
        else None
    return {"scan_id": scan_id, "items": items, "counts": counts,
            "deletions": sum(1 for p in planned if p["a"] == DELETE), "token": token,
            "expires_in": TOKEN_MAX_AGE_SECONDS}


def _redeem(token, user) -> dict:
    if not isinstance(token, str) or not token:
        raise ResolutionRefused("Preview the selection first.", status=400)
    try:
        plan = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise ResolutionRefused("This preview has expired; preview the selection again.") from None
    except signing.BadSignature:
        raise ResolutionRefused("This is not a valid preview token.", status=400) from None
    if not isinstance(plan, dict) or plan.get("u") != user.pk:
        raise ResolutionRefused("This preview was made by another operator.", status=403)
    return plan


def _parse_choices(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ResolutionRefused("choices must be an object keyed by finding id.", status=400)
    out = {}
    for finding_id, choice in raw.items():
        if not isinstance(choice, dict):
            raise ResolutionRefused("Each choice must be an object.", status=400)
        categories = choice.get("categories", [])
        if not isinstance(categories, list) or not all(isinstance(c, int) and not isinstance(c, bool)
                                                       for c in categories):
            raise ResolutionRefused("categories must be a list of category ids.", status=400)
        out[finding_id] = {"include": choice.get("include", True) is True, "categories": set(categories),
                           "acknowledge_voicetracks": choice.get("acknowledge_voicetracks") is True}
    return out


CLEANUP_TEXT = {
    "removed": ("completed", "The discarded file was removed."),
    "missing": ("completed", "The discarded file was already gone."),
    "not_requested": ("retained", "No file removal was requested for this Track."),
    "pending": ("pending", "File removal is pending; its outcome is recorded in the application log."),
    "failed": ("failed", "The Track was deleted, but removing its file failed; remove the file by hand."),
    "retained_live_owner": ("retained", "The file was kept: another Track now uses the same path."),
    "retained_replaced": ("retained", "The file was kept: it changed or was replaced at the moment of deletion."),
    "retained_recent": ("retained", "The file was kept: it changed within the last minute."),
    "retained_busy": ("retained", "The file was kept: another operation was registering the same path."),
    "retained_unverified": ("retained", "The file was kept: its identity could not be verified."),
    "retained_invalid_path": ("retained", "The file was kept: its stored path is not canonical."),
}


def _cleanup(result) -> dict:
    status = getattr(result, "status", "not_requested")
    kind, text = CLEANUP_TEXT.get(status, ("retained", f"The file was kept ({status})."))
    return {"status": status, "outcome": kind, "message": text}


def apply(token, raw_choices, *, confirm, user) -> dict:
    """Apply a previewed selection, one pair at a time (see the module docstring).  Every outcome is reported."""
    from library.services import library_audit

    try:
        plan = _redeem(token, user)
        choices = _parse_choices(raw_choices)
        deletions = [p for p in plan["p"] if p["a"] == DELETE and choices.get(p["f"], {}).get("include", True)]
        if deletions and confirm is not True:
            raise ResolutionRefused("Confirm the permanent deletion first.", status=400)
        report = _report(plan["s"])
    except ResolutionRefused as exc:
        # Nothing was applied; kept for troubleshooting (an expired, foreign, unconfirmed or outdated preview).
        library_audit.record(
            "reconciliation.duplicate_resolution", user=user, outcome="refused", level="warning",
            title=f"Library duplicate resolution refused: {exc}"[:200], detail={"reason": str(exc), "status": exc.status})
        raise
    protection = _protection()
    results = []
    for entry in plan["p"]:
        choice = choices.get(entry["f"], {"include": True, "categories": set(), "acknowledge_voicetracks": False})
        if not choice["include"]:
            results.append({"finding_id": entry["f"], "action": entry["a"], "outcome": "cancelled",
                            "message": "Cancelled: nothing was changed for this pair."})
            continue
        finding = _finding(report, entry["f"])
        if entry["a"] == KEEP_BOTH:
            results.append(_apply_keep_both(report, finding, entry, user))
            continue
        # The engine's state is read again for each pair: a batch never relies on an older snapshot.
        results.append(_apply_delete(report, finding, entry, choice, user=user, protection=protection,
                                     engine=_engine_state()))
    counts = {}
    for r in results:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    deleted = [r for r in results if r["outcome"] == "deleted"]
    problems = [r for r in results if r["outcome"] not in ("deleted", "recorded", "already_decided", "cancelled")]
    outcome = ("completed" if not problems else "partial" if len(problems) < len(results) else "nothing_applied")
    action_id = library_audit.record(
        "reconciliation.duplicate_resolution", user=user, outcome=outcome,
        level="info" if outcome == "completed" else "warning",
        title=f"Library duplicate resolution {outcome.replace('_', ' ')}: {len(deleted)} Track(s) deleted",
        detail={"scan_id": plan["s"], "requested": len(results), "outcomes": counts,
                "deleted_tracks": library_audit.id_sample([r["discard"] for r in deleted])})
    return {"scan_id": plan["s"], "outcome": outcome, "counts": counts, "items": results,
            "audit_action_id": action_id, "rescan_recommended": bool(deleted)}


def _apply_keep_both(report, finding, entry, user) -> dict:
    a, b = entry["t"]
    base = {"finding_id": entry["f"], "action": KEEP_BOTH, "track_a": a, "track_b": b}
    if finding is None or sorted(finding.get("members") or []) != [a, b]:
        return {**base, "outcome": "stale", "message": "This finding no longer names this pair."}
    result = record_keep_both(report["scan_id"], entry["f"], finding, a, b, user=user)
    if result["result"] == "missing":
        return {**base, "outcome": "stale", "message": "One of the Tracks no longer exists."}
    if result["result"] == "recorded":
        return {**base, "outcome": "recorded", "message": "Recorded: keep both (not duplicates)."}
    return {**base, "outcome": "already_decided", "resolution": result["resolution"],
            "message": f"Already decided ({result['resolution']}); left unchanged."}


def _apply_delete(report, finding, entry, choice, *, user, protection, engine) -> dict:
    from library.models import Track
    from library.services import library_audit
    from library.services.track_deletion import delete_track

    keep_id, discard_id = entry["k"], entry["d"]
    base = {"finding_id": entry["f"], "action": DELETE, "keep": keep_id, "discard": discard_id}
    if finding is None or finding_eligibility(finding)[0] is False \
            or sorted(finding.get("members") or []) != sorted((keep_id, discard_id)):
        return {**base, "outcome": "stale", "message": "This finding no longer offers this pair; reload the page."}
    chosen = choice["categories"]
    if not chosen <= set(entry["c"]):
        return {**base, "outcome": "refused",
                "message": "A chosen category was not one of the suggestions; nothing was changed."}
    using = router.db_for_write(Track)
    cleanup, added = None, []
    try:
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:      # this transaction only (SET LOCAL)
                cursor.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            # Both rows locked, lowest id first: concurrent edits, deletions and new log entries naming
            # either Track wait for this transaction.
            locked = {t.pk: t for t in Track.objects.select_for_update(of=("self",))
                      .filter(pk__in=(keep_id, discard_id)).order_by("pk")}
            tracks = {t.pk: t for t in Track.objects.select_related("artist", "album")
                      .filter(pk__in=list(locked))}
            inspection = _inspect(finding, keep_id, discard_id, protection=protection, now=_now(),
                                  engine=engine, describe=False, tracks=tracks)
            if inspection.keep is None or inspection.discard is None:
                return {**base, "outcome": "stale", "message": inspection.blockers[0][:1].upper()
                        + inspection.blockers[0][1:] + "; nothing was changed."}
            if inspection.fingerprints != entry["fp"]:
                return {**base, "outcome": "stale",
                        "message": "One of the Tracks changed since the preview (its file, details or categories); "
                                   "nothing was changed. Preview again."}
            if inspection.blockers:
                return {**base, "outcome": "blocked", "reasons": inspection.blockers,
                        "message": "Blocked; nothing was changed: " + "; ".join(inspection.blockers)}
            if inspection.voicetracks != entry["v"]:
                return {**base, "outcome": "stale", "message": "The discarded Track's voice tracks changed since the "
                                                               "preview; nothing was changed. Preview again."}
            if inspection.voicetracks and not choice["acknowledge_voicetracks"]:
                return {**base, "outcome": "needs_acknowledgment", "voicetracks": inspection.voicetracks,
                        "message": f"Not deleted: the discarded Track has {inspection.voicetracks} voice track(s); "
                                   "acknowledge that they are deleted with it, or cancel this pair."}
            if not chosen <= set(inspection.suggestions):
                return {**base, "outcome": "stale", "message": "The suggested categories changed; preview again."}
            through = Track.additional_categories.through
            if chosen:
                through.objects.bulk_create([through(track_id=keep_id, category_id=c) for c in sorted(chosen)],
                                            ignore_conflicts=True)
                added = sorted(chosen)
            discard = inspection.discard
            rel = _relative(discard.filepath)
            cleanup = delete_track(discard, remove_source=True)
            library_audit.record(
                "reconciliation.duplicate_delete", user=user, outcome="deleted",
                title=f"Library duplicate resolved: Track {discard_id} deleted, Track {keep_id} kept",
                detail={"scan_id": report["scan_id"], "finding_id": entry["f"], "kept": keep_id,
                        "deleted": discard_id, "file": library_audit.library_relative(discard.filepath),
                        "categories_added": added, "voicetracks_deleted": inspection.voicetracks})
    except ProtectedError as exc:
        names = sorted({type(o).__name__ for o in exc.protected_objects})
        return {**base, "outcome": "blocked", "reasons": [f"still referenced by {', '.join(names)}"],
                "message": f"Blocked; nothing was changed: still referenced by {', '.join(names)}."}
    except OperationalError as exc:
        log.warning("duplicate resolution finding=%s busy: %s", entry["f"], type(exc).__name__)
        return {**base, "outcome": "failed", "message": "Not applied: one of the Tracks is locked by another "
                                                        "operation (or the database was unavailable); nothing was "
                                                        "changed for this pair. Try again."}
    except DatabaseError as exc:
        log.warning("duplicate resolution finding=%s keep=%s discard=%s failed: %s", entry["f"], keep_id, discard_id,
                    type(exc).__name__)
        return {**base, "outcome": "failed", "message": f"Not applied ({type(exc).__name__}); nothing was changed "
                                                        "for this pair."}
    except Exception as exc:                  # report it and keep going: the other pairs are independent
        log.exception("duplicate resolution finding=%s unexpected failure", entry["f"])
        return {**base, "outcome": "failed", "message": f"Unexpected error ({type(exc).__name__}); nothing was "
                                                        "changed for this pair."}
    # Committed: the categories and the deletion together; the file cleanup ran after the commit.
    from library.models import Category
    codes = dict(Category.objects.filter(pk__in=added).values_list("pk", "code"))
    return {**base, "outcome": "deleted", "file": rel, "categories_added": [codes.get(c, str(c)) for c in added],
            "voicetracks_deleted": inspection.voicetracks, "cleanup": _cleanup(cleanup),
            "message": f"Track #{discard_id} deleted; Track #{keep_id} kept."}


def record_keep_both(scan_id, finding_id, finding, a, b, *, user) -> dict:
    """The existing not-a-duplicate decision (``DuplicateCandidate`` ``keep_both``): recorded once, never
    overriding a decision already made, audited after commit.  Returns {"result": "recorded" | "already_decided"
    | "missing", "resolution", "applied"}."""
    from library.models import DuplicateCandidate, Track
    from library.services import library_audit

    a, b = sorted((a, b))

    def audit(outcome, resolution):
        library_audit.record(
            "reconciliation.not_duplicate", user=user, outcome=outcome,
            title=(f"Library duplicate review: Tracks {a} and {b} recorded as not duplicates" if outcome == "recorded"
                   else f"Library duplicate review: Tracks {a} and {b} already decided ({resolution})"),
            detail={"track_a": a, "track_b": b, "resolution": resolution, "scan_id": scan_id,
                    "finding_id": finding_id})

    with transaction.atomic():
        tracks = {t.pk: t for t in Track.objects.filter(pk__in=(a, b))}
        if len(tracks) != 2:
            return {"result": "missing"}
        candidate = (DuplicateCandidate.objects.select_for_update()
                     .filter(track_a_id=a, track_b_id=b).first())
        if candidate is not None and candidate.resolution != "unresolved":
            audit("already_decided", candidate.resolution)
            return {"result": "already_decided", "resolution": candidate.resolution, "applied": candidate.applied}
        now = timezone.now()
        if candidate is None:
            DuplicateCandidate.objects.create(
                track_a=tracks[a], track_b=tracks[b], resolution="keep_both", resolved_at=now,
                confidence="exact" if finding.get("confidence") == "exact" else "probable")
        else:
            candidate.resolution, candidate.resolved_at = "keep_both", now
            candidate.save(update_fields=["resolution", "resolved_at"])
        audit("recorded", "keep_both")
    return {"result": "recorded", "resolution": "keep_both"}
