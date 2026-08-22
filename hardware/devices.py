"""Runtime discovery of ALSA audio devices via aplay/arecord.

Returns lists of (value, label) tuples suitable for a Django ChoiceField.
Value is the ALSA device path consumed by GStreamer's alsasink/alsasrc
(e.g. "plughw:2,0"); label is a human-friendly description.
"""

from dataclasses import dataclass
from pathlib import Path
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


_ASOUND_CARDS_PATH = Path("/proc/asound/cards")
_ASOUND_CARD_RE = re.compile(
    r"^\s*(\d+)\s+\[([^]]+)\]\s*:\s*(.+?)\s+-\s+(.+?)\s*$"
)
_PCM_CARD_RE = re.compile(
    r"^card\s+\d+:\s+(\S+)\s+\[.+?\],\s+device\s+(\d+):",
    re.IGNORECASE,
)
_USB_LOCATION_RE = re.compile(r"\bat\s+(usb-[^,\s]+)", re.IGNORECASE)


@dataclass(frozen=True)
class AlsaCardIdentity:
    """One stable ALSA card identity discovered from the live host.

    ``card_id`` is the only value persisted by the admin. The numeric index
    and all descriptive fields are display-time metadata; in particular, a
    USB location helps distinguish otherwise identical interfaces without
    changing the existing ``alsa_card_id`` identity semantics.
    """

    card_index: int
    card_id: str
    driver: str
    name: str
    description: str
    usb_location: str | None
    capabilities: frozenset[str]

    @property
    def label(self):
        description = self.description
        if " at " in description:
            description = description.split(" at ", 1)[0].strip()
        parts = [self.card_id, self.name]
        if description:
            parts.append(description)
        if self.usb_location:
            parts.append(self.usb_location)
        return " — ".join(parts)


def parse_alsa_cards(text):
    """Parse ``/proc/asound/cards`` into structured identity metadata.

    Malformed entries are skipped individually so one unfamiliar driver line
    cannot make the Hardware Config admin unavailable.
    """

    cards = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = _ASOUND_CARD_RE.match(line)
        if not match:
            continue
        card_index, card_id, driver, name = match.groups()
        description = ""
        if index + 1 < len(lines) and not _ASOUND_CARD_RE.match(lines[index + 1]):
            description = lines[index + 1].strip()
        usb_match = _USB_LOCATION_RE.search(description)
        cards.append(
            AlsaCardIdentity(
                card_index=int(card_index),
                card_id=card_id.strip(),
                driver=driver.strip(),
                name=name.strip(),
                description=description,
                usb_location=usb_match.group(1) if usb_match else None,
                capabilities=frozenset(),
            )
        )
    return cards


def _list_pcm_card_ids(command):
    """Return stable IDs with a device-0 PCM for ``command``.

    Production's ``alsa_card_id`` resolver intentionally constructs
    ``plughw:CARD=<id>,DEV=0``. A card that exposes the requested direction
    only on device 1+ is therefore not a usable stable-identity target even
    though aplay/arecord lists it.
    """

    try:
        result = subprocess.run(
            [command, "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return set()

    return {
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := _PCM_CARD_RE.match(line)) and match.group(2) == "0"
    }


def list_alsa_card_identities(direction):
    """Discover stable ALSA card IDs usable in one audio direction.

    ``direction`` is ``"playback"`` or ``"capture"``. Cards are read from
    ``/proc/asound/cards`` for descriptive metadata, then cross-referenced by
    short card ID (never numeric index) with device 0 from ``aplay -l`` or
    ``arecord -l``. Requiring device 0 mirrors production's hardcoded
    ``plughw:CARD=<id>,DEV=0`` resolver contract. Any discovery failure safely
    returns no live choices; the admin separately preserves an object's
    configured identity as unavailable.
    """

    command_by_direction = {"playback": "aplay", "capture": "arecord"}
    if direction not in command_by_direction:
        raise ValueError(f"Unsupported ALSA direction: {direction!r}")

    try:
        cards_text = _ASOUND_CARDS_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    capable_ids = _list_pcm_card_ids(command_by_direction[direction])
    capability = frozenset({direction})
    return [
        AlsaCardIdentity(
            card_index=card.card_index,
            card_id=card.card_id,
            driver=card.driver,
            name=card.name,
            description=card.description,
            usb_location=card.usb_location,
            capabilities=capability,
        )
        for card in parse_alsa_cards(cards_text)
        if card.card_id in capable_ids
    ]


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
