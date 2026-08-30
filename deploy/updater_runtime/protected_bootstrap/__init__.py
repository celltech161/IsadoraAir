"""Phase D1 stable contracts -- pure-stdlib, data-only validators for the
future self-updating protected-runtime bootstrap (Update Center Phase D).

This package is deliberately independent of Django, of
isadoraair_updater.daemon/executor/jobs (the stateful/privileged worker
machinery), and of any application venv -- see docs/UPDATE_CENTER_PHASE_D.md
for the full architecture this exists to support. A future standalone
supervisor (D2+) must be able to import and use every validator in this
package to independently verify a candidate protected-runtime bundle
BEFORE ever executing any of that bundle's code, without dragging in
anything that bundle itself might be replacing.

Every module here answers only "is this data, taken alone (plus the
files/signatures it references), structurally and cryptographically
valid" -- never "should this be installed/activated now." That decision,
and the A/B slot/handoff machinery that acts on it, belongs to D2+.

Modules:
  descriptor.py   -- D1-B: the runtime bundle's file inventory + digest.
  policy.py       -- D1-C: the signed managed-unit policy data contract.
  attestation.py  -- D1-D: the signed statement + fixed OS-verifier boundary.
  trust.py        -- D1-E: the M-of-N trust-policy schema + evaluation.
  verification.py -- D1-F: ties the above into one independent-verification
                     entry point, still importable without Django/executor.
  manifest_field.py -- D1-A: the optional release-manifest protected_runtime
                     field this package's descriptor/attestation concepts
                     are referenced from.
  cross_check.py  -- D1-G: the protected-runtime predecessor-diff contract
                     (not yet wired into the live pre-D cross-check path --
                     see that module's own docstring)."""
