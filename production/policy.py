"""Caller policy and platform ceilings for production media.

Two layers, deliberately separate:

* PLATFORM limits are intrinsic to the substrate (what the station can safely
  store and decode). A violation is a property of the media itself and is
  recorded on the ProductionMedia row as an ``invalid`` verdict.
* A MediaPolicy is supplied by a CONSUMING DOMAIN at intake (a voice track
  fits an intro, a news read has a slot length ...). Violating it refuses the
  intake -- no row is created -- because the bytes may be perfectly good media
  for some other consumer. Policy outcomes are never persisted on the row.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# Hard ceilings enforced no matter what a caller asks for. nginx's own request
# ceiling is 1 GiB (deploy/isadoraair-locations.conf); stay well under it.
PLATFORM_MAX_BYTES = 512 * 1024 * 1024
PLATFORM_MAX_DURATION_SECONDS = Decimal(4 * 3600)
PLATFORM_MAX_CHANNELS = 2
PLATFORM_MIN_SAMPLE_RATE = 8000
PLATFORM_MAX_SAMPLE_RATE = 192000
# More than this many streams of any kind is treated as a malformed/hostile
# container rather than analysed (also keeps tool output bounded).
PLATFORM_MAX_STREAMS = 16
PLATFORM_MAX_ATTACHED_PICTURES = 4


@dataclass(frozen=True)
class MediaPolicy:
    """What the consuming domain will accept. All bounds are optional."""

    max_bytes: int = PLATFORM_MAX_BYTES
    min_duration_seconds: float | None = None
    max_duration_seconds: float | None = None
    # Prove the engine's own decoder (GStreamer) can decode it. Everything that
    # can eventually air must; a consumer that never airs the media may skip it.
    require_engine_decode: bool = True

    def __post_init__(self):
        if not isinstance(self.max_bytes, int) or isinstance(self.max_bytes, bool) \
                or not 0 < self.max_bytes <= PLATFORM_MAX_BYTES:
            raise ValueError(f"max_bytes must be an integer in 1..{PLATFORM_MAX_BYTES}")
        low, high = self.min_duration_seconds, self.max_duration_seconds
        for name, value in (("min_duration_seconds", low), ("max_duration_seconds", high)):
            if value is not None and not (isinstance(value, (int, float)) and not isinstance(value, bool)
                                          and value > 0):
                raise ValueError(f"{name} must be a positive number or None")
        if low is not None and high is not None and low > high:
            raise ValueError("min_duration_seconds must not exceed max_duration_seconds")


DEFAULT_POLICY = MediaPolicy()


def policy_duration_code(decoded_duration: Decimal, policy: MediaPolicy) -> str | None:
    """``too_short`` / ``too_long`` when the decoded duration violates the
    caller's bounds, else None. Pure; works on already-stored facts."""
    if policy.min_duration_seconds is not None and decoded_duration < Decimal(str(policy.min_duration_seconds)):
        return "too_short"
    if policy.max_duration_seconds is not None and decoded_duration > Decimal(str(policy.max_duration_seconds)):
        return "too_long"
    return None
