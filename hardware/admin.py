import json
import re
import subprocess
from pathlib import Path

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse

from .devices import list_input_devices, list_mixer_controls, list_output_devices
from .models import AudioInput, AudioOutput, AudioPipeline, DuckingConfig, RemoteDJAudioInput
from monitoring.models import emit_event

# Must match engine.py's STUDIO_MONITOR_NAME.
STUDIO_MONITOR_NAME = "Studio Monitor"


def _alsa_store(request=None):
    """Snapshot the kernel's live ALSA mixer state to
    /var/lib/alsa/asound.state so a reboot restores it via the
    stock `alsa-restore.service` (static, pulled in by sound.target
    on any box with sound cards). Without this call, admin edits go
    to the LIVE kernel state via `amixer sset` but are lost on any
    hard power cut -- the persistent snapshot only updates on
    `alsa-restore.service` ExecStop, i.e. clean shutdown. Runs once
    per save (snapshot covers ALL cards, so per-control calls would
    be wasted). Failure is logged but non-fatal -- the live state is
    already correct; only the reboot survivability is at risk."""
    try:
        subprocess.run(
            ["sudo", "-n", "/usr/sbin/alsactl", "store"],
            capture_output=True, text=True, timeout=10, check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        stderr = getattr(exc, "stderr", "") or str(exc)
        if request is not None:
            messages.warning(
                request,
                f"alsactl store failed -- live changes applied, but a reboot will "
                f"revert them. See /monitoring/ Recent Events for detail. ({stderr[:120]})",
            )
        emit_event(
            category="hardware",
            level="warning",
            title="alsactl store failed after mixer save (reboot persistence at risk)",
            detail={"error": stderr[:500]},
            dedupe_key="hardware|alsactl_store_failed",
        )

_CARD_RE = re.compile(r"plughw:(\d+),")


def _parse_card_number(device):
    m = _CARD_RE.match(device or "")
    return int(m.group(1)) if m else None


def _mixer_controls_for(obj):
    if not obj or not obj.device:
        return []
    card = _parse_card_number(obj.device)
    if card is None:
        return []
    return list_mixer_controls(card)


# ALSA controls whose name matches this pattern are OUTPUT-side (playback,
# monitoring, digital-out). Everything else on the same card is treated as
# INPUT-side (capture/mic/line-in). Purpose: card 2 exposes BOTH the mic
# and the studio-monitor via one physical device -- without this split
# both admin pages would show every control on the card, and it wasn't
# clear which page was the right place to change e.g. Master. Fall-through
# to INPUT for anything unmatched preserves the pre-split behavior for
# controls with exotic names on cards we haven't seen yet.
_OUTPUT_CONTROL_RE = re.compile(
    r"^(Master|Headphone|Speaker|PCM|Front|Surround|Center|LFE|Side|Digital|IEC958|Beep|Line Boost)\b",
    re.IGNORECASE,
)


def _mixer_role(control):
    return "output" if _OUTPUT_CONTROL_RE.match(control["label"]) else "input"


def _mixer_controls_for_role(obj, role):
    """Card mixer controls filtered to one role (input/output). Preserves
    the source ordering from `amixer controls` so field indices stay
    stable across renders."""
    return [c for c in _mixer_controls_for(obj) if _mixer_role(c) == role]


def _inject_mixer_form_fields(form, controls, existing):
    """Add one form field per control (ChoiceField/BooleanField/IntegerField
    depending on control kind), using `existing` as the initial value when
    present, falling back to the live amixer value. Returns a mapping
    field_name -> control dict for the save-side path to reuse."""
    control_map = {}
    for idx, control in enumerate(controls):
        field_name = f"mixer_{idx}"
        control_map[field_name] = control
        current = existing.get(control["control_id"])
        if control["has_enum"]:
            choices = [(item, item) for item in control["enum_items"]]
            initial = current if current in control["enum_items"] else control["enum_value"]
            form.base_fields[field_name] = forms.ChoiceField(
                label=control["label"], choices=choices, initial=initial, required=False,
            )
        elif control["has_switch"] and not control["has_volume"]:
            initial = current if current is not None else control["on"]
            form.base_fields[field_name] = forms.BooleanField(
                label=control["label"], initial=bool(initial), required=False,
            )
        elif control["has_volume"]:
            initial = current if current is not None else control["value_pct"]
            form.base_fields[field_name] = forms.IntegerField(
                label=f"{control['label']} (%)", initial=initial, required=False,
                min_value=0, max_value=100,
            )
    return control_map


def _apply_mixer_form_changes(request, obj, form, card):
    """Apply any changed mixer fields via `amixer sset`, persist the new
    per-control values back to obj.mixer_control_values, and snapshot the
    live ALSA state so a reboot survives. No-op if no controls changed."""
    control_map = getattr(form, "_mixer_control_map", {})
    if not control_map or card is None:
        return
    values = dict(obj.mixer_control_values or {})
    changed = False
    for field_name, control in control_map.items():
        if field_name not in form.cleaned_data:
            continue
        new_value = form.cleaned_data[field_name]
        control_id = control["control_id"]
        if values.get(control_id) == new_value:
            continue
        values[control_id] = new_value
        changed = True
        if control["has_enum"]:
            amixer_value = new_value
        elif control["has_switch"] and not control["has_volume"]:
            amixer_value = "on" if new_value else "off"
        else:
            amixer_value = f"{new_value}%"
        try:
            subprocess.run(
                ["amixer", "-c", str(card), "sset", control_id, str(amixer_value)],
                capture_output=True, text=True, timeout=5, check=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
            messages.error(request, f"Failed to apply '{control['label']}': {exc}")
    if changed:
        obj.mixer_control_values = values
        obj.save(update_fields=["mixer_control_values"])
        _alsa_store(request=request)


class AudioPipelineForm(forms.ModelForm):
    """Combined form: edits AudioPipeline's own fields plus DuckingConfig
    and RemoteDJAudioInput on one page. Those two are tiny singletons
    (1-2 fields each) whose separate admin listings were more UI noise
    than clarity -- consolidating them here means "all pipeline-wide
    audio config in one place". The extra fields load from and save
    back to their own singleton rows.

    Restart discipline (see save_model): only sample_rate and
    program_gain_db actually require an engine restart -- both are
    baked into pipeline topology at build time. Ducking is re-read by
    the engine on every PTT toggle (per DuckingConfig's docstring);
    Remote DJ gain is re-read at session start (per
    RemoteDJAudioInput's docstring). Editing only those on this page
    should not disrupt on-air audio."""

    ducking_enabled = forms.BooleanField(
        required=False,
        label="Ducking enabled",
        help_text="Off = program audio is unaffected by mic PTT. On = program "
                  "audio is attenuated to the level below whenever ANY mic (studio "
                  "or remote) is live.",
    )
    duck_level_db = forms.FloatField(
        label="Duck level (dB)",
        help_text="How much to attenuate deck/program audio while a mic is live, "
                  "in dB. Negative = quieter (-12 = roughly quarter volume). Ramped "
                  "over ~500ms on PTT toggle. Takes effect on the NEXT toggle.",
    )
    remote_dj_gain_db = forms.FloatField(
        label="Remote DJ mic gain (dB)",
        help_text="Software gain applied to the incoming remote-DJ mic before it "
                  "joins the on-air mix. Default +6 dB compensates for browser "
                  "WebRTC's typical lower output level. Takes effect on the NEXT "
                  "Remote DJ connect.",
    )

    class Meta:
        model = AudioPipeline
        fields = ["sample_rate", "program_gain_db", "vu_meter_min_db"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        ducking = DuckingConfig.load()
        self.fields["ducking_enabled"].initial = ducking.enabled
        self.fields["duck_level_db"].initial = ducking.duck_level_db
        self.fields["remote_dj_gain_db"].initial = RemoteDJAudioInput.load().gain_db


@admin.register(AudioPipeline)
class AudioPipelineAdmin(admin.ModelAdmin):
    form = AudioPipelineForm
    fieldsets = (
        ("Pipeline", {
            "fields": ("sample_rate", "program_gain_db", "vu_meter_min_db"),
            "description": (
                "sample_rate and program_gain_db are baked into the pipeline at "
                "build time -- changing either triggers an engine restart on "
                "save (brief on-air silence). vu_meter_min_db is client-side "
                "only and takes effect on next dashboard reload; no restart."
            ),
        }),
        ("Ducking (mic on-air)", {
            "fields": ("ducking_enabled", "duck_level_db"),
            "description": "Read fresh by the engine on each PTT toggle -- no restart needed.",
        }),
        ("Remote DJ mic input", {
            "fields": ("remote_dj_gain_db",),
            "description": "Read at session start -- takes effect on the next Remote DJ connect. No restart needed.",
        }),
    )

    class Media:
        js = ["hardware/js/audio_pipeline_confirm.js"]

    def has_add_permission(self, request):
        return not AudioPipeline.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        obj = AudioPipeline.load()
        return HttpResponseRedirect(
            reverse("admin:hardware_audiopipeline_change", args=[obj.pk])
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        # Fold the extra fields onto their own singletons.
        ducking = DuckingConfig.load()
        ducking.enabled = form.cleaned_data["ducking_enabled"]
        ducking.duck_level_db = form.cleaned_data["duck_level_db"]
        ducking.save()
        rdj = RemoteDJAudioInput.load()
        rdj.gain_db = form.cleaned_data["remote_dj_gain_db"]
        rdj.save()

        # Only restart the engine if a field that actually requires it
        # changed -- Ducking is read live per PTT, Remote DJ gain per
        # session start, neither needs a rebuild.
        restart_fields = {"sample_rate", "program_gain_db"}
        if restart_fields & set(form.changed_data):
            subprocess.Popen(["sudo", "systemctl", "restart", "isadoraair-engine"])


class _DeviceFieldAdmin(admin.ModelAdmin):
    """Swap the 'device' CharField for a Select populated from the
    runtime hardware enumeration. Subclasses set `_enumerate` to the
    aplay-/arecord-backed discovery function."""
    _enumerate = None

    list_display = ["name", "device", "sort_order"]
    list_editable = ["sort_order"]
    ordering = ["sort_order", "name"]

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if "device" in form.base_fields and callable(self._enumerate):
            choices = list(self._enumerate())
            # preserve current value even if hardware changed since last save
            if obj and obj.device and not any(obj.device == c[0] for c in choices):
                choices = [(obj.device, f"{obj.device} (UNAVAILABLE)")] + choices
            choices = [("", "— not configured —")] + choices
            form.base_fields["device"].widget = forms.Select(choices=choices)
        return form


@admin.register(AudioOutput)
class AudioOutputAdmin(_DeviceFieldAdmin):
    _enumerate = staticmethod(list_output_devices)

    def get_fieldsets(self, request, obj=None):
        fieldsets = [(None, {"fields": ["name", "device", "sort_order"]})]
        if obj and obj.name == STUDIO_MONITOR_NAME:
            fieldsets.append(("AGC (Studio Monitor Leveling)", {
                "fields": ["agc_enabled", "agc_ratio", "agc_threshold", "agc_soft_knee", "agc_makeup_gain_db"],
                "description": "Interim leveling for this output only "
                                "(compressor + makeup gain + safety limiter). "
                                "Not the transmitter feed — StereoTool will "
                                "handle that separately. Changes apply live "
                                "on Save, no engine restart needed.",
            }))
        controls = _mixer_controls_for_role(obj, "output")
        if controls:
            fieldsets.append(("Hardware Mixer Controls (Output)", {
                "fields": [f"mixer_{i}" for i in range(len(controls))],
                "description": "Output-side ALSA mixer controls for this "
                                "output's card (Master/Headphone/Speaker/etc). "
                                "The card's input-side controls (Mic, Capture, "
                                "Input Source, …) live on the matching Audio "
                                "Input page. Changes apply immediately to the "
                                "hardware on Save.",
            }))
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        # Same synthetic-field guard as AudioInputAdmin: keep our
        # mixer_N field names out of modelform_factory's `fields`
        # validation (it only accepts real model fields).
        fields = kwargs.pop("fields", None)
        if fields is None:
            from django.contrib.admin.utils import flatten_fieldsets
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        kwargs["fields"] = [f for f in fields if not f.startswith("mixer_")]
        form = super().get_form(request, obj, **kwargs)
        controls = _mixer_controls_for_role(obj, "output")
        existing = (obj.mixer_control_values if obj else {}) or {}
        form._mixer_control_map = _inject_mixer_form_fields(form, controls, existing)
        return form

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if obj.name == STUDIO_MONITOR_NAME:
            Path("/run/isadoraair/engine_cmd.json").write_text(
                json.dumps({"command": "reload_agc_config"}), encoding="utf-8"
            )
        _apply_mixer_form_changes(request, obj, form, _parse_card_number(obj.device))


@admin.register(AudioInput)
class AudioInputAdmin(_DeviceFieldAdmin):
    _enumerate = staticmethod(list_input_devices)

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            (None, {"fields": ["name", "device", "sort_order"]}),
            ("Software Gain", {
                "fields": ["gain_db"],
                "description": "Applied in the on-air mix, independent of the "
                                "hardware controls below.",
            }),
        ]
        controls = _mixer_controls_for_role(obj, "input")
        if controls:
            fieldsets.append(("Hardware Mixer Controls (Input)", {
                "fields": [f"mixer_{i}" for i in range(len(controls))],
                "description": "Input-side ALSA mixer controls for this "
                                "input's card (Mic/Capture/Input Source/etc). "
                                "The card's output-side controls (Master, "
                                "Headphone, Speaker, IEC958, …) live on the "
                                "matching Audio Output page. Changes apply "
                                "immediately to the hardware on Save.",
            }))
        return fieldsets

    def get_form(self, request, obj=None, **kwargs):
        # _changeform_view calls get_form(fields=flatten_fieldsets(...)).
        # Django's OWN base get_form() ALSO internally derives `fields`
        # from get_fieldsets() whenever it isn't explicitly passed at
        # all (confirmed live -- calling get_form() directly with no
        # `fields` kwarg hit the exact same FieldError as the admin
        # view path). Either way, whatever `fields` ends up being,
        # our own synthetic "mixer_N" names from get_fieldsets() above
        # must never reach modelform_factory (it validates `fields`
        # against real model fields) -- so always compute and filter it
        # explicitly here rather than only when a caller happens to
        # pass one in.
        fields = kwargs.pop("fields", None)
        if fields is None:
            from django.contrib.admin.utils import flatten_fieldsets
            fields = flatten_fieldsets(self.get_fieldsets(request, obj))
        kwargs["fields"] = [f for f in fields if not f.startswith("mixer_")]
        form = super().get_form(request, obj, **kwargs)
        controls = _mixer_controls_for_role(obj, "input")
        existing = (obj.mixer_control_values if obj else {}) or {}
        form._mixer_control_map = _inject_mixer_form_fields(form, controls, existing)
        return form

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _apply_mixer_form_changes(request, obj, form, _parse_card_number(obj.device))


# DuckingConfig and RemoteDJAudioInput used to be their own admin pages;
# they're now edited as folded-in sections on AudioPipeline's page (see
# AudioPipelineForm above), so their standalone registrations are gone.
# The models themselves stay separate -- the engine reads them at
# different lifecycles (per-PTT-toggle for ducking, per-session-start
# for remote DJ gain), which is worth keeping distinct at the code
# level even if the admin UI presents them together.
