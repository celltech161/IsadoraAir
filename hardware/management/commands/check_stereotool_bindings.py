"""Secret-safe pre-start readiness gate for Stereo Tool hardware bindings.

Stereo Tool persists physical audio-device choices in ``~/.stereo_tool.rc``.
On Linux those saved ``Device ID`` values can embed machine-local ALSA
coordinates such as ``(hw:1,0)`` even when the surrounding text contains a
friendly card name. Such coordinates are not portable across bare-metal
replacement hardware.

Normal inspection is deliberately observational only. It never opens an ALSA
PCM, rewrites Stereo Tool state, starts/stops a service, or mutates Django data.

After an operator has deliberately reviewed/reassigned the mappings on the
replacement host, ``--accept-current`` records a small host-local acceptance
marker. The marker contains hashes only (never Device ID strings or unrelated
Stereo Tool state), is tied to this machine ID and current ALSA card index/ID
inventory, and becomes invalid automatically if those inputs change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from django.core.management.base import BaseCommand, CommandError

from library.services.audio_recovery import read_alsa_cards_present


READY = "READY"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
NOT_CONFIGURED = "NOT_CONFIGURED"

DEFAULT_RC_PATH = Path.home() / ".stereo_tool.rc"
DEFAULT_ACCEPTANCE_NAME = ".stereo_tool.bindings.accepted.json"
MACHINE_ID_PATH = Path("/etc/machine-id")
ACCEPTANCE_SCHEMA_VERSION = 1

# Narrow, evidence-backed scope: these are the Linux Stereo Tool sections
# observed to carry physical Device ID settings. Unrelated processor state,
# including registration/license material, is never serialized by this check.
DEVICE_SECTIONS = (
    "Direct soundcard access",
    "Low latency output",
    "Soundcard - Input",
    "Soundcard - Input 2",
    "Soundcard - Normal output",
)
PRIMARY_INPUT_SECTION = "Soundcard - Input"

_SECTION_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_ASSIGNMENT_RE = re.compile(r"^([^=:\t]{1,180})\s*(?:=|:|\t)\s*(.*)$")
_EMBEDDED_NUMERIC_HW_RE = re.compile(
    r"\((?:plug)?hw:\s*(\d+)\s*,\s*(\d+)\s*\)",
    re.IGNORECASE,
)


class AcceptanceError(RuntimeError):
    """Explicit acceptance cannot be recorded safely."""


@dataclass(frozen=True, slots=True)
class StereoToolBindingResult:
    section: str
    status: str
    enabled: bool | None
    device_id: str
    addressing: str
    card_index: int | None
    device_number: int | None
    acceptance_eligible: bool
    accepted: bool
    reason: str

    @property
    def blocking(self) -> bool:
        return self.status == REVIEW_REQUIRED


@dataclass(frozen=True, slots=True)
class StereoToolReadinessResult:
    status: str
    rc_path: str
    rc_present: bool
    rc_mode: str | None
    acceptance_path: str
    acceptance_present: bool
    acceptance_valid: bool
    binding_sha256: str | None
    alsa_inventory_sha256: str | None
    reason: str
    bindings: tuple[StereoToolBindingResult, ...]

    @property
    def ready(self) -> bool:
        return self.status == READY


def _parse_enabled(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def _parse_device_sections(text: str) -> dict[str, list[tuple[str, str]]]:
    """Return only Device ID/Enabled records from known hardware sections."""

    current_section: str | None = None
    parsed = {name: [] for name in DEVICE_SECTIONS}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        section_match = _SECTION_RE.match(line)
        if section_match:
            candidate = section_match.group(1)
            current_section = candidate if candidate in parsed else None
            continue

        if current_section is None:
            continue

        assignment_match = _ASSIGNMENT_RE.match(line)
        if not assignment_match:
            continue

        key = assignment_match.group(1).strip()
        if key.lower() not in {"device id", "enabled"}:
            continue

        parsed[current_section].append(
            (key, assignment_match.group(2).strip())
        )

    return parsed


def _inspect_section(
    section: str,
    entries: list[tuple[str, str]],
) -> StereoToolBindingResult | None:
    device_values = [
        value for key, value in entries if key.lower() == "device id"
    ]
    if not device_values:
        return None

    device_id = device_values[0]
    enabled_values = [
        value for key, value in entries if key.lower() == "enabled"
    ]
    enabled = _parse_enabled(enabled_values[0]) if enabled_values else None

    match = _EMBEDDED_NUMERIC_HW_RE.search(device_id)
    if match:
        addressing = "EMBEDDED_NUMERIC_HW"
        card_index = int(match.group(1))
        device_number = int(match.group(2))
    else:
        addressing = "UNKNOWN"
        card_index = None
        device_number = None

    if enabled_values and enabled is None:
        return StereoToolBindingResult(
            section=section,
            status=REVIEW_REQUIRED,
            enabled=None,
            device_id=device_id,
            addressing=addressing,
            card_index=card_index,
            device_number=device_number,
            acceptance_eligible=False,
            accepted=False,
            reason=(
                "saved Enabled value is not understood; operator review is "
                "required and this ambiguous state cannot be accepted"
            ),
        )

    if enabled is False:
        return StereoToolBindingResult(
            section=section,
            status=READY,
            enabled=False,
            device_id=device_id,
            addressing=addressing,
            card_index=card_index,
            device_number=device_number,
            acceptance_eligible=False,
            accepted=False,
            reason=(
                "saved binding is explicitly disabled and does not gate "
                "activation"
            ),
        )

    if section == PRIMARY_INPUT_SECTION and enabled is None:
        eligible = addressing == "EMBEDDED_NUMERIC_HW"
        return StereoToolBindingResult(
            section=section,
            status=REVIEW_REQUIRED,
            enabled=None,
            device_id=device_id,
            addressing=addressing,
            card_index=card_index,
            device_number=device_number,
            acceptance_eligible=eligible,
            accepted=False,
            reason=(
                "primary input has a saved Device ID but no reliable Enabled "
                "field; production evidence shows this state can be active"
                if eligible
                else
                "primary input has no reliable Enabled field and its Device ID "
                "format is not recognized; operator correction is required"
            ),
        )

    if enabled is None:
        return StereoToolBindingResult(
            section=section,
            status=REVIEW_REQUIRED,
            enabled=None,
            device_id=device_id,
            addressing=addressing,
            card_index=card_index,
            device_number=device_number,
            acceptance_eligible=False,
            accepted=False,
            reason=(
                "saved binding has no reliable Enabled state; operator review "
                "is required and this ambiguous state cannot be accepted"
            ),
        )

    if addressing == "EMBEDDED_NUMERIC_HW":
        return StereoToolBindingResult(
            section=section,
            status=REVIEW_REQUIRED,
            enabled=True,
            device_id=device_id,
            addressing=addressing,
            card_index=card_index,
            device_number=device_number,
            acceptance_eligible=True,
            accepted=False,
            reason=(
                "enabled binding embeds machine-local ALSA hw:N,M coordinates; "
                "confirm/reassign it on this host before explicit acceptance"
            ),
        )

    return StereoToolBindingResult(
        section=section,
        status=REVIEW_REQUIRED,
        enabled=True,
        device_id=device_id,
        addressing=addressing,
        card_index=None,
        device_number=None,
        acceptance_eligible=False,
        accepted=False,
        reason=(
            "enabled Device ID is not a recognized binding form; operator "
            "correction is required before Stereo Tool activation"
        ),
    )


def _canonical_binding_payload(
    parsed: dict[str, list[tuple[str, str]]],
) -> list[dict[str, object]]:
    """Canonicalize only known hardware fields for hashing."""

    payload = []
    for section in DEVICE_SECTIONS:
        entries = parsed[section]
        if not entries:
            continue
        payload.append(
            {
                "section": section,
                "entries": [
                    [key.strip().lower(), value]
                    for key, value in entries
                ],
            }
        )
    return payload


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _binding_fingerprint(
    parsed: dict[str, list[tuple[str, str]]],
) -> str:
    return _sha256_json(_canonical_binding_payload(parsed))


def _alsa_inventory_fingerprint(cards: dict[str, int]) -> str:
    return _sha256_json(
        [
            {"card_id": card_id, "card_index": int(card_index)}
            for card_id, card_index in sorted(cards.items())
        ]
    )


def _read_machine_id(path: Path = MACHINE_ID_PATH) -> str:
    try:
        value = path.read_text(encoding="utf-8", errors="strict").strip()
    except OSError:
        return ""
    return value


def _acceptance_path_for(
    rc_path: Path,
    acceptance_path: str | Path | None,
) -> Path:
    if acceptance_path is not None:
        return Path(acceptance_path).expanduser()
    return rc_path.with_name(DEFAULT_ACCEPTANCE_NAME)


def _read_acceptance(
    path: Path,
) -> tuple[dict[str, object] | None, bool, str | None]:
    """Read a host-local marker; return (payload, present, error)."""

    if not path.exists():
        return None, False, None

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return None, True, f"could not stat acceptance marker: {exc}"

    if mode != 0o600:
        return (
            None,
            True,
            f"acceptance marker mode is {mode:04o}; mode 0600 is required",
        )

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, True, f"could not read acceptance marker: {exc}"

    if not isinstance(payload, dict):
        return None, True, "acceptance marker is not a JSON object"

    required = {
        "schema_version",
        "machine_id",
        "binding_sha256",
        "alsa_inventory_sha256",
    }
    if set(payload) != required:
        return None, True, "acceptance marker has unexpected/missing fields"

    if payload.get("schema_version") != ACCEPTANCE_SCHEMA_VERSION:
        return None, True, "acceptance marker schema version is unsupported"

    for key in ("machine_id", "binding_sha256", "alsa_inventory_sha256"):
        if not isinstance(payload.get(key), str) or not payload[key]:
            return None, True, f"acceptance marker field {key!r} is invalid"

    return payload, True, None


def _acceptance_matches(
    acceptance_path: Path,
    *,
    machine_id: str,
    binding_sha256: str,
    alsa_inventory_sha256: str,
) -> tuple[bool, bool, str | None]:
    payload, present, error = _read_acceptance(acceptance_path)
    if error:
        return False, present, error
    if payload is None:
        return False, present, None

    if payload["machine_id"] != machine_id:
        return False, True, "acceptance marker belongs to a different machine"
    if payload["binding_sha256"] != binding_sha256:
        return False, True, "saved hardware bindings changed after acceptance"
    if payload["alsa_inventory_sha256"] != alsa_inventory_sha256:
        return False, True, "ALSA card index/identity inventory changed after acceptance"

    return True, True, None


def inspect_stereotool_bindings(
    rc_path: str | Path | None = None,
    *,
    acceptance_path: str | Path | None = None,
    machine_id: str | None = None,
    cards: dict[str, int] | None = None,
) -> StereoToolReadinessResult:
    """Inspect saved hardware bindings without changing RC or acceptance state."""

    path = Path(rc_path).expanduser() if rc_path else DEFAULT_RC_PATH
    marker_path = _acceptance_path_for(path, acceptance_path)

    if not path.exists():
        return StereoToolReadinessResult(
            status=NOT_CONFIGURED,
            rc_path=str(path),
            rc_present=False,
            rc_mode=None,
            acceptance_path=str(marker_path),
            acceptance_present=marker_path.exists(),
            acceptance_valid=False,
            binding_sha256=None,
            alsa_inventory_sha256=None,
            reason=(
                "Stereo Tool runtime state is not installed; keep the service "
                "stopped until state/hardware is configured and rechecked"
            ),
            bindings=(),
        )

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        return StereoToolReadinessResult(
            status=REVIEW_REQUIRED,
            rc_path=str(path),
            rc_present=True,
            rc_mode=None,
            acceptance_path=str(marker_path),
            acceptance_present=marker_path.exists(),
            acceptance_valid=False,
            binding_sha256=None,
            alsa_inventory_sha256=None,
            reason=f"could not stat Stereo Tool runtime state: {exc}",
            bindings=(),
        )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return StereoToolReadinessResult(
            status=REVIEW_REQUIRED,
            rc_path=str(path),
            rc_present=True,
            rc_mode=f"{mode:04o}",
            acceptance_path=str(marker_path),
            acceptance_present=marker_path.exists(),
            acceptance_valid=False,
            binding_sha256=None,
            alsa_inventory_sha256=None,
            reason=f"could not read Stereo Tool runtime state: {exc}",
            bindings=(),
        )

    parsed = _parse_device_sections(text)
    bindings = tuple(
        result
        for section in DEVICE_SECTIONS
        if (result := _inspect_section(section, parsed[section])) is not None
    )

    current_cards = (
        read_alsa_cards_present()
        if cards is None
        else {str(key): int(value) for key, value in cards.items()}
    )
    current_machine_id = (
        _read_machine_id()
        if machine_id is None
        else machine_id.strip()
    )

    binding_sha256 = _binding_fingerprint(parsed)
    inventory_sha256 = _alsa_inventory_fingerprint(current_cards)

    acceptance_valid = False
    acceptance_present = marker_path.exists()
    acceptance_error = None

    eligible_bindings = [
        binding
        for binding in bindings
        if binding.acceptance_eligible
    ]

    if eligible_bindings and current_machine_id:
        (
            acceptance_valid,
            acceptance_present,
            acceptance_error,
        ) = _acceptance_matches(
            marker_path,
            machine_id=current_machine_id,
            binding_sha256=binding_sha256,
            alsa_inventory_sha256=inventory_sha256,
        )

    if acceptance_valid:
        bindings = tuple(
            replace(
                binding,
                status=READY,
                accepted=True,
                reason=(
                    "host-specific binding was explicitly accepted for this "
                    "machine and current ALSA card index/identity inventory"
                ),
            )
            if binding.acceptance_eligible
            else binding
            for binding in bindings
        )

    reasons: list[str] = []

    if mode != 0o600:
        reasons.append(
            f"runtime-state mode is {mode:04o}; recovery contract requires 0600"
        )

    blocking = [binding for binding in bindings if binding.blocking]
    if blocking:
        reasons.append(
            f"{len(blocking)} saved hardware binding(s) require operator review"
        )
        if eligible_bindings:
            if not current_machine_id:
                reasons.append(
                    "machine ID is unavailable; explicit host acceptance "
                    "cannot be validated"
                )
            elif acceptance_error:
                reasons.append(acceptance_error)
            elif not acceptance_valid:
                reasons.append(
                    "review/reassign the reported mappings, then record "
                    "explicit host acceptance with --accept-current"
                )

    if not bindings and not reasons:
        return StereoToolReadinessResult(
            status=NOT_CONFIGURED,
            rc_path=str(path),
            rc_present=True,
            rc_mode=f"{mode:04o}",
            acceptance_path=str(marker_path),
            acceptance_present=acceptance_present,
            acceptance_valid=False,
            binding_sha256=binding_sha256,
            alsa_inventory_sha256=inventory_sha256,
            reason=(
                "no recognized Stereo Tool hardware Device ID bindings are "
                "present; keep the service stopped until configured"
            ),
            bindings=(),
        )

    if reasons:
        status = REVIEW_REQUIRED
        reason = "; ".join(reasons)
    else:
        status = READY
        if eligible_bindings:
            reason = (
                "runtime-state permissions are correct and active host-specific "
                "bindings have valid explicit acceptance for this host"
            )
        else:
            reason = (
                "runtime-state permissions are correct and every recognized "
                "saved hardware binding is explicitly disabled/non-blocking"
            )

    return StereoToolReadinessResult(
        status=status,
        rc_path=str(path),
        rc_present=True,
        rc_mode=f"{mode:04o}",
        acceptance_path=str(marker_path),
        acceptance_present=acceptance_present,
        acceptance_valid=acceptance_valid,
        binding_sha256=binding_sha256,
        alsa_inventory_sha256=inventory_sha256,
        reason=reason,
        bindings=bindings,
    )


def record_stereotool_binding_acceptance(
    rc_path: str | Path | None = None,
    *,
    acceptance_path: str | Path | None = None,
    machine_id: str | None = None,
    cards: dict[str, int] | None = None,
) -> StereoToolReadinessResult:
    """Explicitly accept the exact reviewed hw:N,M mappings on this host."""

    path = Path(rc_path).expanduser() if rc_path else DEFAULT_RC_PATH
    marker_path = _acceptance_path_for(path, acceptance_path)

    if not path.exists():
        raise AcceptanceError("Stereo Tool runtime state is not installed")

    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AcceptanceError(
            f"could not inspect Stereo Tool runtime state: {exc}"
        ) from exc

    if mode != 0o600:
        raise AcceptanceError(
            f"runtime-state mode is {mode:04o}; set it to 0600 before acceptance"
        )

    parsed = _parse_device_sections(text)
    bindings = tuple(
        result
        for section in DEVICE_SECTIONS
        if (result := _inspect_section(section, parsed[section])) is not None
    )

    if not bindings:
        raise AcceptanceError(
            "no recognized Stereo Tool hardware bindings are present"
        )

    blocking = [binding for binding in bindings if binding.blocking]
    if not blocking:
        raise AcceptanceError(
            "no active host-specific binding requires explicit acceptance"
        )

    unacceptably_ambiguous = [
        binding
        for binding in blocking
        if not binding.acceptance_eligible
    ]
    if unacceptably_ambiguous:
        sections = ", ".join(
            binding.section for binding in unacceptably_ambiguous
        )
        raise AcceptanceError(
            "cannot accept ambiguous/unrecognized binding state in: "
            f"{sections}; correct it in Stereo Tool first"
        )

    current_cards = (
        read_alsa_cards_present()
        if cards is None
        else {str(key): int(value) for key, value in cards.items()}
    )
    present_indexes = set(current_cards.values())

    missing_indexes = sorted(
        {
            binding.card_index
            for binding in blocking
            if binding.card_index is not None
            and binding.card_index not in present_indexes
        }
    )
    if missing_indexes:
        raise AcceptanceError(
            "cannot accept binding(s) whose saved ALSA card index is absent "
            f"on this host: {missing_indexes}"
        )

    current_machine_id = (
        _read_machine_id()
        if machine_id is None
        else machine_id.strip()
    )
    if not current_machine_id:
        raise AcceptanceError(
            "machine ID is unavailable; host-local acceptance cannot be recorded"
        )

    payload = {
        "schema_version": ACCEPTANCE_SCHEMA_VERSION,
        "machine_id": current_machine_id,
        "binding_sha256": _binding_fingerprint(parsed),
        "alsa_inventory_sha256": _alsa_inventory_fingerprint(current_cards),
    }

    if not marker_path.parent.is_dir():
        raise AcceptanceError(
            f"acceptance marker parent does not exist: {marker_path.parent}"
        )

    temp_path = marker_path.with_name(
        f".{marker_path.name}.tmp-{os.getpid()}"
    )
    fd = None
    try:
        fd = os.open(
            temp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            json.dump(
                payload,
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

        os.replace(temp_path, marker_path)
        os.chmod(marker_path, 0o600)
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise AcceptanceError(
            f"could not write acceptance marker: {exc}"
        ) from exc

    result = inspect_stereotool_bindings(
        path,
        acceptance_path=marker_path,
        machine_id=current_machine_id,
        cards=current_cards,
    )
    if not result.ready:
        raise AcceptanceError(
            "acceptance marker was written but readiness still failed: "
            f"{result.reason}"
        )
    return result


class Command(BaseCommand):
    help = (
        "Read-only pre-start disaster-recovery readiness check for Stereo "
        "Tool's saved physical audio-device bindings. --accept-current is an "
        "explicit operator action that records host-local acceptance."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit the readiness result as JSON.",
        )
        parser.add_argument(
            "--rc-path",
            default=None,
            help=(
                "Stereo Tool runtime-state path. Defaults to "
                "~/.stereo_tool.rc; primarily useful for recovery testing."
            ),
        )
        parser.add_argument(
            "--acceptance-path",
            default=None,
            help=(
                "Host-local acceptance marker path. Defaults beside the RC as "
                f"{DEFAULT_ACCEPTANCE_NAME}; primarily useful for testing."
            ),
        )
        parser.add_argument(
            "--accept-current",
            action="store_true",
            help=(
                "After deliberately reviewing/reassigning every reported active "
                "mapping on this host, record acceptance tied to the exact "
                "hardware fields, machine ID, and current ALSA index/ID map."
            ),
        )

    def handle(self, *args, **options):
        cards = read_alsa_cards_present()

        if options["accept_current"]:
            try:
                result = record_stereotool_binding_acceptance(
                    options["rc_path"],
                    acceptance_path=options["acceptance_path"],
                    cards=cards,
                )
            except AcceptanceError as exc:
                raise CommandError(
                    f"Stereo Tool binding acceptance NOT recorded: {exc}"
                ) from exc
            acceptance_written = True
        else:
            result = inspect_stereotool_bindings(
                options["rc_path"],
                acceptance_path=options["acceptance_path"],
                cards=cards,
            )
            acceptance_written = False

        if options["as_json"]:
            payload = asdict(result)
            payload["ok"] = result.ready
            payload["acceptance_written"] = acceptance_written
            self.stdout.write(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            self.stdout.write(
                f"{result.status}: Stereo Tool hardware binding readiness "
                f"rc_path={result.rc_path} mode={result.rc_mode or '-'} "
                f"acceptance={'valid' if result.acceptance_valid else 'none/invalid'}"
            )

            for binding in result.bindings:
                enabled = (
                    "unknown"
                    if binding.enabled is None
                    else ("yes" if binding.enabled else "no")
                )
                coordinate = (
                    f"hw:{binding.card_index},{binding.device_number}"
                    if binding.card_index is not None
                    and binding.device_number is not None
                    else "-"
                )
                self.stdout.write(
                    f"{binding.status}: {binding.section} "
                    f"enabled={enabled} addressing={binding.addressing} "
                    f"saved_coordinate={coordinate} "
                    f"accepted={'yes' if binding.accepted else 'no'}"
                )
                if binding.status == REVIEW_REQUIRED:
                    self.stdout.write(f"  {binding.reason}")

            if result.status != READY:
                self.stdout.write(f"  {result.reason}")

            if acceptance_written:
                self.stdout.write(
                    self.style.SUCCESS(
                        "Stereo Tool binding acceptance recorded for this host."
                    )
                )

        if not result.ready:
            raise CommandError(
                "Stereo Tool hardware binding readiness FAILED. Review/reassign "
                "the reported physical mapping(s), ensure ~/.stereo_tool.rc is "
                "mode 0600, then use --accept-current only after deliberate "
                "operator confirmation; rerun the normal check before enabling "
                "or starting stereotool.service."
            )

        if not options["as_json"]:
            self.stdout.write(
                self.style.SUCCESS(
                    "Stereo Tool hardware binding readiness: PASS"
                )
            )
