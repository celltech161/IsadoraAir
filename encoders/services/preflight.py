"""Dependency + static syntax preflight -- Phase 2D/2E of the encoder
hardening effort. Runs on a rendered candidate BEFORE it is ever
Popen()'d, so a bad configuration can be rejected without stopping a
currently-healthy encoder.

Absolute safety property: nothing in this module opens, or attempts to
open, an ALSA device. `liquidsoap --check` is a static parse/type
check -- confirmed live (not assumed) by running it against a real
script containing the actual `input.alsa(device="airtap")` line WHILE
the production encoder held that device: it returned in well under a
second with a parser warning, not a device-busy error or a hang. The
live source stays owned by whatever's currently running until a
candidate has passed every check in this module and is about to be
launched as its own, independent process."""
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from isadoraair.runtime_validation import (
    STATUS_PASS,
    validate_fdkaac_capability,
)

from . import lkg
from .encoder_manager import script_requires_fdkaac

# Bounded -- Phase 2E's own requirement. `--check` is a pure static
# pass over a script that, at most, defines one ALSA source, a handful
# of output blocks, and the fixed startup-classifier/heartbeat logic
# already proven fast in production; 20s is generous headroom over
# every observed real run (well under 2s) without being so long a
# genuinely hung `liquidsoap` binary blocks candidate evaluation for
# an unreasonable time.
LIQUIDSOAP_CHECK_TIMEOUT_SECONDS = 20

LIQUIDSOAP_BINARY = "liquidsoap"
RUNTIME_CAPABILITY_FAILURE = "runtime_capability"


@dataclass
class PreflightResult:
    """ok=True means every check in this module passed. `reason` is a
    short, human-readable summary safe to put in an event/admin
    message. `detail` is a JSON-serializable dict with more context
    (never containing secrets -- see check_liquidsoap_syntax's own
    stderr-sanitization for why that specifically matters there)."""
    ok: bool
    reason: str = ""
    detail: dict = field(default_factory=dict)
    failure_kind: str | None = None


def _validate_fdkaac(reasons):
    safe_reasons = tuple(reasons) or ("exact Liquidsoap script invokes fdkaac",)
    evidence = validate_fdkaac_capability(reasons=safe_reasons)
    if evidence.status == STATUS_PASS:
        return PreflightResult(
            ok=True,
            reason="fdkaac runtime capability ok",
            detail={
                "component": "fdkaac",
                "status": evidence.status,
                "reasons": list(evidence.reasons),
                "observed": evidence.observed,
                "capabilities": list(evidence.capabilities),
            },
        )
    return PreflightResult(
        ok=False,
        reason="fdkaac runtime capability validation failed",
        failure_kind=RUNTIME_CAPABILITY_FAILURE,
        detail={
            "component": "fdkaac",
            "status": evidence.status,
            "reasons": list(evidence.reasons),
            "observed": evidence.observed,
            "capabilities": list(evidence.capabilities),
            "diagnostics": list(evidence.diagnostics),
        },
    )


def check_script_runtime_capabilities(script_text, *, reasons=()):
    """Capability-gate the exact Liquidsoap script that may be launched.

    Detection is delegated to encoder_manager's renderer-coupled parser;
    Foundation E remains the sole authority for what constitutes working
    fdkaac/libfdk-aac capability.
    """
    if not script_requires_fdkaac(script_text):
        return PreflightResult(ok=True, reason="runtime capabilities not required")
    return _validate_fdkaac(reasons)


def check_dependencies(encoders, *, script_path=None, script_text=None):
    """Liquidsoap + runtime capability + directory writability checks
    -- every check here is safe to run without competing for the live
    ALSA source. Returns PreflightResult.

    When the exact rendered script is supplied, it is authoritative for
    fdkaac requirement discovery (including Aircheck and persisted LKG
    content). The encoder-list fallback preserves the public helper's
    direct-call behavior for streaming AAC without consulting or creating
    AircheckConfig."""
    problems = []
    capability_detail = None
    capability_failure_kind = None
    ordinary_problem_count = 0

    liquidsoap_path = shutil.which(LIQUIDSOAP_BINARY)
    if liquidsoap_path is None:
        problems.append(f"{LIQUIDSOAP_BINARY!r} binary not found on PATH")
        ordinary_problem_count += 1
    elif not _is_executable(liquidsoap_path):
        problems.append(f"{liquidsoap_path!r} exists but is not executable")
        ordinary_problem_count += 1

    if script_text is None and script_path is not None:
        try:
            script_text = Path(script_path).read_text(encoding="utf-8")
        except OSError:
            problems.append("rendered Liquidsoap script could not be read")
            ordinary_problem_count += 1

    streaming_aac = any(getattr(encoder, "format", None) == "aac" for encoder in encoders)
    needs_fdkaac = (
        script_requires_fdkaac(script_text)
        if script_text is not None
        else streaming_aac
    )
    if needs_fdkaac:
        reasons = []
        if streaming_aac:
            reasons.append("streaming AAC output")
        if script_text is not None:
            reasons.append("exact rendered Liquidsoap script invokes fdkaac")
        capability = (
            check_script_runtime_capabilities(script_text, reasons=tuple(reasons))
            if script_text is not None
            else _validate_fdkaac(tuple(reasons))
        )
        capability_detail = capability.detail
        if not capability.ok:
            problems.append(capability.reason)
            capability_failure_kind = capability.failure_kind

    for label, path in (
        ("candidate directory", lkg.CANDIDATE_DIR),
        ("persistent LKG directory", lkg.LKG_DIR),
    ):
        writable, reason = _dir_writable(path)
        if not writable:
            problems.append(f"{label} ({path}) is not writable: {reason}")
            ordinary_problem_count += 1

    if problems:
        detail = {"problems": problems}
        if capability_detail is not None:
            detail["fdkaac"] = capability_detail
        # A runtime-capability classification is transient only when it
        # is the sole failed dependency.  If Liquidsoap or a required
        # directory is also broken, preserve the existing ordinary
        # preflight classification rather than masking that separate
        # failure behind the fdkaac infrastructure result.
        failure_kind = (
            capability_failure_kind
            if capability_failure_kind == RUNTIME_CAPABILITY_FAILURE
            and ordinary_problem_count == 0
            else None
        )
        return PreflightResult(
            ok=False,
            reason="; ".join(problems),
            detail=detail,
            failure_kind=failure_kind,
        )
    detail = {"fdkaac": capability_detail} if capability_detail is not None else {}
    return PreflightResult(ok=True, reason="dependencies ok", detail=detail)


def _is_executable(path_str):
    import os
    return os.access(path_str, os.X_OK)


def _dir_writable(path):
    """Best-effort check: the directory either already exists and is
    writable, or its nearest existing ancestor is (so a not-yet-
    created-but-creatable directory doesn't falsely fail). Returns
    (bool, reason_str)."""
    import os
    path = Path(path)
    probe = path
    while not probe.exists():
        if probe.parent == probe:
            return False, "no existing ancestor directory found"
        probe = probe.parent
    if not probe.is_dir():
        return False, f"{probe} exists but is not a directory"
    if not os.access(probe, os.W_OK):
        return False, f"{probe} is not writable"
    return True, ""


# Secrets that must never appear in stored/logged diagnostic output --
# every password among the candidate's own encoders. Liquidsoap error
# output CAN echo back fragments of the script it's parsing (e.g. a
# malformed line is often quoted verbatim in the error message), so a
# candidate with a syntax error immediately adjacent to a `password=`
# argument could otherwise leak it into an event/admin-visible field.
def _sanitize_output(text, encoders):
    for enc in encoders:
        password = getattr(enc, "password", "") or ""
        if password:
            text = text.replace(password, "***REDACTED***")
    return text


def check_liquidsoap_syntax(script_path, encoders):
    """Run `liquidsoap --check <script_path>`, bounded by
    LIQUIDSOAP_CHECK_TIMEOUT_SECONDS. Returns PreflightResult.

    Treats a missing binary, a timeout, and a nonzero exit code all as
    failure (Phase 2E's explicit requirement) -- callers must not try
    to distinguish "liquidsoap isn't installed" from "the script is
    invalid" by inspecting exceptions themselves; this function already
    did that and reports either as ok=False with a reason.

    stdout/stderr are captured and sanitized (see _sanitize_output)
    before being placed in `detail` -- this is the only preflight
    output that could plausibly echo configured secrets back (a syntax
    error near a `password=` argument), so it's the one checked here
    even though the CALLER is expected to already be logging/storing
    this detail as non-secret."""
    if shutil.which(LIQUIDSOAP_BINARY) is None:
        return PreflightResult(ok=False, reason=f"{LIQUIDSOAP_BINARY!r} not found on PATH", detail={"exit_code": None})

    try:
        result = subprocess.run(
            [LIQUIDSOAP_BINARY, "--check", str(script_path)],
            capture_output=True, text=True, timeout=LIQUIDSOAP_CHECK_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return PreflightResult(
            ok=False,
            reason=f"liquidsoap --check timed out after {LIQUIDSOAP_CHECK_TIMEOUT_SECONDS}s",
            detail={"exit_code": None, "timed_out": True},
        )
    except OSError as exc:
        return PreflightResult(ok=False, reason=f"failed to run liquidsoap --check: {exc}", detail={"exit_code": None})

    stdout = _sanitize_output(result.stdout or "", encoders)
    stderr = _sanitize_output(result.stderr or "", encoders)
    detail = {"exit_code": result.returncode, "stdout": stdout, "stderr": stderr}

    if result.returncode != 0:
        return PreflightResult(ok=False, reason=f"liquidsoap --check failed (exit {result.returncode})", detail=detail)
    return PreflightResult(ok=True, reason="syntax ok", detail=detail)


def run_preflight(script_path, encoders):
    """Convenience wrapper: dependency checks, then (only if those
    pass) the syntax check. Short-circuits on the first failure so a
    missing binary is reported plainly rather than also attempting
    (and failing differently on) the syntax check."""
    dep_result = check_dependencies(encoders, script_path=script_path)
    if not dep_result.ok:
        return dep_result
    return check_liquidsoap_syntax(script_path, encoders)
