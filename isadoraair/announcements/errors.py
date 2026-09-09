"""Stable errors exposed by the generated-announcement renderer."""


class AnnouncementRenderError(RuntimeError):
    """A generated artifact could not be completed safely."""

    def __init__(self, stage: str, message: str):
        self.stage = stage
        super().__init__(f"{stage}: {message}")
