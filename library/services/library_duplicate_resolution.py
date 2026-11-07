"""Resolve duplicate Track pairs from the Library Reconciliation page (roadmap 3.12; decoded-audio pairs 3.11).

An operator reviews two registered Tracks whose files the latest scan proved to be the same recording -- byte
for byte (``exact``), or by identical decoded audio (``audio_equivalent`` with recorded verification) -- chooses which one
to keep, optionally carries the discarded Track's categories over to the survivor as additional categories,
previews the consequences and deletes the duplicate.  This is a small workflow over existing mechanisms, not a
duplicate-management subsystem:

* **Eligible findings only.**  A duplicate finding of the latest *complete* scan with exactly two registered
  Tracks and either ``exact`` confidence (identical bytes, hashed in that scan) or ``audio_equivalent``
  confidence with the scan's ``audio_verification`` evidence: both files' full decoded-audio identities
  (algorithm, digest, sample rate, frame count; equal) bound to each file's path and stat witness
  (``library_reconciliation.Scanner.upgrade_with_decoded_identity``).  Groups of three or more Tracks, likely /
  uncertain findings, audio-equivalent findings without that evidence and file-only groups are not resolvable by
  evidence (only by the operator's manual editorial decision, below, which is a separate authority).
  The two kinds of evidence are never mixed: an exact finding needs its byte evidence, a decoded-audio one
  its decoded-audio evidence; a title, artist, ISRC or duration never stands in for either.
* **Decisions.**  Per pair: keep A and delete B, keep B and delete A, keep both (the existing non-destructive
  ``DuplicateCandidate`` ``keep_both`` record, unchanged), or nothing.  No ``keep_a`` / ``keep_b`` row is ever
  written, so ``apply_duplicate_resolutions`` has nothing new to act on; deleting a Track removes its legacy
  ``DuplicateCandidate`` rows (CASCADE), including a pending legacy decision.
* **Preview.**  Writes nothing.  For each selection it reports both Tracks, the survivor's suggested additional
  categories, the discarded Track's references and whether the deletion is eligible, needs an explicit
  acknowledgment (it has VoiceTracks, which the deletion would remove with it) or is blocked.  It returns a
  token *per applicable pair*, signed with ``django.core.signing``, bound to the operator, valid
  ``TOKEN_MAX_AGE_SECONDS``, naming the scan, that pair and what was previewed of both Tracks (columns,
  categories and file identity), and good for one apply request only (``_claim``).
* **Apply.**  One pair per request: the page sends a confirmed batch pair by pair, so a request does at most
  one deletion and its lock waits are bounded (``PAIR_LOCK_BUDGET_SECONDS``, ``CLAIM_LOCK_TIMEOUT``,
  ``CLEANUP_LOCK_TIMEOUT_SECONDS``, ``KEEP_BOTH_LOCK_TIMEOUT``) well inside the Gunicorn worker timeout.  The
  pair is its own transaction: lock both Track rows (in id order), recheck everything against the current
  state and the preview, add the chosen categories to the survivor, delete the discarded Track through
  ``library.services.track_deletion.delete_track`` (PROTECT references, publication ownership and commit-safe
  source cleanup unchanged).  The category additions and the deletion commit together or not at all.  Anything
  changed since the preview refuses the pair; nothing is guessed.  Pairs are independent: one blocked, stale or
  failing pair neither blocks nor rolls back another.

Manual editorial disposition (3.11, final operator-review layer).  A duplicate finding the scan could not prove
(likely or uncertain, a pair whose decoded audio differs or could not be decoded, a group of three or more
Tracks) can still be resolved by an operator's own editorial decision -- a third, separate authority
(``MANUAL_EDITORIAL``), never a relabelling of the finding as exact or audio-equivalent.  It needs: a complete
scan that recorded the files of *every* member of the finding (``member_files``: exactly one well-formed record --
path and six-field walk witness -- per member, no other, all distinct files;
``library_reconciliation.Scanner.record_member_files``; groups of at most
``library_reconciliation.DUPLICATE_GROUP_LIMIT`` Tracks; a member the scan left out as unsafe -- shared inode,
protected or non-canonical path, failed hash -- or a malformed record makes the whole group ineligible, never
skipped); an explicitly selected pair of two different members of that finding; and the operator's explicit
acknowledgment that the recordings were not verified identical, given before the preview issues the pair's token
(which names the authority and the acknowledgment) and repeated with the apply request.  The token also names
which members of the group exist at preview.  Apply serializes decisions against the group: inside the pair's
transaction it locks the rows of *all* the finding's members (at most ``DUPLICATE_GROUP_LIMIT``, ascending id,
within the same ``PAIR_LOCK_BUDGET_SECONDS``; a group it cannot lock in time is refused as busy), then rechecks
that the scan and the finding are still the previewed ones and that exactly the previewed members still exist
(any other deletion in between -- another pair of the same group, even one applied concurrently -- makes it
stale, so a group is reviewed again after each deletion), and only then runs the same inspection, blockers,
engine observation, transaction, category carry-forward, voice-track acknowledgment and cleanup as above, for
the selected pair only.  The audit records ``evidence: manual_editorial``.  Keep both may name a pair of a larger group
(``tracks``) and records that pair only.  A report without ``member_files`` (an earlier version's) stays review
only.

Blocked (never deleted here): a discarded Track in a playlist or a rotation slot (PROTECT), in the log of the
current or a later hour (or the engine's active log, which can be an earlier hour's), on air or claimed by the
engine, protected by another subsystem (spoken projection, Active Messages), remote-hosted, or whose file -- or
the survivor's -- is no longer exactly the file the scan hashed.  Every deletion also needs a fresh, well-formed
observation of the playout engine (``_engine_state``); without one nothing is deleted.  Logs are never
modified or regenerated.

The duplicate evidence is the scan's: identical SHA-256 for both files (exact), or identical decoded-audio
identities (audio-equivalent), and the stat witness each file was hashed or identified under (device, inode,
size, mtime, ctime, link count; ``library_reconciliation.FileEntry.witness``).  Nothing is re-hashed or decoded
in the web worker; at preview and at apply both files must still be regular files at the same path with exactly
that witness, and with the identity the preview saw.  A report without witnesses (made before
they were recorded) stays reviewable but authorizes no deletion.  The witness detects ordinary modification or
replacement (any write changes the ctime); it is not a cryptographic proof against a privileged user who
rewrites the bytes and then resets every recorded metadata field.
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import secrets
import stat
import time
from dataclasses import dataclass, field
from datetime import timedelta

from django.core import signing
from django.db import DatabaseError, IntegrityError, OperationalError, connections, router, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from library.services import library_reconciliation

log = logging.getLogger(__name__)

TOKEN_SALT = "library.reconciliation.duplicate_resolution.v2"      # v2: one pair per token, single use
TOKEN_MAX_AGE_SECONDS = 10 * 60
#: Spent tokens: rows of their own in ``django_session`` (see ``_claim``), a prefix Django never generates.
CLAIM_PREFIX = "dupres:"
#: Most selections one preview or apply handles: one findings page (the page shows 25 findings).
MAX_SELECTIONS = 25
#: Bounded reference listings in a preview (the full counts are always given).
REFERENCE_SAMPLE = 10
#: A LogItem the engine claimed this recently, but has not yet played, may be loaded on a deck.
CLAIM_WINDOW = timedelta(hours=3)
#: A Track that started this long ago plus its duration may still be on air.
PLAY_MARGIN = timedelta(minutes=10)
# Lock waits of one apply request, each answered "busy, try again" when exceeded (the Gunicorn worker timeout
# is 30 s; together these are at most 1 + 1 + 3 + 2 + 2 = 9 s of lock waiting -- not the request's duration):
#: Spending the token (waits only for a concurrent request spending the same token), and separately the
#: best-effort removal of expired claim markers after it.
CLAIM_LOCK_TIMEOUT = "1s"
#: All lock waits of the pair's transaction together: Track rows, category rows, the deletion's cascade.
PAIR_LOCK_BUDGET_SECONDS = 3.0
#: Each after-commit cleanup lock: the source path's and the waveform cache's (track_deletion).
CLEANUP_LOCK_TIMEOUT_SECONDS = 2.0
#: Keep both: the pair's DuplicateCandidate row.
KEEP_BOTH_LOCK_TIMEOUT = "2s"
#: An engine state stamped further in the future than this is not trusted (clock confusion).
ENGINE_CLOCK_TOLERANCE_SECONDS = 5
#: The playback decks the engine's state always names (``engine.SLOTS``; an empty deck is ``null``).
ENGINE_DECKS = ("A", "B")

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
    if confidence not in (EXACT, AUDIO_EQUIVALENT):
        verification = finding.get("audio_verification")
        note = (f" ({verification['reason']})" if isinstance(verification, dict)
                and isinstance(verification.get("reason"), str) and verification["reason"] else "")
        return False, ("only identical-byte (exact) findings, and pairs whose decoded audio the scan verified "
                       "identical, can be resolved here; this one is "
                       f"{str(confidence or 'unknown').replace('_', ' ')}{note}: review it by hand")
    if len(members) < 2:
        return False, "file review only: fewer than two registered Tracks"
    if len(members) > 2 or finding.get("member_count", len(members)) != 2:
        shared = "these bytes" if confidence == EXACT else "this audio"
        return False, f"{finding.get('member_count', len(members))} Tracks share {shared}: resolve by hand"
    evidence, why = _evidence(finding)
    if evidence is None:
        return False, why
    return True, ""


EXACT, AUDIO_EQUIVALENT = "exact", "audio_equivalent"
#: The operator's editorial decision: the third authority (never machine-verified sameness).
MANUAL_EDITORIAL = "manual_editorial"
MANUAL_WARNING = ("These recordings have not been verified as identical. They may contain different edits, masters, "
                  "audio quality or performances. You are authorizing deletion of the selected version based on your "
                  "own review.")
NO_MANUAL_WITNESS = ("this scan did not record the Tracks' files for manual resolution (it was made by an earlier "
                     "version): run reconciliation again to resolve it here")
#: A group whose file record is incomplete or malformed: no pair of it is deletable (Keep both still is).
MANUAL_INCOMPLETE = ("this finding does not contain valid file evidence for every Track; review the unsafe or "
                     "missing members and run reconciliation again when the source issue is resolved")
MANUAL_SAME_FILE = "two members name the same file: resolve by hand"
NO_EVIDENCE = "the scan did not record identical-byte evidence for both Tracks"
NO_AUDIO_EVIDENCE = ("the scan did not record verified decoded-audio evidence for both Tracks' files (run "
                     "reconciliation with \"Verify audio for likely duplicates\" to resolve this pair here)")
AUDIO_MISMATCH = "the scan's decoded-audio identities of the two files do not match"
NO_WITNESS = ("this scan did not record the files' full identity (it was made by an earlier version): run "
              "reconciliation again to resolve this pair here")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def evidence_kind(finding) -> str | None:
    """What proves the pair the same recording: "exact_bytes", "decoded_audio", or None (nothing deletable)."""
    confidence = finding.get("confidence") if isinstance(finding, dict) else None
    return {EXACT: "exact_bytes", AUDIO_EQUIVALENT: "decoded_audio"}.get(confidence)


def _witness_ok(entry) -> str:
    """"" when a file entry names a path and the six-field witness of its size and mtime; else the reason."""
    if not isinstance(entry.get("path"), str) or not _is_int(entry.get("size")) or not _is_int(entry.get("mtime_ns")):
        return NO_EVIDENCE
    witness = entry.get("witness")
    if not (isinstance(witness, list) and len(witness) == 6 and all(_is_int(v) for v in witness)):
        return NO_WITNESS
    if witness[2:4] != [entry["size"], entry["mtime_ns"]]:
        return NO_EVIDENCE
    return ""


def _evidence(finding) -> tuple[dict | None, str]:
    """({track_id: file entry}, "") for the two members, when the finding's own kind of evidence is complete:
    exact -- both carry the same scan-time SHA-256 and the stat witness they were hashed under; audio-equivalent
    -- both carry well-formed, equal decoded-audio identities and the stat witness they were identified under.
    (None, why) otherwise."""
    if evidence_kind(finding) == "decoded_audio":
        return _audio_evidence(finding)
    if evidence_kind(finding) != "exact_bytes":
        return None, NO_EVIDENCE
    members = finding.get("members") or []
    by_track = {}
    for entry in finding.get("files") or []:
        track_id = entry.get("track_id")
        if track_id in members:
            if track_id in by_track:
                return None, NO_EVIDENCE
            by_track[track_id] = entry
    if len(members) != 2 or set(by_track) != set(members):
        return None, NO_EVIDENCE
    digests = {by_track[t].get("sha256") for t in members}
    if len(digests) != 1 or not all(isinstance(d, str) and len(d) == 64 for d in digests):
        return None, NO_EVIDENCE
    for entry in by_track.values():
        if not isinstance(entry.get("path"), str) or not _is_int(entry.get("size")) \
                or not _is_int(entry.get("mtime_ns")):
            return None, NO_EVIDENCE
    for entry in by_track.values():
        witness = entry.get("witness")
        if not (isinstance(witness, list) and len(witness) == 6 and all(_is_int(v) for v in witness)):
            return None, NO_WITNESS
        if witness[2:4] != [entry["size"], entry["mtime_ns"]]:
            return None, NO_EVIDENCE
    return by_track, ""


def _audio_evidence(finding) -> tuple[dict | None, str]:
    """The decoded-audio kind of ``_evidence``: the scan's ``audio_verification`` record, complete and
    consistent -- status verified, exactly the two members, distinct files, each with its witness and a
    well-formed identity of the current algorithm, the two identities equal (``audio_identity.same_audio``:
    algorithm, digest, sample rate, frame count)."""
    from library.services import audio_identity

    members = finding.get("members") or []
    record = finding.get("audio_verification")
    if not isinstance(record, dict) or record.get("status") != "verified" or len(members) != 2:
        return None, NO_AUDIO_EVIDENCE
    files = record.get("files")
    if not isinstance(files, list) or len(files) != 2 or not all(isinstance(f, dict) for f in files):
        return None, NO_AUDIO_EVIDENCE
    by_track = {}
    for entry in files:
        track_id = entry.get("track_id")
        if track_id not in members or track_id in by_track:
            return None, NO_AUDIO_EVIDENCE
        by_track[track_id] = entry
    for entry in by_track.values():
        problem = _witness_ok(entry)
        if problem:
            return None, problem if problem == NO_WITNESS else NO_AUDIO_EVIDENCE
        identity = entry.get("identity")
        if not audio_identity.well_formed(identity) or identity.get("algorithm") != audio_identity.ALGORITHM:
            return None, NO_AUDIO_EVIDENCE
    first, second = (by_track[t] for t in members)
    if first["path"] == second["path"] or first["witness"][:2] == second["witness"][:2]:
        return None, NO_AUDIO_EVIDENCE
    if not audio_identity.same_audio(first["identity"], second["identity"]):
        return None, AUDIO_MISMATCH
    return by_track, ""


def evidence_summary(finding) -> dict | None:
    """What the page shows about the proof of an eligible finding (display only; never trusted back)."""
    kind = evidence_kind(finding)
    by_track, _why = _evidence(finding)
    if kind is None or by_track is None:
        return None
    if kind == "exact_bytes":
        return {"kind": kind, "sha256": next(iter(by_track.values())).get("sha256")}
    identity = next(iter(by_track.values()))["identity"]
    record = finding.get("audio_verification") or {}
    return {"kind": kind, "sha256": identity["sha256"], "sample_rate": identity["sample_rate"],
            "frames": identity["frames"], "bytes": record.get("bytes") if record.get("bytes") in (
                "different_size", "different_bytes", "identical_bytes") else "not_compared",
            "sources": {str(t): e.get("identity_source") for t, e in by_track.items()
                        if e.get("identity_source") in ("decoded", "waveform_cache")}}


def _manual_member_ids(members) -> bool:
    """A finding's member list as a manual decision needs it: a list of distinct positive Track ids."""
    return (isinstance(members, list) and all(_is_int(t) and t > 0 for t in members)
            and len(set(members)) == len(members))


def _manual_record_problem(entry) -> str:
    """"" when one ``member_files`` record is complete and consistent: an absolute path, its Library-relative
    form, size and mtime, and the six-field witness agreeing with them; else why not."""
    if _witness_ok(entry):
        return MANUAL_INCOMPLETE
    path, rel = entry["path"], entry.get("rel")
    if not path or "\0" in path or not os.path.isabs(path) or not isinstance(rel, str) or not rel \
            or not path.endswith(os.sep + rel):
        return MANUAL_INCOMPLETE
    witness = entry["witness"]
    if witness[2] < 0 or witness[5] < 1:
        return MANUAL_INCOMPLETE
    return ""


def _manual_files(finding) -> tuple[dict, str]:
    """({track_id: file entry}, "") when the scan recorded exactly one complete, consistent, distinct file record
    for EVERY member of the finding and none for anything else; ({}, why) otherwise.  Nothing is skipped: one
    missing (a member the scan left out as unsafe), malformed, duplicated or foreign record makes the whole group
    ineligible for a manual deletion.  A report with no record at all is an earlier version's (rescan)."""
    members = finding.get("members")
    if not _manual_member_ids(members) or len(members) > library_reconciliation.DUPLICATE_GROUP_LIMIT:
        return {}, MANUAL_INCOMPLETE
    if "member_files" not in finding:
        return {}, NO_MANUAL_WITNESS
    files = finding["member_files"]
    if not isinstance(files, list) or len(files) != len(members):
        return {}, MANUAL_INCOMPLETE
    wanted = set(members)
    by_track, inodes, paths = {}, set(), set()
    for entry in files:
        if not isinstance(entry, dict):
            return {}, MANUAL_INCOMPLETE
        track_id = entry.get("track_id")
        if not _is_int(track_id) or track_id not in wanted or track_id in by_track:
            return {}, MANUAL_INCOMPLETE
        if _manual_record_problem(entry):
            return {}, MANUAL_INCOMPLETE
        inode = tuple(entry["witness"][:2])
        if inode in inodes or entry["path"] in paths:
            return {}, MANUAL_SAME_FILE
        inodes.add(inode)
        paths.add(entry["path"])
        by_track[track_id] = entry
    if set(by_track) != wanted:
        return {}, MANUAL_INCOMPLETE
    return by_track, ""


def manual_eligibility(finding) -> tuple[bool, str, list]:
    """Whether the operator may resolve a pair of this finding by an editorial decision (``MANUAL_EDITORIAL``),
    why not, and its members (all of which have recorded files when it may).  Findings the scan proved (exact
    bytes, verified decoded audio) use that evidence instead; nothing here relabels them."""
    if not isinstance(finding, dict) or finding.get("type") != "duplicate":
        return False, "not a duplicate finding", []
    if finding_eligibility(finding)[0]:
        return False, "the scan proved this pair: resolve it with its verified evidence", []
    members = finding.get("members")
    if not _manual_member_ids(members):
        return False, MANUAL_INCOMPLETE, []
    count = finding.get("member_count", len(members))
    if len(members) < 2 or count != len(members):
        return False, "fewer than two registered Tracks", []
    if count > library_reconciliation.DUPLICATE_GROUP_LIMIT:
        return False, (f"{count} Tracks: too many to review as pairs here "
                       f"(at most {library_reconciliation.DUPLICATE_GROUP_LIMIT})"), []
    _by_track, why = _manual_files(finding)
    if why:
        return False, why, []
    return True, "", sorted(members)


def _manual_evidence(finding, keep_id, discard_id) -> tuple[dict | None, str]:
    """({keep: entry, discard: entry}, "") when the operator may decide this pair of this finding."""
    eligible, why, _members = manual_eligibility(finding)
    if not eligible:
        return None, why
    if keep_id == discard_id:
        return None, "choose two different Tracks: one to keep and one to delete"
    members = finding.get("members") or []
    if keep_id not in members or discard_id not in members:
        return None, "those Tracks are not both members of this finding"
    by_track, _why = _manual_files(finding)
    return {keep_id: by_track[keep_id], discard_id: by_track[discard_id]}, ""


def _manual_group(finding, entry) -> list | None:
    """The Track ids a manual deletion locks (every member of the finding, ascending; bounded by
    ``DUPLICATE_GROUP_LIMIT``), or None when the token's previewed members (``g``) are not a well-formed subset
    of them naming both Tracks of the pair."""
    members = finding.get("members") if isinstance(finding, dict) else None
    previewed = entry.get("g")
    if not _manual_member_ids(members) or len(members) > library_reconciliation.DUPLICATE_GROUP_LIMIT:
        return None
    if not _manual_member_ids(previewed) or previewed != sorted(previewed) or not set(previewed) <= set(members) \
            or not {entry.get("k"), entry.get("d")} <= set(previewed):
        return None
    return sorted(members)


def manual_context(finding) -> dict:
    """What the scan established about the audio of a manually resolved pair (display and audit; never proof):
    "different" (decoded and compared: it differs), "unverified" (not compared, or the comparison failed, with
    the recorded reason) or "not_compared" (no record, e.g. a larger group)."""
    record = finding.get("audio_verification") if isinstance(finding, dict) else None
    status = record.get("status") if isinstance(record, dict) else None
    reason = record.get("reason") if isinstance(record, dict) and isinstance(record.get("reason"), str) else ""
    if status == "different":
        return {"audio": "different", "audio_reason": reason[:300]}
    if status == "unverified":
        return {"audio": "unverified", "audio_reason": reason[:300]}
    return {"audio": "not_compared", "audio_reason": ""}


def manual_summary(finding, *, complete=True) -> dict:
    """The page's view of the manual route (display only; preview and apply recheck everything)."""
    eligible, why, members = manual_eligibility(finding)
    if eligible and not complete:
        eligible, why, members = False, "the scan is not complete", []
    return {"available": eligible, "reason": why, "members": members, "warning": MANUAL_WARNING,
            **manual_context(finding)}


def _existing_members(finding) -> list:
    """The finding's members that exist now (bounded: a manual finding has at most DUPLICATE_GROUP_LIMIT)."""
    from library.models import Track
    members = (finding.get("members") or [])[:library_reconciliation.DUPLICATE_GROUP_LIMIT]
    return sorted(Track.objects.filter(pk__in=members).values_list("pk", flat=True))


# --------------------------------------------------------------------------- current state

def _file_identity(path):
    """The stat witness (as the scan records it) of exactly this regular file (no symlink followed), or a reason."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None, "its file no longer exists"
    except OSError as exc:
        return None, f"its file cannot be checked ({exc.strerror or type(exc).__name__})"
    if not stat.S_ISREG(info.st_mode):
        return None, "its path is no longer a regular file"
    return library_reconciliation.stat_witness(info), ""


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


def _engine_state() -> tuple[dict | None, str]:
    """(state, "") for a fresh, well-formed observation of the playout engine; (None, why) otherwise.

    A deletion needs one.  The database cannot show what the running engine has loaded or queued -- its active
    log can be an earlier hour's, still playing after the clock has moved on -- so a missing, stale, unreadable
    or incomplete state never counts as "nothing on air".  Read with the shared reader
    (``webrequests.services._read_engine_state``: present, parseable and written within
    ``STATE_STALENESS_SECONDS``); this checks that the fields the deletion checks use are there."""
    from webrequests import services as engine_status

    try:
        state = engine_status._read_engine_state()
    except (AttributeError, TypeError, ValueError):       # not an object, or a timestamp that is not a number
        return None, "its state file is malformed"
    except Exception as exc:
        return None, f"its state file cannot be read ({type(exc).__name__})"
    if state is None:
        try:
            present = engine_status.ENGINE_STATE_PATH.exists()
        except OSError:
            present = True
        if not present:
            return None, "there is no engine state file (is the playout engine running?)"
        return None, (f"its state file is unreadable, malformed or older than "
                      f"{engine_status.STATE_STALENESS_SECONDS} seconds (the engine may be stopped or stalled)")
    stamp = state.get("timestamp") if isinstance(state, dict) else None
    # JSON accepts NaN and +/-Infinity, and every comparison with NaN is false: only a finite number can be
    # checked for freshness.
    if not isinstance(stamp, (int, float)) or isinstance(stamp, bool) or not math.isfinite(stamp):
        return None, "its state file's timestamp is not a valid time"
    age = time.time() - stamp
    if age > engine_status.STATE_STALENESS_SECONDS:
        return None, (f"its state file is older than {engine_status.STATE_STALENESS_SECONDS} seconds "
                      f"(the engine may be stopped or stalled)")
    if -age > ENGINE_CLOCK_TOLERANCE_SECONDS:
        return None, "its state file's timestamp is in the future"
    decks, queue = state.get("decks"), state.get("queue")
    # Every deck must be reported; a missing one is unknown, not empty.
    if not isinstance(decks, dict) or not all(slot in decks for slot in ENGINE_DECKS) or not all(
            deck is None or (isinstance(deck, dict) and _is_int(deck.get("track_id"))) for deck in decks.values()):
        return None, "its state file has no valid list of playback decks"
    if not isinstance(queue, list) or not all(isinstance(q, dict) and _is_int(q.get("track_id")) for q in queue):
        return None, "its state file has no valid upcoming queue"
    if "log_id" not in state or not (state["log_id"] is None or _is_int(state["log_id"])):
        return None, "its state file does not name the active log"
    return state, ""


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
    for deck in ((engine or {}).get("decks") or {}).values():   # a deletion always has a checked state
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


def _inspect(finding, keep_id, discard_id, *, protection, now, engine, engine_problem="", describe=True,
             tracks=None, manual=False) -> _Inspection:
    """Everything a deletion depends on, read now.  ``tracks``: rows the caller has already locked.  ``engine``:
    a checked engine state, or None with ``engine_problem`` saying why there is none (the deletion is blocked).
    ``manual``: the pair is an editorial decision; the files must still be the ones the scan recorded."""
    from library.models import Track

    out = _Inspection()
    evidence = (_manual_evidence(finding, keep_id, discard_id)[0] if manual else _evidence(finding)[0]) or {}
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
        elif identity != scanned.get("witness"):
            out.blockers.append(f"the {role} Track's file was modified or replaced since the scan compared it "
                                "(it is no longer exactly the file " + (
                                    "the scan recorded" if manual else "whose bytes were hashed"
                                    if evidence_kind(finding) == "exact_bytes" else "whose decoded audio was verified")
                                + "); run reconciliation again")
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
    if engine is None:
        out.blockers.append("the playout engine's current state cannot be confirmed: "
                            f"{engine_problem or 'it is unavailable'}. Nothing is deleted until it can be (Keep "
                            "both is still available)")
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
            tracks = item.get("tracks")
            if tracks is not None and not (isinstance(tracks, list) and len(tracks) == 2
                                           and all(_is_int(t) for t in tracks)):
                raise ResolutionRefused("Keep both names the pair as two Track ids.", status=400)
            parsed.append({"finding_id": item["finding_id"], "action": KEEP_BOTH,
                           "tracks": sorted(tracks) if tracks is not None else None})
            continue
        if action != DELETE:
            raise ResolutionRefused("Each selection's action is 'delete' or 'keep_both'.", status=400)
        keep, discard = item.get("keep"), item.get("discard")
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (keep, discard)):
            raise ResolutionRefused("A deletion names the Track to keep and the Track to discard.", status=400)
        authority = item.get("authority")
        if authority not in (None, MANUAL_EDITORIAL):
            raise ResolutionRefused("A deletion's authority is the scan's evidence or 'manual_editorial'.", status=400)
        parsed.append({"finding_id": item["finding_id"], "action": DELETE, "keep": keep, "discard": discard,
                       "manual": authority == MANUAL_EDITORIAL,
                       "acknowledge_manual": item.get("acknowledge_manual") is True})
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
    """What the selected resolutions would do now, and a token for each pair that can be applied.  Writes
    nothing."""
    report = _report(scan_id)
    selections = _parse_selections(raw_selections)
    conflicts = _conflicts(selections)
    protection, now = _protection(), _now()
    engine, engine_problem = _engine_state()
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
            pair = sel["tracks"] or (sorted(members) if len(members) == 2 else None)
            if pair is None:
                item.update(status=BLOCKED, reasons=["Choose the two Tracks of this group to keep both of"])
                continue
            if pair[0] == pair[1] or not set(pair) <= set(members):
                item.update(status=BLOCKED, reasons=["those Tracks are not two different Tracks of this finding"])
                continue
            decision = _legacy_decision(finding, *pair)
            item.update(status=ELIGIBLE, track_a=pair[0], track_b=pair[1], legacy_decision=decision)
            if decision and decision.get("resolution") in ("keep_a", "keep_b") and not decision.get("applied"):
                item["warnings"] = [f"A legacy decision ({decision['resolution']}) is recorded for this pair and is "
                                    "left unchanged; apply_duplicate_resolutions could still act on it."]
            item["token"] = _token(user, scan_id, {"f": sel["finding_id"], "a": KEEP_BOTH, "t": pair})
            planned.append(item)
            continue
        if sel["manual"]:
            _preview_manual(item, sel, finding, user=user, scan_id=scan_id, protection=protection, now=now,
                            engine=engine, engine_problem=engine_problem)
            if item.get("token"):
                planned.append(item)
            continue
        eligible, why = finding_eligibility(finding)
        if not eligible:
            item.update(status=BLOCKED, reasons=[why])
            continue
        if {sel["keep"], sel["discard"]} != set(members) or sel["keep"] == sel["discard"]:
            item.update(status=BLOCKED, reasons=["those Tracks are not the two Tracks of this finding"])
            continue
        inspection = _inspect(finding, sel["keep"], sel["discard"], protection=protection, now=now, engine=engine,
                              engine_problem=engine_problem)
        item.update(keep=sel["keep"], discard=sel["discard"], evidence=evidence_summary(finding), **inspection.described,
                    voicetracks=inspection.voicetracks, warnings=inspection.warnings,
                    legacy_decision=_legacy_decision(finding, sel["keep"], sel["discard"]))
        if inspection.blockers:
            item.update(status=BLOCKED, reasons=inspection.blockers)
            continue
        item.update(status=NEEDS_ACK if inspection.voicetracks else ELIGIBLE, reasons=[])
        item["token"] = _token(user, scan_id, {"f": sel["finding_id"], "a": DELETE, "k": sel["keep"],
                                               "d": sel["discard"], "fp": inspection.fingerprints,
                                               "c": inspection.suggestions, "v": inspection.voicetracks})
        planned.append(item)
    counts = {s: sum(1 for i in items if i["status"] == s) for s in (ELIGIBLE, NEEDS_ACK, BLOCKED)}
    return {"scan_id": scan_id, "items": items, "counts": counts, "applicable": len(planned),
            "deletions": sum(1 for i in planned if i["action"] == DELETE), "expires_in": TOKEN_MAX_AGE_SECONDS}


def _preview_manual(item, sel, finding, *, user, scan_id, protection, now, engine, engine_problem):
    """One manually decided pair of a preview (``MANUAL_EDITORIAL``): the same inspection as a verified pair; a
    token only with the operator's acknowledgment and nothing in the way."""
    keep, discard = sel["keep"], sel["discard"]
    item.update(authority=MANUAL_EDITORIAL, keep=keep, discard=discard,
                manual={"warning": MANUAL_WARNING, **manual_context(finding)})
    evidence, why = _manual_evidence(finding, keep, discard)
    if evidence is None:
        item.update(status=BLOCKED, reasons=[why])
        return
    inspection = _inspect(finding, keep, discard, protection=protection, now=now, engine=engine,
                          engine_problem=engine_problem, manual=True)
    item.update(evidence={"kind": MANUAL_EDITORIAL}, **inspection.described, voicetracks=inspection.voicetracks,
                warnings=inspection.warnings, legacy_decision=_legacy_decision(finding, keep, discard))
    reasons = list(inspection.blockers)
    if not sel["acknowledge_manual"]:
        reasons.append("acknowledge that this is your own editorial decision (the recordings were not verified "
                       "identical) before it can be authorized")
    if reasons:
        item.update(status=BLOCKED, reasons=reasons)
        return
    item.update(status=NEEDS_ACK if inspection.voicetracks else ELIGIBLE, reasons=[], manual_acknowledged=True)
    item["token"] = _token(user, scan_id, {"f": sel["finding_id"], "a": DELETE, "k": keep, "d": discard,
                                           "fp": inspection.fingerprints, "c": inspection.suggestions,
                                           "v": inspection.voicetracks, "e": MANUAL_EDITORIAL, "m": True,
                                           "g": _existing_members(finding)})


def _token(user, scan_id, entry) -> str:
    """One pair's authorization: this operator, this scan, this pair as previewed, a nonce spent at apply."""
    return signing.dumps({"u": user.pk, "s": scan_id, "n": secrets.token_urlsafe(16), "p": entry},
                         salt=TOKEN_SALT, compress=True)


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
    if not isinstance(plan.get("n"), str) or not isinstance(plan.get("p"), dict) \
            or plan["p"].get("a") not in (DELETE, KEEP_BOTH) or plan["p"].get("e") not in (None, MANUAL_EDITORIAL) \
            or (plan["p"].get("e") == MANUAL_EDITORIAL and (plan["p"].get("a") != DELETE or plan["p"].get("m") is not True
                                                            or not isinstance(plan["p"].get("g"), list))):
        raise ResolutionRefused("This is not a valid preview token.", status=400)
    return plan


def _claim(nonce) -> None:
    """Spend a token: exactly one apply request ever gets past this for it (any worker, any connection).

    The bulk edit's claim marker (``track_bulk_edit.SessionPreviews``): a row of its own in ``django_session``,
    keyed by a digest of the token's nonce under a prefix Django never generates, carrying no user and expiring
    after the token could be used.  Its primary key can be inserted once; the insert commits before the pair is
    applied, so a token is spent even when its pair is then refused (preview again to retry)."""
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
                session_key=key, session_data=SessionStore().encode({"duplicate_resolution_claimed": True}),
                expire_date=now + timedelta(seconds=TOKEN_MAX_AGE_SECONDS + 300))
    except IntegrityError:
        raise ResolutionRefused("This pair's preview was already used. Preview again.") from None
    except OperationalError:
        raise ResolutionRefused("This pair's preview is being used by another request. Preview again.") from None
    try:                                     # best effort: an expired marker is inert (clearsessions removes it too)
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:
                cursor.execute(f"SET LOCAL lock_timeout = '{CLAIM_LOCK_TIMEOUT}'")
            Session.objects.using(using).filter(session_key__startswith=CLAIM_PREFIX, expire_date__lt=now).delete()
    except DatabaseError:
        pass


def _parse_choice(raw) -> dict:
    """The operator's options for the pair: the suggested categories to add and the voice-track acknowledgment."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ResolutionRefused("choice must be an object.", status=400)
    categories = raw.get("categories", [])
    if not isinstance(categories, list) or not all(_is_int(c) for c in categories):
        raise ResolutionRefused("categories must be a list of category ids.", status=400)
    return {"categories": set(categories), "acknowledge_voicetracks": raw.get("acknowledge_voicetracks") is True,
            "acknowledge_manual": raw.get("acknowledge_manual") is True}


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


SETTLED = ("deleted", "recorded", "already_decided")


def apply(token, raw_choice, *, confirm, user) -> dict:
    """Apply ONE previewed pair (see the module docstring); its outcome is reported and audited."""
    from library.services import library_audit

    try:
        plan = _redeem(token, user)
        entry = plan["p"]
        choice = _parse_choice(raw_choice)
        if entry["a"] == DELETE and confirm is not True:
            raise ResolutionRefused("Confirm the permanent deletion first.", status=400)
        if entry.get("e") == MANUAL_EDITORIAL and not choice["acknowledge_manual"]:
            raise ResolutionRefused("Acknowledge that this deletion is your own editorial decision (the recordings "
                                    "were not verified identical) first.", status=400)
        report = _report(plan["s"])
        _claim(plan["n"])
    except ResolutionRefused as exc:
        # Nothing was applied; kept for troubleshooting (an expired, foreign, spent, unconfirmed or outdated preview).
        library_audit.record(
            "reconciliation.duplicate_resolution", user=user, outcome="refused", level="warning",
            title=f"Library duplicate resolution refused: {exc}"[:200], detail={"reason": str(exc), "status": exc.status})
        raise
    finding = _finding(report, entry["f"])
    if entry["a"] == KEEP_BOTH:
        result = _apply_keep_both(report, finding, entry, user)
    else:
        # The engine is observed again for this request: the preview's observation is never relied on.
        engine, engine_problem = _engine_state()
        result = _apply_delete(report, finding, entry, choice, user=user, protection=_protection(), engine=engine,
                               engine_problem=engine_problem)
    outcome = result["outcome"]
    action_id = library_audit.record(
        "reconciliation.duplicate_resolution", user=user, outcome=outcome,
        level="info" if outcome in SETTLED else "warning",
        title=f"Library duplicate resolution: {outcome.replace('_', ' ')} ({entry['f']})"[:200],
        detail={"scan_id": plan["s"], "finding_id": entry["f"], "resolution": entry["a"],
                "tracks": entry["t"] if entry["a"] == KEEP_BOTH else [entry["k"], entry["d"]],
                **({"authority": MANUAL_EDITORIAL, "manual_acknowledged": True,
                    "cleanup": (result.get("cleanup") or {}).get("status")}
                   if entry.get("e") == MANUAL_EDITORIAL else {})})
    return {"scan_id": plan["s"], "item": result, "audit_action_id": action_id,
            "rescan_recommended": outcome == "deleted"}


class _LockBudget:
    """``execute_wrapper`` for one pair's transaction: each statement may wait for locks only for what is left
    of the budget (``lock_timeout``, local to this transaction).  Once it is spent, a lock another transaction
    holds is refused at once (1 ms) rather than waited for; uncontended statements still run, so a slow but
    unobstructed deletion is never cut short."""

    SQL = "SELECT set_config('lock_timeout', %s, true)"

    def __init__(self, seconds):
        self.deadline = time.monotonic() + seconds

    def __call__(self, execute, sql, params, many, context):
        value = f"{max(1, int((self.deadline - time.monotonic()) * 1000))}ms"
        execute(self.SQL, [[value]] if many else [value], many, context)
        return execute(sql, params, many, context)


def _apply_keep_both(report, finding, entry, user) -> dict:
    a, b = entry["t"]
    base = {"finding_id": entry["f"], "action": KEEP_BOTH, "track_a": a, "track_b": b}
    members = (finding.get("members") or []) if finding is not None else []
    if finding is None or a == b or not {a, b} <= set(members):
        return {**base, "outcome": "stale", "message": "This finding no longer names this pair."}
    result = record_keep_both(report["scan_id"], entry["f"], finding, a, b, user=user)
    if result["result"] == "missing":
        return {**base, "outcome": "stale", "message": "One of the Tracks no longer exists."}
    if result["result"] == "busy":
        return {**base, "outcome": "failed", "message": "Not recorded: another operation is changing this pair's "
                                                        "decision. Preview again and retry."}
    if result["result"] == "recorded":
        return {**base, "outcome": "recorded", "message": "Recorded: keep both (not duplicates)."}
    return {**base, "outcome": "already_decided", "resolution": result["resolution"],
            "message": f"Already decided ({result['resolution']}); left unchanged."}


def _apply_delete(report, finding, entry, choice, *, user, protection, engine, engine_problem="") -> dict:
    from library.models import Track
    from library.services import library_audit
    from library.services.track_deletion import delete_track

    keep_id, discard_id = entry["k"], entry["d"]
    manual = entry.get("e") == MANUAL_EDITORIAL
    base = {"finding_id": entry["f"], "action": DELETE, "keep": keep_id, "discard": discard_id,
            **({"authority": MANUAL_EDITORIAL} if manual else {})}
    group = None
    if manual:
        group = _manual_group(finding, entry)
        if finding is None or group is None or _manual_evidence(finding, keep_id, discard_id)[0] is None:
            return {**base, "outcome": "stale", "message": "This finding no longer offers this pair; reload the page."}
    elif finding is None or finding_eligibility(finding)[0] is False \
            or sorted(finding.get("members") or []) != sorted((keep_id, discard_id)):
        return {**base, "outcome": "stale", "message": "This finding no longer offers this pair; reload the page."}
    chosen = choice["categories"]
    if not chosen <= set(entry["c"]):
        return {**base, "outcome": "refused",
                "message": "A chosen category was not one of the suggestions; nothing was changed."}
    using = router.db_for_write(Track)
    cleanup, added = None, []
    try:
        with transaction.atomic(using=using), \
                connections[using].execute_wrapper(_LockBudget(PAIR_LOCK_BUDGET_SECONDS)):
            # Both rows locked, lowest id first: concurrent edits, deletions and new log entries naming
            # either Track wait for this transaction.  A manual decision locks every member of its group
            # (ascending id, one statement, the same budget), so decisions against one group are serialized.
            locked = {t.pk: t for t in Track.objects.select_for_update(of=("self",))
                      .filter(pk__in=group if manual else (keep_id, discard_id)).order_by("pk")}
            if manual:
                stale = _manual_group_stale(report, finding, entry, locked)
                if stale:
                    return {**base, "outcome": "stale", "message": stale}
            tracks = {t.pk: t for t in Track.objects.select_related("artist", "album")
                      .filter(pk__in=[t for t in (keep_id, discard_id) if t in locked])}
            inspection = _inspect(finding, keep_id, discard_id, protection=protection, now=_now(),
                                  engine=engine, engine_problem=engine_problem, describe=False, tracks=tracks,
                                  manual=manual)
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
            cleanup = delete_track(discard, remove_source=True, cleanup_lock_timeout=CLEANUP_LOCK_TIMEOUT_SECONDS)
            library_audit.record(
                "reconciliation.duplicate_delete", user=user, outcome="deleted",
                title=f"Library duplicate resolved: Track {discard_id} deleted, Track {keep_id} kept",
                detail={"scan_id": report["scan_id"], "finding_id": entry["f"], "kept": keep_id,
                        "deleted": discard_id, "evidence": MANUAL_EDITORIAL if manual else evidence_kind(finding),
                        "file": library_audit.library_relative(discard.filepath),
                        "categories_added": added, "voicetracks_deleted": inspection.voicetracks,
                        **({"manual_acknowledged": True, "finding_confidence": finding.get("confidence"),
                            "members": entry["g"], **manual_context(finding)} if manual else {})})
    except ProtectedError as exc:
        names = sorted({type(o).__name__ for o in exc.protected_objects})
        return {**base, "outcome": "blocked", "reasons": [f"still referenced by {', '.join(names)}"],
                "message": f"Blocked; nothing was changed: still referenced by {', '.join(names)}."}
    except OperationalError as exc:
        log.warning("duplicate resolution finding=%s busy: %s", entry["f"], type(exc).__name__)
        return {**base, "outcome": "failed", "message": "Not applied: " + (
            "a Track of this group" if manual else "one of the Tracks") + " is busy (another operation holds it, or "
            "the database was unavailable); nothing was changed for this pair. Preview again and retry."}
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


def _manual_group_stale(report, finding, entry, locked) -> str:
    """Once the group's rows are locked: why a manual decision is stale ("" when it is not).  The scan and the
    finding must still be the previewed ones, the pair must exist, and exactly the members that existed at
    preview must exist now (read through the locks and again in a fresh statement)."""
    current = library_reconciliation.load_report()
    current_finding = _finding(current, entry["f"]) if isinstance(current, dict) \
        and current.get("scan_id") == report["scan_id"] and current.get("status") == "complete" else None
    if current_finding is None or current_finding.get("members") != finding.get("members") \
            or current_finding.get("member_files") != finding.get("member_files"):
        return "The scan or this finding changed since the preview; nothing was changed. Reload the page."
    for role, track_id in (("kept", entry["k"]), ("discarded", entry["d"])):
        if track_id not in locked:
            return f"The {role} Track no longer exists; nothing was changed."
    if sorted(locked) != entry["g"] or _existing_members(finding) != entry["g"]:
        return ("Another Track of this group was deleted since the preview; nothing was changed. Review the group "
                "again.")
    return ""


def record_keep_both(scan_id, finding_id, finding, a, b, *, user) -> dict:
    """The existing not-a-duplicate decision (``DuplicateCandidate`` ``keep_both``): recorded once, never
    overriding a decision already made, audited after commit.  Returns {"result": "recorded" | "already_decided"
    | "missing" | "busy", "resolution", "applied"}; "busy" when the decision row stayed locked by another
    operation for ``KEEP_BOTH_LOCK_TIMEOUT`` (nothing recorded)."""
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

    using = router.db_for_write(DuplicateCandidate)
    try:
        with transaction.atomic(using=using):
            with connections[using].cursor() as cursor:      # this transaction only (SET LOCAL)
                cursor.execute(f"SET LOCAL lock_timeout = '{KEEP_BOTH_LOCK_TIMEOUT}'")
            tracks = {t.pk: t for t in Track.objects.filter(pk__in=(a, b))}
            if len(tracks) != 2:
                return {"result": "missing"}
            candidate = (DuplicateCandidate.objects.select_for_update()
                         .filter(track_a_id=a, track_b_id=b).first())
            if candidate is not None and candidate.resolution != "unresolved":
                audit("already_decided", candidate.resolution)
                return {"result": "already_decided", "resolution": candidate.resolution,
                        "applied": candidate.applied}
            now = timezone.now()
            if candidate is None:
                DuplicateCandidate.objects.create(
                    track_a=tracks[a], track_b=tracks[b], resolution="keep_both", resolved_at=now,
                    confidence="exact" if finding.get("confidence") == "exact" else "probable")
            else:
                candidate.resolution, candidate.resolved_at = "keep_both", now
                candidate.save(update_fields=["resolution", "resolved_at"])
            audit("recorded", "keep_both")
    except OperationalError as exc:
        log.warning("keep both tracks=%s,%s busy: %s", a, b, type(exc).__name__)
        return {"result": "busy"}
    return {"result": "recorded", "resolution": "keep_both"}
