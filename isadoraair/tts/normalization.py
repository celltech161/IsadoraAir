"""Speech-text normalization preserved from the production Kokoro helper."""

from __future__ import annotations

import re


# Keep these expressions and their application order aligned with the
# production helper captured in Runtime Foundation A. They describe proven
# spoken-output behavior, not a general-purpose text-normalization policy.
_DECIMAL_POINT_RE = re.compile(r"(?<=\d)\.(?=\d)")
_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\(\s*(\d{3})\s*\)|(\d{3}))"
    r"[\s.\-]?\s*"
    r"(\d{3})"
    r"[\s.\-]"
    r"(\d{4})"
    r"(?!\d)"
)
_EMERGENCY_RE = re.compile(r"\b911\b")
_HASHTAG_RE = re.compile(r"#\w+")

_DIGIT_WORDS = {
    "0": "zero",
    "1": "one",
    "2": "two",
    "3": "three",
    "4": "four",
    "5": "five",
    "6": "six",
    "7": "seven",
    "8": "eight",
    "9": "nine",
}


def _digits_worded(value: str) -> str:
    return " ".join(_DIGIT_WORDS.get(character, character) for character in value)


def _phone_replacement(match: re.Match[str]) -> str:
    area = match.group(1) or match.group(2)
    return (
        f"{_digits_worded(area)}, "
        f"{_digits_worded(match.group(3))}, "
        f"{_digits_worded(match.group(4))}"
    )


def preprocess_text(text: str) -> str:
    """Return exactly the text normalization used by production Kokoro.

    Processing order is significant: hashtags are removed before telephone
    numbers, telephone numbers before standalone 911, then decimal points are
    spoken. Finally, all whitespace is collapsed and edge whitespace removed.
    """

    text = _HASHTAG_RE.sub("", text)
    text = _PHONE_RE.sub(_phone_replacement, text)
    text = _EMERGENCY_RE.sub("nine one one", text)
    text = _DECIMAL_POINT_RE.sub(" point ", text)
    return re.sub(r"\s+", " ", text).strip()
