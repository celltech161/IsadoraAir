"""Native active messages through the ORDINARY station playout (iPortal D2B).

D2B changes where OGRemote's processing happens, not how IsadoraAir plays
the result. A READY aggregate (D2A) becomes ordinary library content and is
played by the existing machinery, exactly like a Weather/AMBER alert:

    READY execution
      -> publish(): the exact D2A ProductionMedia bytes, verified, placed
         atomically at the execution-specific immutable path
         ``LIBRARY_ROOT/UrgentPA/am-<execution hex>.flac`` (never overwritten:
         a later aggregate can never replace a file an earlier deck loaded),
         ingested by ``sync_track_file`` (normal Track, normal analysis,
         normal waveform) -- the Track is NOT Ready-to-Air, so neither a
         rotation nor a category-only ``insert_urgent`` (legacy OGRemote,
         Weather) can ever pick it;
      -> dispatch(): ``insert_urgent`` over the existing engine-command queue,
         extended with the EXACT Track and the execution/attempt identity;
      -> the engine's existing urgent splice (``_splice_log_item_db_at`` ->
         ``_apply_log_item_insert`` / forced queue) records the insertion
         here in the SAME transaction (``admit`` + ``record_insertion``):
         ACCEPTED;
      -> ``_start_next_track`` -> ``_create_deck`` under the deck-open authority:
         ``authorize_open`` re-reads this domain and either CLAIMS the
         execution or refuses (a stale one is SUPPRESSED; the next ordinary
         item then plays -- nothing is consumed);
      -> the deck's own first-real-post-primer-buffer evidence
         (``LogItem.played_at`` + ``PlayEvent``) -> STARTED;
         its own clean EOS -> COMPLETED (D1 cadence); anything else after a
         real start -> INTERRUPTED (no immediate replay) -- ``sync_evidence``
         reads that evidence; it never invents any.

Correlation is exact and durable (ActiveBroadcastPublication: execution ->
Track; ActiveBroadcastInsertion: attempt -> LogItem), never inferred from a
category, title or "the first Track in UrgentPA". The reserved file name
``am-<32 hex>.flac`` is only the database-free *candidate* test that keeps
ordinary Tracks query-free on the engine's hot paths (as spoken does).

Every attempt has its own token. Only the insertion of the execution's
CURRENT attempt may open a deck, so a duplicate, delayed or superseded
command and a stranded LogItem from an earlier attempt are all harmless.

Recovery acts only on POSITIVE evidence that the current attempt can no
longer air -- never on elapsed time alone (an accepted priority item may
legitimately wait behind long-form programming, a Manual hold, the mic or
Remote DJ for any length of time):

* a command never ACCEPTED within DISPATCH_TIMEOUT_SECONDS is re-sent;
* the engine reports an unaired insertion that left its queue
  (``report_not_aired``): discarded as unusable before a deck, dropped by a
  queue replacement/takeover, or refused/failed at deck open. An ACCEPTED
  attempt never reached deck-open authority, so it is re-sent (or
  SUPPRESSED if stale). A CLAIMED attempt is re-sent only when this process
  positively never built a deck for it (the LogItem's playback claim, which
  precedes every deck construction, never committed); otherwise it is held
  for operator attention and the restart fence;
* the accepted LogItem row was deleted (worker);
* the engine process that held an unstarted attempt is gone: the new engine
  rotates it at start-up (a restart fences the old process's audio).

A CLAIMED attempt is never superseded by age.

Publication failures are classified: a RETRYABLE one (analysis, waveform,
library I/O, category configuration) keeps the SAME READY execution and its
exact ``mix_media`` -- ``publication_retry`` events, exponential backoff,
deduplicated operator attention -- and the worker does not move on to another
occurrence meanwhile; a PERMANENT integrity contradiction (the aggregate no
longer proves its bytes, other bytes at the reserved path, a deleted
publication) FAILS the execution with its own code.

Nothing here can air in production: every step requires the native-airing
gate (services.NATIVE_AIRING_AVAILABLE and the policy switch), which stays
closed until Phase G.
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from production.errors import MediaInconsistent, MediaPurged
from production.services import media_io

from . import _store, services
from .models import (
    ActiveBroadcastExecution, ActiveBroadcastInsertion, ActiveBroadcastPublication, ActorKind, ExecutionState,
)

X = ExecutionState
CATEGORY_CODE = "UrgentPA"
TRACK_TITLE = TRACK_ARTIST = "Urgent PA"          # what legacy OGRemote's mix carried
_NATIVE_NAME = re.compile(r"am-[0-9a-f]{32}\.flac")
DISPATCH_TIMEOUT_SECONDS = 60                     # a command not accepted by then is presumed lost
# A CLAIMED attempt without air evidence by then raises operator attention.
# Attention only: a claimed attempt is never superseded by age.
CLAIMED_ATTENTION_SECONDS = 600
MAX_ATTEMPTS = 50                                 # then FAILED dispatch_exhausted (operator attention)
CLEAN_END_REASONS = frozenset({"eos", "eos_deferred"})
ENGINE_HELD = (X.ACCEPTED, X.CLAIMED)
_COPY_CHUNK = 1024 * 1024
# The engine-thread bound on every active-message lock wait (deck-open
# authority, not-aired reports): contention fails closed, never open.
AUTHORITY_LOCK_TIMEOUT = "250ms"
# Publication retry backoff, as D2A's render retry.
PUBLICATION_BACKOFF_BASE_SECONDS = 15
PUBLICATION_BACKOFF_MAX_SECONDS = 600
PUBLICATION_ATTENTION_AFTER = 3

# Stable codes of D2B outcomes (execution failure_code / event detail).
ANALYSIS_FAILED = "analysis_failed"
LIBRARY_UNAVAILABLE = "library_unavailable"
CATEGORY_MISSING = "category_missing"
PUBLICATION_FAILED = "publication_failed"          # an unclassified publication error (retryable)
AGGREGATE_UNAVAILABLE = "aggregate_unavailable"
DESTINATION_CONFLICT = "destination_conflict"
PUBLICATION_UNAVAILABLE = "publication_unavailable"  # the published Track/file is gone
DISPATCH_EXHAUSTED = "dispatch_exhausted"
# The exact READY aggregate is still valid: retry publication of the SAME execution.
PUBLICATION_RETRYABLE = frozenset({ANALYSIS_FAILED, LIBRARY_UNAVAILABLE, CATEGORY_MISSING, PUBLICATION_FAILED})
# Integrity/authority contradictions: the execution FAILS with the code.
PUBLICATION_PERMANENT = frozenset({AGGREGATE_UNAVAILABLE, DESTINATION_CONFLICT})


class PlayoutError(services.ActiveMessageError):
    code = "playout_error"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""


# -- identity -------------------------------------------------------------------------------

def gate_open(policy=None) -> bool:
    """The native-airing gate: closed in every production configuration
    (services.NATIVE_AIRING_AVAILABLE is False until Phase G)."""
    policy = policy or services.current_policy()
    return bool(services.NATIVE_AIRING_AVAILABLE and policy.native_airing_enabled)


def relpath_for(execution_id) -> str:
    return f"{CATEGORY_CODE}/am-{uuid.UUID(str(execution_id)).hex}.flac"


def is_candidate(track) -> bool:
    """Database- and filesystem-free: does this Track carry the reserved
    native file name? Only candidates are ever inspected; every ordinary
    Track (including legacy OGRemote's UrgentPA mix) keeps the engine's
    exact pre-D2B behaviour."""
    filepath = getattr(track, "filepath", None)
    return isinstance(filepath, str) and bool(_NATIVE_NAME.fullmatch(filepath.rsplit("/", 1)[-1]))


def _library_root() -> Path:
    return Path(getattr(settings, "LIBRARY_ROOT", "/srv/isadoraair/music")).resolve()


def _generation() -> int:
    return services.current_policy().generation


def _event(name, row, *, actor_kind, detail=None, from_state="", to_state=""):
    return services._event(name, execution=row, generation=_generation(), actor_kind=actor_kind,
                           from_state=from_state, to_state=to_state, detail=detail)


# -- publication ---------------------------------------------------------------------------

@contextmanager
def _destination_lock(dest: Path):
    """Cross-process exclusive lock keyed by the destination (an flock on a
    hidden sidecar, released by the kernel on exit or crash)."""
    lock_path = dest.parent / f".{dest.name}.publish.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            try:
                os.unlink(lock_path)
            except OSError:
                pass


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_COPY_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_verified(media, target: Path) -> str:
    """Copy the media's stored bytes (opened by identity, never a caller's
    path) to ``target`` while hashing them; returns the SHA-256."""
    opened = media_io.open_media(media, require_valid=True)
    digest = hashlib.sha256()
    try:
        with open(target, "xb") as out:
            for chunk in iter(lambda: opened.file.read(_COPY_CHUNK), b""):
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    finally:
        opened.close()
    return digest.hexdigest()


def publish(execution, *, now=None) -> ActiveBroadcastPublication:
    """The execution's library representation, creating it once. Idempotent:
    an execution that already has a publication returns it.

    The source is the execution's exact ``mix_media``: present, valid, and its
    stored bytes re-hashed to the recorded SHA-256 (and hashed again as they
    are copied). Then, under a destination lock: stage in a hidden scratch
    directory beside the destination, place with ``os.link`` (atomic, never
    overwrites), ``sync_track_file`` into a scratch waveform directory, move
    the waveform into the real directory, record the publication -- all in
    one transaction, rolled back with the placed file and waveform on any
    failure.

    Raises PlayoutError: a code in PUBLICATION_RETRYABLE leaves the exact
    aggregate valid (retry this execution); one in PUBLICATION_PERMANENT is
    an integrity contradiction."""
    from library.models import Category

    now = now or timezone.now()
    row = ActiveBroadcastExecution.objects.select_related("mix_media").get(pk=getattr(execution, "pk", execution))
    existing = ActiveBroadcastPublication.objects.filter(execution=row).first()
    if existing is not None:
        return existing
    policy = services.current_policy()
    if not gate_open(policy):
        raise PlayoutError("native airing is closed", code="native_airing_closed")
    if row.state != X.READY or row.mix_media_id is None:
        raise PlayoutError(f"a {row.state} execution is not published", code="not_ready")
    if row.policy_generation != policy.generation:
        raise PlayoutError("the execution is no longer the current composition", code="stale_generation")
    media = row.mix_media
    try:
        media_io.verify_integrity(media)
    except (MediaPurged, MediaInconsistent):
        raise PlayoutError("the aggregate's stored bytes are missing or do not verify",
                           code=AGGREGATE_UNAVAILABLE) from None
    if not media.is_valid:
        raise PlayoutError("the aggregate is not valid", code=AGGREGATE_UNAVAILABLE)
    category = Category.objects.filter(code=CATEGORY_CODE).first()
    if category is None:
        raise PlayoutError(f"no {CATEGORY_CODE} category exists", code=CATEGORY_MISSING)

    relpath = relpath_for(row.pk)
    dest = _library_root() / relpath
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return _place_and_ingest(row, media, dest, relpath, now)
    except OSError as exc:                  # the lock/scratch directory itself (library I/O)
        raise PlayoutError(f"the library is unavailable: {type(exc).__name__}", code=LIBRARY_UNAVAILABLE) from exc


def _retry_code(exc) -> str:
    """The retryable code of an unexpected error during placement/ingest."""
    from django.core.management.base import CommandError
    if isinstance(exc, CommandError):       # sync_track_file: analysis or category configuration
        return ANALYSIS_FAILED
    if isinstance(exc, OSError):
        return LIBRARY_UNAVAILABLE
    return PUBLICATION_FAILED


def _place_and_ingest(row, media, dest, relpath, now) -> ActiveBroadcastPublication:
    from library.management.commands.analyze_tracks import get_waveforms_dir
    from library.management.commands.sync_track_file import sync_track_file
    from library.models import Artist, Track

    with _destination_lock(dest):
        existing = ActiveBroadcastPublication.objects.filter(execution=row).first()
        if existing is not None:
            return existing
        placed, real_wave = False, None
        with tempfile.TemporaryDirectory(prefix=".am-publish-", dir=dest.parent) as scratch_name:
            scratch = Path(scratch_name)
            wave_scratch = scratch / "waveforms"
            wave_scratch.mkdir()
            try:
                if dest.exists():
                    # Only an interrupted earlier publish of THIS execution can have
                    # placed it; it is reused only if it is exactly the aggregate.
                    if _file_sha256(dest) != media.sha256:
                        raise PlayoutError("the reserved destination holds other bytes", code=DESTINATION_CONFLICT)
                else:
                    staged = scratch / "aggregate.flac"
                    try:
                        copied = _copy_verified(media, staged)
                    except (MediaPurged, MediaInconsistent):
                        raise PlayoutError("the aggregate's stored bytes are missing or do not verify",
                                           code=AGGREGATE_UNAVAILABLE) from None
                    if copied != media.sha256:
                        raise PlayoutError("the aggregate bytes changed while copying", code=AGGREGATE_UNAVAILABLE)
                    os.chmod(staged, 0o644)
                    os.link(staged, dest)                 # atomic and never overwrites
                    placed = True
                with transaction.atomic():
                    track, _created = sync_track_file(str(dest), ready2air=False, wave_dir=wave_scratch)
                    scratch_wave = wave_scratch / f"{track.id}.json"
                    if not scratch_wave.is_file():
                        raise PlayoutError("analysis wrote no waveform", code=ANALYSIS_FAILED)
                    real_wave = Path(get_waveforms_dir()) / f"{track.id}.json"
                    shutil.move(str(scratch_wave), str(real_wave))
                    artist, _ = Artist.get_or_create_ci(TRACK_ARTIST)
                    Track.objects.filter(pk=track.pk).update(
                        waveform_path=str(real_wave), title=TRACK_TITLE, artist=artist, ready2air=False)
                    publication = _store.insert(
                        ActiveBroadcastPublication, id=uuid.uuid4(), execution=row, media=media,
                        library_relpath=relpath, sha256=media.sha256, track=track, track_id_snapshot=track.pk,
                        published_at=now)
                    _event("published", row, actor_kind=ActorKind.WORKER,
                           detail={"track": track.pk, "library_relpath": relpath, "sha256": media.sha256})
            except BaseException as exc:
                if real_wave is not None:
                    real_wave.unlink(missing_ok=True)
                if placed:
                    dest.unlink(missing_ok=True)
                if isinstance(exc, PlayoutError) or not isinstance(exc, Exception):
                    raise
                code = _retry_code(exc)
                raise PlayoutError(f"publication failed ({code}): {type(exc).__name__}: {exc}", code=code) from exc
    return publication


def _publication_backoff_remaining(row, now) -> float:
    """Seconds until the next publication retry of ``row`` is due (the same
    exponential shape as D2A's render retry)."""
    from .models import ActiveBroadcastMessageEvent
    retries = list(ActiveBroadcastMessageEvent.objects.filter(execution=row, event="publication_retry")
                   .order_by("-id").values_list("created_at", flat=True)[:12])
    if not retries:
        return 0.0
    wait = min(PUBLICATION_BACKOFF_BASE_SECONDS * 2 ** (len(retries) - 1), PUBLICATION_BACKOFF_MAX_SECONDS)
    return max(0.0, (retries[0] + timedelta(seconds=wait) - now).total_seconds())


def record_publication_retry(execution, *, code, detail="") -> int:
    """A retryable publication failure of a READY execution: a
    ``publication_retry`` event (the backoff reads these); the execution and
    its exact ``mix_media`` are unchanged. Returns the consecutive failure
    count (0 when the execution is no longer READY)."""
    from .models import ActiveBroadcastMessageEvent
    with transaction.atomic():
        services._lock_policy()
        row = services._lock_execution(execution)
        if row.state != X.READY:
            return 0
        _event("publication_retry", row, actor_kind=ActorKind.WORKER,
               detail={"code": str(code)[:64], "detail": services._bounded_detail(detail)})
    failures = ActiveBroadcastMessageEvent.objects.filter(execution=row, event="publication_retry").count()
    if failures >= PUBLICATION_ATTENTION_AFTER:
        _attention("publication_unavailable", row.pk, f"{failures} attempts; last {code}: {detail}",
                   level="warning")
    return failures


def _publication_problem(publication) -> str:
    """Why a recorded publication can no longer be inserted, or "" (a
    database read plus one stat -- no hashing)."""
    current = ActiveBroadcastPublication.objects.filter(pk=publication.pk).values_list("track_id", flat=True).first()
    if current is None:
        return "the published Track was deleted"
    if not (_library_root() / publication.library_relpath).is_file():
        return "the published file is missing"
    return ""


# -- dispatch -------------------------------------------------------------------------------

def command_payload(row, publication, attempt) -> dict:
    """The EXISTING urgent-insertion command, plus exact identity. A
    category-only ``insert_urgent`` (Weather/AMBER, legacy OGRemote) is
    unchanged."""
    return {"command": "insert_urgent", "category": CATEGORY_CODE, "track": publication.track_id_snapshot,
            "active_execution": str(row.pk), "attempt": str(attempt)}


def _send(payload) -> None:
    from isadoraair.engine_commands import enqueue_engine_command
    try:
        enqueue_engine_command(payload)
    except Exception as exc:                 # noqa: BLE001 -- the dispatch timeout re-sends it
        _attention("command_not_queued", payload.get("active_execution"), type(exc).__name__, level="warning")


def _attention(code, execution_id, detail="", *, level="error"):
    from monitoring.models import emit_event
    emit_event("active_messages", f"Active messages playout: {code}", level=level,
               detail={"execution": str(execution_id), "code": code, "detail": services._bounded_detail(detail)},
               dedupe_key=f"active_messages|playout|{code}")


def _fail(row, code, detail="", *, actor_kind=ActorKind.WORKER):
    now = timezone.now()
    stored = _store.write(ActiveBroadcastExecution, row.pk, match={"state": row.state,
                                                                   "attempt_token": row.attempt_token},
                          values={"state": X.FAILED, "open_slot": None, "terminal_at": now, "failure_code": code,
                                  "failure_detail": services._bounded_detail(detail)})
    _event("playout_failed", stored, actor_kind=actor_kind, from_state=row.state, to_state=X.FAILED,
           detail={"code": code})
    transaction.on_commit(lambda: _attention(code, row.pk, detail))
    return stored


def _suppress(row, *, actor_kind, reason="stale_generation"):
    now = timezone.now()
    stored = _store.write(ActiveBroadcastExecution, row.pk, match={"state": row.state,
                                                                   "attempt_token": row.attempt_token},
                          values={"state": X.SUPPRESSED, "open_slot": None, "terminal_at": now,
                                  "failure_code": reason, "failure_detail": "authority moved on before it aired"})
    _event("execution_suppressed", stored, actor_kind=actor_kind, from_state=row.state, to_state=X.SUPPRESSED,
           detail={"code": reason})
    return stored


def _new_attempt(row, publication, *, now, actor_kind, redispatch) -> uuid.UUID | None:
    """Rotate to a fresh attempt and queue its command after commit (caller
    holds the policy and execution locks). Returns the token, or None when
    attempts are exhausted (the execution then FAILS)."""
    if row.attempt_count >= MAX_ATTEMPTS:
        _fail(row, DISPATCH_EXHAUSTED, f"{row.attempt_count} attempts", actor_kind=actor_kind)
        return None
    token = uuid.uuid4()
    stored = _store.write(ActiveBroadcastExecution, row.pk, match={"state": row.state,
                                                                   "attempt_token": row.attempt_token},
                          values={"attempt_token": token, "dispatched_at": now, "attempt_count": row.attempt_count + 1})
    _event("dispatched", stored, actor_kind=actor_kind,
           detail={"attempt": str(token), "track": publication.track_id_snapshot, "redispatch": redispatch})
    payload = command_payload(stored, publication, token)
    transaction.on_commit(lambda: _send(payload))
    return token


def _redispatch(row, publication, *, now, actor_kind, redispatch=True) -> uuid.UUID | None:
    """A new attempt for ``row`` (locks held), after proving its publication
    is still insertable; a deleted Track or missing file FAILS the execution
    (publication_unavailable) instead of re-sending a command that can only
    be refused."""
    problem = _publication_problem(publication)
    if problem:
        _fail(row, PUBLICATION_UNAVAILABLE, problem, actor_kind=actor_kind)
        return None
    return _new_attempt(row, publication, now=now, actor_kind=actor_kind, redispatch=redispatch)


def dispatch(execution, *, now=None):
    """READY and published -> queue the exact insertion (a new attempt). A
    command not accepted within DISPATCH_TIMEOUT_SECONDS is presumed lost and
    re-sent under a new attempt (the old one can then never open a deck).
    Returns the new attempt token, or None when nothing was sent."""
    now = now or timezone.now()
    with transaction.atomic():
        policy = services._lock_policy()
        row = services._lock_execution(execution)
        if row.state != X.READY or row.policy_generation != policy.generation or not gate_open(policy):
            return None
        publication = ActiveBroadcastPublication.objects.filter(execution=row).first()
        if publication is None:
            return None
        if row.dispatched_at is not None and now - row.dispatched_at < timedelta(seconds=DISPATCH_TIMEOUT_SECONDS):
            return None                                        # in flight
        return _redispatch(row, publication, now=now, actor_kind=ActorKind.WORKER,
                           redispatch=row.dispatched_at is not None)


# -- engine side: admission inside the splice transaction ------------------------------------

@dataclass(frozen=True)
class Admission:
    execution_id: uuid.UUID
    attempt: uuid.UUID
    track: object
    category: object
    publication_id: uuid.UUID


class Refused(Exception):
    """The command must not insert anything (stable reason)."""


def admit(data) -> Admission:
    """Inside the engine's splice transaction (PlaylistLog lock held, bounded
    lock_timeout set): may this exact insertion command splice its Track?
    Raises Refused for a duplicate, delayed, superseded, stale or mismatched
    command -- nothing is inserted then.

    Authority is taken in the established order, policy -> execution, and
    both locks are held by the splice transaction until ``record_insertion``
    commits: a composition change that won the policy lock first is seen
    (stale: refused); one that comes later waits, and deck-open authority
    remains the final binding check. The PlaylistLog lock is the engine's
    outer context; no active-message service ever takes it, so there is no
    reverse dependency. A lock-wait timeout raises: nothing is inserted (the
    engine retries; a command never accepted is re-sent by the worker)."""
    from library.models import Category, Track
    try:
        execution_id = uuid.UUID(str(data.get("active_execution")))
        attempt = uuid.UUID(str(data.get("attempt")))
        track_id = int(data.get("track"))
    except (TypeError, ValueError):
        raise Refused("malformed native insertion command") from None
    policy = services._lock_policy()
    row = ActiveBroadcastExecution.objects.select_for_update(of=("self",)).filter(pk=execution_id).first()
    if row is None:
        raise Refused("unknown execution")
    if row.attempt_token != attempt:
        raise Refused("superseded or unknown attempt")
    if ActiveBroadcastInsertion.objects.filter(attempt_token=attempt).exists():
        raise Refused("duplicate command: this attempt is already inserted")
    if row.state not in (X.READY, *ENGINE_HELD):
        raise Refused(f"execution is {row.state}")
    if row.policy_generation != policy.generation:
        raise Refused("stale generation")
    if not gate_open(policy):
        raise Refused("native airing is closed")
    publication = ActiveBroadcastPublication.objects.filter(execution=row).first()
    track = Track.objects.select_related("artist").filter(pk=track_id).first()
    if (publication is None or track is None or publication.track_id_snapshot != track_id
            or publication.media_id != row.mix_media_id
            or track.filepath != str(_library_root() / publication.library_relpath) or not is_candidate(track)):
        raise Refused("publication does not match")
    category = Category.objects.filter(code=CATEGORY_CODE).first()
    if category is None:
        raise Refused("category missing")
    return Admission(execution_id, attempt, track, category, publication.pk)


def record_insertion(admission: Admission, log_item, engine_instance: str, *, now=None) -> None:
    """In the SAME transaction as the LogItem splice: the insertion exists
    exactly when the LogItem does. READY/ACCEPTED/CLAIMED -> ACCEPTED."""
    now = now or timezone.now()
    row = ActiveBroadcastExecution.objects.get(pk=admission.execution_id)
    publication = ActiveBroadcastPublication.objects.get(pk=admission.publication_id)
    _store.insert(ActiveBroadcastInsertion, id=uuid.uuid4(), execution=row,
                  publication=publication, attempt_token=admission.attempt, log_item=log_item,
                  log_item_id_snapshot=log_item.pk, engine_instance=engine_instance, inserted_at=now)
    stored = _store.write(ActiveBroadcastExecution, row.pk, match={"state": row.state,
                                                                   "attempt_token": admission.attempt},
                          values={"state": X.ACCEPTED, "accepted_at": now, "engine_instance": engine_instance})
    _event("accepted", stored, actor_kind=ActorKind.ENGINE, from_state=row.state, to_state=X.ACCEPTED,
           detail={"attempt": str(admission.attempt), "log_item": log_item.pk})


# -- engine side: queue filtering and deck-open authority ----------------------------------------

def _insertion_for(log_item_id):
    return (ActiveBroadcastInsertion.objects.select_related("execution", "publication")
            .filter(log_item_id_snapshot=log_item_id).first())


def _judge(insertion, track, engine_instance, policy) -> str:
    """"" when this insertion may play, else the reason (no writes)."""
    if insertion is None:
        return "native urgent/PA Track without insertion authority"
    row = insertion.execution
    if track is None or insertion.publication.track_id_snapshot != getattr(track, "pk", None):
        return "native insertion does not name this Track"
    if row.attempt_token != insertion.attempt_token:
        return "superseded native insertion attempt"
    if not gate_open(policy):
        return "native airing is closed"
    if row.state == X.ACCEPTED:
        if row.policy_generation != policy.generation:
            return "stale native urgent/PA aggregate (composition changed)"
        if row.mix_media_id != insertion.publication.media_id:
            return "native aggregate identity changed"
        return ""
    if row.state in (X.CLAIMED, X.STARTED) and row.engine_instance == engine_instance:
        return ""                                             # this process's own continuation
    return f"native urgent/PA execution is {row.state}"


def inspect(log_item, engine_instance) -> Decision:
    """Advisory, read-only (queue filtering / preview). Call only for a
    candidate Track."""
    insertion = _insertion_for(getattr(log_item, "pk", None))
    reason = _judge(insertion, getattr(log_item, "track", None), engine_instance, services.current_policy())
    return Decision(not reason, reason)


def authorize_open(log_item, engine_instance, *, now=None) -> Decision | None:
    """THE deck-open authority for native Tracks (None for every other Track,
    query-free). Either CLAIMS the execution (ACCEPTED -> CLAIMED: from here
    it airs even if a clear commits later) or refuses -- a stale one is
    SUPPRESSED so its slot frees; the caller simply moves on to the next
    queued item.

    Serialized against composition changes by the policy lock, in the
    established order policy -> execution, both held in ONE short
    transaction through the gate, generation, attempt and LogItem checks,
    the claim and its event -- and released when it commits, before any
    deck construction or file open. A change that won the policy lock first
    is seen (no claim); a claim that won first stands. A lock wait beyond
    AUTHORITY_LOCK_TIMEOUT raises: the engine treats that as a refusal (no
    deck), and its not-aired report recovers the airing."""
    track = getattr(log_item, "track", None)
    if not is_candidate(track):
        return None
    now = now or timezone.now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{AUTHORITY_LOCK_TIMEOUT}'")
        insertion = _insertion_for(log_item.pk)
        if insertion is None:
            return Decision(False, "native urgent/PA Track without insertion authority")
        policy = services._lock_policy()
        row = services._lock_execution(insertion.execution_id)
        insertion.execution = row
        reason = _judge(insertion, track, engine_instance, policy)
        if reason:
            if (row.state == X.ACCEPTED and row.attempt_token == insertion.attempt_token
                    and row.policy_generation != policy.generation):
                _suppress(row, actor_kind=ActorKind.ENGINE)
            return Decision(False, reason)
        if row.state == X.ACCEPTED:
            from library.models import LogItem
            if not LogItem.objects.filter(pk=log_item.pk).exists():
                # Never claim for a deleted occurrence: it stays ACCEPTED, so
                # the engine's not-aired report re-sends it.
                return Decision(False, "native LogItem no longer exists")
            stored = _store.write(ActiveBroadcastExecution, row.pk,
                                  match={"state": X.ACCEPTED, "attempt_token": row.attempt_token},
                                  values={"state": X.CLAIMED, "claimed_at": now, "engine_instance": engine_instance})
            _event("claimed", stored, actor_kind=ActorKind.ENGINE, from_state=X.ACCEPTED, to_state=X.CLAIMED,
                   detail={"log_item": log_item.pk})
        return Decision(True)


NOT_AIRED_STAGES = frozenset({"discarded", "left_queue", "deck_not_opened"})


def report_not_aired(log_item_id, engine_instance, *, stage, now=None) -> str:
    """The engine's POSITIVE report that a native LogItem left its queue
    without airing (``stage``): ``discarded`` as unusable before a deck,
    ``left_queue`` (a queue replacement/takeover dropped it, or it was
    consumed without a deck), or ``deck_not_opened`` (deck-open refused or
    construction returned without a deck). Correlated by the insertion's
    ``log_item_id_snapshot`` -- also after the Track or LogItem row is gone.

    Only the execution's CURRENT, unstarted attempt is acted on:

    * a stale one (composition moved on, gate closed) is SUPPRESSED -- never
      re-sent;
    * ACCEPTED never passed deck-open authority, so nothing of it can have
      reached program: re-sent under a new attempt (the reported LogItem is
      then superseded forever);
    * CLAIMED is re-sent only when THIS process claimed it and positively
      never built a deck for it: the LogItem's playback claim -- committed
      before any deck construction, in every process -- never committed, and
      there is no air evidence. Anything less is ambiguous (a deck may have
      existed): it is held for operator attention and the restart fence,
      never re-sent.

    Returns a short outcome."""
    from library.models import LogItem
    if stage not in NOT_AIRED_STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    if not ActiveBroadcastInsertion.objects.filter(log_item_id_snapshot=log_item_id).exists():
        return "unknown"                     # not native (e.g. an ordinary Track-less ghost): no lock taken
    now = now or timezone.now()
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{AUTHORITY_LOCK_TIMEOUT}'")
        policy = services._lock_policy()
        insertion = _insertion_for(log_item_id)
        if insertion is None:
            return "unknown"
        row = services._lock_execution(insertion.execution_id)
        if row.attempt_token != insertion.attempt_token:
            return "superseded"
        if row.state not in ENGINE_HELD or row.started_at is not None:
            return row.state
        if _play_event(log_item_id) is not None:
            return "evidence"                                   # sync_evidence decides
        detail = {"stage": stage, "log_item": log_item_id, "attempt": str(row.attempt_token)}
        if row.state == X.CLAIMED:
            facts = (LogItem.objects.filter(pk=log_item_id)
                     .values_list("playback_claimed_at", "played_at").first())
            if row.engine_instance != engine_instance or facts != (None, None):
                _event("claimed_not_aired", row, actor_kind=ActorKind.ENGINE, detail=detail)
                transaction.on_commit(lambda: _attention(
                    "claimed_not_aired", row.pk, f"{stage}; held for the restart fence", level="warning"))
                return "held"
        if row.policy_generation != policy.generation or not gate_open(policy):
            _suppress(row, actor_kind=ActorKind.ENGINE)
            return "suppressed"
        _event("not_aired", row, actor_kind=ActorKind.ENGINE, detail=detail)
        token = _redispatch(row, insertion.publication, now=now, actor_kind=ActorKind.ENGINE)
        return "resent" if token else "failed"


# -- evidence -----------------------------------------------------------------------------------

def _play_event(log_item_id):
    from library.models import PlayEvent
    return PlayEvent.objects.filter(log_item_id_snapshot=log_item_id).first()


def _terminal(play_event):
    """(terminal?, clean?, last evidence time) from the deck's own ledger."""
    if play_event.duration_evidence_state not in ("complete", "interrupted"):
        return False, False, None
    last = play_event.duration_segments.order_by("-started_at").first()
    clean = (play_event.duration_evidence_state == "complete" and play_event.ended_at is not None
             and last is not None and last.termination_reason in CLEAN_END_REASONS)
    evidence_at = play_event.ended_at
    if evidence_at is None and last is not None:
        evidence_at = last.ended_at or last.last_confirmed_at
    return True, clean, evidence_at or play_event.started_at


def sync_evidence(execution) -> ActiveBroadcastExecution:
    """Project the deck's OWN evidence for the current attempt onto the
    execution: a PlayEvent (first real post-primer buffer) -> STARTED; its
    terminal ledger -> COMPLETED only for a clean natural end (EOS), else
    INTERRUPTED. Idempotent; invents nothing (no evidence, no change)."""
    from library.models import LogItem
    row = ActiveBroadcastExecution.objects.get(pk=getattr(execution, "pk", execution))
    if row.state not in (*ENGINE_HELD, X.STARTED) or row.attempt_token is None:
        return row
    insertion = ActiveBroadcastInsertion.objects.filter(execution=row, attempt_token=row.attempt_token).first()
    if insertion is None:
        return row
    play_event = _play_event(insertion.log_item_id_snapshot)
    if play_event is None:
        return row
    if row.state in ENGINE_HELD:
        claimed = (LogItem.objects.filter(pk=insertion.log_item_id_snapshot)
                   .values_list("playback_claimed_at", flat=True).first())
        row = services.record_air_started(row, attempt_token=row.attempt_token, started_at=play_event.started_at,
                                          claimed_at=claimed)
    terminal, clean, evidence_at = _terminal(play_event)
    if not terminal:
        return row
    seconds = Decimal(str(round(play_event.duration_played_seconds or 0.0, 3)))
    if clean:
        return services.record_air_completed(row, attempt_token=row.attempt_token, ended_at=play_event.ended_at,
                                             confirmed_duration_seconds=seconds)
    return services.record_air_interrupted(row, attempt_token=row.attempt_token,
                                           last_evidence_at=max(evidence_at, row.started_at),
                                           confirmed_duration_seconds=seconds)


def sync_for_log_item(log_item_id):
    """Engine hook after its own air-start / terminal accounting commits.
    Runs on the engine's thread, so its lock waits are bounded
    (AUTHORITY_LOCK_TIMEOUT); a timeout raises and the worker's evidence pass
    repeats the projection."""
    insertion = ActiveBroadcastInsertion.objects.filter(log_item_id_snapshot=log_item_id).first()
    if insertion is None:
        return None
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL lock_timeout = '{AUTHORITY_LOCK_TIMEOUT}'")
        return sync_evidence(insertion.execution_id)


def _abandoned_air(row):
    """A STARTED airing a previous engine process was playing: the new process
    never resumes it (deck-open authority refuses), so it ends here as
    INTERRUPTED at the last real evidence the deck ledger holds -- even when
    a clean shutdown left that ledger non-terminal. No replay follows."""
    insertion = ActiveBroadcastInsertion.objects.filter(execution=row, attempt_token=row.attempt_token).first()
    play_event = None if insertion is None else _play_event(insertion.log_item_id_snapshot)
    evidence_at, seconds = row.started_at, Decimal("0")
    if play_event is not None:
        last = play_event.duration_segments.order_by("-started_at").first()
        evidence_at = play_event.ended_at or (last and (last.ended_at or last.last_confirmed_at)) or row.started_at
        seconds = Decimal(str(round(play_event.duration_played_seconds or 0.0, 3)))
    return services.record_air_interrupted(row, attempt_token=row.attempt_token,
                                           last_evidence_at=max(evidence_at, row.started_at),
                                           confirmed_duration_seconds=seconds)


def engine_started(engine_instance, *, now=None) -> None:
    """At engine start-up, AFTER the deck ledger's own reconciliation (which
    marks a crash-cut occurrence interrupted): project evidence for in-flight
    executions; end as INTERRUPTED any airing a previous process had STARTED
    (never resumed, never replayed at once); and rotate any attempt a previous
    process accepted or claimed but never started -- its LogItem may be gone
    with that process's queue, and that process can no longer produce audio
    (the restart is the fence) -- so the airing still happens. A stale one
    is SUPPRESSED."""
    now = now or timezone.now()
    for execution_id in (ActiveBroadcastExecution.objects
                         .filter(state__in=(*ENGINE_HELD, X.STARTED)).values_list("pk", flat=True)):
        row = sync_evidence(execution_id)
        if row.state == X.STARTED and row.engine_instance != engine_instance:
            _abandoned_air(row)
            continue
        if row.state not in ENGINE_HELD or row.engine_instance == engine_instance:
            continue
        with transaction.atomic():
            policy = services._lock_policy()
            row = services._lock_execution(execution_id)
            if row.state not in ENGINE_HELD or row.engine_instance == engine_instance:
                continue
            if row.policy_generation != policy.generation or not gate_open(policy):
                _suppress(row, actor_kind=ActorKind.ENGINE)
                continue
            publication = ActiveBroadcastPublication.objects.filter(execution=row).first()
            if publication is not None:
                _redispatch(row, publication, now=now, actor_kind=ActorKind.ENGINE)


# -- the worker's playout step --------------------------------------------------------------------

def _engine_held_step(row, now) -> str:
    """Worker step for an ACCEPTED/CLAIMED execution after evidence sync.
    Never rotates an attempt because time passed: an ACCEPTED item may
    legitimately wait in the queue indefinitely, and a CLAIMED one may have
    a deck. Acts only on positive evidence:

    * a stale ACCEPTED one is SUPPRESSED (deck-open would refuse it);
    * a re-sent attempt the engine never ACCEPTED within the command-delivery
      bound is re-sent again (as for a READY dispatch);
    * an ACCEPTED one whose LogItem row was deleted is re-sent;
    * a CLAIMED one without air evidence after CLAIMED_ATTENTION_SECONDS
      raises attention -- only."""
    with transaction.atomic():
        policy = services._lock_policy()
        locked = services._lock_execution(row.pk)
        if locked.state not in ENGINE_HELD:
            return locked.state
        insertion = (ActiveBroadcastInsertion.objects.select_related("publication")
                     .filter(execution=locked, attempt_token=locked.attempt_token).first())
        stale = locked.policy_generation != policy.generation or not gate_open(policy)
        if stale and (locked.state == X.ACCEPTED or insertion is None):
            _suppress(locked, actor_kind=ActorKind.WORKER)
            return "suppressed"
        if insertion is None:
            # The current attempt was re-sent and its command not yet accepted.
            if (locked.dispatched_at is not None
                    and now - locked.dispatched_at < timedelta(seconds=DISPATCH_TIMEOUT_SECONDS)):
                return "in_flight"
            publication = ActiveBroadcastPublication.objects.filter(execution=locked).first()
            if publication is None:
                return locked.state
            return "resent" if _redispatch(locked, publication, now=now, actor_kind=ActorKind.WORKER) else "failed"
        if locked.state == X.ACCEPTED and insertion.log_item_id is None:
            _event("not_aired", locked, actor_kind=ActorKind.WORKER,
                   detail={"stage": "log_item_deleted", "log_item": insertion.log_item_id_snapshot,
                           "attempt": str(locked.attempt_token)})
            return "resent" if _redispatch(locked, insertion.publication, now=now,
                                           actor_kind=ActorKind.WORKER) else "failed"
        if (locked.state == X.CLAIMED and locked.claimed_at is not None
                and now - locked.claimed_at > timedelta(seconds=CLAIMED_ATTENTION_SECONDS)):
            transaction.on_commit(lambda: _attention(
                "claimed_not_started", locked.pk, "claimed without air evidence; not re-sent (restart fences)",
                level="warning"))
        return locked.state


def _publication_step(row, now) -> str:
    """Publish a READY execution (or retry its publication) and dispatch."""
    if not ActiveBroadcastPublication.objects.filter(execution=row).exists():
        if _publication_backoff_remaining(row, now) > 0:
            return "publication_backoff"
        try:
            publish(row, now=now)
        except PlayoutError as exc:
            if exc.code in PUBLICATION_RETRYABLE:
                record_publication_retry(row, code=exc.code, detail=str(exc))
                return "publication_retry"
            if exc.code in PUBLICATION_PERMANENT:
                with transaction.atomic():
                    services._lock_policy()
                    locked = services._lock_execution(row.pk)
                    if locked.state == X.READY:
                        _fail(locked, exc.code, str(exc))
                return "publication_failed"
            return exc.code                                    # not_ready / stale_generation / gate (a race)
    return "dispatched" if dispatch(row, now=now) else "in_flight"


def advance(*, now=None) -> str:
    """One worker step for the current unfinished execution past rendering:
    project deck evidence (engine-held states), act on positive recovery
    evidence, and publish + dispatch a READY one (gate permitting). Returns a
    short outcome."""
    now = now or timezone.now()
    row = (ActiveBroadcastExecution.objects
           .filter(state__in=(X.READY, *ENGINE_HELD, X.STARTED)).first())
    if row is None:
        return "none"
    if row.state in (*ENGINE_HELD, X.STARTED):
        row = sync_evidence(row)
        if row.state in ENGINE_HELD:
            return _engine_held_step(row, now)
        return row.state
    if not gate_open():
        return "gated"
    return _publication_step(row, now)
