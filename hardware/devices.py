"""Runtime discovery of ALSA audio devices via aplay/arecord.

Returns lists of (value, label) tuples suitable for a Django ChoiceField.
Value is the ALSA device path consumed by GStreamer's alsasink/alsasrc
(e.g. "plughw:2,0"); label is a human-friendly description.
"""

import re
import subprocess

_LINE_RE = re.compile(
    r"^card (\d+): .+? \[(.+?)\], device (\d+): .+? \[(.+?)\]"
)


def _enumerate(cmd):
    try:
        result = subprocess.run(
            [cmd, "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    devices = []
    for line in result.stdout.splitlines():
        m = _LINE_RE.match(line)
        if not m:
            continue
        card, card_name, device, device_name = m.groups()
        value = f"plughw:{card},{device}"
        label = f"{value} — {card_name} / {device_name}"
        devices.append((value, label))
    return devices


def list_output_devices():
    return _enumerate("aplay")


def list_input_devices():
    return _enumerate("arecord")


_SCONTROL_RE = re.compile(r"^Simple mixer control '(.+?)',(\d+)$")
_LIMITS_RE = re.compile(r"Limits:.*?(\d+)\s*-\s*(\d+)")
_PCT_RE = re.compile(r"\[(\d+)%\]")
_SWITCH_RE = re.compile(r"\[(on|off)\]")
_ENUM_ITEMS_RE = re.compile(r"Items:\s*(.+)")
_ENUM_ITEM_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")
_ENUM_CURRENT_RE = re.compile(r"Item0:\s*'((?:[^'\\]|\\.)*)'")


def list_mixer_controls(card):
    """Enumerate real ALSA simple-mixer controls for `card` (the int
    embedded in an AudioInput.device string like 'plughw:2,0'). Verified
    live against this box's card 2: 'Capture' (cvolume+cswitch, range
    0-80), 'Mic Boost' (volume-only, range 0-4 -- NOT a switch, contrary
    to an earlier assumption; corrected here after checking the real
    `amixer sget` output), 'Input Source'/'Auto-Mute Mode' (cenum/enum,
    a named-item list + current selection -- a third control shape not
    originally accounted for, added after finding two real examples of
    it on this exact card).

    Real gotcha caught here: `amixer scontrols` can list the SAME name
    at multiple indices (e.g. 'Headphone',0 and 'Headphone',1 on this
    box) that are genuinely DISTINCT controls, not duplicates --
    confirmed live (`amixer sget 'Headphone',1` returns independent
    state from index 0). Every control is addressed and returned as its
    full "name,index" identifier throughout, never the bare name alone,
    so these don't collide."""
    try:
        result = subprocess.run(
            ["amixer", "-c", str(card), "scontrols"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    ids = []
    for line in result.stdout.splitlines():
        m = _SCONTROL_RE.match(line)
        if m:
            ids.append((m.group(1), m.group(2)))  # (name, index)
    return [c for c in (_get_mixer_control(card, name, index) for name, index in ids) if c]


def _get_mixer_control(card, name, index="0"):
    control_id = f"{name},{index}"
    try:
        result = subprocess.run(
            ["amixer", "-c", str(card), "sget", control_id],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    text = result.stdout
    if not text.strip():
        return None

    # Display label only disambiguates with an index suffix when this
    # card actually has more than one control sharing the same name --
    # keeps the common case (index 0, no sibling) looking plain.
    label = name if index == "0" else f"{name} ({index})"

    if "cenum" in text or ("enum" in text and "Items:" in text):
        items_match = _ENUM_ITEMS_RE.search(text)
        items = _ENUM_ITEM_RE.findall(items_match.group(1)) if items_match else []
        current_match = _ENUM_CURRENT_RE.search(text)
        return {
            "control_id": control_id, "name": name, "index": index, "label": label,
            "has_volume": False, "has_switch": False, "has_enum": True,
            "min": None, "max": None, "value_pct": None, "on": None,
            "enum_items": items,
            "enum_value": current_match.group(1) if current_match else None,
        }

    limits = _LIMITS_RE.search(text)
    pct = _PCT_RE.search(text)
    switch = _SWITCH_RE.search(text)
    return {
        "control_id": control_id, "name": name, "index": index, "label": label,
        "has_volume": "cvolume" in text or "volume" in text,
        "has_switch": "cswitch" in text or ("switch" in text and switch is not None),
        "has_enum": False,
        "min": int(limits.group(1)) if limits else None,
        "max": int(limits.group(2)) if limits else None,
        "value_pct": int(pct.group(1)) if pct else None,
        "on": (switch.group(1) == "on") if switch else None,
        "enum_items": [], "enum_value": None,
    }
