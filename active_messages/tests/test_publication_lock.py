"""D3E-C correction 1: the publication destination lock is ONE stable inode.

``playout._destination_lock`` guards every placement, retirement and
cleanup of ``UrgentPA/am-<hex>.flac``. Its sidecar
``.am-<hex>.flac.publish.lock`` must never be unlinked: a process that had
already opened the old inode (a waiter) and a newcomer that creates a new one
would otherwise hold "exclusive" locks on different inodes at the same time.

Real, independent processes (fork), synchronized by events -- never by
sleeps: a wrapped ``fcntl.flock`` signals the instant a process is about to
block, and each critical section records the inode of the descriptor it holds
and a shared overlap counter. No database is used by any child.
"""
import fcntl
import multiprocessing
import os
import stat
import tempfile
import unittest
from pathlib import Path

from django.test import SimpleTestCase, TestCase

from active_messages import playout
from production.tests.support import ffmpeg_available

from .d3ec_support import Lifecycle

CONTEXT = multiprocessing.get_context("fork")
WAIT = 20                                    # generous bound for an event that must happen


class Shared:
    """Cross-process instruments: an overlap counter and recorded inodes."""

    def __init__(self):
        self.inside = CONTEXT.Value("i", 0)
        self.max_inside = CONTEXT.Value("i", 0)
        self.overlaps = CONTEXT.Value("i", 0)
        self.inodes = CONTEXT.Manager().list()

    def enter(self, inode):
        with self.inside.get_lock():
            self.inside.value += 1
            if self.inside.value > 1:
                self.overlaps.value += 1
            self.max_inside.value = max(self.max_inside.value, self.inside.value)
        self.inodes.append(inode)

    def leave(self):
        with self.inside.get_lock():
            self.inside.value -= 1


def _instrument(about_to_block=None):
    """In a child: wrap fcntl.flock so the moment it is about to wait on an
    exclusive lock is signalled, and remember the descriptor it locks."""
    real = fcntl.flock
    held = {}

    def flock(fd, operation):
        if operation & fcntl.LOCK_EX and not operation & fcntl.LOCK_NB:
            held["fd"] = fd
            if about_to_block is not None:
                about_to_block.set()
        return real(fd, operation)

    fcntl.flock = flock
    return held


def _critical(shared, held, *, entered=None, release=None):
    shared.enter(os.fstat(held["fd"]).st_ino)
    try:
        if entered is not None:
            entered.set()
        if release is not None:
            release.wait(WAIT)
    finally:
        shared.leave()


def _first(dest, shared, entered, release):
    held = _instrument()
    with playout._destination_lock(dest):
        _critical(shared, held, entered=entered, release=release)


def _waiter(dest, shared, blocked, entered, release):
    held = _instrument(about_to_block=blocked)
    with playout._destination_lock(dest):
        _critical(shared, held, entered=entered, release=release)


def _newcomer(dest, shared, about_to_lock, entered):
    held = _instrument(about_to_block=about_to_lock)
    with playout._destination_lock(dest):
        _critical(shared, held, entered=entered)


def _repeat(dest, shared, rounds, start):
    held = _instrument()
    start.wait(WAIT)
    for _ in range(rounds):
        with playout._destination_lock(dest):
            _critical(shared, held)


def _crash_holding(dest, entered):
    with playout._destination_lock(dest):
        entered.set()
        os._exit(17)                              # killed while holding the lock


def _try_lock(dest, result):
    try:
        with playout._destination_lock(dest):
            result.value = 1
    except OSError:
        result.value = 2


class PublicationLockProcessTests(SimpleTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="am-lock-")
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name)
        self.dest = self.dir / "am-0123456789abcdef0123456789abcdef.flac"
        self.sidecar = self.dir / f".{self.dest.name}.publish.lock"

    def start(self, target, *args):
        process = CONTEXT.Process(target=target, args=args)
        process.start()
        self.addCleanup(lambda: (process.is_alive() and process.kill(), process.join(WAIT)))
        return process

    def test_a_waiter_and_a_newcomer_never_hold_different_inodes(self):
        """A holds; B opens the SAME inode and waits; A releases; C opens the
        pathname while B holds. The old helper unlinked the sidecar on release,
        so C created a new inode and entered while B was still inside."""
        shared = Shared()
        a_in, a_go = CONTEXT.Event(), CONTEXT.Event()
        b_blocked, b_in, b_go = CONTEXT.Event(), CONTEXT.Event(), CONTEXT.Event()
        c_about, c_in = CONTEXT.Event(), CONTEXT.Event()
        a = self.start(_first, self.dest, shared, a_in, a_go)
        self.assertTrue(a_in.wait(WAIT))
        b = self.start(_waiter, self.dest, shared, b_blocked, b_in, b_go)
        self.assertTrue(b_blocked.wait(WAIT))                 # B has opened the inode and is waiting
        a_go.set()
        a.join(WAIT)
        self.assertTrue(b_in.wait(WAIT))                      # B now holds the lock
        c = self.start(_newcomer, self.dest, shared, c_about, c_in)
        self.assertTrue(c_about.wait(WAIT))                   # C has opened the pathname and asks for the lock
        entered_while_b_inside = c_in.wait(1.0)               # the vulnerable helper lets C in at once
        b_go.set()
        b.join(WAIT)
        self.assertTrue(c_in.wait(WAIT))
        c.join(WAIT)
        self.assertFalse(entered_while_b_inside, "a second process entered on a different inode")
        self.assertEqual((shared.overlaps.value, shared.max_inside.value), (0, 1))
        self.assertEqual(len(set(shared.inodes)), 1, list(shared.inodes))
        self.assertEqual(os.lstat(self.sidecar).st_ino, shared.inodes[0])   # the one persistent inode

    def test_many_processes_repeatedly_share_one_inode(self):
        shared, start = Shared(), CONTEXT.Event()
        workers = [self.start(_repeat, self.dest, shared, 25, start) for _ in range(4)]
        start.set()
        for worker in workers:
            worker.join(60)
            self.assertEqual(worker.exitcode, 0)
        self.assertEqual((shared.overlaps.value, shared.max_inside.value, len(shared.inodes)), (0, 1, 100))
        self.assertEqual(set(shared.inodes), {os.lstat(self.sidecar).st_ino})

    def test_a_crash_while_holding_leaves_the_same_usable_inode(self):
        entered = CONTEXT.Event()
        crashed = self.start(_crash_holding, self.dest, entered)
        self.assertTrue(entered.wait(WAIT))
        crashed.join(WAIT)
        self.assertEqual(crashed.exitcode, 17)
        inode = os.lstat(self.sidecar).st_ino
        result = CONTEXT.Value("i", 0)
        follower = self.start(_try_lock, self.dest, result)
        follower.join(WAIT)
        self.assertEqual(result.value, 1)                     # the kernel released the crashed holder's lock
        self.assertEqual(os.lstat(self.sidecar).st_ino, inode)

    def test_the_sidecar_persists_after_release(self):
        with playout._destination_lock(self.dest):
            inode = os.lstat(self.sidecar).st_ino
        info = os.lstat(self.sidecar)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual((info.st_ino, info.st_size), (inode, 0))
        with playout._destination_lock(self.dest):
            self.assertEqual(os.lstat(self.sidecar).st_ino, inode)

    def test_an_unexpected_sidecar_object_is_never_accepted_as_the_lock(self):
        victim = self.dir / "victim"
        victim.write_bytes(b"precious")
        cases = {
            "symlink": lambda: self.sidecar.symlink_to(victim),
            "directory": lambda: self.sidecar.mkdir(),
            "fifo": lambda: os.mkfifo(self.sidecar),
            "hard link": lambda: os.link(victim, self.sidecar),
        }
        for label, make in cases.items():
            with self.subTest(label):
                make()
                result = CONTEXT.Value("i", 0)
                process = self.start(_try_lock, self.dest, result)
                process.join(WAIT)
                self.assertEqual(result.value, 2, f"{label} sidecar was accepted as the lock")
                self.assertEqual(victim.read_bytes(), b"precious")
                if self.sidecar.is_dir() and not self.sidecar.is_symlink():
                    self.sidecar.rmdir()
                else:
                    self.sidecar.unlink()


def _hold_until(dest, entered, release):
    with playout._destination_lock(dest):
        entered.set()
        release.wait(WAIT)


@unittest.skipUnless(ffmpeg_available(), "ffmpeg not available")
class SweepNeverRemovesLockTests(Lifecycle, TestCase):
    """The D3E-C sweep and reconciler against real persistent sidecars."""

    def later(self):
        from datetime import timedelta
        from django.utils import timezone
        return timezone.now() + timedelta(days=400)

    def test_an_old_sidecar_is_never_removed(self):
        from active_messages import reconcile, retention
        execution, publication, _item = self.completed()
        sidecar = playout.lock_sidecar(self.file(publication))
        inode = os.lstat(sidecar).st_ino
        os.utime(sidecar, (0, 0))                                 # artificially ancient
        for _ in range(2):
            retention.sweep_publication_namespace(apply=True, now=self.later())
            retention.finish_retirements(apply=True)
        self.assertEqual(os.lstat(sidecar).st_ino, inode)
        self.assertNotIn("publication_lock_leftover", [f.code for f in reconcile.reconcile(now=self.later()).findings])

    def test_a_sweeper_while_another_process_holds_the_lock(self):
        from active_messages import retention
        execution, publication, _item = self.completed()
        dest = self.file(publication)
        entered, release = CONTEXT.Event(), CONTEXT.Event()
        holder = CONTEXT.Process(target=_hold_until, args=(dest, entered, release))
        holder.start()
        self.addCleanup(lambda: (release.set(), holder.join(WAIT)))
        self.assertTrue(entered.wait(WAIT))
        inode = os.lstat(playout.lock_sidecar(dest)).st_ino
        report = retention.sweep_publication_namespace(apply=True, now=self.later())
        self.assertEqual(report.count("removed"), 0)
        self.assertEqual(os.lstat(playout.lock_sidecar(dest)).st_ino, inode)
        release.set()
        holder.join(WAIT)
        self.assertEqual(os.lstat(playout.lock_sidecar(dest)).st_ino, inode)

    def test_cleanup_after_a_crashed_holder(self):
        from active_messages import retention
        execution, publication, _item = self.completed()
        dest = self.file(publication)
        entered = CONTEXT.Event()
        crashed = CONTEXT.Process(target=_crash_holding, args=(dest, entered))
        crashed.start()
        self.assertTrue(entered.wait(WAIT))
        crashed.join(WAIT)
        inode = os.lstat(playout.lock_sidecar(dest)).st_ino
        outcome = self.retire(execution)
        self.assertEqual(outcome.action, "retired")              # the crashed holder's lock was released
        self.assertFalse(dest.exists())
        self.assertEqual(os.lstat(playout.lock_sidecar(dest)).st_ino, inode)
