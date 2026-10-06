"""The reusable iPortal recorder/editor contract (2.22B B1).

The recorder/editor is a MEDIA WORKSTATION, not a workflow. It knows how to
capture, edit, validate and save immutable ProductionMedia; it knows nothing
about VoiceTracks, logs, news or messages. A consuming domain plugs in through
an explicit ``RecordingAdapter`` registered under a fixed key (see registry):

* the domain resolves a *subject* from a few validated request parameters
  (e.g. ``track`` + ``position``) -- never a model name, a Python path or a
  generic foreign key;
* the domain supplies a ``RecordingContext`` -- an in-memory description the
  recorder renders, never persisted: there is no generic "recording job";
* the domain authorizes every operation server-side and decides what
  "commit" means for it (for an evergreen VoiceTrack: atomically repoint the
  binding through the locked binding service).

The recorder's only durable output is a validated, immutable ProductionMedia
created through production.services.intake; the domain's only durable output
is its own reference to it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from production.policy import MediaPolicy

# Operations a context may allow. The recorder renders only what is allowed;
# the server authorizes each one again regardless.
OPERATIONS = ("open", "record", "import", "edit", "save", "remove", "export")


class RecorderError(Exception):
    """A refusal the recorder reports to the browser as JSON."""

    status = 400

    def __init__(self, code: str, message: str, *, status: int | None = None, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        if status is not None:
            self.status = status


class SubjectNotFound(RecorderError):
    status = 404


class Forbidden(RecorderError):
    status = 403


class Conflict(RecorderError):
    """The domain state changed since the context was issued (stale editor)."""
    status = 409


@dataclass(frozen=True)
class CurrentAudio:
    """What the subject currently has (if anything), for display and editing."""

    origin: str                      # "production_media" | "legacy"
    label: str                       # e.g. "Current on-air take"
    duration_seconds: float | None
    media_id: str | None = None      # only for origin == production_media
    preview_url: str = ""            # an authorization-checked recorder URL; never a path


@dataclass(frozen=True)
class RecordingContext:
    adapter: str
    subject: dict                    # adapter-validated parameters, echoed back on every call
    title: str
    purpose: str
    allowed_operations: tuple[str, ...]
    max_duration_seconds: float
    max_bytes: int
    revision: str                    # opaque optimistic-concurrency token for the domain state
    current: CurrentAudio | None = None
    display: tuple[tuple[str, str], ...] = ()
    return_url: str = ""
    blocked_reason: str = ""         # why recording/saving is unavailable right now, if it is
    air_label: str = "On air"        # what the domain calls the committed state

    def as_json(self) -> dict:
        data = asdict(self)
        data["allowed_operations"] = list(self.allowed_operations)
        data["display"] = [list(row) for row in self.display]
        return data


@dataclass
class RecordingAdapter:
    """Base class for a consuming domain. Subclasses override the hooks."""

    key: str = ""
    label: str = ""
    description: str = ""
    entry_url: str = ""
    extra: dict = field(default_factory=dict)

    # -- authorization (server-side, authoritative) ------------------------
    def authorize(self, user, operation: str) -> bool:
        raise NotImplementedError

    # -- the subject ---------------------------------------------------------
    def resolve(self, params) -> object:
        """Resolve the subject from request parameters (a QueryDict or dict).
        Raise SubjectNotFound / RecorderError; never trust anything else."""
        raise NotImplementedError

    def subject_params(self, subject) -> dict:
        raise NotImplementedError

    def context(self, request, subject) -> RecordingContext:
        raise NotImplementedError

    def media_policy(self, subject) -> MediaPolicy:
        raise NotImplementedError

    # -- media the recorder may show or derive from --------------------------
    def can_access_media(self, user, subject, media) -> bool:
        raise NotImplementedError

    def source(self, subject):
        """("production_media", ProductionMedia) | ("legacy", absolute path the
        DOMAIN owns) | None -- the audio an edit starts from."""
        return None

    # -- the domain's own lifecycle ------------------------------------------
    def commit(self, user, subject, media, expected_revision) -> None:
        raise NotImplementedError

    def remove(self, user, subject, expected_revision) -> None:
        raise RecorderError("not_supported", "this workspace cannot remove its audio")
