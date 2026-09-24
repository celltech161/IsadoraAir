"""Read-only replacement-host readiness gate for the configured studio microphone.

This command deliberately does NOT open an ALSA PCM, start/restart the engine,
change any Django row, or silently substitute replacement hardware.

Before an operator starts isadoraair-engine after bare-metal/disaster recovery,
it verifies that the configured ``Studio Microphone 1`` uses a supported stable
ALSA card identity and that the identity is capture-capable on device 0 on THIS
host. Capture capability comes from the same direction-aware discovery used by
Django Admin, which intentionally matches the production
``plughw:CARD=<id>,DEV=0` resolver contract.

An entirely unconfigured/missing studio-microphone row is allowed: the engine
already treats that as "mic disabled". A configured-but-nonportable row fails
closed and requires an explicit operator rebind before engine activation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from django.core.management.base import BaseCommand, CommandError

from hardware.devices import list_alsa_card_identities
from hardware.models import AudioInput
from library.services.audio_recovery import resolve_runtime_device


STUDIO_MIC_NAME = "Studio Microphone 1"

READY = "READY"
NOT_CONFIGURED = "NOT_CONFIGURED"
REBIND_REQUIRED = "REBIND_REQUIRED"


@dataclass(frozen=True, slots=True)
class InputBindingResult:
    name: str
    status: str
    identity_kind: str
    identity: str
    runtime_device: str | None
    reason: str

    @property
    def ready(self) -> bool:
        return self.status in {READY, NOT_CONFIGURED}


def _capture_capable_card_ids() -> set[str]:
    """Return stable IDs that support capture on DEV=0.

    ``list_alsa_card_identities("capture")`` already enforces the DEV=0
    contract used by ``resolve_runtime_device`. Keep that rule in one place
    instead of re-parsing ``arecord -l`  here.
    """

    return {
        identity.card_id
        for identity in list_alsa_card_identities("capture")
    }


def inspect_audio_input_binding(
    capture_card_ids: set[str] | None = None,
) -> InputBindingResult:
    """Inspect Studio Microphone 1 without opening a PCM device."""

    capable_ids = (
        _capture_capable_card_ids()
        if capture_card_ids is None
        else set(capture_card_ids)
    )

    row = (
        AudioInput.objects.filter(name=STUDIO_MIC_NAME)
        .only(
            "name",
            "device",
            "device_identity_kind",
            "device_identity",
        )
        .first()
    )

    if row is None:
        return InputBindingResult(
            name=STUDIO_MIC_NAME,
            status=NOT_CONFIGURED,
            identity_kind="",
            identity="",
            runtime_device=None,
            reason=(
                "Studio Microphone 1 is not configured; the engine will "
                "run with the local studio microphone disabled"
            ),
        )

    legacy_device = row.device or ""
    identity_kind = row.device_identity_kind or ""
    identity = row.device_identity or ""

    configured = bool(
        legacy_device
        or identity_kind
        or identity
    )

    if not configured:
        return InputBindingResult(
            name=STUDIO_MIC_NAME,
            status=NOT_CONFIGURED,
            identity_kind=identity_kind,
            identity=identity,
            runtime_device=None,
            reason=(
                "Studio Microphone 1 is not configured; the engine will "
                "run with the local studio microphone disabled"
            ),
        )

    if identity_kind != "alsa_card_id":
        return InputBindingResult(
            name=STUDIO_MIC_NAME,
            status=REBIND_REQUIRED,
            identity_kind=identity_kind,
            identity=identity,
            runtime_device=None,
            reason=(
                "configured studio microphone has no supported stable ALSA "
                "card identity; raw/legacy binding is not accepted for "
                "replacement-host readiness"
            ),
        )

    if not identity:
        return InputBindingResult(
            name=STUDIO_MIC_NAME,
            status=REBIND_REQUIRED,
            identity_kind=identity_kind,
            identity=identity,
            runtime_device=None,
            reason=(
                "ALSA card identity kind is configured but the identity "
                "value is blank"
            ),
        )

    runtime_device = resolve_runtime_device(
        identity_kind,
        identity,
        legacy_device,
    )

    if identity not in capable_ids:
        return InputBindingResult(
            name=STUDIO_MIC_NAME,
            status=REBIND_REQUIRED,
            identity_kind=identity_kind,
            identity=identity,
            runtime_device=runtime_device,
            reason=(
                f"ALSA card identity {identity!r} is not capture-capable "
                "on DEV=0 on this host"
            ),
        )

    return InputBindingResult(
        name=STUDIO_MIC_NAME,
        status=READY,
        identity_kind=identity_kind,
        identity=identity,
        runtime_device=runtime_device,
        reason=(
            "stable ALSA card identity is capture-capable on DEV=0 "
            "on this host"
        ),
    )


def input_binding_ready(result: InputBindingResult) -> bool:
    return result.ready


class Command(BaseCommand):
    help = (
        "Read-only pre-engine replacement-host readiness check for the "
        "configured Studio Microphone 1 stable ALSA capture binding."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit the readiness result as JSON.",
        )

    def handle(self, *args, **options):
        capture_card_ids = _capture_capable_card_ids()
        result = inspect_audio_input_binding(capture_card_ids)
        ready = input_binding_ready(result)

        if options["as_json"]:
            payload = {
                "ok": ready,
                "capture_capable_card_ids": sorted(capture_card_ids),
                "binding": asdict(result),
            }
            self.stdout.write(
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            self.stdout.write(
                "Capture-capable ALSA card IDs (DEV=0): "
                + (
                    ", ".join(sorted(capture_card_ids))
                    if capture_card_ids
                    else "(none)"
                )
            )

            identity = result.identity if result.identity else "-"
            runtime = (
                result.runtime_device
                if result.runtime_device
                else "-"
            )

            self.stdout.write(
                f"{result.status}: {result.name} "
                f"identity={identity} "
                f"runtime_device={runtime}"
            )

            if result.status != READY:
                self.stdout.write(
                    f"  {result.reason}"
                )

        if not ready:
            raise CommandError(
                "Audio input binding readiness FAILED. "
                "Rebind Studio Microphone 1 in Django Admin to a "
                "capture-capable stable ALSA identity and rerun this "
                "command before starting isadoraair-engine."
            )

        if not options["as_json"]:
            self.stdout.write(
                self.style.SUCCESS(
                    "Audio input binding readiness: PASS"
                )
            )
