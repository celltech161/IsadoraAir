"""The explicit set of recorder consumers (2.22B B1).

A consumer registers ONE adapter instance under a fixed key from its own
AppConfig.ready(). There is no lookup by model name, import path or content
type: the browser can only name a registered key, and an unknown key is a 404.
"""
from __future__ import annotations

import re

from .contracts import RecordingAdapter, SubjectNotFound

KEY_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
_ADAPTERS: dict[str, RecordingAdapter] = {}


def register(adapter: RecordingAdapter) -> RecordingAdapter:
    if not isinstance(adapter, RecordingAdapter):
        raise TypeError("recorder adapters must subclass RecordingAdapter")
    if not KEY_RE.fullmatch(adapter.key or ""):
        raise ValueError(f"invalid recorder adapter key {adapter.key!r}")
    existing = _ADAPTERS.get(adapter.key)
    if existing is not None and type(existing) is not type(adapter):
        raise ValueError(f"recorder adapter key {adapter.key!r} is already registered")
    _ADAPTERS[adapter.key] = adapter
    return adapter


def get(key: str) -> RecordingAdapter:
    adapter = _ADAPTERS.get(key) if isinstance(key, str) else None
    if adapter is None:
        raise SubjectNotFound("unknown_workspace", "no such iPortal workspace")
    return adapter


def all_adapters() -> list[RecordingAdapter]:
    return [_ADAPTERS[key] for key in sorted(_ADAPTERS)]
