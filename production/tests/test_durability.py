"""Codex Blocker 4: the power-loss durability ORDER of intake.

A real power cut cannot be simulated in a test, so this proves the syscall
contract instead. The real os.* calls (and the row INSERT) are wrapped with
recorders that still perform the real operation, so the test runs the actual
filesystem sequence and checks its order:

    layout dirs:  mkdir -> fsync(dir) -> fsync(parent)
    part file:    write ... fchmod(0440) -> fsync(file)      (mode before sync)
    new shard:    mkdir(shard) -> fsync(shard) -> fsync(media/)
    publish:      link(part -> key) -> fsync(shard)
    staging:      unlink(part) -> fsync(incoming/)
    row:          INSERT only after all of the above

Any reordering or omission later fails here.
"""
import io
import os
import uuid
from unittest import mock

from django.test import TestCase

from production.errors import IntakeError
from production.models import ProductionMedia
from production.services import intake, layout

from .support import IsolatedMediaRootMixin, fixture


def _fd_path(fd):
    try:
        return os.readlink(f"/proc/self/fd/{fd}")
    except OSError:
        return f"<fd {fd}>"


class SyscallRecorder:
    def __init__(self, testcase):
        self.events = []
        real = {name: getattr(os, name) for name in ("fsync", "fchmod", "chmod", "mkdir", "link", "unlink")}
        real_create = ProductionMedia.objects.create

        def fsync(fd):
            self.events.append(("fsync", _fd_path(fd)))
            return real["fsync"](fd)

        def fchmod(fd, mode):
            self.events.append(("fchmod", _fd_path(fd), mode))
            return real["fchmod"](fd, mode)

        def chmod(path, mode, *args, **kwargs):
            self.events.append(("chmod", os.fspath(path), mode))
            return real["chmod"](path, mode, *args, **kwargs)

        def mkdir(path, mode=0o777, *args, **kwargs):
            result = real["mkdir"](path, mode, *args, **kwargs)
            self.events.append(("mkdir", os.fspath(path)))
            return result

        def link(src, dst, *args, **kwargs):
            result = real["link"](src, dst, *args, **kwargs)
            self.events.append(("link", os.fspath(src), os.fspath(dst)))
            return result

        def unlink(path, *args, **kwargs):
            self.events.append(("unlink", os.fspath(path)))
            return real["unlink"](path, *args, **kwargs)

        def create(**columns):
            self.events.append(("insert", columns["storage_key"]))
            return real_create(**columns)

        self.patchers = [mock.patch.object(os, name, side_effect=fn) for name, fn in (
            ("fsync", fsync), ("fchmod", fchmod), ("chmod", chmod), ("mkdir", mkdir), ("link", link),
            ("unlink", unlink))]
        self.patchers.append(mock.patch.object(ProductionMedia.objects, "create", side_effect=create))
        for patcher in self.patchers:
            patcher.start()
            testcase.addCleanup(patcher.stop)

    def index(self, *event, after=-1):
        for position, recorded in enumerate(self.events):
            if position > after and recorded[: len(event)] == event:
                return position
        raise AssertionError(f"event {event} not found after #{after} in {self.events}")

    def last_index(self, *event):
        matches = [i for i, recorded in enumerate(self.events) if recorded[: len(event)] == event]
        if not matches:
            raise AssertionError(f"event {event} never happened in {self.events}")
        return matches[-1]


class DurabilityOrderTests(IsolatedMediaRootMixin, TestCase):
    def ingest(self, data=None):
        return intake.ingest_stream(io.BytesIO(data or fixture("wav16_mono.wav")), kind="upload", validate=False)

    def test_first_intake_creates_and_publishes_everything_in_the_durable_order(self):
        recorder = SyscallRecorder(self)
        media = self.ingest().media
        root, media_dir = str(self.root), str(self.root / "media")
        incoming = str(self.root / "incoming")
        dest = str(layout.resolve_storage_path(media.storage_key))
        shard = os.path.dirname(dest)
        part = recorder.events[recorder.index("link")][1]
        insert = recorder.index("insert")

        # layout directories: created, synced, and their parent synced
        for directory, parent in ((root, str(self.root.parent)), (media_dir, root), (incoming, root)):
            made = recorder.index("mkdir", directory)
            recorder.index("fsync", directory, after=made)
            recorder.index("fsync", parent, after=made)

        # part file: FINAL mode set before the file sync, and nothing after it
        final_chmod = recorder.last_index("fchmod", part)
        self.assertEqual(recorder.events[final_chmod][2], 0o440)
        file_sync = recorder.index("fsync", part, after=final_chmod)
        self.assertFalse([e for e in recorder.events[file_sync + 1:] if e[:2] == ("fchmod", part)])

        # new shard: created, synced, parent (media/) synced -- all before publication
        made_shard = recorder.index("mkdir", shard)
        link = recorder.index("link", part, dest)
        self.assertLess(recorder.index("fsync", shard, after=made_shard), link)
        self.assertLess(recorder.index("fsync", media_dir, after=made_shard), link)
        self.assertLess(file_sync, link)

        # publication durable, staging removal durable, THEN the row
        self.assertLess(recorder.index("fsync", shard, after=link), insert)
        unlink = recorder.index("unlink", part, after=link)
        self.assertLess(recorder.index("fsync", incoming, after=unlink), insert)
        self.assertEqual(recorder.events[insert][1], media.storage_key)
        self.assertEqual(stat_mode(dest), 0o440)

    def test_an_existing_shard_is_still_parent_synced_before_the_row(self):
        first = self.ingest().media
        same_shard = uuid.UUID(first.storage_key[:2] + uuid.uuid4().hex[2:])
        recorder = SyscallRecorder(self)
        with mock.patch.object(layout, "new_media_id", return_value=same_shard):
            second = self.ingest(b"second payload bytes").media
        shard = os.path.dirname(str(layout.resolve_storage_path(second.storage_key)))
        self.assertFalse([e for e in recorder.events if e == ("mkdir", shard)])          # shard reused
        insert = recorder.index("insert")
        link = recorder.index("link")
        self.assertLess(recorder.index("fsync", str(self.root / "media")), link)
        self.assertLess(recorder.index("fsync", shard, after=link), insert)

    def test_a_failed_directory_sync_aborts_before_any_row_and_leaves_no_permanent_file(self):
        real_fsync = os.fsync
        shard_holder = {}

        def failing_fsync(fd):
            path = _fd_path(fd)
            if os.path.dirname(path) == str(self.root / "media") and shard_holder.get("linked"):
                raise OSError(5, "I/O error")
            return real_fsync(fd)

        real_link = os.link

        def link(src, dst, *args, **kwargs):
            result = real_link(src, dst, *args, **kwargs)
            shard_holder["linked"] = True
            return result
        with mock.patch.object(os, "fsync", side_effect=failing_fsync), \
                mock.patch.object(os, "link", side_effect=link), self.assertRaises(IntakeError):
            self.ingest()
        self.assertEqual(ProductionMedia.objects.count(), 0)
        self.assertEqual(self.list_files("media"), [])

    def test_a_failed_file_sync_aborts_before_publication(self):
        real_fsync = os.fsync

        def failing(fd):
            if _fd_path(fd).endswith(".part"):
                raise OSError(5, "I/O error")
            return real_fsync(fd)
        with mock.patch.object(os, "fsync", side_effect=failing), self.assertRaises(OSError):
            self.ingest()
        self.assertEqual(ProductionMedia.objects.count(), 0)
        self.assertEqual(self.list_files("media") + self.list_files("incoming"), [])

    def test_ensure_durable_dir_syncs_every_created_ancestor_and_tightens_modes(self):
        recorder = SyscallRecorder(self)
        deep = self.root.parent / "a" / "b" / "c"
        layout.ensure_durable_dir(deep)
        for directory in (deep.parent.parent, deep.parent, deep):
            made = recorder.index("mkdir", str(directory))
            recorder.index("fsync", str(directory.parent), after=made)
        self.assertEqual(stat_mode(str(deep)), 0o750)
        loose = self.root.parent / "loose"
        os.mkdir(loose, 0o777)
        os.chmod(loose, 0o777)
        layout.ensure_durable_dir(loose)
        self.assertEqual(stat_mode(str(loose)), 0o750)


def stat_mode(path):
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)
