"""Disaster-recovery freshness checks for protected updater payloads.

A Runtime Foundation recovery payload can be internally valid while still
being stale relative to the application checkout being backed up.  The
physical bare-metal acceptance drill exposed exactly that condition: a clean
r0084 application checkout (protected runtime generation 5) was paired with
an older, internally-valid generation-2 recovery payload.

This module supplies one narrow, read-only product-contract comparison used by
the backup validator.  It deliberately does not read privileged live Phase-D
state: the scheduled backup runs as the ordinary application account, while
installed supervisor/runtime state is root-protected.  Instead it compares the
captured protected-updater component against the protected-runtime descriptor
committed in the same application checkout whose Git SHA the backup records.
That is sufficient to reject a stale recovery payload before it can be
promoted as self_contained_v3, and keeps the check available to an unprivileged
nightly backup.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from isadoraair.phase_d_recovery import validate_phase_d_component


PRODUCT_DESCRIPTOR_RELATIVE = Path("deploy/updater_runtime/protected-runtime-descriptor.json")


@dataclass(frozen=True, slots=True)
class ProtectedUpdaterFreshnessEvidence:
    checked: bool
    current: bool
    expected_generation: int | None = None
    observed_generation: int | None = None
    expected_descriptor_sha256: str | None = None
    observed_descriptor_sha256: str | None = None
    diagnostic: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "current": self.current,
            "diagnostic": self.diagnostic,
            "expected_descriptor_sha256": self.expected_descriptor_sha256,
            "expected_generation": self.expected_generation,
            "observed_descriptor_sha256": self.observed_descriptor_sha256,
            "observed_generation": self.observed_generation,
        }


def compare_protected_updater_to_product(
    *, component_root: str | Path, repository_root: str | Path
) -> ProtectedUpdaterFreshnessEvidence:
    """Compare one validated recovery component with this checkout's contract.

    ``validate_phase_d_component`` remains the sole authority for the embedded
    Phase-D component's signature/inventory/descriptor correctness.  This
    function adds only the cross-component freshness invariant that was missing
    from backup acceptance: the payload's active generation and descriptor must
    equal the descriptor committed in the application checkout being backed up.

    The function is read-only and raises no expected validation exception.  A
    malformed/missing product descriptor or invalid recovery component returns
    ``checked=False/current=False`` with a bounded diagnostic so callers can
    fail closed without leaking unrelated content.
    """

    component = Path(component_root)
    repository = Path(repository_root)
    descriptor_path = repository / PRODUCT_DESCRIPTOR_RELATIVE

    try:
        phase_evidence = validate_phase_d_component(component)
    except (OSError, ValueError) as exc:
        return ProtectedUpdaterFreshnessEvidence(
            checked=False,
            current=False,
            diagnostic=f"protected-updater recovery component could not be validated: {' '.join(str(exc).split())[:256]}",
        )

    try:
        descriptor_bytes = descriptor_path.read_bytes()
        descriptor_value = json.loads(descriptor_bytes.decode("utf-8"))
        expected_generation = descriptor_value["generation"]
        if not isinstance(expected_generation, int) or expected_generation < 1:
            raise ValueError("generation is not a positive integer")
        expected_descriptor_sha256 = hashlib.sha256(descriptor_bytes).hexdigest()
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        return ProtectedUpdaterFreshnessEvidence(
            checked=False,
            current=False,
            observed_generation=phase_evidence.get("active_generation"),
            observed_descriptor_sha256=phase_evidence.get("active_descriptor_sha256"),
            diagnostic=(
                "current application protected-runtime descriptor is unavailable or invalid: "
                + " ".join(str(exc).split())[:256]
            ),
        )

    observed_generation = phase_evidence.get("active_generation")
    observed_descriptor_sha256 = phase_evidence.get("active_descriptor_sha256")
    current = (
        observed_generation == expected_generation
        and observed_descriptor_sha256 == expected_descriptor_sha256
    )
    diagnostic = None
    if not current:
        diagnostic = (
            "protected-updater recovery payload is stale for this application checkout: "
            f"expected generation {expected_generation} descriptor {expected_descriptor_sha256}, "
            f"observed generation {observed_generation} descriptor {observed_descriptor_sha256}"
        )

    return ProtectedUpdaterFreshnessEvidence(
        checked=True,
        current=current,
        expected_generation=expected_generation,
        observed_generation=observed_generation,
        expected_descriptor_sha256=expected_descriptor_sha256,
        observed_descriptor_sha256=observed_descriptor_sha256,
        diagnostic=diagnostic,
    )
