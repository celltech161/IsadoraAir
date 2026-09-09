"""Shared, DB-touching persona/voice readiness check for schedule slots.

Extracted (r0050) from weather/forms.py's HourlyScheduleField.validate()
so the Weather diagnostics authority (weather/diagnostics.py) and the
admin form can never disagree about what makes a scheduled persona slot
"ready" -- one authority, two consumers. Deliberately kept separate from
weather/voice_schedule.py's own provider-free/DB-free boundary: this
module touches the database (WeatherVoicePersona + its StationTTSVoice),
voice_schedule.py never does.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import WeatherVoicePersona


@dataclass(frozen=True)
class PersonaSlotCheck:
    """One referenced schedule slot's readiness. `problem` is None when
    the slot is fully ready (persona exists, has a logical voice, that
    voice is enabled); otherwise it is the exact operator-facing
    sentence forms.py has always raised for this condition -- unchanged
    wording, now produced in one place."""

    slot: str
    exists: bool
    label: str
    tts_voice_id: int | None
    tts_voice_name: str | None
    tts_voice_enabled: bool | None
    problem: str | None


def check_persona_slots(slots) -> list[PersonaSlotCheck]:
    """slots: an iterable of persona slot strings referenced by a
    schedule (e.g. the values of an expand_to_hours() result). Returns
    one PersonaSlotCheck per input slot, in input order -- callers that
    want deduplicated/sorted output should pass sorted(set(slots))."""
    slots = list(slots)
    personas = {
        persona.slot: persona
        for persona in WeatherVoicePersona.objects.filter(
            slot__in=set(slots)
        ).select_related("tts_voice")
    }
    results = []
    for slot in slots:
        persona = personas.get(slot)
        if persona is None:
            results.append(PersonaSlotCheck(
                slot=slot, exists=False, label=slot,
                tts_voice_id=None, tts_voice_name=None, tts_voice_enabled=None,
                problem=f'"{slot}" is scheduled but no Weather Voice Persona exists for that slot.',
            ))
            continue
        label = persona.display_name or persona.full_name or slot
        if persona.tts_voice_id is None:
            results.append(PersonaSlotCheck(
                slot=slot, exists=True, label=label,
                tts_voice_id=None, tts_voice_name=None, tts_voice_enabled=None,
                problem=f'"{label}" is scheduled but has no logical station voice selected.',
            ))
            continue
        if not persona.tts_voice.enabled:
            results.append(PersonaSlotCheck(
                slot=slot, exists=True, label=label,
                tts_voice_id=persona.tts_voice_id, tts_voice_name=persona.tts_voice.name,
                tts_voice_enabled=False,
                problem=f'"{label}" is scheduled but its station voice ({persona.tts_voice.name}) is disabled.',
            ))
            continue
        results.append(PersonaSlotCheck(
            slot=slot, exists=True, label=label,
            tts_voice_id=persona.tts_voice_id, tts_voice_name=persona.tts_voice.name,
            tts_voice_enabled=True, problem=None,
        ))
    return results
