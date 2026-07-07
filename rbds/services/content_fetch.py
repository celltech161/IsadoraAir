"""Per-message content-fetch cache for RBDSMessage rows with
source_type "file"/"url" -- avoids re-reading a file or re-fetching a
URL on every ~1s engine tick when the message's own poll_interval_seconds
is much longer. The actual read/fetch is injected as a callable so this
caching logic can be unit tested without real file/network I/O."""
import time


class ContentFetchCache:
    def __init__(self, clock=time.time):
        self._clock = clock
        self._cache = {}  # key -> (last_fetched_at, text)

    def get(self, key, poll_interval_seconds, fetch_fn):
        """fetch_fn() -> str. On failure (any exception), keeps the
        last-good cached value (or "" if never successfully fetched) --
        never lets a bad file/URL crash the caller."""
        now = self._clock()
        cached = self._cache.get(key)
        if cached is not None and (now - cached[0]) < poll_interval_seconds:
            return cached[1]
        try:
            text = fetch_fn()
        except Exception:
            return cached[1] if cached is not None else ""
        self._cache[key] = (now, text)
        return text
