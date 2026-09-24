"""Read-only replacement-host readiness gate for configured audio outputs.

This command deliberately does NOT open an ALSA PCM, start/restart the
engine, change any Django row, or silently substitute replacement hardware.

Its purpose is narrower: before an operator starts isadoraair-engine after
bare-metal/disaster recovery, verify the engine-owned AudioOutput roles use
stable ALSA card identities and that those identities exist on THIS host.

A failed result means operator rebind is required in Django Admin before
engine activation. AudioInput/microphone capability is deliberately outside
this command's scope; that is a separate hardware-readiness concern.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from django.core.management.base import BaseCommand, CommandError

from hardware.models import AudioOutput
from library.services.audio_recovery import (
    read_alsa_cards_present,
    resolve_runtime_device,
)


STUDIO_MONITOR_NAME = "Studio Monitor"
STEREOTOOL_OUTPUT_NAME = "Stereotool Input"
ENGINE_OUTPUT_NAMES = (STUDIO_MONITOR_NAME, STEREOTOOL_OUTPUT_NAME)

READY = "READY"
REBIND_REQUIRED = "REBIND_REQUIRED"
UNCONFIGURED = "UNCONFIGURED"


@dataclass(frozen=True, slots=True)
class OutputBindingResult:
    name: str
    status: str
    identity_kind: str
    identity: str
    runtime_device: str | None
    reason: str

    @property
    def ready(self) -> bool:
        return self.status == READY


def inspect_audio_output_bindings(
    cards: dict[str, int] | None = None,
) -> tuple[OutputBindingResult, ...]:
    """Inspect engine-owned AudioOutput rows without opening a PCM device."""

    present_cards = (
        read_alsa_cards_present()
        if cards is None
        else dict(cards)
    )

    rows = list(
        AudioOutput.objects.filter(
            name__in=ENGINE_OUTPUT_NAMES,
        ).order_by("sort_order", "name").only(
            "name",
            "device",
            "device_identity_kind",
            "device_identity",
        )
    )

    results: list[OutputBindingResult] = []
    saw_studio_monitor = False

    for row in rows:
        name = row.name
        legacy_device = row.device or ""
        identity_kind = row.device_identity_kind or ""
        identity = row.device_identity or ""

        if name == STUDIO_MONITOR_NAME:
            saw_studio_monitor = True

        configured = bool(
            legacy_device
            or identity_kind
            or identity
        )

        if not configured:
            # Optional output rows that are completely unconfigured do not
            # prevent engine bring-up. The Studio Monitor is different:
            # engine output is fundamental, so its empty row must fail closed.
            if name == STUDIO_MONITOR_NAME:
                results.append(
                    OutputBindingResult(
                        name=name,
                        status=UNCONFIGURED,
                        identity_kind=identity_kind,
                        identity=identity,
                        runtime_device=None,
                        reason=(
                            "Studio Monitor has no configured output; "
                            "configure/rebind it before engine start"
                        ),
                    )
                )
            continue

        if identity_kind != "alsa_card_id":
            results.append(
                OutputBindingResult(
                    name=name,
                    status=REBIND_REQUIRED,
                    identity_kind=identity_kind,
                    identity=identity,
                    runtime_device=None,
                    reason=(
                        "configured output has no supported stable ALSA "
                        "card identity; raw/legacy binding is not accepted "
                        "for replacement-host readiness"
                    ),
                )
            )
            continue

        if not identity:
            results.append(
                OutputBindingResult(
                    name=name,
                    status=REBIND_REQUIRED,
                    identity_kind=identity_kind,
                    identity=identity,
                    runtime_device=None,
                    reason=(
                        "ALSA card identity kind is configured but the "
                        "identity value is blank"
                    ),
                )
            )
            continue

        runtime_device = resolve_runtime_device(
            identity_kind,
            identity,
            legacy_device,
        )

        if identity not in present_cards:
            results.append(
                OutputBindingResult(
                    name=name,
                    status=REBIND_REQUIRED,
                    identity_kind=identity_kind,
                    identity=identity,
                    runtime_device=runtime_device,
                    reason=(
                        f"ALSA card identity {identity!r} is not present "
                        "on this host"
                    ),
                )
            )
            continue

        results.append(
            OutputBindingResult(
                name=name,
                status=READY,
                identity_kind=identity_kind,
                identity=identity,
                runtime_device=runtime_device,
                reason="stable ALSA card identity is present on this host",
            )
        )

    if not saw_studio_monitor:
        results.append(
            OutputBindingResult(
                name=STUDIO_MONITOR_NAME,
                status=UNCONFIGURED,
                identity_kind="",
                identity="",
                runtime_device=None,
                reason=(
                    "required Studio Monitor AudioOutput row is missing; "
                    "configure/rebind it before engine start"
                ),
            )
        )

    return tuple(results)


def output_bindings_ready(
    results: tuple[OutputBindingResult, ...],
) -> bool:
    return bool(results) and all(result.ready for result in results)


class Command(BaseCommand):
    help = (
        "Read-only pre-engine replacement-host readiness check for "
        "engine-owned AudioOutput stable ALSA bindings."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help="Emit the readiness result as JSON.",
        )

    def handle(self, *args, **options):
        cards = read_alsa_cards_present()
        results = inspect_audio_output_bindings(cards)
        ready = output_bindings_ready(results)

        if options["as_json"]:
            payload = {
                "ok": ready,
                "enumerated_card_ids": sorted(cards),
                "bindings": [
                    asdict(result)
                    for result in results
                ],
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
                "Enumerated ALSA card IDs: "
                + (
                    ", ".join(sorted(cards))
                    if cards
                    else "(none)"
                )
            )

            for result in results:
                identity = (
                    result.identity
                    if result.identity
                    else "-"
                )
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

                if not result.ready:
                    self.stdout.write(
                        f"  {result.reason}"
                    )

        if not ready:
            raise CommandError(
                "Audio output binding readiness FAILED. "
                "Rebind the reported output(s) in Django Admin and "
                "rerun this command before starting isadoraair-engine."
            )

        if not options["as_json"]:
            self.stdout.write(
                self.style.SUCCESS(
                    "Audio output binding readiness: PASS"
                )
            )
