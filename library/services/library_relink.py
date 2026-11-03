"""Explicit relink of a missing-media Track to its moved file (roadmap 3.11 Phase B3).

The evidence contract is in docs/LIBRARY_RECONCILIATION.md.  In short: the scan offers a relink only when a
candidate file's strict decoded-audio identity equals the identity the Track's own waveform cache recorded
for the path it names, and nothing (no cue-point approval) points at other audio.  This module never decodes:
the candidate's ``stat_source`` witness, recorded when the scan decoded it, proves its content is still the
decoded content (path, device, inode, size, mtime and the kernel-maintained ctime).

``preview`` revalidates and returns the exact change and a signed token (operator-bound, ten minutes).
``apply`` takes the old and new path locks (sorted; library.services.source_paths), then the Track row lock,
revalidates everything, and writes ``filepath``/``filename`` through the guarded ``TrackQuerySet.update``.
Any difference refuses and writes nothing.  A relink keeps the Track id, metadata, cue points and approvals,
readiness, play history, logs and PlayEvents and the waveform cache; it moves, copies and deletes no file and
requests no analysis.  The cache then names the old path, so it is displayable but unverified until
Reanalyze Track re-decodes the (same) audio.
"""
from __future__ import annotations

import os
import stat

from django.core import signing
from django.db import OperationalError, router, transaction
from django.utils import timezone

from library.services import audio_identity, library_reconciliation, source_paths

TOKEN_SALT = "library.reconciliation.relink.v1"
TOKEN_MAX_AGE_SECONDS = 10 * 60
WARNINGS = [
    "The waveform stays displayable but is unverified until you run Reanalyze Track on the relinked file "
    "(Reset Cue Points is unavailable until then); cue points and their approvals are kept.",
    "No file is moved, copied or deleted, and the Track is not reanalyzed.",
]


class RelinkRefused(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _fingerprint(track_id):
    from library.models import Track
    row = Track.objects.filter(pk=track_id).values(
        "filepath", "filename", "format", "waveform_path", "cue_marker_revision").first()
    return row


def _plan(report, finding_id):
    finding = next((f for f in report["findings"] if f["id"] == finding_id), None)
    if finding is None or finding["type"] != "moved" or finding.get("relink") is None:
        raise RelinkRefused("This finding offers no relink (only one decoded-audio match qualifies).", status=400)
    candidate = finding["candidates"][finding["relink"]]
    track = finding["track"]
    if not candidate.get("eligible") or not candidate.get("witness") or not candidate.get("identity"):
        raise RelinkRefused("The candidate's evidence is incomplete; rescan.", status=400)
    return {"scan_id": report["scan_id"], "finding_id": finding_id, "track_id": track["id"],
            "expected": track["fingerprint"], "from": track["filepath"], "to": candidate["path"],
            "identity": candidate["identity"], "witness": candidate["witness"]}


def _check(plan):
    """Every condition a relink needs, against the current state.  Raises RelinkRefused."""
    from library.models import Track

    current = _fingerprint(plan["track_id"])
    if current is None:
        raise RelinkRefused("The Track no longer exists.")
    if current != plan["expected"]:
        raise RelinkRefused("The Track changed since the scan (its file, cache or cue points); rescan before relinking.")
    old, new = plan["from"], plan["to"]
    try:
        os.lstat(old)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise RelinkRefused(f"The Track's original path cannot be checked ({exc.strerror}).") from None
    else:
        raise RelinkRefused("The Track's original file is back: nothing to relink.")
    root = os.path.realpath(library_reconciliation.library_root())
    if not os.path.isdir(root):
        raise RelinkRefused("The Library storage is unavailable.")
    if os.path.realpath(new) != new or not new.startswith(root + os.sep):
        raise RelinkRefused("The candidate is not a canonical path inside the Library.")
    try:
        info = os.lstat(new)
    except FileNotFoundError:
        raise RelinkRefused("The candidate file no longer exists.") from None
    except OSError as exc:
        raise RelinkRefused(f"The candidate file cannot be checked ({exc.strerror}).") from None
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        raise RelinkRefused("The candidate is not a non-empty regular file.")
    if os.path.splitext(new)[1].lower() != os.path.splitext(old)[1].lower():
        raise RelinkRefused("The candidate's format differs from the Track's.")
    if not audio_identity.same_witness(plan["witness"], audio_identity.stat_source(new)):
        raise RelinkRefused("The candidate file changed since its audio was compared; rescan.")
    if Track.objects.filter(filepath=new).exists():
        raise RelinkRefused("Another Track already uses the candidate file.")
    path_owner, track_owner = library_reconciliation._protection_predicates()
    owner = path_owner(new, os.path.relpath(new, root), os.path.realpath(new)) or track_owner(plan["track_id"])
    if owner:
        raise RelinkRefused(f"Protected: {owner}.")
    evidence = library_reconciliation.track_audio_evidence(plan["track_id"], old)
    if evidence["identity"] is None or evidence["identity"] != plan["identity"]:
        raise RelinkRefused("The Track's recorded audio no longer matches the candidate's; rescan. "
                            + "; ".join(evidence["problems"]))


def preview(report, finding_id, *, user) -> dict:
    plan = _plan(report, finding_id)
    _check(plan)
    token = signing.dumps({"u": user.pk, **plan}, salt=TOKEN_SALT)
    return {"track_id": plan["track_id"], "from": plan["from"], "to": plan["to"],
            "changes": [{"field": "filepath", "before": plan["from"], "after": plan["to"]},
                        {"field": "filename", "before": os.path.basename(plan["from"]),
                         "after": os.path.basename(plan["to"])}],
            "unchanged": ["Track id", "title, artist, album and all other metadata", "cue points and approvals",
                          "Ready to Air", "play count and history, logs, PlayEvents", "waveform cache"],
            "evidence": f"decoded audio identical (sha256 {plan['identity'][:16]}…)", "warnings": WARNINGS,
            "token": token, "expires_in": TOKEN_MAX_AGE_SECONDS}


def apply(token, *, user) -> dict:
    from library.models import Track, TrackWriteRefused

    if not isinstance(token, str) or not token:
        raise RelinkRefused("Preview the relink first.", status=400)
    try:
        plan = signing.loads(token, salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE_SECONDS)
    except signing.SignatureExpired:
        raise RelinkRefused("This relink preview has expired; preview it again.") from None
    except signing.BadSignature:
        raise RelinkRefused("This is not a valid relink preview token.", status=400) from None
    if not isinstance(plan, dict) or plan.get("u") != user.pk:
        raise RelinkRefused("This relink was previewed by another operator.", status=403)
    using = router.db_for_write(Track)
    try:
        with transaction.atomic(using=using):
            # Path locks first (sorted), then the Track row: the order every path-registering write uses.
            for path in sorted({plan["from"], plan["to"]}):
                try:
                    source_paths.acquire(path, using, timeout_ms=source_paths.REGISTRATION_LOCK_TIMEOUT_MS)
                except OperationalError:
                    raise RelinkRefused("A registration, recall or cleanup of one of the paths is in progress; "
                                        "try again.") from None
            if Track.objects.select_for_update(of=("self",)).filter(pk=plan["track_id"]).first() is None:
                raise RelinkRefused("The Track no longer exists.")
            _check(plan)
            updated = Track.objects.filter(pk=plan["track_id"]).update(
                filepath=plan["to"], filename=os.path.basename(plan["to"]), updated_at=timezone.now())
            if updated != 1:
                raise RelinkRefused("The Track no longer exists.")
    except TrackWriteRefused as exc:
        raise RelinkRefused(f"Protected: {exc}") from None
    return {"result": "relinked", "track_id": plan["track_id"], "from": plan["from"], "to": plan["to"],
            "warnings": WARNINGS}
