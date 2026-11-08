"""Review and resolve category drift from the Library Reconciliation page (roadmap 3.11).

A *category drift* finding (``library_reconciliation.Scanner.check_drift``) is a Track whose file sits in a
first-level Library folder other than its primary Category's code.  A Category is a programming decision and a
folder is a storage location: when they disagree the operator decides which one is right, per finding:

* **A -- Move file to primary Category** (``move_file``): the file moves to
  ``LIBRARY_ROOT/<primary Category code>/<the same file name>`` and ``Track.filepath`` follows; the primary and
  additional Categories stay exactly as they are.  Also the repair for a Track whose Category was changed while
  its file was left behind.
* **B -- Change primary Category to match the folder** (``set_category``): the file stays where it is; the primary
  Category becomes the existing Category whose code is exactly the folder's name.  An additional membership that
  would duplicate the new primary is dropped; the old primary is *not* added as an additional Category unless the
  operator explicitly asks for it (``keep_old_primary``), previewed and audited.  No Category is ever created and
  none is ever guessed from artist, genre, album, file name or tags.
* **C -- No action**: nothing is selected, nothing changes.

Option A is unavailable when the primary Category's code cannot be a folder name, and B when the file is in the
Library root (no folder) or no Category's code matches its folder exactly.  Either is blocked (with the reason)
when it is not safe now: the file is not exactly the file the scan walked (path and six-field witness), it is
shared (hard links), in delivery, protected by another subsystem or remote-hosted, the destination exists or is
another Track's, the folder is on another filesystem, or the playout engine may be playing or about to play the
Track (``media_relocation.playout_conflicts``: loaded on a deck, in its queue, claimed, recently started, in the
current or next log hour or the engine's active log, or the engine's state cannot be confirmed).  A healthy
Track can therefore be temporarily ineligible; retrying later is the remedy.  Later approved logs are reported
as a warning: their existing entries are never rewritten or rebuilt.

**Evidence and authorization.**  Only a finding of the latest *complete* scan that recorded ``drift`` evidence
(the walked path and witness and the Track's ``filepath``/``filename``/``category_id``) can be repaired; older
reports stay review only.  ``preview`` writes nothing; it rechecks everything against the current state and
issues one token per eligible finding, signed (``django.core.signing``), bound to the operator and the
authenticated session that previewed it (a keyed digest of its session key: another session of the same operator, or
a renewed session, is refused), the scan and the finding, the Track, the direction, the source path and witness, the destination path (A) or the target Category
(B), the Track's fingerprint including its additional Categories, valid ``TOKEN_MAX_AGE_SECONDS`` and spent at
its first apply (``_claim``).  A token for A never authorizes B, another Track or another destination.

**Apply** -- one Track per request (the page sends a confirmed batch of at most ``MAX_SELECTIONS`` one by one),
through ``media_relocation.execute``: path locks, then the Track row (bounded waits), every check again under the
locks, the file staged (A), the row written, commit, and only then the old directory entry removed.  The outcome
is ``moved`` (A) or ``updated`` (B), ``unchanged`` (already resolved), ``stale``, ``refused``, ``collision``,
``busy``, ``failed_before_commit``, ``committed_cleanup_required`` (the move committed; the old entry needs the
operator) or ``uncertain`` (the database did not confirm the commit; both entries kept).  Nothing is retried.
Nothing here transcodes, retags, reanalyzes, reschedules or rebuilds anything; a moved file's waveform cache
becomes unverified until Reanalyze Track (see ``media_relocation``).
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import timedelta

from django.core import signing
from django.db import DatabaseError, IntegrityError, OperationalError, connections, router, transaction
from django.utils import timezone

from library.services import library_reconciliation, media_relocation

TOKEN_SALT = "library.reconciliation.category_drift.v1"
TOKEN_MAX_AGE_SECONDS = 10 * 60
#: Spent tokens: rows of their own in ``django_session`` (as ``library_duplicate_resolution._claim``).
CLAIM_PREFIX = "driftfix:"
CLAIM_LOCK_TIMEOUT = "1s"
#: Most findings one preview handles (one findings page), and so the most one confirmed batch applies.
MAX_SELECTIONS = 25
#: All lock waits of one Track's transaction together.
LOCK_BUDGET_SECONDS = 3.0

MOVE_FILE, SET_CATEGORY = "move_file", "set_category"
DIRECTIONS = (MOVE_FILE, SET_CATEGORY)
ELIGIBLE, BLOCKED, RESOLVED = "eligible", "blocked", "already_resolved"
NO_EVIDENCE = ("this scan did not record the evidence a repair needs (it was made by an earlier version): run "
               "reconciliation again")


class DriftRefused(Exception):
    """The whole request is refused; nothing was changed."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- the scan's evidence

def evidence(finding) -> tuple[dict | None, str]:
    """The finding's well-formed drift evidence, or (None, why it cannot authorize a repair)."""
    if not isinstance(finding, dict) or finding.get("type") != "category_drift":
        return None, "this is not a category-drift finding"
    drift = finding.get("drift")
    if drift is None:
        return None, NO_EVIDENCE
    fingerprint = drift.get("fingerprint") if isinstance(drift, dict) else None
    witness = drift.get("witness") if isinstance(drift, dict) else None
    track = finding.get("track") or {}
    ok = (isinstance(drift, dict) and isinstance(drift.get("path"), str) and os.path.isabs(drift["path"])
          and isinstance(drift.get("rel"), str) and drift["rel"] and isinstance(drift.get("folder"), str)
          and isinstance(drift.get("category"), str) and drift["category"] and _is_int(drift.get("category_id"))
          and isinstance(witness, list) and len(witness) == 6 and all(_is_int(v) for v in witness)
          and witness[2] >= 0 and witness[5] >= 1
          and isinstance(fingerprint, dict) and set(fingerprint) == {"filepath", "filename", "category_id"}
          and fingerprint["filepath"] == drift["path"] and fingerprint["category_id"] == drift["category_id"]
          and drift["path"].endswith(os.sep + drift["rel"]) and _is_int(track.get("id")))
    if not ok:
        return None, "this finding's recorded evidence is incomplete or malformed: run reconciliation again"
    return drift, ""


def _folder_category(folder):
    from library.models import Category
    return Category.objects.select_related("kind").filter(code=folder).first() if folder else None


def summary(finding, *, complete=True) -> dict:
    """What the page shows for one drift finding (display only; preview and apply recheck everything)."""
    from library.models import Category
    drift, why = evidence(finding)
    out = {"evidence": drift is not None, "reason": "" if complete else "the scan is not complete",
           "folder": None, "category": None, "move": {"available": False, "reason": ""},
           "set_category": {"available": False, "reason": ""}}
    if drift is None:
        out["reason"] = why
        out["move"]["reason"] = out["set_category"]["reason"] = why
        return out
    out["folder"] = drift["folder"]
    primary = Category.objects.select_related("kind").filter(pk=drift["category_id"]).first()
    out["category"] = _cat(primary) if primary is not None else {"id": drift["category_id"], "code": drift["category"],
                                                                  "name": "", "kind": ""}
    try:
        root = media_relocation.library_root()
    except media_relocation.Stop as stop:
        root, out["reason"] = None, stop.message
    if root is not None:
        try:
            out["move"]["destination"] = os.path.relpath(
                media_relocation.destination_for(drift["path"], drift["category"], root), root)
            out["move"]["available"] = complete
        except media_relocation.Stop as stop:
            out["move"]["reason"] = stop.message
    target = _folder_category(drift["folder"])
    if not drift["folder"]:
        out["set_category"]["reason"] = "the file is in the Library root, not in a Category folder"
    elif target is None:
        out["set_category"]["reason"] = (f"no Category has the code '{drift['folder']}' (Categories are never created "
                                         "or guessed here)")
    else:
        out["set_category"].update(available=complete, category=_cat(target))
    if not complete:
        out["move"]["reason"] = out["move"]["reason"] or out["reason"]
        out["set_category"]["reason"] = out["set_category"]["reason"] or out["reason"]
    return out


def keys(finding) -> tuple[str, str]:
    """(current folder, primary Category code) of a drift finding, for filtering and grouping."""
    drift = finding.get("drift") if isinstance(finding.get("drift"), dict) else None
    if drift is not None:
        return drift.get("folder") or "", drift.get("category") or ""
    rel = (finding.get("file") or {}).get("rel") or ""
    parts = rel.split(os.sep)
    return (parts[0] if len(parts) > 1 else ""), (finding.get("track") or {}).get("category") or ""


def _cat(category):
    if category is None:
        return None
    return {"id": category.pk, "code": category.code, "name": category.name,
            "kind": category.kind.name if category.kind_id else ""}


# --------------------------------------------------------------------------- current state

def _additional(track_id) -> list:
    from library.models import Track
    return sorted(Track.additional_categories.through.objects.filter(track_id=track_id)
                  .values_list("category_id", flat=True))


def _fingerprint(track, additional) -> dict:
    """What a repair relies on about the Track now: compared between the scan, the preview and the apply."""
    return {"filepath": track.filepath, "filename": track.filename, "category_id": track.category_id,
            "additional": list(additional)}


def _plan(track, drift, direction, *, root, keep_old):
    """(plan, blockers): what the repair would do to this Track now, or why it cannot."""
    from library.models import Category
    blockers = []
    plan = {"direction": direction, "source": track.filepath, "destination": None, "category_from": track.category_id,
            "category_to": track.category_id, "additional_add": [], "additional_remove": []}
    folder = media_relocation.category_folder(track.filepath, root)
    primary = track.category
    if primary is not None and folder == primary.code:
        return plan, [], True
    additional = _additional(track.pk)
    if direction == MOVE_FILE:
        if primary is None:
            blockers.append("the Track has no primary Category to move its file to")
        else:
            try:
                plan["destination"] = media_relocation.destination_for(track.filepath, primary.code, root)
            except media_relocation.Stop as stop:
                blockers.append(stop.message)
    else:
        target = Category.objects.filter(code=folder).first() if folder else None
        if not folder:
            blockers.append("the file is in the Library root, not in a Category folder: changing the Category "
                            "cannot match it")
        elif target is None:
            blockers.append(f"no Category has the code '{folder}' (Categories are never created or guessed here)")
        else:
            plan["category_to"] = target.pk
            if target.pk in additional:
                plan["additional_remove"] = [target.pk]
            if keep_old and track.category_id is not None and track.category_id not in additional:
                plan["additional_add"] = [track.category_id]
    return plan, blockers, False


def _inspect(finding, drift, direction, *, keep_old, protection, now, engine, engine_problem, root, track=None):
    """Everything one repair depends on, read now (``track``: a row the caller has locked)."""
    from library.models import Track, TrackWriteRefused

    out = {"blockers": [], "warnings": [], "plan": None, "fingerprint": None, "witness": None, "track": None,
           "resolved": False, "collision": False, "scheduling": False, "file_review": False}
    if track is None:
        track = Track.objects.select_related("category", "category__kind", "artist").filter(
            pk=finding["track"]["id"]).first()
    if track is None:
        out["blockers"].append("the Track no longer exists")
        return out
    out["track"] = track
    additional = _additional(track.pk)
    out["fingerprint"] = _fingerprint(track, additional)
    scanned = drift["fingerprint"]
    if {k: out["fingerprint"][k] for k in scanned} != scanned:
        out["blockers"].append("the Track changed since the scan (its file path or primary Category); run "
                               "reconciliation again")
        return out
    plan, blockers, resolved = _plan(track, drift, direction, root=root, keep_old=keep_old)
    out["plan"], out["resolved"] = plan, resolved
    if resolved:
        return out
    out["blockers"].extend(blockers)
    try:
        out["witness"] = media_relocation.check_source(track.filepath, root=root, expected=drift["witness"],
                                                       strict=True, protection=protection, track=track)
    except media_relocation.Stop as stop:
        out["file_review"] = True
        out["blockers"].append(stop.message.rstrip(".") + (
            "; run reconciliation again" if stop.outcome == media_relocation.STALE else ""))
    if direction == MOVE_FILE and plan["destination"]:
        try:
            media_relocation.check_destination(track.filepath, plan["destination"], root=root, track_id=track.pk,
                                               allow_copy=False)
        except media_relocation.Stop as stop:
            out["collision" if stop.outcome == media_relocation.COLLISION else "file_review"] = True
            out["blockers"].append(stop.message)
    if direction == SET_CATEGORY and plan["category_to"] != track.category_id:
        original = track.category_id
        try:
            track.category_id = plan["category_to"]
            track.check_write_guards(["category"])
        except TrackWriteRefused as exc:
            out["blockers"].append(f"Protected: {exc}")
        finally:
            track.category_id = original
    blockers, warnings = media_relocation.playout_conflicts(track, now=now, engine=engine,
                                                            engine_problem=engine_problem)
    out["scheduling"] = bool(blockers)
    out["blockers"].extend(blockers)
    out["warnings"].extend(warnings)
    return out


def _describe(item, inspection, root):
    from library.models import Category
    track, plan = inspection["track"], inspection["plan"]
    if track is None:
        return
    ids = {plan["category_from"], plan["category_to"], *plan["additional_add"], *plan["additional_remove"],
           *inspection["fingerprint"]["additional"]} if plan else set(inspection["fingerprint"]["additional"])
    cats = {c.pk: c for c in Category.objects.select_related("kind").filter(pk__in=[i for i in ids if i])}
    item["track"] = {"id": track.pk, "url": f"/track/{track.pk}/", "title": track.title,
                     "artist": track.artist.name if track.artist_id else "",
                     "filepath": track.filepath, "rel": os.path.relpath(track.filepath, root)
                     if (track.filepath or "").startswith(root + os.sep) else track.filepath,
                     "folder": media_relocation.category_folder(track.filepath, root),
                     "additional_categories": [_cat(cats[c]) for c in inspection["fingerprint"]["additional"]
                                               if c in cats]}
    if plan:
        item["category_from"] = _cat(cats.get(plan["category_from"]))
        item["category_to"] = _cat(cats.get(plan["category_to"]))
        item["destination"] = os.path.relpath(plan["destination"], root) if plan["destination"] else None
        item["additional_add"] = [_cat(cats[c]) for c in plan["additional_add"] if c in cats]
        item["additional_remove"] = [_cat(cats[c]) for c in plan["additional_remove"] if c in cats]
        before, after = cats.get(plan["category_from"]), cats.get(plan["category_to"])
        if plan["direction"] == SET_CATEGORY and before is not None and after is not None:
            if before.kind_id != after.kind_id:
                item["warnings"].insert(0, f"Content kind changes from {before.kind.name} to {after.kind.name}: "
                                           "how the Track is scheduled and played may change.")
            item["warnings"].append(
                f"Future scheduling will select this Track as {after.code} instead of {before.code}. Entries "
                "already in approved logs are not changed.")
        if plan["additional_remove"]:
            item["warnings"].append("Its additional membership in the new primary Category is dropped (a Track is "
                                    "never its own additional Category).")


# --------------------------------------------------------------------------- requests

def _root() -> str:
    try:
        return media_relocation.library_root()
    except media_relocation.Stop as stop:
        raise DriftRefused(stop.message) from None


def _report(scan_id):
    report = library_reconciliation.load_report()
    if report is None or report.get("scan_id") != scan_id:
        raise DriftRefused("A newer scan replaced the one this page shows. Reload the page.")
    if report.get("status") != "complete":
        raise DriftRefused("The latest scan is not complete (the Library storage may have been unavailable); run a "
                           "new scan before resolving category drift.")
    return report


def _finding(report, finding_id):
    return next((f for f in report["findings"] if f.get("id") == finding_id and f.get("type") == "category_drift"),
                None)


def _parse_selections(raw):
    if not isinstance(raw, list) or not raw:
        raise DriftRefused("Select at least one category-drift finding.", status=400)
    if len(raw) > MAX_SELECTIONS:
        raise DriftRefused(f"At most {MAX_SELECTIONS} findings at a time.", status=400)
    parsed, seen = [], set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("finding_id"), str):
            raise DriftRefused("Each selection needs a finding_id.", status=400)
        if item.get("direction") not in DIRECTIONS:
            raise DriftRefused("Each selection's direction is 'move_file' or 'set_category'.", status=400)
        keep_old = item.get("keep_old_primary", False)
        if not isinstance(keep_old, bool) or (keep_old and item["direction"] != SET_CATEGORY):
            raise DriftRefused("keep_old_primary is true or false, and only for 'set_category'.", status=400)
        if item["finding_id"] in seen:
            raise DriftRefused("A finding is selected twice.", status=400)
        seen.add(item["finding_id"])
        parsed.append({"finding_id": item["finding_id"], "direction": item["direction"], "keep_old": keep_old})
    return parsed


def _session_binding(session_key) -> str:
    """The issuing session, as the token carries it: a keyed digest of the session key (never the key itself)."""
    if not isinstance(session_key, str) or not session_key:
        raise DriftRefused("Your session has ended. Sign in again, then preview again.")
    return signing.Signer(salt=TOKEN_SALT + ".session").signature(session_key)


def preview(scan_id, raw_selections, *, user, session_key=None) -> dict:
    """What the selected repairs would do now, and a token for each one that can be applied.  Writes nothing.
    Each token is bound to the authenticated session that issued it (``session_key``)."""
    from library.services.library_duplicate_resolution import _engine_state
    session = _session_binding(session_key)
    report = _report(scan_id)
    selections = _parse_selections(raw_selections)
    root = _root()
    protection = library_reconciliation._protection_predicates()
    now = timezone.now()
    engine, engine_problem = _engine_state()
    items, track_seen = [], {}
    for sel in selections:
        item = {"finding_id": sel["finding_id"], "direction": sel["direction"], "keep_old_primary": sel["keep_old"],
                "reasons": [], "warnings": []}
        items.append(item)
        finding = _finding(report, sel["finding_id"])
        if finding is None:
            item.update(status=BLOCKED, reasons=["this finding is not a category-drift finding of the current scan"])
            continue
        drift, why = evidence(finding)
        if drift is None:
            item.update(status=BLOCKED, reasons=[why])
            continue
        track_id = finding["track"]["id"]
        if track_id in track_seen:
            item.update(status=BLOCKED, reasons=["another selection in this batch names the same Track"])
            continue
        track_seen[track_id] = sel["finding_id"]
        inspection = _inspect(finding, drift, sel["direction"], keep_old=sel["keep_old"], protection=protection,
                              now=now, engine=engine, engine_problem=engine_problem, root=root)
        item["warnings"].extend(inspection["warnings"])
        _describe(item, inspection, root)
        if inspection["resolved"]:
            item.update(status=RESOLVED, reasons=["the Track's file is already in its primary Category's folder: "
                                                  "nothing to do"])
            continue
        item["conflicts"] = {k: inspection[k] for k in ("collision", "scheduling", "file_review")}
        if inspection["blockers"]:
            item.update(status=BLOCKED, reasons=inspection["blockers"])
            continue
        plan = inspection["plan"]
        item["status"] = ELIGIBLE
        item["token"] = _token(user, scan_id, session, {
            "f": sel["finding_id"], "t": track_id, "d": sel["direction"], "src": plan["source"],
            "dst": plan["destination"], "cat": plan["category_to"], "cf": plan["category_from"],
            "k": sel["keep_old"], "w": inspection["witness"], "fp": inspection["fingerprint"],
            "aa": plan["additional_add"], "ar": plan["additional_remove"]})
    def flagged(kind):
        return sum(1 for i in items if (i.get("conflicts") or {}).get(kind))

    counts = {
        "selected": len(items),
        "eligible": sum(1 for i in items if i["status"] == ELIGIBLE),
        "refused": sum(1 for i in items if i["status"] == BLOCKED),
        "already_resolved": sum(1 for i in items if i["status"] == RESOLVED),
        "expected_moves": sum(1 for i in items if i["status"] == ELIGIBLE and i["direction"] == MOVE_FILE),
        "expected_category_changes": sum(1 for i in items if i["status"] == ELIGIBLE
                                         and i["direction"] == SET_CATEGORY),
        "destination_collisions": flagged("collision"),
        "scheduling_conflicts": flagged("scheduling"),
        "files_requiring_review": flagged("file_review"),
    }
    return {"scan_id": scan_id, "items": items, "counts": counts, "expires_in": TOKEN_MAX_AGE_SECONDS,
            "max_selections": MAX_SELECTIONS}


def _token(user, scan_id, session, entry) -> str:
    return signing.dumps({"u": user.pk, "k": session, "s": scan_id, "n": secrets.token_urlsafe(16), "p": entry},
                         salt=TOKEN_SALT, compress=True)


def _redeem(token, user, session_key=None) -> dict:
    if not isinstance(token, str) or not token:
        raise DriftRefused("Preview the repair first.", status=400)
    try:
        plan = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise DriftRefused("This preview has expired; preview the selection again.") from None
    except signing.BadSignature:
        raise DriftRefused("This is not a valid preview token.", status=400) from None
    if not isinstance(plan, dict) or plan.get("u") != user.pk:
        raise DriftRefused("This preview was made by another operator.", status=403)
    if not isinstance(plan.get("k"), str) or plan["k"] != _session_binding(session_key):
        raise DriftRefused("This preview was made in another sign-in session (or yours was renewed); preview it "
                           "again here.", status=403)
    entry = plan.get("p")
    if not isinstance(plan.get("n"), str) or not isinstance(entry, dict) or entry.get("d") not in DIRECTIONS \
            or not _is_int(entry.get("t")) or not isinstance(entry.get("src"), str) \
            or (entry["d"] == MOVE_FILE) != isinstance(entry.get("dst"), str) \
            or not isinstance(entry.get("fp"), dict) or not isinstance(entry.get("w"), list):
        raise DriftRefused("This is not a valid preview token.", status=400)
    return plan


def _claim(nonce) -> None:
    """Spend a token: exactly one apply request ever gets past this for it (``library_duplicate_resolution._claim``)."""
    from django.contrib.sessions.backends.db import SessionStore
    from django.contrib.sessions.models import Session

    using = router.db_for_write(Session)
    now = timezone.now()
    key = CLAIM_PREFIX + hashlib.sha256(nonce.encode()).hexdigest()[:40 - len(CLAIM_PREFIX)]
    try:
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{CLAIM_LOCK_TIMEOUT}'")
            Session.objects.using(using).create(
                session_key=key, session_data=SessionStore().encode({"category_drift_claimed": True}),
                expire_date=now + timedelta(seconds=TOKEN_MAX_AGE_SECONDS + 300))
    except IntegrityError:
        raise DriftRefused("This repair's preview was already used. Preview again.") from None
    except OperationalError:
        raise DriftRefused("This repair's preview is being used by another request. Preview again.") from None
    try:                                     # best effort: an expired marker is inert (clearsessions removes it too)
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{CLAIM_LOCK_TIMEOUT}'")
            Session.objects.using(using).filter(session_key__startswith=CLAIM_PREFIX, expire_date__lt=now).delete()
    except DatabaseError:
        pass


def apply(token, *, confirm, user, session_key=None) -> dict:
    """Apply ONE previewed repair (see the module docstring); its outcome is reported and audited."""
    from library.services import library_audit
    from library.services.library_duplicate_resolution import _engine_state

    try:
        plan = _redeem(token, user, session_key)
        entry = plan["p"]
        if confirm is not True:
            raise DriftRefused("Confirm the repair first.", status=400)
        report = _report(plan["s"])
        finding = _finding(report, entry["f"])
        drift, why = evidence(finding) if finding is not None else (None, "the finding is gone")
        if drift is None or finding["track"]["id"] != entry["t"] or drift["path"] != entry["src"] \
                or drift["witness"] != entry["w"]:
            raise DriftRefused("This finding changed or is no longer in the current scan. Reload the page.")
        _claim(plan["n"])
    except DriftRefused as exc:
        library_audit.record(
            "reconciliation.category_drift", user=user, outcome="refused", level="warning",
            title=f"Library category-drift repair refused: {exc}"[:200], detail={"reason": str(exc), "status": exc.status})
        raise
    engine, engine_problem = _engine_state()          # observed again for this request
    result = _apply_one(entry, drift, finding, scan_id=plan["s"], user=user, engine=engine,
                        engine_problem=engine_problem, nonce=plan["n"])
    item = {"finding_id": entry["f"], "direction": entry["d"], **result.as_dict()}
    item["stop_batch"] = result.attention
    return {"scan_id": plan["s"], "item": item, "rescan_recommended": result.committed}


def _apply_one(entry, drift, finding, *, scan_id, user, engine, engine_problem, nonce):
    from library.models import Category, Track
    root = _root()
    protection = library_reconciliation._protection_predicates()
    direction = entry["d"]

    def check(locked):
        track = Track.objects.select_related("category", "category__kind", "artist").get(pk=locked.pk)
        inspection = _inspect(finding, drift, direction, keep_old=entry.get("k") is True, protection=protection,
                              now=timezone.now(), engine=engine, engine_problem=engine_problem, root=root,
                              track=track)
        if inspection["resolved"]:
            raise media_relocation.Stop(media_relocation.UNCHANGED, "Already resolved: the Track's file is in its "
                                                                    "primary Category's folder. Nothing was changed.")
        if inspection["fingerprint"] != entry["fp"]:
            raise media_relocation.Stop(media_relocation.STALE, "The Track changed since the preview (its file, "
                                        "primary or additional Categories); nothing was changed. Preview again.")
        plan = inspection["plan"] or {}
        if plan.get("destination") != entry.get("dst") or plan.get("category_to") != entry.get("cat") \
                or plan.get("additional_add") != entry.get("aa") or plan.get("additional_remove") != entry.get("ar"):
            raise media_relocation.Stop(media_relocation.STALE, "The repair would now differ from the preview; "
                                                                "nothing was changed. Preview again.")
        if inspection["witness"] is not None and inspection["witness"] != entry["w"]:
            raise media_relocation.Stop(media_relocation.STALE, "The Track's file changed since the preview.")
        if inspection["blockers"]:
            raise media_relocation.Stop(media_relocation.COLLISION if inspection["collision"] else media_relocation.REFUSED,
                                        "Not applied: " + "; ".join(inspection["blockers"]))

    def write(locked, filepath):
        through = Track.additional_categories.through
        if direction == MOVE_FILE:
            locked.filepath = filepath
            locked.save(update_fields=["filepath", "updated_at"])
        else:
            locked.category_id = entry["cat"]
            locked.save(update_fields=["category", "updated_at"])
            if entry.get("ar"):
                through.objects.filter(track_id=locked.pk, category_id__in=entry["ar"]).delete()
            if entry.get("aa"):
                through.objects.bulk_create([through(track_id=locked.pk, category_id=c) for c in entry["aa"]],
                                            ignore_conflicts=True)
        codes = dict(Category.objects.filter(pk__in=[entry["cf"], entry["cat"], *entry.get("aa", []),
                                                     *entry.get("ar", [])]).values_list("pk", "code"))
        return {"category_from": codes.get(entry["cf"]), "category_to": codes.get(entry["cat"]),
                "additional_added": [codes.get(c) for c in entry.get("aa", [])],
                "additional_removed": [codes.get(c) for c in entry.get("ar", [])],
                "file_moved": direction == MOVE_FILE}

    return media_relocation.execute(
        track_id=entry["t"], source=entry["src"], destination=entry["dst"] if direction == MOVE_FILE else None,
        origin="reconciliation", user=user, write=write, check=check, expected_witness=entry["w"], strict=True,
        allow_copy=False, lock_budget_seconds=LOCK_BUDGET_SECONDS, protection=protection,
        audit={"operation": "category_drift", "direction": direction, "scan_id": scan_id, "finding_id": entry["f"],
               "witness": entry["w"], "keep_old_primary": entry.get("k") is True,
               "preview": hashlib.sha256(nonce.encode()).hexdigest()[:16]})
