"""Roadmap 3.12 corrections after Codex's first review: one regression per finding.

1. (High) Exact-byte evidence could go stale undetected: a same-size rewrite with the original mtime restored.
2. (High) A missing or untrustworthy engine state was treated as "nothing on air".
3. (Medium) One apply request could delete up to 25 pairs, each with several lock waits (Gunicorn's 30 s timeout).
4. (Low) Changing page, filter or search silently discarded the operator's selections.

Each test that names a finding *reproduces* it: it fails against the starting implementation (75656a6) and passes
after the correction.  So that the same tests run against both, ``_Fixture.apply_all`` / ``first_token`` accept
either apply contract (the starting one batch token, the corrected one token per pair) and the page scenarios
answer either request shape; everything asserted is the behaviour the finding is about.

Fixtures as in test_library_duplicate_resolution: a per-test Library root, waveform and report directories; the
engine state is a real file in the test's own directory (``webrequests.services.ENGINE_STATE_PATH`` patched), read
by the shared reader -- never /run.
"""
import json
import os
import threading
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.db import connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from library.models import Category, DuplicateCandidate, LogItem, PlaylistLog, Track
from library.services import library_duplicate_resolution as resolution
from library.services import library_reconciliation as reconciliation
from library.services import source_paths, waveform_cache
from library.tests.test_library_duplicate_resolution import PAGE_EXTRA, _node
from library.tests.test_library_reconciliation_b import User, _ReconFixture
from library.tests.test_library_reconciliation_launch import PAGE_HARNESS, TEMPLATE
from webrequests import services as engine_status

PREVIEW = "library:api-reconciliation-duplicates-preview"
APPLY = "library:api-reconciliation-duplicates-apply"
NOT_DUPLICATE = "library:api-reconciliation-not-duplicate"
SAME = b"\x02identical-audio" * 400


class _Fixture(_ReconFixture):
    def setUp(self):
        super().setUp()
        self.state_path = self.base / "run" / "engine_state.json"
        self.state_path.parent.mkdir()
        patcher = mock.patch.object(engine_status, "ENGINE_STATE_PATH", self.state_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine()
        self.jazz = Category.objects.create(code="JAZZ", name="Jazz", kind=self.rock.kind)

    def engine(self, **fields):
        """A fresh state as the engine writes it (library.services.engine._write_state), with ``fields`` replaced."""
        state = {"transport": "PLAYING", "timestamp": time.time(), "decks": {"A": None, "B": None}, "queue": [],
                 "log_id": None, "hour": None, "date": None}
        state.update(fields)
        self.state_path.write_text(json.dumps(state))

    def pair(self, name):
        data = SAME + name.encode()
        return (self.track(f"ROCK/{name}-a.flac", data=data, category=self.rock),
                self.track(f"POP/{name}-b.flac", data=data, category=self.pop))

    def scanned(self):
        self.report = reconciliation.run_scan()
        return self.report

    def finding(self, *tracks):
        ids = sorted(t.pk for t in tracks)
        return next(f for f in self.report["findings"] if f["type"] == "duplicate" and f.get("members") == ids)

    def post(self, name, payload, client=None):
        response = (client or self.client).post(reverse(name), payload, content_type="application/json")
        return response.status_code, response.json()

    def preview(self, *selections, client=None):
        return self.post(PREVIEW, {"scan_id": self.report["scan_id"], "selections": list(selections)}, client)

    def delete(self, keep, discard):
        return {"finding_id": self.finding(keep, discard)["id"], "action": "delete", "keep": keep.pk,
                "discard": discard.pk}

    def keep_both(self, a, b):
        return {"finding_id": self.finding(a, b)["id"], "action": "keep_both"}

    # Either apply contract (see the module docstring).
    @staticmethod
    def first_token(body):
        if "token" in body:                                   # the starting implementation: one batch token
            return body["token"]
        return next((i["token"] for i in body["items"] if i.get("token")), None)

    def apply_token(self, token, choice=None, confirm=True, client=None):
        """One apply request; returns (status, [pair results])."""
        status, result = self.post(APPLY, {"token": token, "confirm": confirm, "choice": choice or {},
                                           "choices": {}}, client)
        if status != 200:
            return status, [result]
        return status, (result["items"] if "items" in result else [result["item"]])

    def apply_all(self, body, confirm=True):
        """Apply every applicable pair of a preview, as the page does; returns the pair results."""
        if "token" in body:
            return self.apply_token(body["token"], confirm=confirm)[1] if body["token"] else []
        results = []
        for item in body["items"]:
            if item.get("token"):
                results.extend(self.apply_token(item["token"], confirm=confirm)[1])
        return results

    def exists(self, *tracks):
        return [Track.objects.filter(pk=t.pk).exists() for t in tracks]


class _Fix(_Fixture, TestCase):
    pass


# --------------------------------------------------------------------------- 1. the scan's stat witness

def _rewrite_same_size(path: Path, *, replace=False):
    """Different bytes, same length, original mtime restored: size and mtime say "unchanged"."""
    before = path.stat()
    data = path.read_bytes()
    changed = bytes([data[0] ^ 0xFF]) + data[1:]
    target = path.with_name(path.name + ".new") if replace else path
    target.write_bytes(changed)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    if replace:
        os.replace(target, path)
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    assert path.read_bytes() != data


class StatWitnessTests(_Fix):
    def test_finding1_a_same_size_rewrite_with_its_mtime_restored_is_refused_at_preview(self):
        """Codex High 1 reproduced: before the correction this pair was eligible and its Track deleted."""
        for replace in (False, True):
            with self.subTest(replace=replace):
                a, b = self.pair(f"rewrite{int(replace)}")
                self.scanned()
                _rewrite_same_size(Path(b.filepath), replace=replace)
                status, body = self.preview(self.delete(a, b))
                self.assertEqual(status, 200, body)
                item = body["items"][0]
                self.assertEqual(item["status"], "blocked")
                self.assertTrue(any("modified or replaced since the scan" in r for r in item["reasons"]), item)
                self.apply_all(body)
                self.assertEqual(self.exists(a, b), [True, True])

    def test_a_rewrite_between_preview_and_apply_is_refused_at_apply(self):
        a, b = self.pair("late")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        _rewrite_same_size(Path(a.filepath))                 # the survivor's bytes are evidence too
        results = self.apply_all(body)
        self.assertEqual([r["outcome"] for r in results], ["stale"])
        self.assertEqual(self.exists(a, b), [True, True])

    def test_unchanged_files_are_still_resolved(self):
        a, b = self.pair("unchanged")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        self.assertEqual([r["outcome"] for r in self.apply_all(body)], ["deleted"])
        self.assertEqual(self.exists(a, b), [True, False])

    def test_the_scan_records_the_full_witness_for_exact_findings(self):
        a, b = self.pair("witness")
        self.scanned()
        files = {f["track_id"]: f for f in self.finding(a, b)["files"]}
        for track in (a, b):
            info = os.stat(track.filepath)
            self.assertEqual(files[track.pk].get("witness"), [info.st_dev, info.st_ino, info.st_size,
                                                              info.st_mtime_ns, info.st_ctime_ns, info.st_nlink])

    def test_a_report_without_witnesses_stays_reviewable_but_authorizes_no_deletion(self):
        """An r0126 report: readable and reviewable; deletion needs a new scan."""
        a, b = self.pair("older")
        self.scanned()
        for finding in self.report["findings"]:
            for entry in finding.get("files") or []:
                entry.pop("witness", None)
        reconciliation.write_report(self.report)
        response = self.client.get(reverse("library:api-reconciliation-findings"), {"type": "duplicate"})
        self.assertEqual(response.status_code, 200)
        listed = {tuple(f["members"]): f for f in response.json()["items"]}
        self.assertFalse(listed[(a.pk, b.pk)]["resolution"]["deletable"])
        self.assertIn("run reconciliation again", listed[(a.pk, b.pk)]["resolution"]["reason"])
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "blocked")
        self.assertIn("run reconciliation again", body["items"][0]["reasons"][0])
        status, body = self.preview(self.keep_both(a, b))      # the non-destructive review stays available
        self.assertEqual(body["items"][0]["status"], "eligible")
        self.apply_all(body, confirm=False)
        self.assertEqual(DuplicateCandidate.objects.get().resolution, "keep_both")
        self.assertEqual(self.exists(a, b), [True, True])


# --------------------------------------------------------------------------- 2. trusted engine state

class EngineStateTests(_Fix):
    BROKEN = {
        "missing": lambda self: self.state_path.unlink(),
        "stale": lambda self: self.engine(timestamp=time.time() - 120),
        "unreadable": lambda self: (self.state_path.unlink(), self.state_path.mkdir()),
        "not JSON": lambda self: self.state_path.write_text("{not json"),
        "not an object": lambda self: self.state_path.write_text("[1, 2]"),
        "timestamp not a number": lambda self: self.engine(timestamp="now"),
        "timestamp in the future": lambda self: self.engine(timestamp=time.time() + 3600),
        "no decks": lambda self: self.engine(decks=None),
        "decks not an object": lambda self: self.engine(decks=[{"track_id": 1}]),
        "deck without a track": lambda self: self.engine(decks={"A": {"title": "x"}, "B": None}),
        "no queue": lambda self: self.engine(queue=None),
        "queue entry without a track": lambda self: self.engine(queue=[{"item_id": 3}]),
        "no active log field": lambda self: self.state_path.write_text(json.dumps(
            {"timestamp": time.time(), "decks": {"A": None}, "queue": []})),
        "active log not an id": lambda self: self.engine(log_id="12"),
    }

    def test_finding2_an_untrustworthy_engine_state_refuses_deletion(self):
        """Codex High 2 reproduced: before the correction every one of these allowed the deletion."""
        for name, damage in self.BROKEN.items():
            with self.subTest(state=name):
                if self.state_path.is_dir():                 # left by the "unreadable" case
                    self.state_path.rmdir()
                a, b = self.pair("eng-" + name.replace(" ", "-"))
                self.scanned()
                self.engine()
                damage(self)
                status, body = self.preview(self.delete(a, b))
                self.assertEqual(status, 200, body)
                item = body["items"][0]
                self.assertEqual(item["status"], "blocked", item)
                self.assertTrue(any("playout engine's current state cannot be confirmed" in r
                                    for r in item["reasons"]), item["reasons"])
                self.apply_all(body)
                self.assertEqual(self.exists(a, b), [True, True])

    def test_keep_both_needs_no_engine_state(self):
        a, b = self.pair("keepboth")
        self.scanned()
        self.state_path.unlink()
        status, body = self.preview(self.keep_both(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        self.assertEqual([r["outcome"] for r in self.apply_all(body, confirm=False)], ["recorded"])

    def test_finding2_a_previous_hour_log_the_engine_still_plays_protects_its_unclaimed_track(self):
        """Codex High 2 scenario: the clock is in the next hour; the engine's active log is the previous hour's,
        with the discarded Track queued and not yet claimed.  Without a trusted state (``missing``) the database
        sees no current or later log for it -- before the correction it was deleted."""
        fixed = timezone.now()
        previous = timezone.localtime(fixed) - timedelta(hours=1)
        with mock.patch.object(resolution, "_now", lambda: fixed):
            for case in ("missing", "engine names the log"):
                with self.subTest(case=case):
                    a, b = self.pair("prev-" + case.replace(" ", "-"))
                    log, _ = PlaylistLog.objects.get_or_create(date=previous.date(), hour=previous.hour,
                                                               defaults={"status": "approved"})
                    item = LogItem.objects.create(playlist_log=log, position=LogItem.objects.count() + 1,
                                                  scheduled_time=previous, track=b)
                    self.scanned()
                    if case == "missing":
                        self.state_path.unlink()
                    else:
                        self.engine(log_id=log.pk, hour=log.hour, date=log.date.isoformat(),
                                    queue=[{"item_id": item.pk, "track_id": b.pk}])
                    status, body = self.preview(self.delete(a, b))
                    self.assertEqual(body["items"][0]["status"], "blocked", body["items"][0])
                    self.apply_all(body)
                    self.assertEqual(self.exists(a, b), [True, True])
                    self.assertEqual(LogItem.objects.get(pk=item.pk).track_id, b.pk)

    def test_finding2_the_engine_is_observed_again_at_apply(self):
        """Healthy at preview; at apply the discarded Track is on a deck, or the state has gone.  The second case
        reproduces the finding (before the correction an absent state at apply let the deletion through)."""
        for case in ("on a deck", "state gone"):
            with self.subTest(case=case):
                a, b = self.pair("apply-" + case.replace(" ", "-"))
                self.scanned()
                self.engine()
                status, body = self.preview(self.delete(a, b))
                self.assertEqual(body["items"][0]["status"], "eligible")
                if case == "on a deck":
                    self.engine(decks={"A": {"track_id": b.pk, "slot": "A"}, "B": None})
                else:
                    self.state_path.unlink()
                results = self.apply_all(body)
                self.assertEqual([r["outcome"] for r in results], ["blocked"])
                self.assertEqual(self.exists(a, b), [True, True])

    def test_a_healthy_state_permits_an_eligible_deletion(self):
        a, b = self.pair("healthy")
        self.scanned()
        self.engine(log_id=None, queue=[{"item_id": 1, "track_id": a.pk}])   # the survivor may be queued
        status, body = self.preview(self.delete(a, b))
        self.assertEqual(body["items"][0]["status"], "eligible")
        self.assertEqual([r["outcome"] for r in self.apply_all(body)], ["deleted"])


# --------------------------------------------------------------------------- 3. one pair per request

class OnePairPerRequestTests(_Fix):
    def test_finding3_one_apply_request_deletes_at_most_one_pair(self):
        """Codex Medium 3 reproduced: before the correction one request deleted every pair of the preview."""
        pairs = [self.pair(f"one{i}") for i in range(3)]
        self.scanned()
        status, body = self.preview(*(self.delete(a, b) for a, b in pairs))
        self.assertEqual(body["counts"]["eligible"], 3)
        status, results = self.apply_token(self.first_token(body))
        self.assertEqual(status, 200)
        self.assertEqual(len(results), 1)
        self.assertEqual(Track.objects.filter(pk__in=[b.pk for a, b in pairs]).count(), 2)

    def test_finding3_a_pair_token_is_single_use_and_bound_to_its_operator(self):
        """Before the correction a replayed token was accepted (and found nothing left to do)."""
        a, b = self.pair("once")
        c, d = self.pair("twice")
        self.scanned()
        status, body = self.preview(self.delete(a, b), self.delete(c, d))
        token = self.first_token(body)
        other = User.objects.create_superuser("other-op", "o@example.invalid", "pw")
        client = Client()
        client.force_login(other)
        self.assertEqual(self.apply_token(token, client=client)[0], 403)       # another operator
        self.assertEqual(self.exists(a, b), [True, True])
        status, results = self.apply_token(token)
        self.assertEqual((status, results[0]["outcome"]), (200, "deleted"))
        status, again = self.apply_token(token)                                   # the replay is refused
        self.assertEqual(status, 409, again)
        self.assertIn("already used", again[0]["error"])
        self.assertEqual(self.exists(c, d), [True, True])                       # and touched nothing else


class _Threads(_Fixture, TransactionTestCase):
    """Real transactions on other connections, held open while the request under test runs."""

    def hold(self, fn, release_after=None):
        """Run ``fn`` in a transaction of its own (another connection) and keep it open until released."""
        ready, release, box = threading.Event(), threading.Event(), {}

        def run():
            try:
                with transaction.atomic():
                    fn()
                    ready.set()
                    release.wait(40)
            except Exception as exc:        # reported by the test
                box["error"] = exc
                ready.set()
            finally:
                connections.close_all()
        thread = threading.Thread(target=run, name="holder")
        thread.start()
        self.assertTrue(ready.wait(10))
        self.assertNotIn("error", box)
        if release_after is not None:       # the starting implementation waited forever: end the hold eventually
            threading.Timer(release_after, release.set).start()
        self.addCleanup(lambda: (release.set(), thread.join(40)))
        return release

    def timed(self, call):
        started = time.monotonic()
        value = call()
        return value, time.monotonic() - started


class LockBudgetTests(_Threads):
    def test_finding3_a_locked_track_is_answered_busy_within_the_budget(self):
        """Before the correction the request waited 5 s for the row (and up to 25 pairs did so in one request);
        now the pair's transaction has 3 s in all (PAIR_LOCK_BUDGET_SECONDS)."""
        a, b = self.pair("rowlock")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.hold(lambda: list(Track.objects.select_for_update().filter(pk=b.pk)))
        results, elapsed = self.timed(lambda: self.apply_all(body))
        self.assertLess(elapsed, 4.0)
        self.assertEqual([r["outcome"] for r in results], ["failed"])
        self.assertIn("busy", results[0]["message"])
        self.assertEqual(self.exists(a, b), [True, True])

    def test_a_busy_track_does_not_stop_the_later_pairs(self):
        pairs = [self.pair(f"busy{i}") for i in range(3)]
        self.scanned()
        status, body = self.preview(*(self.delete(a, b) for a, b in pairs))
        self.hold(lambda: list(Track.objects.select_for_update().filter(pk=pairs[1][1].pk)))
        results = self.apply_all(body)
        self.assertEqual([r["outcome"] for r in results], ["deleted", "failed", "deleted"])
        self.assertEqual(Track.objects.filter(pk__in=[b.pk for a, b in pairs]).count(), 1)

    def test_finding3_keep_both_cannot_wait_indefinitely(self):
        """Before the correction keep both waited without limit for the decision row (here: until the holder
        gives up after 8 s)."""
        a, b = self.pair("kb")
        low, high = sorted((a, b), key=lambda t: t.pk)
        candidate = DuplicateCandidate.objects.create(track_a=low, track_b=high, confidence="exact")
        self.scanned()
        status, body = self.preview(self.keep_both(a, b))
        self.hold(lambda: list(DuplicateCandidate.objects.select_for_update().filter(pk=candidate.pk)),
                  release_after=8)
        results, elapsed = self.timed(lambda: self.apply_all(body, confirm=False))
        self.assertLess(elapsed, 4.0)
        self.assertEqual([r["outcome"] for r in results], ["failed"])
        candidate.refresh_from_db()
        self.assertEqual(candidate.resolution, "unresolved")

    def test_finding3_the_not_duplicate_button_cannot_wait_indefinitely(self):
        a, b = self.pair("nd")
        low, high = sorted((a, b), key=lambda t: t.pk)
        candidate = DuplicateCandidate.objects.create(track_a=low, track_b=high, confidence="exact")
        self.scanned()
        self.hold(lambda: list(DuplicateCandidate.objects.select_for_update().filter(pk=candidate.pk)),
                  release_after=8)
        (status, body), elapsed = self.timed(lambda: self.post(NOT_DUPLICATE, {
            "scan_id": self.report["scan_id"], "finding_id": self.finding(a, b)["id"], "track_a": a.pk,
            "track_b": b.pk}))
        self.assertLess(elapsed, 4.0)
        self.assertEqual(status, 409, body)
        candidate.refresh_from_db()
        self.assertEqual(candidate.resolution, "unresolved")

    def test_finding3_after_commit_cleanup_lock_waits_are_bounded(self):
        """The discarded file's path lock and its waveform-cache lock are both held elsewhere.  Before the
        correction the request then waited 5 s + 15 s after the commit; now each wait is bounded and the file is
        retained (reported), the database deletion having committed."""
        a, b = self.pair("cleanup")
        self.scanned()
        status, body = self.preview(self.delete(a, b))
        self.hold(lambda: source_paths.acquire(Path(b.filepath), "default", timeout_ms=1000))
        lock_fd = waveform_cache._acquire_track_lock(b.pk, 1)       # another open file: flock conflicts
        self.addCleanup(os.close, lock_fd)
        results, elapsed = self.timed(lambda: self.apply_all(body))
        self.assertLess(elapsed, 6.0)                              # 2 s + 2 s (CLEANUP_LOCK_TIMEOUT_SECONDS), margin
        self.assertEqual(results[0]["outcome"], "deleted")
        self.assertEqual((results[0]["cleanup"]["status"], results[0]["cleanup"]["outcome"]),
                         ("retained_busy", "retained"))
        self.assertTrue(Path(b.filepath).exists())
        self.assertEqual(self.exists(a, b), [True, False])


# --------------------------------------------------------------------------- 3 + 4. the page

# A scenario's apply responder answers either request shape (see the module docstring).
SCENARIO_EXTRA = r"""
const exact = (i, a, b) => dup('G' + i, a, b);
function scenarioItem(f, blocked) {
  const [keep, discard] = f.members;
  return { finding_id: f.id, action: 'delete', status: blocked ? 'blocked' : 'eligible', keep, discard,
    keep_track: desc(keep), discard_track: desc(discard), suggested_categories: [], voicetracks: 0, warnings: [],
    reasons: blocked ? ['the discarded Track is in playlist(s) \'Morning\': remove it from them first'] : [],
    ...(blocked ? {} : { token: 'tok-' + f.id }) };
}
function answerPreview(items, blockedIds) {
  const req = open(r => r.url === PREVIEW)[0];
  const list = items.map(f => scenarioItem(f, blockedIds.includes(f.id)));
  const ok = list.filter(i => i.status !== 'blocked').length;
  respond(req, 200, { scan_id: 'A', applicable: ok, deletions: ok, token: 'batch', expires_in: 600,
    counts: { eligible: ok, needs_acknowledgment: 0, blocked: list.length - ok }, items: list });
}
function confirmAndApply() {
  const panel = ids.resolvePanel;
  const confirm = walk(panel, n => n.tag === 'input' && n.type === 'checkbox' && walk(panel, l => l.tag === 'label' &&
    l.children[0] === n && l.text.startsWith('I understand')).length)[0];
  confirm.checked = true; confirm.listeners.change[0]();
  button(panel, 'Delete ').listeners.click[0]();
}
const deletedItem = (f, outcome = 'deleted') => ({ finding_id: f, action: 'delete', outcome,
  message: outcome === 'deleted' ? 'Deleted.' : 'Not applied: one of the Tracks is busy. Preview again and retry.',
  cleanup: { status: 'removed', outcome: 'completed', message: 'The discarded file was removed.' }, file: 'x' });
function answerApply(req, outcomeFor) {
  const body = JSON.parse(req.body);
  if (body.choices) {                       // the starting implementation's batch request
    const items = Object.keys(body.choices).map(f => deletedItem(f, outcomeFor(f)));
    respond(req, 200, { scan_id: 'A', outcome: 'partial', counts: {}, items, rescan_recommended: true });
  } else {
    const f = body.token.replace('tok-', '');
    respond(req, 200, { scan_id: 'A', item: deletedItem(f, outcomeFor(f)), rescan_recommended: true });
  }
}
"""


class _Page(_Fix):
    def run_page(self, scenario):
        source = TEMPLATE.read_text()
        page = source[source.index("<script>") + len("<script>"):source.index("</script>")]
        return _node(self, PAGE_HARNESS + PAGE_EXTRA + page + SCENARIO_EXTRA + "\n(async () => {\n" + scenario +
                     "\n})().catch(e => { console.error(e); process.exit(1); });\n")


class PageTests(_Page):
    def test_finding3_a_ten_pair_batch_is_sent_one_pair_per_request_and_keeps_the_blocked(self):
        """Ten selected, two blocked, one of the eight busy: eight requests, in order, one pair each, one at a
        time, with progress; the blocked and the busy pairs stay selected.  Before the correction one request
        carried all eight."""
        out = self.run_page(r"""
  const items = Array.from({ length: 10 }, (_, i) => exact(i, 100 + 2 * i, 101 + 2 * i));
  await bootDup({ page: 1, pages: 1, total: 10, scan_id: 'A', items });
  items.forEach(f => choose('resolve-' + f.id, 'keep_a'));
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  answerPreview(items, ['G3', 'G7']);
  await flush();
  confirmAndApply();
  await flush();
  const bodies = [], progress = [];
  let maxOpen = 0;
  for (let guard = 0; guard < 20; guard++) {
    const pending = open(r => r.url === APPLY);
    if (!pending.length) break;
    maxOpen = Math.max(maxOpen, pending.length);
    progress.push(ids.resolvePanel.text);
    bodies.push(JSON.parse(pending[0].body));
    answerApply(pending[0], f => f === 'G1' ? 'failed' : 'deleted');
    await flush();
  }
  const results = ids.resolvePanel.text;
  respond(open(isFindings)[0], 200, { page: 1, pages: 1, total: 10, scan_id: 'A', items });
  await flush();
  console.log(JSON.stringify({ bodies, maxOpen, progress, results, selected: [...choices.keys()].sort(),
                               bar: ids.resolveBar.text }));
""")
        expected = [f"G{i}" for i in range(10) if i not in (3, 7)]
        self.assertEqual(len(out["bodies"]), 8, out["bodies"])
        self.assertEqual([b["token"] for b in out["bodies"]], [f"tok-{f}" for f in expected])
        self.assertEqual(out["bodies"][0], {"token": "tok-G0", "confirm": True,
                                            "choice": {"categories": [], "acknowledge_voicetracks": False}})
        self.assertEqual(out["maxOpen"], 1)
        self.assertIn("0 of 8 pair(s) processed", out["progress"][0])
        self.assertIn("6 of 8 pair(s) processed", out["progress"][6])
        self.assertIn("Processed 8 of 8 pair(s)", out["results"])
        self.assertIn("Deleted: 7", out["results"])
        self.assertIn("Failed: 1", out["results"])
        self.assertEqual(out["selected"], ["G1", "G3", "G7"])
        self.assertIn("3 pair(s) selected", out["bar"])

    def test_finding3_a_lost_answer_stops_the_batch(self):
        """The second answer is lost: nothing more is sent, that pair is "unknown", the rest "not attempted",
        and the findings are reloaded.  Before the correction there was a single request for all pairs."""
        out = self.run_page(r"""
  const items = [exact(1, 11, 12), exact(2, 13, 14), exact(3, 15, 16)];
  await bootDup({ page: 1, pages: 1, total: 3, scan_id: 'A', items });
  items.forEach(f => choose('resolve-' + f.id, 'keep_a'));
  button(ids.resolveBar, 'Review selected').listeners.click[0]();
  await flush();
  answerPreview(items, []);
  await flush();
  confirmAndApply();
  await flush();
  const first = open(r => r.url === APPLY)[0];
  const firstBody = JSON.parse(first.body);
  answerApply(first, () => 'deleted');
  await flush();
  const second = open(r => r.url === APPLY);
  if (second.length) fail(second[0]);
  await flush();
  console.log(JSON.stringify({ firstBody, sentAfterLoss: open(r => r.url === APPLY).length,
    applyRequests: requests.filter(r => r.url === APPLY).length, results: ids.resolvePanel.text,
    reloaded: open(isFindings).length, selected: [...choices.keys()].sort() }));
""")
        self.assertEqual(out["firstBody"]["token"], "tok-G1")
        self.assertEqual(out["applyRequests"], 2)
        self.assertEqual(out["sentAfterLoss"], 0)
        self.assertIn("Unknown (no answer): 1", out["results"])
        self.assertIn("Not attempted: 1", out["results"])
        self.assertIn("whether it was applied is not known", out["results"])
        self.assertEqual(out["reloaded"], 1)
        self.assertEqual(out["selected"], ["G2", "G3"])


class NavigationGuardTests(_Page):
    def test_finding4_page_filter_and_search_changes_ask_before_discarding_selections(self):
        """Before the correction each of these loaded the other findings at once and dropped the selection."""
        out = self.run_page(r"""
  await bootDup();
  choose('resolve-F1', 'keep_a');
  const before = open(isFindings).length;
  ids.nextBtn.listeners.click[0]();
  await flush();
  const next = { guard: !ids.navGuard.hidden, text: ids.navGuard.text, requested: open(isFindings).length - before };
  button(ids.navGuard, 'Stay').listeners.click[0]();
  await flush();
  const stay = { guard: !ids.navGuard.hidden, kept: radio('resolve-F1', 'keep_a').checked, size: choices.size, page };
  ids.typeFilter.value = 'missing'; ids.typeFilter.listeners.change[0]();
  await flush();
  const filter = { guard: !ids.navGuard.hidden, control: ids.typeFilter.value, requested: open(isFindings).length - before };
  button(ids.navGuard, 'Stay').listeners.click[0]();
  filter.afterStay = ids.typeFilter.value;                   // Stay: the control matches the findings shown again
  for (const text of ['a', 'ab', 'abc']) { ids.search.value = text; ids.search.listeners.input[0](); }
  const typing = { guard: !ids.navGuard.hidden };
  await fire();                                              // the debounce: one question once the typing pauses
  const paused = { guard: !ids.navGuard.hidden, control: ids.search.value, requested: open(isFindings).length - before };
  button(ids.navGuard, 'Discard').listeners.click[0]();      // what the operator typed is what continues
  await flush();
  const req = open(isFindings).pop();
  const discard = { guard: !ids.navGuard.hidden, size: choices.size, url: req && req.url };
  console.log(JSON.stringify({ next, stay, filter, typing, paused, discard }));
""")
        self.assertTrue(out["next"]["guard"])
        self.assertIn("1 duplicate selection(s) on this page have not been applied", out["next"]["text"])
        self.assertIn("Stay and keep selections", out["next"]["text"])
        self.assertIn("Discard selections and continue", out["next"]["text"])
        self.assertEqual(out["next"]["requested"], 0)
        self.assertEqual(out["stay"], {"guard": False, "kept": True, "size": 1, "page": 1})
        self.assertEqual(out["filter"], {"guard": True, "control": "missing", "requested": 0, "afterStay": ""})
        self.assertFalse(out["typing"]["guard"])
        self.assertEqual(out["paused"], {"guard": True, "control": "abc", "requested": 0})
        self.assertFalse(out["discard"]["guard"])
        self.assertEqual(out["discard"]["size"], 0)
        self.assertIn("q=abc", out["discard"]["url"])

    def test_finding4_a_newer_scan_clears_selections_and_says_so(self):
        out = self.run_page(r"""
  await bootDup();
  choose('resolve-F1', 'keep_a');
  await fire();                                              // the status poll finds a newer complete report
  respond(open(r => r.url === SUMMARY)[0], 200, status(B, B));
  await flush();
  respond(open(isFindings)[0], 200, { ...PAGE1, scan_id: 'B' });
  await flush();
  console.log(JSON.stringify({ guard: !ids.navGuard.hidden, text: ids.navGuard.text, size: choices.size,
                               bar: ids.resolveBar.hidden }));
""")
        self.assertTrue(out["guard"])
        self.assertIn("A newer reconciliation scan (B) replaced the report", out["text"])
        self.assertIn("1 duplicate selection(s) from scan A were cleared", out["text"])
        self.assertEqual((out["size"], out["bar"]), (0, True))
