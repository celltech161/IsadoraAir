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
