"""The immutable Update Center Phase-D bootstrap supervisor.

DELIBERATELY SEPARATE from deploy/updater_runtime/isadoraair_updater/**
(the REPLACEABLE worker runtime, verified and activated INTO an A/B
slot by this very package) and from deploy/updater_runtime/
protected_bootstrap/** (the worker-side verification contracts D1
built). Nothing in this package imports anything from either of those
trees, from a runtime slot, from the application checkout/venv, or from
Django -- see docs/UPDATE_CENTER_PHASE_D.md's "supervisor independence"
section for why, and this package's own parity tests
(updatecenter/tests/test_phase_d2_parity.py) for proof the two
independent validator implementations (worker-side protected_bootstrap,
supervisor-side this package) actually agree, without either importing
the other.

Python-stdlib-only. Fixed absolute OS-utility invocations only, never a
PATH-resolved executable, never a shell. This package IS the trust
anchor after the one final manual bootstrap -- it must remain correct
and reviewable even years after the worker it manages has been replaced
many times over, so it stays intentionally small and boring (see
supervisor.py's own module docstring for the explicit audited list of
what does NOT belong here).

Independent of the worker's own isadoraair_updater/__init__.py's three
protocol constants -- this package tracks only the ONE protocol
concept that is genuinely its own: BOOTSTRAP_PROTOCOL_VERSION, what a
candidate worker generation and this supervisor must agree on to safely
hand off. See docs/UPDATE_CENTER_PHASE_D.md's "Phase-D version bridge"
section for the full sequencing this constant participates in -- not
bumped in this slice merely because supervisor code was written."""

BOOTSTRAP_PROTOCOL_VERSION = 1
