import json
import os
import subprocess
import time
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import close_old_connections, connection, transaction
from django.db.utils import OperationalError
from django.utils import timezone

from library.models import Category, LogItem, PlaylistLog, RecencyConfig, Track
from library.services.log_builder import get_recent_exclusions, get_separation
from monitoring.models import emit_event

from .models import SongRequest, WebRequestConfig

# Kokoro CLI wrapper -- same binary/argument shape weather-ingest's
# lib/voices.py already uses successfully in production
# (`[binary, "--model", model, "--output_file", wav_path]`, verified
# directly against the installed wrapper's --help too). am_fenrir is the
# voice earmarked in PROJECT_NOTES.md for exactly this kind of
# machine-driven announcement -- distinct enough from the weather
# personas (Claira/Max) that a listener won't mistake one for the other.
KOKORO_BINARY = "/home/jreed/kokoro/bin/kokoro_synth"
DEDICATION_VOICE = "am_fenrir"
DEDICATION_ROOT = Path(settings.LIBRARY_ROOT) / "Dedications"

# Same path engine.py's STATE_PATH writes to -- redefined here rather
# than imported, since library.services.engine imports FROM this module
# (maybe_schedule_song_request) and importing engine.py back would be
# circular. Just a plain path constant, safe to duplicate.
ENGINE_STATE_PATH = Path("/run/isadoraair/engine_state.json")
# If the engine hasn't written a fresh state in this long, don't trust
# it (crashed/hung) -- fall back to the static schedule instead.
STATE_STALENESS_SECONDS = 30

# Postgres SQLSTATE for "lock not available" -- fired identically
# whether contention is immediate (NOWAIT) or a bounded lock_timeout
# expires, so one check covers both acquisition strategies below.
_LOCK_NOT_AVAILABLE = "55P03"

# Distinct sentinel maybe_schedule_song_request can return instead of a
# LogItem: "we genuinely don't know if this row's data is current right
# now -- do not use ANY version of it for playback." Deliberately not a
# fresh unlocked read (see maybe_schedule_song_request's docstring) --
# an unlocked read while another transaction is still uncommitted would
# just return the same pre-contention data, offering no real guarantee.
SCHEDULING_CONTENDED = object()


def _read_engine_state():
    """Parses engine_state.json if it exists and is fresh; None
    otherwise (missing, unparseable, or the engine hasn't written a
    fresh tick in STATE_STALENESS_SECONDS -- crashed/hung). Shared by
    every function below that needs to know what the running engine is
    actually doing right now, so there's exactly one definition of
    "trustworthy state" across all of them."""
    try:
        state = json.loads(ENGINE_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if time.time() - state.get("timestamp", 0) > STATE_STALENESS_SECONDS:
        return None
    return state


def _live_eta_datetime(log_item_id):
    """Real-world air time for `log_item_id`, from the running engine's
    own drift-corrected eta_seconds (engine_state.json, written by
    _write_state every poll tick) rather than the static
    LogItem.scheduled_time baked in when the hour's log was built. Real
    playback drifts from that original plan over a broadcast hour
    (crossfade points and actual track durations don't line up exactly
    with the built schedule) -- exactly the gap an operator caught live
    comparing this feature's estimate against the real "Coming Up"
    dashboard, which already computes its "Time On" column this same
    way. Returns None (caller falls back to scheduled_time) if the
    engine isn't running, the state is stale, or the item isn't in its
    current preview (e.g. a later hour the engine hasn't loaded yet)."""
    state = _read_engine_state()
    if state is None:
        return None

    for deck in state.get("decks", {}).values():
        if deck and deck.get("log_item_id") == log_item_id:
            return timezone.now()  # already airing

    for item in state.get("queue", []):
        if item.get("item_id") == log_item_id:
            return timezone.now() + timedelta(seconds=item.get("eta_seconds", 0))

    return None


def estimate_air_time(log_item):
    """Best available air-time estimate for `log_item` -- the running
    engine's live, drift-corrected ETA when available, else the static
    scheduled_time it was built with. Shared by both scheduling sites
    so a requester sees the same kind of number the real dashboard
    shows, not the log-build-time plan."""
    return _live_eta_datetime(log_item.id) or log_item.scheduled_time


def track_is_available(track):
    """Ready to air right now, independent of recency: deleted,
    ready2air flipped off, recategorized out of music, or its file gone
    are permanent-until-fixed problems -- unlike recency, which is
    temporary and time-shifting and shouldn't invalidate an
    already-committed schedule slot the same way. Factored out of
    is_track_eligible_at's first three checks so both real scheduling
    and reconciliation's "is this still airable" check share one
    definition instead of two that could drift apart."""
    if track is None or not track.ready2air:
        return False
    if track.category_id is None or track.category.kind.code != "music":
        return False
    return bool(track.filepath) and Path(track.filepath).is_file()


def is_track_eligible_at(track, target_datetime, recency_cfg):
    """Would `track` actually be allowed to air at `target_datetime`
    right now -- available (see track_is_available) and not
    recency-blocked (get_separation/get_recent_exclusions, the same
    functions log_builder itself uses for a normal rotation pick,
    evaluated relative to target_datetime rather than always "now" -- a
    track blocked at the current moment can still be legitimately
    eligible at a slot far enough in the future). Shared between
    maybe_schedule_song_request (real scheduling, checked against
    max(now, the target slot's own scheduled_time) -- see its
    docstring) and refresh_song_request_statuses' advisory estimate
    pass (checked against each candidate future slot's own time, so the
    estimate it shows a requester doesn't promise a slot recency would
    actually block)."""
    if not track_is_available(track):
        return False

    artist_sep, title_sep = get_separation(track.category, recency_cfg)
    exclude_track_ids, exclude_artist_ids = get_recent_exclusions(
        target_datetime, artist_sep, title_sep, set(), set(),
    )
    return track.id not in exclude_track_ids and track.artist_id not in exclude_artist_ids


def classify_log_item(log_item, state):
    """Classifies one LogItem against a single engine_state.json
    snapshot (the caller reads it ONCE per command run and passes the
    same `state` to every call in that run, so every request/candidate
    considered in one run is judged against the same reality instead of
    a torn snapshot from a mid-run rewrite by the engine):

      AIRING   -- present on a deck right now.
      QUEUED   -- present in the engine's live upcoming queue.
      FUTURE   -- belongs to an hour strictly newer than the engine's
                  current active hour -- the engine hasn't loaded it
                  yet, but nothing says it's invalid (e.g. an admin
                  built and approved it well ahead of time).
      STRANDED -- log_item is None, its PlaylistLog isn't approved (so
                  the engine will never load it as-is), or it belongs
                  to the active hour or an OLDER one but isn't airing
                  or queued -- the engine's active queue has already
                  moved past it without ever playing it.
      UNKNOWN  -- state is missing/stale, and none of the
                  state-independent checks above already resolved it.
                  Callers must NOT treat this as stranded -- only skip
                  and recheck next cycle."""
    if log_item is None:
        return "STRANDED"
    if log_item.playlist_log.status != "approved":
        return "STRANDED"
    if state is None:
        return "UNKNOWN"

    for deck in state.get("decks", {}).values():
        if deck and deck.get("log_item_id") == log_item.id:
            return "AIRING"
    if any(item.get("item_id") == log_item.id for item in state.get("queue", [])):
        return "QUEUED"

    if state.get("date") is None or state.get("hour") is None:
        return "UNKNOWN"
    active_key = (state["date"], state["hour"])
    item_key = (log_item.playlist_log.date.isoformat(), log_item.playlist_log.hour)
    return "FUTURE" if item_key > active_key else "STRANDED"


def mark_song_requests_aired(log_item, aired_at):
    """Promotes every SongRequest scheduled into log_item to fulfilled,
    the instant it actually starts playing -- called from engine.py's
    _create_deck right after LogItem.played_at itself is successfully
    written (and only then; see _create_deck's played_at_written guard
    -- this must not fire off a failed or skipped played_at write).

    Filtered on BOTH log_item_id and track_id: if the LogItem's track
    changed again after this request was scheduled into it (a
    concurrent re-swap, a manual admin edit), this correctly declines
    to credit the request with a song that isn't actually what aired.
    A bulk .update() rather than a single row's .save() so multiple
    requests collapsed onto the same track (see maybe_schedule_song_
    request's collapse step) all promote together, same as scheduling."""
    SongRequest.objects.filter(
        status="scheduled", log_item_id=log_item.id, track_id=log_item.track_id,
    ).update(
        status="fulfilled", fulfilled_at=aired_at, resolved_at=aired_at,
        estimated_play_time=aired_at, status_updated_at=aired_at,
    )


def maybe_schedule_song_request(log_item):
    """Web Requests scheduling. If `log_item` is a music-kind slot in
    an open request hour and there's an eligible waiting request, swaps
    the track in-place (mutating track / track_title / track_artist)
    and marks that request `scheduled` -- NOT `fulfilled`: fulfillment
    now only happens once the track actually starts airing (see
    mark_song_requests_aired, called from engine.py's _create_deck).

    Called from two places:
      1. refresh_song_request_statuses (every ~20s, on every upcoming
         open/eligible LogItem within the lookahead window) -- this is
         the one that makes a scheduled request show up in "Up Next"
         well before it airs.
      2. engine.py's _start_next_track, right before _create_deck, as a
         last-second safety net for a request that only became
         eligible in the last few seconds.

    Returns the LogItem the caller must use from here on -- almost
    always a DIFFERENT Python object than the one passed in, since
    locking (below) requires a fresh database fetch. A caller that
    ignores this and keeps using its original object (as the old,
    pre-locking version of this function allowed, since it mutated the
    caller's object in place) can end up handing a stale, unswapped
    track to _create_deck while the database and public site correctly
    show the requested one -- the engine's call site MUST do
    `log_item = maybe_schedule_song_request(log_item) or log_item`.

    On genuine lock contention that doesn't clear within a short bounded
    wait, returns the SCHEDULING_CONTENDED sentinel instead of any
    LogItem at all -- see the SCHEDULING_CONTENDED docstring above for
    why a fallback read can't safely stand in here. The engine's call
    site must treat that exactly like its existing "couldn't play this
    one" path: skip _create_deck for this item entirely and move on to
    whatever's queued next. The skipped item's played_at simply stays
    NULL, same as any other unplayed item -- whatever request was
    concurrently being scheduled into it by the other transaction is
    unaffected, and reconciliation (refresh_song_request_statuses) will
    correctly pick up from wherever things land.

    Locks the target PlaylistLog row (not just the LogItem) before
    counting used capacity, so two concurrent calls targeting DIFFERENT
    LogItems in the SAME hour can't each read a stale capacity count and
    both schedule past the cap -- locking only the target LogItem would
    serialize calls to that one item but do nothing for that broader
    race. Lock order is always PlaylistLog then LogItem.

    Capacity ("max_fulfilled_per_hour") is spent by the DISTINCT LogItem
    being reserved, not by request rows: an already-reserved LogItem
    (whether reserved via a fresh swap or because rotation had already
    organically picked the same song some other pending request wants)
    lets any number of OTHER waiting requests for that same track
    collapse onto it for free -- N listeners requesting one song still
    costs exactly one slot. A LogItem being reserved for the FIRST time,
    whether via a fresh swap or as the first organic-match collapse,
    consumes exactly one unit either way -- both the primary pick and
    the collapse step are gated on the same capacity check for that
    reason (an earlier draft let collapse run completely uncapped,
    which was internally inconsistent under either possible capacity
    policy, not just imprecise under one of them).

    Recency and open_slots are checked against the TARGET SLOT's own
    hour/time (max(now, log_item.scheduled_time)), not against "now" --
    this function can be called for a LogItem well ahead of the current
    hour, and checking "now"'s open_slots membership or recency state
    against a future item's own hour was wrong.

    Wrapped in a blanket try/except: a bug in this feature must never
    be able to stop a track from starting (engine caller) or wedge the
    periodic refresh cycle (management-command caller). That outer
    handler returns the ORIGINAL log_item unchanged (not the
    SCHEDULING_CONTENDED sentinel, which is reserved specifically for
    the "we don't know if this row is current" case) -- an unexpected
    error doesn't mean the row's existing data is untrustworthy, so
    falling back to "play what was already there" remains the safer
    non-fatal default."""
    try:
        if log_item is None or log_item.track_id is None:
            return log_item
        if log_item.category_id is None or not log_item.category.kind_id:
            return log_item
        if log_item.category.kind.code != "music":
            return log_item

        close_old_connections()
        cfg = WebRequestConfig.load()
        if not cfg.enabled:
            return log_item

        recency_cfg = RecencyConfig.load()
        now = timezone.now()

        try:
            with transaction.atomic():
                with connection.cursor() as cur:
                    # Bounded wait rather than NOWAIT: gives a
                    # genuinely concurrent, short transaction (the
                    # common case) a real chance to finish and be seen,
                    # instead of failing instantly -- while still
                    # keeping the engine's playback thread from ever
                    # blocking unboundedly.
                    cur.execute("SET LOCAL lock_timeout = '250ms'")

                locked_log = PlaylistLog.objects.select_for_update().get(
                    pk=log_item.playlist_log_id, status="approved",
                )
                # of=("self",): lock only the LogItem row itself, not
                # the joined category/category__kind rows -- Postgres
                # refuses FOR UPDATE across a LEFT OUTER JOIN on a
                # nullable FK ("FOR UPDATE cannot be applied to the
                # nullable side of an outer join"), and category is
                # nullable here.
                locked_item = (
                    LogItem.objects.select_for_update(of=("self",))
                    .select_related("category", "category__kind")
                    .get(pk=log_item.pk)
                )

                # Re-validate against the FRESH, locked row -- the
                # caller's object (checked above) may be stale by the
                # time the lock is actually acquired.
                if (
                    locked_item.played_at is not None
                    or locked_item.track_id is None
                    or locked_item.category_id is None
                    or locked_item.category.kind.code != "music"
                ):
                    return locked_item

                slot_time = timezone.localtime(locked_item.scheduled_time)
                if slot_time.weekday() * 24 + slot_time.hour not in cfg.open_slots:
                    return locked_item
                eligibility_time = max(now, locked_item.scheduled_time)

                already_reserved = SongRequest.objects.filter(
                    status="scheduled", log_item_id=locked_item.id,
                ).exists()
                used_slots = (
                    SongRequest.objects.filter(
                        status__in=("scheduled", "fulfilled"),
                        log_item__playlist_log_id=locked_log.id,
                        log_item_id__isnull=False,
                    )
                    .values("log_item_id").distinct().count()
                )
                capacity_available = already_reserved or used_slots < cfg.max_fulfilled_per_hour

                if capacity_available and not already_reserved:
                    # Same of=("self",) reasoning as locked_item above --
                    # track/track__category are both nullable FKs, so an
                    # unrestricted FOR UPDATE here hits the same Postgres
                    # outer-join restriction.
                    candidates = (
                        SongRequest.objects.select_for_update(skip_locked=True, of=("self",))
                        .filter(status__in=SongRequest.WAITING_STATUSES)
                        .exclude(track__isnull=True)
                        .select_related("track", "track__artist", "track__category", "track__category__kind")
                        .order_by("submitted_at")
                    )
                    for candidate in candidates:
                        track = candidate.track
                        if not is_track_eligible_at(track, eligibility_time, recency_cfg):
                            continue  # ineligible or recency-blocked right now -- leave waiting for a later slot

                        locked_item.track = track
                        locked_item.track_title = track.title
                        locked_item.track_artist = track.artist.name if track.artist_id else ""
                        locked_item.save(update_fields=["track", "track_title", "track_artist"])

                        candidate.status = "scheduled"
                        candidate.log_item = locked_item
                        candidate.scheduled_at = now
                        candidate.fulfilled_at = None
                        candidate.resolved_at = None
                        candidate.estimated_play_time = estimate_air_time(locked_item)
                        candidate.status_updated_at = now
                        candidate.save(update_fields=[
                            "status", "log_item", "scheduled_at", "fulfilled_at",
                            "resolved_at", "estimated_play_time", "status_updated_at",
                        ])
                        print(f"  Web request scheduled: {track.artist.name if track.artist_id else '?'} - {track.title} (request id={candidate.external_request_id})")
                        break

                if capacity_available:
                    # Collapse: any OTHER still-waiting request for
                    # whatever track locked_item now holds (whether
                    # just swapped above, or already there via ordinary
                    # rotation) rides along on this same slot --
                    # unconditional given capacity_available, since it
                    # doesn't consume a SECOND unit of capacity (see
                    # docstring). select_for_update(skip_locked=True)
                    # first, then update only the ids actually locked,
                    # so this can't block waiting on a row a DIFFERENT
                    # concurrent scheduling attempt holds -- the
                    # bounded-wait guarantee for the engine's caller
                    # must hold end-to-end, not just for the primary pick.
                    collapse_ids = list(
                        SongRequest.objects.select_for_update(skip_locked=True)
                        .filter(status__in=SongRequest.WAITING_STATUSES, track_id=locked_item.track_id)
                        .values_list("id", flat=True)
                    )
                    if collapse_ids:
                        SongRequest.objects.filter(id__in=collapse_ids).update(
                            status="scheduled", log_item=locked_item, scheduled_at=now,
                            fulfilled_at=None, resolved_at=None,
                            estimated_play_time=estimate_air_time(locked_item),
                            status_updated_at=now,
                        )

                return locked_item
        except OperationalError as exc:
            pgcode = getattr(getattr(exc, "__cause__", None), "pgcode", None)
            if pgcode != _LOCK_NOT_AVAILABLE:
                raise  # a real DB error -- must not be silently swallowed as "just contention"
            print(f"  Web request scheduling contended for log_item={log_item.id} -- skipping this cycle")
            return SCHEDULING_CONTENDED
    except Exception as exc:
        print(f"  Web request scheduling check failed (non-fatal): {exc}")
        emit_event(
            category="engine", level="warning",
            title="Web request scheduling check failed",
            detail={"error": str(exc), "log_item_id": getattr(log_item, "id", None)},
            dedupe_key="engine|webrequest-fulfill-error",
        )
        return log_item


def build_dedication_intro_text(track, requester_name, dedication_message):
    """'Now here's TITLE by ARTIST[, dedication text]. Thanks NAME for
    your dedication/request.' requester_name is required on the public
    site (client+server validated there), so the no-name case isn't
    expected here -- handled defensively anyway (drop the closing
    sentence) for consistency with how the site's own live preview
    resolves the same edge case. Track.artist is a non-nullable FK, so
    no defensive empty-string branch is needed there."""
    sentence = f"Now here's {track.title} by {track.artist.name}"
    dedication_message = " ".join((dedication_message or "").split())  # collapse newlines/whitespace
    if dedication_message:
        sentence += f", {dedication_message}"
    if not sentence.endswith((".", "!", "?")):
        sentence += "."
    requester_name = (requester_name or "").strip()
    if requester_name:
        kind = "dedication" if dedication_message else "request"
        sentence += f" Thanks {requester_name} for your {kind}."
    return sentence


def _probe_duration(path):
    """Same ffprobe pattern as library/management/commands/
    prep_mitd_show.py's _probe_duration -- duration in seconds as a float."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return float(out.stdout.strip())


def synthesize_dedication_intro(req):
    """Renders req's spoken intro via Kokoro, converts to FLAC, and
    attaches it as req.intro_track -- called from the standalone
    generate_dedication_intros command (its own timer, deliberately kept
    OUT of refresh_song_request_statuses, which must stay fast and
    reliable; Kokoro+ffmpeg together can take tens of seconds worst
    case). Whole body in one try/except: a failure on one request must
    not stop the command's loop over the others.

    Returns True iff req.intro_track actually got set -- callers should
    use this return value rather than a follow-up refresh_from_db() (a
    deleted request or a DB hiccup right at that moment would otherwise
    raise OUTSIDE this function's own exception boundary and abort the
    caller's whole loop over the remaining candidates)."""
    try:
        track = req.track  # the REQUEST's own stable FK, not log_item.track,
        # which can in principle change during the several seconds synthesis takes
        text = build_dedication_intro_text(track, req.requester_name, req.dedication_message)

        DEDICATION_ROOT.mkdir(parents=True, exist_ok=True)
        final_path = DEDICATION_ROOT / f"request-{req.id}.flac"  # LOCAL pk only --
        # external_request_id is an opaque string from a system we don't
        # control, never trusted unsanitized in a filesystem path
        tmp_wav = DEDICATION_ROOT / f".request-{req.id}.{os.getpid()}.tmp.wav"
        tmp_flac = DEDICATION_ROOT / f".request-{req.id}.{os.getpid()}.tmp.flac"
        try:
            subprocess.run(
                [KOKORO_BINARY, "--model", DEDICATION_VOICE, "--output_file", str(tmp_wav)],
                input=text.encode("utf-8"), check=True, timeout=30,
            )
            subprocess.run(["ffmpeg", "-y", "-i", str(tmp_wav), str(tmp_flac)],
                            check=True, timeout=30, capture_output=True)
            duration = _probe_duration(tmp_flac)
            os.replace(tmp_flac, final_path)  # atomic -- no reader ever sees a partial file
        finally:
            for p in (tmp_wav, tmp_flac):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass

        with transaction.atomic():
            # Metadata mirrors the REQUESTED SONG -- _create_deck() calls
            # _write_now_playing(track) for every fresh deck, so stream
            # metadata, RBDS, and the engine dashboard correctly show
            # "Free Fallin' - Tom Petty" from the moment the intro
            # starts, not an internal id. TuneIn specifically is
            # DIFFERENT -- it's driven by the PlayEvent ledger (dedupes
            # on PlayEvent id), not _write_now_playing, and
            # Dedications-category plays deliberately don't create a
            # PlayEvent (see _create_deck) -- so TuneIn correctly keeps
            # showing the previous song through the announcement and
            # only updates once the requested song's own PlayEvent is
            # created a few seconds later. That's correct, not a gap:
            # driven by the same royalty-safe ledger as everything else,
            # rather than updating early off an announcement that isn't
            # itself a performance.
            intro_track, _ = Track.objects.update_or_create(
                filepath=str(final_path),
                defaults=dict(
                    filename=final_path.name, format="flac",
                    title=track.title, artist=track.artist,
                    duration_seconds=duration, cue_in_seconds=0,
                    # Explicit non-null next_start_seconds opts OUT of
                    # isadoraair-analyze.timer's periodic sweep (only
                    # re-analyzes next_start_seconds__isnull=True rows)
                    # -- that sweep's envelope-threshold cue-point
                    # detection is built for music, not a few seconds of
                    # speech, and risks firing the crossfade mid-sentence.
                    next_start_seconds=duration,
                    category=Category.objects.get(code="Dedications"),
                    ready2air=True,
                ),
            )
            updated = SongRequest.objects.filter(
                id=req.id, status="scheduled", track_id=req.track_id, intro_track__isnull=True,
            ).update(intro_track=intro_track)

        if not updated:
            # Request resolved to something else while synthesis ran, or
            # a concurrent run already attached this same Track (the
            # deterministic path + update_or_create means both runs
            # converge on the SAME row). Only clean up if NOTHING still
            # references it -- a SongRequest FK or a LogItem (already
            # spliced/aired).
            still_referenced = (
                SongRequest.objects.filter(intro_track=intro_track).exists()
                or LogItem.objects.filter(track=intro_track).exists()
            )
            if not still_referenced:
                intro_track.delete()
                try:
                    final_path.unlink()
                except FileNotFoundError:
                    pass
            return False

        # Waveform display data (samples_left/right + waveform_path) --
        # the same analyze_one_track() call api_library_upload and
        # sync_track_file already use for a fresh Track outside the
        # normal timer sweep (see library/views.py). Needed here for
        # the same reason those call sites need it: next_start_seconds
        # being pre-set above (deliberately, to opt OUT of
        # isadoraair-analyze.timer's periodic sweep) means that sweep's
        # own `next_start_seconds__isnull=True` filter would otherwise
        # never pick this row up, leaving it with no waveform in the UI
        # at all, forever. Own try/except: a failure here must not
        # undo the intro_track attachment that already succeeded above,
        # or report this call as a failure -- the only thing that
        # actually matters (the request having a playable intro) is
        # already done regardless.
        try:
            from library.management.commands.analyze_tracks import analyze_one_track, get_waveforms_dir
            from library.models import AnalysisConfig
            cfg = AnalysisConfig.load()
            cfg_values = (
                cfg.analysis_sample_rate, cfg.analysis_window_seconds, cfg.waveform_points,
                cfg.next_start_threshold_db, cfg.cue_in_threshold_db, cfg.cue_in_min_seconds,
            )
            row = (
                intro_track.id, intro_track.filepath, intro_track.filename,
                intro_track.duration_seconds, intro_track.title,
                intro_track.artist.name if intro_track.artist_id else "", "",
            )
            analyze_one_track(row, cfg_values, get_waveforms_dir(), force=True)
            # analyze_one_track's own envelope-threshold next_start/cue_in
            # detection is built for music, not a few seconds of speech --
            # re-assert the deliberate values from above regardless of
            # whatever it guessed, same reasoning as the comment on
            # next_start_seconds up in the update_or_create call.
            Track.objects.filter(id=intro_track.id).update(
                next_start_seconds=duration, cue_in_seconds=0,
            )
        except Exception as exc:
            print(f"  Dedication waveform generation failed for request {req.id} (non-fatal): {exc}")

        return True
    except Exception as exc:
        print(f"  Dedication intro synthesis failed for request {req.id} (non-fatal, retried next cycle): {exc}")
        emit_event(
            category="webrequests", level="warning", title="Dedication intro synthesis failed",
            detail={"request_id": req.external_request_id, "error": str(exc)},
            dedupe_key=f"webrequests|dedication-synth-failed|{req.id}",
        )
        # The FLAC write happens BEFORE the DB transaction -- if that
        # transaction is what failed (Track never got committed), the
        # file is now a true orphan (not just "unattached," genuinely
        # unowned). Best-effort, conservative cleanup: only remove it if
        # no Track row claims this exact path.
        try:
            final_path = DEDICATION_ROOT / f"request-{req.id}.flac"
            if final_path.exists() and not Track.objects.filter(filepath=str(final_path)).exists():
                final_path.unlink()
        except Exception:
            pass  # already in the outer failure handler -- never let cleanup itself raise
        return False
