"""D2-B: the exactly-two runtime slot layout. Slot names are a closed
enum, never an arbitrary string from a repository, manifest, or IPC
request -- there is no code path anywhere in this package that accepts
a slot name as free text and uses it to build a filesystem path."""
from __future__ import annotations

import dataclasses
import enum
import os
from pathlib import Path
import shutil
import tempfile

from .security import ProtectionError, assert_no_symlink_in_tree, assert_root_protected, assert_root_protected_parents


class Slot(enum.Enum):
    A = "A"
    B = "B"

    def other(self) -> "Slot":
        return Slot.B if self is Slot.A else Slot.A


class SlotError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class SlotLayout:
    """Owns exactly the two slot directories plus one staging area, all
    under one configured, root-owned slots_root -- never a dynamically
    named path. Staging is a THIRD, separate directory from either
    final slot, so a crash mid-populate can never leave a partially
    written tree at a path anything else would mistake for a real,
    already-verified slot (D2-B's own explicit staging requirement)."""
    slots_root: Path

    def slot_path(self, slot: Slot) -> Path:
        return self.slots_root / slot.value

    @property
    def staging_root(self) -> Path:
        return self.slots_root / ".staging"

    def new_staging_directory(self) -> Path:
        """A fresh, uniquely-named directory under staging_root -- the
        caller populates it, this module's publish_slot() then verifies
        the FINAL destination is safe and atomically renames staging
        content into place. Never reused across two different staging
        attempts (tempfile.mkdtemp's own uniqueness guarantee)."""
        self.staging_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return Path(tempfile.mkdtemp(dir=self.staging_root, prefix="candidate-"))

    def discard_staging_directory(self, path: Path) -> None:
        path = Path(path)
        if path.parent != self.staging_root:
            raise SlotError(f"refusing to discard a path outside staging_root: {path}")
        shutil.rmtree(path, ignore_errors=True)


def assert_layout_root_protected(layout: SlotLayout) -> None:
    assert_root_protected_parents(layout.slots_root)
    assert_root_protected(layout.slots_root, recursive=False)


def publish_slot(layout: SlotLayout, slot: Slot, staged: Path, *, active_slot: Slot | None) -> None:
    """Atomically promotes a fully-populated, already-verified staging
    directory into `slot`'s final path. Refuses outright if `slot` is
    currently the active slot -- "the active slot is never overwritten
    in place" is enforced here structurally, not merely by convention;
    a caller that tries anyway gets SlotError, not a silently-clobbered
    running generation.

    Promotion is a single os.replace() of a DIRECTORY, so it is atomic
    at the filesystem level (same slots_root, same filesystem, no
    partial-rename window) -- there is no moment where `slot`'s path
    exists but is half-old/half-new content."""
    if active_slot is not None and slot is active_slot:
        raise SlotError(f"refusing to publish into the currently active slot {slot.value}")
    staged = Path(staged)
    if staged.parent != layout.staging_root:
        raise SlotError("staged content must come from this layout's own staging_root")
    try:
        assert_no_symlink_in_tree(staged)
    except ProtectionError as exc:
        raise SlotError(f"staged content is unsafe: {exc}") from exc
    destination = layout.slot_path(slot)
    if destination.exists():
        # Reclaiming an inactive, non-LKG slot -- move the old content
        # aside first (same directory, so still same-filesystem) rather
        # than deleting-then-renaming, which would have a window where
        # the slot path does not exist at all.
        discard_target = layout.staging_root / f"discarded-{slot.value}-{os.getpid()}-{id(staged)}"
        os.replace(destination, discard_target)
        shutil.rmtree(discard_target, ignore_errors=True)
    os.replace(staged, destination)
    _fsync_directory(layout.slots_root)


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def slot_is_reclaimable(candidate_slot: Slot, *, active_slot: Slot, previous_lkg_slot: Slot | None) -> bool:
    """D2-M: an inactive slot may be reclaimed ONLY once it is proven
    to be neither the active slot nor the previous-LKG slot. Deliberately
    a pure function of already-validated RuntimeState facts (see
    state.py) -- never inferred from "which slot merely looks unused,"
    since a crash-recovered supervisor's own idea of "unused" cannot be
    trusted without checking against the durable state record."""
    if candidate_slot is active_slot:
        return False
    if previous_lkg_slot is not None and candidate_slot is previous_lkg_slot:
        return False
    return True
