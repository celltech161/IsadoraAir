"""D2-B: the A/B slot layout, staging area, and atomic publish."""
from pathlib import Path
import tempfile

from django.test import SimpleTestCase

from .phase_b_helpers import BOOTSTRAP_ROOT  # noqa: F401

from isadoraair_updater_bootstrap.slots import Slot, SlotError, SlotLayout, publish_slot, slot_is_reclaimable


class SlotEnumTests(SimpleTestCase):
    def test_exactly_two_slots(self):
        self.assertEqual({s.value for s in Slot}, {"A", "B"})

    def test_other_flips(self):
        self.assertIs(Slot.A.other(), Slot.B)
        self.assertIs(Slot.B.other(), Slot.A)


class SlotLayoutTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.layout = SlotLayout(slots_root=Path(self.temp.name))

    def _populate(self, directory: Path):
        (directory / "updaterd.py").write_text("print(1)\n")
        (directory / "updaterd.py").chmod(0o755)

    def test_slot_path_is_under_slots_root(self):
        self.assertEqual(self.layout.slot_path(Slot.A), Path(self.temp.name) / "A")
        self.assertEqual(self.layout.slot_path(Slot.B), Path(self.temp.name) / "B")

    def test_staging_directory_is_unique_each_call(self):
        first = self.layout.new_staging_directory()
        second = self.layout.new_staging_directory()
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.layout.staging_root)

    def test_discard_staging_directory_removes_it(self):
        staged = self.layout.new_staging_directory()
        self._populate(staged)
        self.layout.discard_staging_directory(staged)
        self.assertFalse(staged.exists())

    def test_discard_refuses_a_path_outside_staging_root(self):
        with self.assertRaises(SlotError):
            self.layout.discard_staging_directory(Path(self.temp.name) / "A")

    def test_publish_into_inactive_slot_succeeds(self):
        staged = self.layout.new_staging_directory()
        self._populate(staged)
        publish_slot(self.layout, Slot.B, staged, active_slot=Slot.A)
        self.assertTrue((self.layout.slot_path(Slot.B) / "updaterd.py").is_file())
        self.assertFalse(staged.exists())  # renamed away, not copied

    def test_publish_into_active_slot_refused(self):
        staged = self.layout.new_staging_directory()
        self._populate(staged)
        with self.assertRaises(SlotError):
            publish_slot(self.layout, Slot.A, staged, active_slot=Slot.A)
        # Refused BEFORE anything was touched.
        self.assertTrue(staged.exists())

    def test_publish_refuses_staged_content_outside_staging_root(self):
        rogue = Path(self.temp.name) / "not-staging"
        rogue.mkdir()
        self._populate(rogue)
        with self.assertRaises(SlotError):
            publish_slot(self.layout, Slot.B, rogue, active_slot=Slot.A)

    def test_publish_refuses_a_symlink_in_staged_content(self):
        staged = self.layout.new_staging_directory()
        self._populate(staged)
        (staged / "sneaky-link").symlink_to(staged / "updaterd.py")
        with self.assertRaises(SlotError):
            publish_slot(self.layout, Slot.B, staged, active_slot=Slot.A)

    def test_reclaiming_an_already_populated_inactive_slot_replaces_it_atomically(self):
        first = self.layout.new_staging_directory()
        (first / "generation-old.txt").write_text("old")
        publish_slot(self.layout, Slot.B, first, active_slot=Slot.A)

        second = self.layout.new_staging_directory()
        (second / "generation-new.txt").write_text("new")
        publish_slot(self.layout, Slot.B, second, active_slot=Slot.A)

        self.assertTrue((self.layout.slot_path(Slot.B) / "generation-new.txt").is_file())
        self.assertFalse((self.layout.slot_path(Slot.B) / "generation-old.txt").exists())

    def test_never_a_moment_where_slot_path_is_missing_after_first_publish(self):
        staged = self.layout.new_staging_directory()
        self._populate(staged)
        publish_slot(self.layout, Slot.B, staged, active_slot=Slot.A)
        self.assertTrue(self.layout.slot_path(Slot.B).is_dir())


class SlotReclaimabilityTests(SimpleTestCase):
    def test_active_slot_never_reclaimable(self):
        self.assertFalse(slot_is_reclaimable(Slot.A, active_slot=Slot.A, previous_lkg_slot=Slot.B))

    def test_previous_lkg_slot_never_reclaimable(self):
        self.assertFalse(slot_is_reclaimable(Slot.B, active_slot=Slot.A, previous_lkg_slot=Slot.B))

    def test_neither_active_nor_lkg_is_reclaimable(self):
        # With only two slots this can only happen when there is no
        # previous LKG yet (the very first-ever activation) -- the
        # inactive slot is then genuinely free.
        self.assertTrue(slot_is_reclaimable(Slot.B, active_slot=Slot.A, previous_lkg_slot=None))
