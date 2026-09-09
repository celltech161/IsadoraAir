"""Reusable generated-announcement artifact rendering."""

from .errors import AnnouncementRenderError
from .renderer import AnnouncementRenderer, render_announcement
from .types import (
    AnnouncementMode,
    AnnouncementRenderResult,
    AnnouncementTrackMetadata,
    PreviewAnnouncement,
    RotationAssetAnnouncement,
    SpeechSpliceAnnouncement,
)

__all__ = [
    "AnnouncementMode",
    "AnnouncementRenderError",
    "AnnouncementRenderResult",
    "AnnouncementRenderer",
    "AnnouncementTrackMetadata",
    "PreviewAnnouncement",
    "RotationAssetAnnouncement",
    "SpeechSpliceAnnouncement",
    "render_announcement",
]
