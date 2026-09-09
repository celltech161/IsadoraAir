"""Explicit request and result types for announcement artifact modes."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from library.models import Artist, Track


class AnnouncementMode(StrEnum):
    PREVIEW = "preview"
    SPEECH_SPLICE = "speech_splice"
    ROTATION_ASSET = "rotation_asset"


@dataclass(frozen=True, slots=True)
class AnnouncementTrackMetadata:
    """Feature-owned library identity supplied to a Track-producing mode."""

    title: str
    artist: Artist
    category_code: str
    ready2air: bool


@dataclass(frozen=True, slots=True)
class PreviewAnnouncement:
    """A caller-owned WAV preview which never enters the library."""

    mode: ClassVar[AnnouncementMode] = AnnouncementMode.PREVIEW
    text: str
    logical_voice: str
    destination: Path
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class SpeechSpliceAnnouncement:
    """A short FLAC library clip with non-negotiable speech cue points."""

    mode: ClassVar[AnnouncementMode] = AnnouncementMode.SPEECH_SPLICE
    text: str
    logical_voice: str
    destination: Path
    timeout_seconds: float
    metadata: AnnouncementTrackMetadata


@dataclass(frozen=True, slots=True)
class RotationAssetAnnouncement:
    """A stable FLAC Track whose cue points are owned by normal analysis."""

    mode: ClassVar[AnnouncementMode] = AnnouncementMode.ROTATION_ASSET
    text: str
    logical_voice: str
    destination: Path
    timeout_seconds: float
    metadata: AnnouncementTrackMetadata


AnnouncementSpec = PreviewAnnouncement | SpeechSpliceAnnouncement | RotationAssetAnnouncement


@dataclass(frozen=True, slots=True)
class AnnouncementRenderResult:
    path: Path
    duration_seconds: float
    track: Track | None
    mode: AnnouncementMode
    analysis_attempted: bool = False
    analysis_succeeded: bool = False
    analysis_error: str | None = None
