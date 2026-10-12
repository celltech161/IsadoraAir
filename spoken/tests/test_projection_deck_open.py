"""C2B corrective: the REAL engine deck-open path for spoken projection.

Nothing on the deck path is mocked here: the real ``_create_deck`` wrapper,
the real ``_claim_playback_occurrence`` (whose connection recycling exposed
B1 under the project's CONN_MAX_AGE=0), a real GStreamer pipeline set to
PLAYING so ``sync_state_with_parent()`` really opens the file, and a real
second PostgreSQL session for promotion.  It pins:

* B1 -- the shared deck-open lock is held by ONE backend from the
  authoritative re-read, through the real claim, to the file open, and is
  released on that backend with no exception after the deck is live;
* R16/R17 -- crash-renamed bytes and cached occurrences never open;
* B2 -- ordinary Tracks keep pre-C2B eligibility (no Ready-to-Air rule);
* B3 -- the 250 ms preview path adds no query for an ordinary item.
"""
import hashlib
import itertools
import json
import os
import threading
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from django.contrib.auth import get_user_model  # noqa: E402
from django.db import connection, connections  # noqa: E402
from django.db.models import QuerySet  # noqa: E402
from django.test import RequestFactory, TestCase, TransactionTestCase  # noqa: E402
from django.test.utils import CaptureQueriesContext  # noqa: E402
from django.utils import timezone  # noqa: E402

from library.models import Artist, Category, CategoryKind, LogItem, PlayEvent, PlaylistLog, Track  # noqa: E402
from library.services import engine as engine_module  # noqa: E402
from library.services.engine import PlaybackEngine  # noqa: E402
from library.tests.test_engine_deck_lifecycle import _make_real_engine  # noqa: E402
from monitoring.models import SystemEvent  # noqa: E402
from spoken import delivery, library_files, playout, projection_lock  # noqa: E402
from spoken.models import AssetState, EditionState, SpokenAsset, SpokenAssetEdition  # noqa: E402

from .projection_support import FFMPEG, ProjectionMixin, flac  # noqa: E402

_HOURS = itertools.count()


def occurrence(track):
    """A persisted future LogItem, loaded the way the engine loads its queue
    (the Track instance is cached on it from this moment)."""
    n = next(_HOURS)
    log = PlaylistLog.objects.create(date=date(2031, 1, 1) + timedelta(days=n // 24), hour=n % 24, status="approved")
    item = LogItem.objects.create(playlist_log=log, position=0, scheduled_time=timezone.now() + timedelta(hours=1),
                                  track=track, track_title=track.title)
    return LogItem.objects.select_related("track", "category").get(pk=item.pk)


def backend_pid():
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_backend_pid()")
        return cursor.fetchone()[0]


def shared_holders(asset_id):
    """Backends holding the asset's deck-open lock in shared mode."""
    first, second = projection_lock.key(asset_id)
    with connection.cursor() as cursor:
        cursor.execute("SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND granted AND mode = 'ShareLock' "
                       "AND classid = %s::bigint::oid AND objid = %s::bigint::oid AND objsubid = 2",
                       [first & 0xFFFFFFFF, second & 0xFFFFFFFF])
        return sorted(row[0] for row in cursor.fetchall())


def exclusive_available_elsewhere(asset_id):
    """Could ANOTHER session take the exclusive projection lock right now?"""
    result = []

    def probe():
        try:
            with projection_lock.session(asset_id, wait=False) as acquired:
                result.append(acquired)
        finally:
            connections.close_all()
    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(30)
    return result[0]


def open_fds(path):
    found = []
    for name in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{name}")
        except OSError:
            continue
        if target in (str(path), f"{path} (deleted)"):
            found.append(int(name))
    return found


def fd_sha(fd):
    with open(f"/proc/self/fd/{fd}", "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class RealEngineMixin(ProjectionMixin):
    """A real-topology engine whose deck path is entirely real, including
    the playback-occurrence claim."""

    def setUp(self):
        super().setUp()
        self.engine = _make_real_engine()
        del self.engine._claim_playback_occurrence        # the harness stubs it; use PlaybackEngine's
        self.engine.main_pipeline.set_state(Gst.State.PLAYING)   # so synchronization opens the file
        self.addCleanup(self._stop_engine)

    def _stop_engine(self):
        for coordinator in self.engine._deck_teardowns.values():
            coordinator.stop()
        self.engine.main_pipeline.set_state(Gst.State.NULL)
        for coordinator in self.engine._deck_teardowns.values():
            if coordinator._worker is not None:
                coordinator._worker.join(timeout=5.0)

    def discard(self, deck):
        deck.pipeline.set_state(Gst.State.NULL)
        self.engine.main_pipeline.remove(deck.pipeline)
        self.engine.mixer.release_request_pad(deck.mixer_pad)
        with self.engine._lock:
            for slot, current in self.engine.decks.items():
                if current is deck:
                    self.engine.decks[slot] = None
            self.engine._deck_bin_map.pop(id(deck.pipeline), None)

    def claimed(self, item):
        return LogItem.objects.get(pk=item.pk).playback_claimed_at is not None

    def open_a(self, item, track, expected_sha):
        """The deck opens and its file descriptor holds ``expected_sha``."""
        deck = self.engine._create_deck("A", item)
        self.assertIsNotNone(deck)
        fds = open_fds(track.filepath)
        self.assertTrue(fds, "the deck did not open the file")
        self.assertEqual(fd_sha(fds[0]), expected_sha)
        self.discard(deck)

    def refused(self, item):
        played = PlayEvent.objects.count()
        claimed = self.claimed(item)
        # (a paused promotion in this process may itself hold the published
        # file open; threads share the fd table, so compare before/after)
        before = set(open_fds(item.track.filepath))
        deck = self.engine._create_deck("A", item)
        self.assertIsNone(deck)
        self.assertEqual(self.claimed(item), claimed, "a refused deck claimed its occurrence")
        self.assertEqual(PlayEvent.objects.count(), played)
        self.assertEqual(set(open_fds(item.track.filepath)) - before, set(), "a refused deck opened the file")

    def pause_delivery(self, item, *, point, module=delivery):
        """Deliver ``item`` in another session, paused at fault ``point``."""
        reached, release, outcome = threading.Event(), threading.Event(), []
        real = module._fault

        def hook(name):
            if name == point and threading.current_thread() is thread:
                reached.set()
                if not release.wait(60):
                    raise AssertionError("test never released the promotion")
            return real(name)

        def run():
            try:
                outcome.append(delivery.deliver_item(item))
            except BaseException as exc:  # noqa: BLE001 -- asserted by the caller
                outcome.append(exc)
            finally:
                connections.close_all()
        patcher = mock.patch.object(module, "_fault", hook)
        patcher.start()
        self.addCleanup(patcher.stop)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(reached.wait(60), f"promotion never reached {point}")
        return thread, release, outcome


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class DeckOpenLockContinuityTests(RealEngineMixin, TransactionTestCase):
    def test_one_backend_holds_the_lock_from_reread_through_real_claim_to_file_open(self):
        ctype = self.make_type()
        a_item, a = self.delivered(ctype)
        asset = SpokenAsset.objects.get(pk=a.asset_id)
        track = Track.objects.get(pk=a.track_id)
        item = occurrence(track)
        b_item = self.processed(ctype, data=flac(2345))
        a_sha, b_sha = a_item.rendition_media.sha256, b_item.rendition_media.sha256
        stages, contention = [], {}

        def record(stage):
            lease = playout.current_lease()
            pid = backend_pid()
            stages.append((stage, pid, lease.backend_pid if lease else None, pid in shared_holders(asset.pk)))

        real_claim = PlaybackEngine._claim_playback_occurrence

        def claim(log_item):                                   # the real claim, observed on both sides
            record("authority re-read (before claim)")
            result = real_claim(self.engine, log_item)
            record("after real claim")
            return result
        self.engine._claim_playback_occurrence = claim

        def before_open(_deck):
            record("immediately before file open")
            # Engine wins: start B's promotion now; it must not get the
            # exclusive lock while this deck is being opened.
            contention["thread"], contention["outcome"] = threading.Thread(target=self._deliver, args=(b_item,)), []
            contention["thread"].start()
            time.sleep(1.0)
            contention["exclusive_elsewhere"] = exclusive_available_elsewhere(asset.pk)
            mid = SpokenAsset.objects.get(pk=asset.pk)
            contention["during"] = (mid.state, Track.objects.get(pk=track.pk).ready2air,
                                    self.sha_on_disk(mid.library_relpath) == a_sha)

        real_sync = Gst.Element.sync_state_with_parent

        def synchronize(bin_):
            result = real_sync(bin_)
            record("immediately after sync_state_with_parent")
            contention["fds_after_sync"] = open_fds(track.filepath)
            return result

        with mock.patch.object(Gst.Bin, "sync_state_with_parent", synchronize, create=True):
            deck = self.engine._create_deck("A", item, before_sync=before_open)

        self.assertIsNotNone(deck, "the real deck path did not complete")
        self.assertTrue(self.claimed(item))                     # the real claim ran
        lease_pid = stages[0][2]
        self.assertIsNotNone(lease_pid)
        self.assertEqual([s[0] for s in stages], ["authority re-read (before claim)", "after real claim",
                                                  "immediately before file open",
                                                  "immediately after sync_state_with_parent"])
        for stage, pid, held_by, holds in stages:
            self.assertEqual((pid, held_by, holds), (lease_pid, lease_pid, True), stage)
        self.assertEqual(contention["exclusive_elsewhere"], False)
        self.assertEqual(contention["during"], (AssetState.LIVE, True, True))
        self.assertTrue(contention["fds_after_sync"], "sync_state_with_parent did not open the file")
        # released on the owning backend, no exception, deck handed back live
        self.assertEqual(shared_holders(asset.pk), [])
        # only now may B publish; the open deck keeps A's inode
        contention["thread"].join(60)
        self.assertFalse(contention["thread"].is_alive())
        final = SpokenAsset.objects.get(pk=asset.pk)
        self.assertEqual((final.state, final.current_item_id), (AssetState.LIVE, b_item.pk))
        self.assertEqual(self.sha_on_disk(final.library_relpath), b_sha)
        self.assertEqual(fd_sha(contention["fds_after_sync"][0]), a_sha)
        self.assertFalse(SystemEvent.objects.filter(title__contains="deck-open lock").exists())
        self.discard(deck)

    def _deliver(self, item):
        try:
            delivery.deliver_item(item)
        finally:
            connections.close_all()

    def test_promotion_first_refuses_new_decks_fast_without_claim_or_open(self):
        for point, module in (("before_barrier", None), ("barrier_committed", delivery),
                              ("published", library_files)):
            with self.subTest(point=point):
                ctype = self.make_type(code=f"first-{point}")
                a_item, a = self.delivered(ctype)
                track = Track.objects.get(pk=a.track_id)
                item = occurrence(track)
                b_item = self.processed(ctype, data=flac(2600 + len(point)))
                if module is None:               # exclusive lock held, barrier not yet committed
                    reached, release, outcome = threading.Event(), threading.Event(), []
                    real = delivery._enter_unavailable

                    def paused(*args, **kwargs):
                        reached.set()
                        release.wait(60)
                        return real(*args, **kwargs)

                    def run():
                        try:
                            outcome.append(delivery.deliver_item(b_item))
                        finally:
                            connections.close_all()
                    patcher = mock.patch.object(delivery, "_enter_unavailable", paused)
                    patcher.start()
                    thread = threading.Thread(target=run)
                    thread.start()
                    self.assertTrue(reached.wait(60))
                    self.assertTrue(playout.inspect(track.pk).allowed)    # readiness alone still says yes
                else:
                    thread, release, outcome = self.pause_delivery(b_item, point=point, module=module)
                started = time.monotonic()
                self.refused(item)
                self.assertLess(time.monotonic() - started, 1.0, "the engine waited on the projection")
                release.set()
                thread.join(60)
                if module is None:
                    patcher.stop()
                self.assertFalse(thread.is_alive())
                # after promotion resolves, the same cached occurrence observes B
                self.open_a(occurrence(Track.objects.get(pk=track.pk)), track, b_item.rendition_media.sha256)

    def test_a_session_lost_inside_the_scope_stops_before_the_live_pipeline(self):
        ctype = self.make_type()
        _a_item, a = self.delivered(ctype)
        track = Track.objects.get(pk=a.track_id)
        item = occurrence(track)
        children = self.engine.main_pipeline.get_children_count()
        self.engine._write_now_playing = lambda _track: connection.close()     # the backend goes away
        deck = self.engine._create_deck("A", item)
        self.assertIsNone(deck)
        self.assertEqual(self.engine.main_pipeline.get_children_count(), children)
        self.assertEqual(open_fds(track.filepath), [])
        titles = set(SystemEvent.objects.values_list("title", flat=True))
        self.assertIn("Spoken projection deck open abandoned: lock lost before file open", titles)
        self.assertIn("Spoken projection: deck-open lock release problem", titles)

    def test_recycling_reintroduced_inside_the_scope_cannot_open_the_file(self):
        """If the claim's recycling ever bypasses the scope again (the B1
        regression), the pre-open check refuses instead of opening the file
        unprotected -- and the continuity test above would fail."""
        ctype = self.make_type()
        _a_item, a = self.delivered(ctype)
        track = Track.objects.get(pk=a.track_id)
        item = occurrence(track)
        with mock.patch.object(engine_module, "_close_old_connections", engine_module.close_old_connections):
            deck = self.engine._create_deck("A", item)
        self.assertIsNone(deck)
        self.assertEqual(open_fds(track.filepath), [])


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class SpokenDeckAuthorityTests(RealEngineMixin, TransactionTestCase):
    """Only a fully live, witnessed projection opens.  Preview may be cheap
    or stale; the actual deck open re-reads committed authority."""

    def test_r16_bytes_renamed_by_a_crashed_promotion_never_open(self):
        ctype = self.make_type()
        a_item, a = self.delivered(ctype)
        track = Track.objects.get(pk=a.track_id)
        item = occurrence(track)                                   # cached while ready2air=True
        b_item = self.processed(ctype, data=flac(2345))

        def crash(point):
            if point == "after_publish":
                raise delivery.SimulatedCrash(point)
        with mock.patch.object(delivery, "_fault", crash), self.assertRaises(delivery.SimulatedCrash):
            delivery.deliver_item(b_item)
        asset = SpokenAsset.objects.get(pk=a.asset_id)
        self.assertEqual(self.sha_on_disk(asset.library_relpath), b_item.rendition_media.sha256)
        self.assertEqual(shared_holders(asset.pk), [])            # the crashed session's lock is gone
        self.assertFalse(engine_module._log_item_playable(item)[0])
        self.refused(item)
        delivery.reconcile()
        self.open_a(occurrence(Track.objects.get(pk=track.pk)), track, a_item.rendition_media.sha256)

    def test_r17_recall_with_failed_removal_refuses_the_cached_occurrence(self):
        ctype = self.make_type()
        a_item, a = self.delivered(ctype)
        item = occurrence(Track.objects.get(pk=a.track_id))
        with mock.patch.object(library_files, "remove", side_effect=OSError("held open")):
            delivery.recall_delivered(a_item, actor=self.manager)
        self.assertTrue(Path(item.track.filepath).exists())
        self.assertTrue(item.track.ready2air)                      # the cached instance is stale
        self.refused(item)

    def test_r17_expiry_with_failed_removal_refuses_the_cached_occurrence(self):
        ctype = self.make_type(default_lifetime=timedelta(seconds=1))
        a_item, a = self.delivered(ctype)
        item = occurrence(Track.objects.get(pk=a.track_id))
        with mock.patch("spoken.delivery.timezone.now", return_value=timezone.now() + timedelta(minutes=1)), \
                mock.patch.object(library_files, "remove", side_effect=OSError("held open")):
            delivery.expire_item(a_item)
        self.assertTrue(Path(item.track.filepath).exists())
        self.refused(item)

    def test_detach_with_lingering_bytes_refuses_the_cached_occurrence(self):
        ctype = self.make_type()
        _a_item, a = self.delivered(ctype)
        item = occurrence(Track.objects.get(pk=a.track_id))
        with mock.patch.object(library_files, "remove", side_effect=OSError("held open")):
            Track.objects.get(pk=a.track_id).delete()
        self.assertEqual(SpokenAsset.objects.get(pk=a.asset_id).state, AssetState.DETACHED)
        self.assertTrue(Path(item.track.filepath).exists())
        self.refused(item)

    def test_only_a_fully_live_projection_opens(self):
        def live():
            ctype = self.make_type()
            a_item, a = self.delivered(ctype)
            return a_item, SpokenAsset.objects.get(pk=a.asset_id), occurrence(Track.objects.get(pk=a.track_id))

        a_item, _asset, item = live()
        self.open_a(item, item.track, a_item.rendition_media.sha256)

        _a_item, asset, item = live()                        # retiring: an abandoned barrier
        QuerySet.update(SpokenAsset._base_manager.filter(pk=asset.pk), state=AssetState.RETIRING)
        self.refused(item)

        _a_item, asset, item = live()                        # lost link: the reserved path still protects
        QuerySet.update(SpokenAsset._base_manager.filter(pk=asset.pk), track=None)
        self.assertFalse(engine_module._log_item_playable(item)[0])
        self.refused(item)

        _a_item, asset, item = live()                        # wrong edition authority
        QuerySet.update(SpokenAssetEdition._base_manager.filter(asset=asset, state=EditionState.ACTIVE),
                        library_sha256="0" * 64)
        self.refused(item)

        _a_item, asset, item = live()                        # missing current item authority
        QuerySet.update(SpokenAsset._base_manager.filter(pk=asset.pk), current_item=None)
        self.refused(item)

    def test_a_reserved_filename_without_an_asset_never_opens(self):
        stray = self.library / "LocalNews" / f"spoken-{'a' * 32}.flac"
        stray.parent.mkdir(exist_ok=True)
        stray.write_bytes(flac(900))
        track = Track(filepath=str(stray), filename=stray.name, title="stray", ready2air=True,
                      artist=Artist.objects.get_or_create(name="Stray")[0])
        QuerySet.bulk_create(Track._base_manager.all(), [track])            # raw: no guard reserves it
        item = occurrence(Track.objects.get(filepath=str(stray)))
        self.assertFalse(engine_module._log_item_playable(item)[0])
        self.refused(item)

    def test_preview_is_cheap_but_the_deck_open_rereads_the_link(self):
        """A Track linked by SpokenAsset.track_id whose path was corrupted out
        of the reserved namespace looks ordinary to the cheap preview, but the
        actual deck open still finds its asset and refuses."""
        ctype = self.make_type()
        _a_item, a = self.delivered(ctype)
        corrupt = Path(self.library) / "elsewhere.flac"
        corrupt.write_bytes(flac(950))
        QuerySet.update(Track._base_manager.filter(pk=a.track_id), filepath=str(corrupt))
        item = occurrence(Track.objects.get(pk=a.track_id))
        self.assertEqual(engine_module._log_item_playable(item), (True, None))   # preview: legacy, cheap
        self.refused(item)                                                      # authority: refused


def _legacy_playable(log_item):
    """The engine's pre-C2B rule (92c1777..f11b72f), verbatim."""
    if log_item is None:
        return False, "log_item is None"
    track = log_item.track
    if track is None:
        return False, "track deleted from library (FK set NULL)"
    fp = track.filepath
    if not fp:
        return False, f"track id={track.id} has no filepath"
    if not Path(fp).is_file():
        return False, f"file missing on disk: {fp}"
    return True, None


class OrdinaryFixtures:
    def ordinary(self, *, ready, title="Song", category=None, uploaded_by=None):
        path = Path(self.library) / "Music" / f"song-{next(_HOURS)}.flac"
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(flac(800))
        return Track.objects.create(filepath=str(path), filename=path.name, title=title, ready2air=ready,
                                    artist=Artist.objects.get_or_create(name="Ordinary")[0], category=category,
                                    uploaded_by=uploaded_by, duration_seconds=0.8)

    def imaging_category(self):
        kind, _ = CategoryKind.objects.get_or_create(code="imaging", defaults={"name": "Imaging"})
        return Category.objects.get_or_create(code="Imaging", defaults={"name": "Imaging", "kind": kind})[0]


@unittest.skipUnless(FFMPEG, "ffmpeg not available")
class OrdinaryTrackDeckTests(OrdinaryFixtures, RealEngineMixin, TestCase):
    """B2: ordinary Tracks keep pre-C2B engine behaviour -- including
    not-Ready-to-Air ones, which operator insertion, warning-only logs and
    contributor uploads rely on."""

    def setUp(self):
        super().setUp()
        # A TestCase cannot recycle its connection mid-transaction; the
        # recycling itself is covered by the TransactionTestCase classes.
        patcher = mock.patch.object(engine_module, "close_old_connections", lambda: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assert_legacy_and_opens(self, item):
        self.assertEqual(engine_module._log_item_playable(item), _legacy_playable(item))
        self.assertEqual(engine_module._log_item_playable(item), (True, None))
        self.open_a(item, item.track, hashlib.sha256(Path(item.track.filepath).read_bytes()).hexdigest())
        self.assertTrue(self.claimed(item))

    def test_ordinary_ready_and_not_ready_tracks(self):
        self.assert_legacy_and_opens(occurrence(self.ordinary(ready=True)))
        self.assert_legacy_and_opens(occurrence(self.ordinary(ready=False)))

    def test_contributor_upload_style_not_ready_track(self):
        contributor = get_user_model().objects.create_user(f"contrib-{next(_HOURS)}")
        self.assert_legacy_and_opens(occurrence(self.ordinary(ready=False, uploaded_by=contributor)))

    def test_cached_future_occurrence_of_a_track_later_marked_not_ready(self):
        track = self.ordinary(ready=True)
        item = occurrence(track)
        Track.objects.filter(pk=track.pk).update(ready2air=False)
        self.assert_legacy_and_opens(item)

    def test_operator_engine_insert_of_a_not_ready_track(self):
        from library import views
        existing = occurrence(self.ordinary(ready=True))
        track = self.ordinary(ready=False, title="Unreviewed upload")
        staff = get_user_model().objects.create_superuser(f"op-{next(_HOURS)}", password="x")
        request = RequestFactory().post("/api/engine/insert/", data=f'{{"track_id": {track.pk}}}',
                                        content_type="application/json")
        request.user = staff
        with mock.patch.object(views, "_read_engine_queue_state", return_value=([existing], 0)):
            response = views.api_engine_insert_track(request)
        self.assertEqual(response.status_code, 200, response.content)
        inserted = LogItem.objects.select_related("track").get(pk=json.loads(response.content)["item_id"])
        self.assertEqual(inserted.track_id, track.pk)
        self.assert_legacy_and_opens(inserted)


class PreviewQueryTests(OrdinaryFixtures, ProjectionMixin, TestCase):
    """B3: the 250 ms state/preview path (_write_state ->
    _get_upcoming_preview -> _log_item_playable) adds no query per ordinary
    item; only spoken candidates are inspected."""

    def preview_engine(self, items):
        engine = PlaybackEngine.__new__(PlaybackEngine)
        engine._forced_next_items = []
        engine.log_items = items
        engine._queue_cursor = 0
        engine._poison_skip_identities = []
        return engine

    def queued(self, tracks):
        return [occurrence(track) for track in tracks]

    def test_an_ordinary_hour_preview_makes_no_query(self):
        for size in (21, 200):
            with self.subTest(size=size):
                items = self.queued([self.ordinary(ready=index % 3 != 0) for index in range(size)])
                engine = self.preview_engine(items)
                with CaptureQueriesContext(connection) as queries:
                    preview = engine._get_upcoming_preview()
                self.assertEqual(len(preview), size)                  # not-ready ordinary items stay
                self.assertEqual(len(queries), 0, [q["sql"] for q in queries])

    @unittest.skipUnless(FFMPEG, "ffmpeg not available")
    def test_a_mixed_preview_inspects_only_the_spoken_candidate(self):
        music = [self.ordinary(ready=True) for _ in range(10)]
        imaging = [self.ordinary(ready=True, category=self.imaging_category()) for _ in range(5)]
        ctype = self.make_type()
        _a_item, a = self.delivered(ctype)
        spoken = Track.objects.get(pk=a.track_id)
        items = self.queued(music[:5] + [spoken] + imaging + music[5:])
        engine = self.preview_engine(items)
        with CaptureQueriesContext(connection) as queries:
            preview = engine._get_upcoming_preview()
        self.assertEqual(len(preview), 16)
        self.assertLessEqual(len(queries), 3)
        # the spoken candidate's inspection is the only source of queries
        with CaptureQueriesContext(connection) as only_spoken:
            engine_module._log_item_playable(items[5])
        self.assertEqual(len(only_spoken), len(queries))

    @unittest.skipUnless(FFMPEG, "ffmpeg not available")
    def test_a_spoken_candidate_lookup_is_bounded(self):
        ctype = self.make_type()
        _a_item, a = self.delivered(ctype)
        with CaptureQueriesContext(connection) as queries:
            self.assertTrue(playout.inspect(a.track_id).allowed)
        self.assertLessEqual(len(queries), 3)

    def test_the_fast_classifier(self):
        self.assertTrue(playout.is_candidate(Track(filepath=f"/srv/music/News/spoken-{'0a' * 16}.flac")))
        for path in ("/srv/music/Music/song.flac", f"/srv/music/News/spoken-{'0a' * 15}.flac",
                     f"/srv/music/News/spoken-{'0A' * 16}.flac", f"/srv/music/News/x-spoken-{'0a' * 16}.flac", ""):
            self.assertFalse(playout.is_candidate(Track(filepath=path)), path)
