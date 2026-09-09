"""r0028: the 24-hour announcer schedule grid -- replaces raw
WeatherConfig.voice_schedule JSON editing with a friendly per-hour
picker, without changing the underlying JSONField or its stored
representation at all (see weather/voice_schedule.py's own
expand_to_hours()/compress_from_hours(), which do the actual
round-trip; this module owns the Django form/widget/DB-touching
persona validation layer around them, deliberately kept separate from
that module's own provider-free/DB-free boundary)."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.utils import timezone

from .models import WeatherConfig, WeatherVoicePersona, normalize_alert_sound_trigger_events
from .persona_readiness import check_persona_slots
from .voice_schedule import ScheduleError, compress_from_hours, expand_to_hours


def hour_label(hour: int) -> str:
    """0 -> "12 AM", 13 -> "1 PM", etc. -- AM/PM display, matching the
    project's own existing weather-admin convention (helper is shared
    by the widget context, the plain-English range summary, and the
    tests -- one source of truth for the format)."""
    period = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display} {period}"


def station_local_hour(now=None):
    """The SAME authoritative station-time semantics already used for
    schedule resolution elsewhere (road_conditions.voice._resolve_
    schedule_slot's own dj_timezone.localtime(...).hour) -- never
    browser-local time, which is exactly why this is computed here,
    server-side, and handed to the template as a plain int rather than
    left for client-side JS to (mis)compute."""
    return timezone.localtime(now or timezone.now()).hour


def persona_ranges_from_hours(hour_to_voice):
    """A complete {0: voice, ..., 23: voice} mapping -> {slot: "3 AM-8
    AM, 3 PM-8 PM"} plain-English ranges, one entry per slot that
    appears anywhere in the schedule, multiple non-contiguous ranges
    for the same persona joined in schedule order. Built directly on
    compress_from_hours() (the same canonical-merge logic the stored
    JSON round-trips through), so this never has to separately
    reconcile a non-minimal-but-valid schedule."""
    canonical = compress_from_hours(hour_to_voice)
    ranges_by_slot: dict[str, list[str]] = {}
    for slot, start, end in canonical:
        ranges_by_slot.setdefault(slot, []).append(f"{hour_label(start)}–{hour_label(end)}")
    return {slot: ", ".join(parts) for slot, parts in ranges_by_slot.items()}


class _BoundHours(dict):
    """Marks a widget value as having come from HourlyScheduleWidget's
    own value_from_datadict() (a bound-form re-render after a failed
    validation) rather than from the database. Real bug found while
    writing this feature's own tests: a plain `isinstance(value, dict)`
    check cannot tell those apart -- a malformed STORED voice_schedule
    that happens to itself be dict-shaped (e.g. corrupt/legacy JSON
    like {"day": [3, 8]} instead of the correct triple-list) would be
    silently read as "24 unassigned hours" instead of surfacing the
    required "fail clearly and safely, never silently reinterpret"
    behavior. See HourlyScheduleField.bound_data() (the only place
    this wrapper is ever constructed) and the widget's own
    get_context() below (the only place it's ever unwrapped)."""


class HourlyScheduleWidget(forms.Widget):
    """Renders 24 real, native, individually-labelled <select> elements
    (one per local hour) -- never a single JS-only canvas/grid. This is
    deliberate, not a fallback: keyboard/screen-reader access and "the
    schedule must still work if JavaScript initialization fails" both
    come for free from using real form controls, and
    weather/static/weather/admin/voice_schedule_grid.js only ever
    re-skins/positions these same 24 selects into a compact grid with
    optional click-drag painting -- it never becomes the thing that
    actually holds the value. Server-side Field.clean() below is what
    actually validates and persists; the JS is cosmetic only."""

    template_name = "weather/widgets/hourly_schedule.html"

    def __init__(self, attrs=None):
        super().__init__(attrs)

    class Media:
        css = {"all": ("weather/css/voice_schedule_grid.css",)}
        js = ("weather/js/voice_schedule_grid.js",)

    def value_from_datadict(self, data, files, name):
        """Collects the 24 posted <select> values into {hour: slot}.
        A hidden/removed hour (e.g. a tampered POST) is simply absent
        from the dict -- HourlyScheduleField.clean() below is what
        turns "not exactly 24 keys present" into a real validation
        error, never a silent partial-schedule save."""
        hours = {}
        for hour in range(24):
            raw = data.get(f"{name}_{hour}")
            if raw:
                hours[hour] = raw
        return hours

    def get_context(self, name, value, attrs):
        context = super().get_context(name, value, attrs)
        personas = list(WeatherVoicePersona.objects.order_by("slot"))
        persona_labels = {
            persona.slot: persona.display_name or persona.full_name or persona.slot
            for persona in personas
        }

        if isinstance(value, _BoundHours):
            # Re-rendering a bound (possibly invalid) POST -- show
            # exactly what the operator submitted, not the DB value.
            # Deliberately NOT a bare `isinstance(value, dict)` check --
            # see _BoundHours' own docstring for the real bug that was.
            hour_to_slot = {hour: value.get(hour) for hour in range(24)}
            schedule_error = None
        else:
            try:
                hour_to_slot = expand_to_hours(value or [])
                schedule_error = None
            except ScheduleError as exc:
                # Malformed stored data: fail clearly and safely --
                # never silently reinterpret it. The grid still
                # renders (every hour starts unassigned) so the
                # operator can fix it from a known-empty state instead
                # of being locked out of the page entirely.
                hour_to_slot = {}
                schedule_error = str(exc)

        current_hour = station_local_hour()
        hours = [
            {
                "hour": hour,
                "label": hour_label(hour),
                "field_name": f"{name}_{hour}",
                "selected_slot": hour_to_slot.get(hour),
                "is_current": hour == current_hour,
            }
            for hour in range(24)
        ]

        on_duty_slot = hour_to_slot.get(current_hour)
        persona_ranges = None
        if set(hour_to_slot) == set(range(24)) and all(hour_to_slot.values()):
            try:
                ranges_by_slot = persona_ranges_from_hours(hour_to_slot)
            except ScheduleError:
                ranges_by_slot = None
            if ranges_by_slot is not None:
                persona_ranges = [
                    {"label": persona_labels.get(slot, slot), "ranges": ranges}
                    for slot, ranges in ranges_by_slot.items()
                ]

        context["widget"].update({
            "hours": hours,
            "personas": personas,
            "current_hour": current_hour,
            "current_hour_label": hour_label(current_hour),
            "on_duty_label": persona_labels.get(on_duty_slot, "(unassigned)") if on_duty_slot else "(unassigned)",
            "schedule_error": schedule_error,
            "persona_ranges": persona_ranges,
        })
        return context


class HourlyScheduleField(forms.Field):
    """Server-side authoritative validation lives here, not in the
    widget or in JS -- see the field's own clean()/validate() below.
    A malformed POST (tampered request, JS-disabled partial submit,
    direct API misuse) is refused exactly the same way an operator's
    own mistaken click sequence would be."""

    widget = HourlyScheduleWidget

    def bound_data(self, data, initial):
        """Called by BoundField.value() only for a BOUND form -- wraps
        the widget's own value_from_datadict() result so get_context()
        can tell it apart from a raw (possibly malformed) database
        value unambiguously. See _BoundHours' own docstring."""
        return _BoundHours(data)

    def to_python(self, value):
        if not isinstance(value, dict):
            return value
        if set(value) != set(range(24)):
            missing = sorted(set(range(24)) - set(value))
            raise forms.ValidationError(
                f"Every hour must have an assigned announcer -- missing: "
                f"{', '.join(hour_label(h) for h in missing)}."
            )
        try:
            return compress_from_hours(value)
        except ScheduleError as exc:
            raise forms.ValidationError(str(exc)) from exc

    def validate(self, value):
        super().validate(value)
        if not value:
            return
        try:
            hour_to_slot = expand_to_hours(value)
        except ScheduleError as exc:
            raise forms.ValidationError(str(exc)) from exc

        referenced_slots = sorted(set(hour_to_slot.values()))
        problems = [
            check.problem
            for check in check_persona_slots(referenced_slots)
            if check.problem
        ]
        if problems:
            raise forms.ValidationError(problems)


class WeatherConfigForm(forms.ModelForm):
    """r0053 (Pass D): also shadows alert_sound_interval_seconds with an
    operator-facing minutes field -- see alert_sound_interval_minutes/
    save() below. The stored seconds column and weather_ingest's own
    runtime contract (WeatherConfig.alert_sound_interval_seconds via
    dump_weather_config) are completely unchanged; only this form's
    presentation converts."""

    voice_schedule = HourlyScheduleField(
        label="Announcer schedule",
        help_text="Click an hour to assign the announcer on duty. Times shown are station-local.",
    )
    alert_sound_interval_minutes = forms.DecimalField(
        label="Repeat alert beep every",
        min_value=Decimal("0.01"),
        max_digits=8,
        decimal_places=2,
        help_text="Minutes. Stored internally as alert_sound_interval_seconds -- "
                   "the value weather_ingest actually reads is unchanged, this is "
                   "presentation only. An existing value that isn't an exact whole "
                   "minute (e.g. 90 seconds) round-trips exactly as 1.50 here.",
    )
    # r0053 amendment: shadows alert_sound_trigger_events (a JSONField)
    # with a plain one-event-name-per-line editor -- friendlier than
    # typing raw JSON, and avoids the "must be valid JSON syntax"
    # failure mode for an operator who just wants to add one event
    # name. Structured storage (a JSON list) is unchanged; only this
    # form's presentation is a text box.
    alert_sound_trigger_events_text = forms.CharField(
        label="Alert types that trigger the repeating beep",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4, "cols": 40}),
        help_text="One NWS event name per line, e.g. \"Tornado Warning\" -- select/configure "
                   "the NWS event names that activate the repeating Alert Beep FX Cart. This "
                   "setting does not control generated spoken Weather or AMBER alert "
                   "statements. Matching is case-insensitive substring, same as before this "
                   "was configurable. Leave blank so no NWS event triggers the beep.",
    )

    class Meta:
        model = WeatherConfig
        exclude = ["alert_sound_interval_seconds", "alert_sound_trigger_events"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound and self.instance is not None and self.instance.pk:
            seconds = self.instance.alert_sound_interval_seconds
            minutes = (Decimal(seconds) / Decimal(60)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            self.fields["alert_sound_interval_minutes"].initial = minutes
            # normalize_..., NOT `value or defaults` -- that would wrongly
            # turn a deliberately-saved empty list into the defaults too
            # (both are falsy). Only a genuine stored NULL becomes the
            # four legacy defaults; a stored [] stays an empty textarea.
            events = normalize_alert_sound_trigger_events(self.instance.alert_sound_trigger_events)
            self.fields["alert_sound_trigger_events_text"].initial = "\n".join(events)

    def clean_alert_sound_trigger_events_text(self):
        raw = self.cleaned_data.get("alert_sound_trigger_events_text", "")
        # Blank lines dropped; order and exact wording of each
        # non-blank line preserved verbatim (arbitrary event-name
        # strings, not a fixed choice list) -- an entirely blank
        # field is the valid, deliberate "no event triggers the
        # beep" empty list, not an error.
        return [line.strip() for line in raw.splitlines() if line.strip()]

    def save(self, commit=True):
        instance = super().save(commit=False)
        minutes = self.cleaned_data["alert_sound_interval_minutes"]
        seconds = int((minutes * 60).to_integral_value(rounding=ROUND_HALF_UP))
        instance.alert_sound_interval_seconds = seconds
        instance.alert_sound_trigger_events = self.cleaned_data["alert_sound_trigger_events_text"]
        if commit:
            instance.save()
            self.save_m2m()
        return instance
