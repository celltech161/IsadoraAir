"""3.11 Codex re-review: the three filesystem fault-injection probes of the relocation staging, as tests.

Reconstructed from Codex's findings (the probe scripts themselves were not supplied):

1. **Wrong file accepted.**  The destination is replaced by an unrelated recording of exactly the same length
   after the hard link was created and checked, while the staging re-examines the source.
2. **Staging unwind deletes a replacement.**  The destination is replaced immediately after the hard link is
   created; the staging then fails, and its unwind must not delete the replacement.
3. **Check-then-unlink of the old source.**  After staging, the old source entry is replaced by an unrelated
   file just before the removal's destructive step; the removal must not delete it.

They use only ``stage_no_replace`` and the staged handle's ``verify_destination_binding``, ``remove_source`` and
``remove_destination`` -- names that exist before and after the correction -- so the same file runs against
the starting commit (where they fail: the reproduction) and the corrected one.  Real files in a per-test
temporary Library root; the interleaving is injected at the filesystem calls."""
import os
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from django.test import TestCase

from library.services import media_relocation as m
from library.tests.test_library_reconciliation_b import _ReconFixture

AUTHORIZED = b"AUTHORIZED-AUDIO"
UNRELATED = b"UNRELATED-AUDIO!"          # exactly the same length


def _bytes_anywhere(root, data):
    """Every file under ``root`` (hidden directories included) whose content is ``data``."""
    found = []
    for directory, _dirs, files in os.walk(root):
        for name in files:
            path = Path(directory) / name
            try:
                if not path.is_symlink() and path.read_bytes() == data:
                    found.append(str(path))
            except OSError:
                pass
    return found


class CodexProbeTests(_ReconFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.source = self.root / "POP" / "song.flac"
        self.source.parent.mkdir()
        self.source.write_bytes(AUTHORIZED)
        (self.root / "ROCK").mkdir()
        self.destination = self.root / "ROCK" / "song.flac"
        self.real_stat, self.real_link, self.real_unlink = os.stat, os.link, os.unlink

    def substitute(self, path, data=UNRELATED):
        """Atomically replace the entry at ``path`` with a new, unrelated file (a different inode)."""
        temp = path.with_name(".substitute-" + path.name)
        with open(temp, "wb") as handle:
            handle.write(data)
        info = self.real_stat(path) if os.path.lexists(path) else None
        if info is not None:                  # the replacement even copies the timestamps
            os.utime(temp, ns=(info.st_atime_ns, info.st_mtime_ns))
        os.rename(temp, path)

    def stage(self):
        return m.stage_no_replace(self.source, self.destination, library_root=self.root, allow_copy=False)

    def test_probe_1_a_same_length_substitution_after_the_link_is_never_accepted(self):
        state = {"linked": False, "done": False}

        def link(*args, **kwargs):
            result = self.real_link(*args, **kwargs)
            state["linked"] = True
            return result

        def stat(path, *args, **kwargs):
            info = self.real_stat(path, *args, **kwargs)
            if state["linked"] and not state["done"] and os.fsdecode(path) == self.source.name:
                state["done"] = True                         # the source is being re-examined
                self.substitute(self.destination)
            return info

        accepted, staged = False, None
        with mock.patch("os.link", side_effect=link), mock.patch("os.stat", side_effect=stat):
            try:
                staged = self.stage()
                staged.verify_destination_binding()
                accepted = True
            except OSError:
                pass
        if staged is not None:
            staged.close()
        destination = self.destination.read_bytes() if self.destination.exists() else None
        print(f"\nprobe1 substituted={state['done']} stage_returned={accepted} source_bytes={self.source.read_bytes()!r} "
              f"destination_bytes={destination!r}")
        self.assertTrue(state["done"], "the probe did not inject")
        self.assertFalse(accepted and destination == UNRELATED, "a substituted recording was accepted as the destination")
        self.assertFalse(accepted)
        self.assertEqual(self.source.read_bytes(), AUTHORIZED)
        self.assertTrue(_bytes_anywhere(self.root, UNRELATED), "the unrelated recording was deleted")

    def test_probe_2_the_staging_unwind_never_deletes_a_replacement(self):
        def link(*args, **kwargs):
            result = self.real_link(*args, **kwargs)
            self.substitute(self.destination)                # replaced right after its creation
            return result

        raised = False
        with mock.patch("os.link", side_effect=link):
            try:
                self.stage().close()
            except OSError:
                raised = True
        survivors = _bytes_anywhere(self.root, UNRELATED)
        print(f"\nprobe2 stage_raised={raised} unrelated_survivors={[os.path.relpath(p, self.root) for p in survivors]}")
        self.assertTrue(raised)
        self.assertTrue(survivors, "the staging unwind deleted the unrelated replacement")
        self.assertEqual(self.source.read_bytes(), AUTHORIZED)

    def test_probe_3_removing_the_old_source_never_deletes_a_replacement(self):
        staged = self.stage()
        staged.verify_destination_binding()
        state = {"done": False}

        def unlink(path, *args, **kwargs):                   # the starting commit's destructive step
            if not state["done"] and os.fsdecode(path) == self.source.name:
                state["done"] = True
                self.substitute(self.source)
            return self.real_unlink(path, *args, **kwargs)

        patches = [mock.patch("os.unlink", side_effect=unlink)]
        if hasattr(m, "_rename_noreplace"):                  # the corrected commit's destructive step
            real_rename = m._rename_noreplace

            def rename(src_fd, src, dst_fd, dst):
                if not state["done"] and os.fsdecode(src) == self.source.name:
                    state["done"] = True
                    self.substitute(self.source)
                return real_rename(src_fd, src, dst_fd, dst)
            patches.append(mock.patch.object(m, "_rename_noreplace", side_effect=rename))
        outcome = None
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                outcome = staged.remove_source()
            except OSError as exc:
                outcome = f"raised {exc}"
        staged.close()
        survivors = _bytes_anywhere(self.root, UNRELATED)
        print(f"\nprobe3 substituted={state['done']} outcome={outcome!r} "
              f"unrelated_survivors={[os.path.relpath(p, self.root) for p in survivors]} "
              f"destination_bytes={self.destination.read_bytes()!r}")
        self.assertTrue(state["done"], "the probe did not inject")
        self.assertTrue(survivors, "the old-source removal deleted the unrelated replacement")
        self.assertEqual(self.destination.read_bytes(), AUTHORIZED)
