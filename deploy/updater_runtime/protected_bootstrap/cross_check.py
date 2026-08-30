"""D1-G: the protected-runtime predecessor-diff cross-check contract.

    deploy/updater_runtime/** changed
        -> protected_runtime metadata required
        -> descriptor exact
        -> descriptor inventory exact
        -> signature threshold exact

and the converse:

    protected_runtime metadata present
        -> a real protected-runtime change/generation transition must exist

A signed protected-policy-only change (D1-C's protected-policy.json,
required to live INSIDE the descriptor's own file inventory under
deploy/updater_runtime/ -- see policy.py's own docstring) is therefore
already covered by the SAME "deploy/updater_runtime/** changed" test as
an ordinary Python source change: this module does not need a second,
separate "policy-only" code path, because a signed policy file's
mandatory location makes it structurally indistinguishable, at the
diff-detection level, from any other protected-runtime-tree change --
exactly the "one simple trust boundary" this task's own D1-G
recommendation asks for (a policy change increments generation and
walks the identical verified path as a code change, never a shortcut).

NOT YET WIRED into isadoraair_updater.release._cross_check()'s live
execution path. That function's existing "protected updater runtime
changes require manual_bootstrap_required=true" gate (r0022+) is left
completely unchanged in this workorder, per its own explicit
instruction not to prematurely remove pre-Phase-D manual-bootstrap
enforcement. cross_check_protected_runtime() below is a complete,
independently-tested contract D2 can splice into that gate once Phase D
activates (see `phase_d_active` below, which makes this function an
explicit no-op -- not merely "not yet called" -- for every release
before that happens, so even an accidental early wire-in cannot change
current behavior for r0025/r0026)."""
from __future__ import annotations

import dataclasses

from .descriptor import generation_advances
from .manifest_field import ProtectedRuntimeField


class CrossCheckError(ValueError):
    """Raised only for a caller-input contract violation -- an ordinary
    failed cross-check is a CrossCheckResult with violations, not this."""


@dataclasses.dataclass(frozen=True)
class CrossCheckResult:
    ok: bool
    violations: tuple[str, ...]


def cross_check_protected_runtime(
    *,
    phase_d_active: bool,
    runtime_paths_changed: bool,
    protected_runtime_field: ProtectedRuntimeField | None,
    previous_generation: int | None,
    current_bootstrap_protocol_version: int,
    current_wire_protocol_version: int,
) -> CrossCheckResult:
    """`runtime_paths_changed` is the caller's own predecessor-diff fact
    (True iff ANY path under deploy/updater_runtime/** differs between
    the previous release's commit and this release's commit -- the
    exact same fact isadoraair_updater.release._cross_check() already
    computes today via TrustedRepository.changed_paths(), just not yet
    passed to this function).

    When `phase_d_active` is False, this is an explicit, total no-op
    (ok=True, no violations) regardless of every other argument --
    this is the D0 bridge: protected_runtime cross-checking has no
    opinion at all about r0025/r0026, whose parser does not even know
    the field exists."""
    if not phase_d_active:
        return CrossCheckResult(ok=True, violations=())

    violations: list[str] = []

    if runtime_paths_changed and protected_runtime_field is None:
        violations.append(
            "deploy/updater_runtime/** changed but the manifest declares no protected_runtime "
            "metadata -- a protected-runtime change (code OR the signed policy file living in "
            "that same tree) requires the full descriptor/attestation contract, once Phase D is active"
        )
    if protected_runtime_field is not None and not runtime_paths_changed:
        violations.append(
            "manifest declares protected_runtime metadata but deploy/updater_runtime/** did not "
            "change -- protected_runtime metadata must correspond to a real change/generation transition"
        )

    if protected_runtime_field is not None:
        if not generation_advances(protected_runtime_field.generation, previous_generation):
            if previous_generation is None:
                violations.append(
                    f"first-ever protected_runtime.generation must be exactly 1, "
                    f"got {protected_runtime_field.generation}"
                )
            else:
                violations.append(
                    f"protected_runtime.generation {protected_runtime_field.generation} does not "
                    f"strictly exceed the previous generation {previous_generation} -- "
                    "replay/rollback refused"
                )
        if protected_runtime_field.minimum_bootstrap_protocol_version > current_bootstrap_protocol_version:
            violations.append(
                f"protected_runtime requires bootstrap protocol "
                f"{protected_runtime_field.minimum_bootstrap_protocol_version}, this station's "
                f"supervisor only understands up to {current_bootstrap_protocol_version} -- "
                "unsupported bootstrap protocol, manual supervisor upgrade required"
            )
        if current_wire_protocol_version not in protected_runtime_field.supported_wire_protocols:
            violations.append(
                f"protected_runtime.supported_wire_protocols {list(protected_runtime_field.supported_wire_protocols)!r} "
                f"does not include this station's current wire protocol "
                f"{current_wire_protocol_version} -- unsupported wire compatibility, would strand "
                "an already-connected client"
            )

    return CrossCheckResult(ok=not violations, violations=tuple(violations))
