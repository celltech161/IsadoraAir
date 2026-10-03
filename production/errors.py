"""Typed errors for the production-media substrate.

Every error carries a stable machine-readable ``code`` so a consuming domain
(Voice Tracking, spoken content, active messages ...) can branch on it without
parsing prose. Messages never contain filesystem paths supplied by a caller,
subprocess output or stack traces.
"""
from __future__ import annotations


class ProductionMediaError(Exception):
    code = "production_media_error"

    def __init__(self, message: str = "", *, code: str | None = None):
        super().__init__(message or (code or self.code))
        if code is not None:
            self.code = code


class ImmutableMediaError(ProductionMediaError):
    """An operation would change or delete immutable ProductionMedia facts."""

    code = "immutable_media"


class IntakeError(ProductionMediaError):
    """Intake could not store the bytes (I/O failure, bad request, collision)."""

    code = "intake_error"


class MediaRejected(ProductionMediaError):
    """The submitted bytes were refused: no ProductionMedia row exists.

    ``code`` is one of the stable media-verdict / policy codes (see
    production.services.validation.MEDIA_VERDICT_CODES) or an intake code such
    as ``too_large`` / ``empty``. ``outcome`` is the full validation outcome
    when validation produced one, else None.
    """

    code = "media_rejected"

    def __init__(self, code: str, message: str = "", *, outcome=None):
        super().__init__(message or code, code=code)
        self.outcome = outcome


class MediaPurged(ProductionMediaError):
    code = "media_purged"


class MediaNotValidated(ProductionMediaError):
    code = "media_not_validated"


class MediaInconsistent(ProductionMediaError):
    """A non-purged row does not resolve to the bytes its evidence describes."""

    code = "media_inconsistent"


class PurgeRefused(ProductionMediaError):
    code = "purge_refused"

    def __init__(self, message: str, *, references=()):
        super().__init__(message)
        # [(model_label, field_name, count)] -- counts only, never row contents.
        self.references = tuple(references)


class RangeNotSatisfiable(ProductionMediaError):
    code = "range_not_satisfiable"
