"""Pure-logic PS/RT rotation state machines -- no I/O, no Django models,
just plain tuples in/out so this can be unit tested without a DB or real
clock. The engine (rbds_manager.py) is responsible for turning real
RBDSPSFrame/RBDSMessage querysets into the tuples these expect and
calling advance() once per tick."""
import time

NOWPLAYING_MIN_SECONDS = 20  # floor before a promo can interrupt now-playing again


class PSRotation:
    """Cycles through enabled PS frames, each shown for its own
    hold_seconds. Zero frames -> advance() returns None (caller falls
    back to a static PS)."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._frames_key = None
        self._index = 0
        self._frame_started_at = None

    def advance(self, frames):
        """frames: list of (text, hold_seconds) tuples, enabled only,
        already ordered by sort_order."""
        if not frames:
            self._frames_key = None
            return None

        now = self._clock()
        key = tuple(frames)
        if key != self._frames_key:
            # Admin edit changed the enabled-frame set -- don't try to
            # preserve a now-possibly-invalid index, just restart.
            self._frames_key = key
            self._index = 0
            self._frame_started_at = now
        elif now - self._frame_started_at >= frames[self._index][1]:
            self._index = (self._index + 1) % len(frames)
            self._frame_started_at = now

        return frames[self._index][0]

    def reset(self):
        """Explicitly restarts rotation state -- the next advance()
        call behaves exactly as a brand-new PSRotation instance would,
        regardless of whether the frame list it's given happens to be
        unchanged. advance() already restarts on its own whenever the
        frame list itself changes (see the `key != self._frames_key`
        check above); this method exists for the case that alone can't
        catch -- e.g. RBDSManager switching between entirely different
        PS SOURCES (Static/Manual/Generated -- 2026-08-18) whose frame
        lists could coincidentally produce the same key. A
        service/engine restart also naturally lands here (a fresh
        PSRotation() already starts at index 0), so this method adds
        no persistence of its own -- there's deliberately nothing to
        persist."""
        self._frames_key = None
        self._index = 0
        self._frame_started_at = None


class RTRotation:
    """Priority-interrupt model: now-playing is the default/continuous
    state; promo messages periodically take over for their own
    display_seconds, then control returns to now-playing. advance()
    returns (rt_source, rt_source_name) -- rt_source is "nowplaying" or
    "promo"; the caller resolves the actual text separately (now-playing
    from now_playing.json, promo text from the RBDSMessage row named by
    rt_source_name)."""

    def __init__(self, clock=time.time, nowplaying_min_seconds=NOWPLAYING_MIN_SECONDS):
        self._clock = clock
        self._nowplaying_min_seconds = nowplaying_min_seconds
        self.mode = "nowplaying"
        self._mode_started_at = None
        self._promo_index = 0
        self._promos_key = None

    def advance(self, active_promos):
        """active_promos: list of (name, display_seconds) tuples, already
        filtered to enabled + is_active_today(), ordered by sort_order."""
        now = self._clock()
        if self._mode_started_at is None:
            self._mode_started_at = now

        promos_key = tuple(active_promos)
        promos_changed = promos_key != self._promos_key
        self._promos_key = promos_key

        if self.mode == "promo" and promos_changed:
            # The active-promo set changed mid-rotation (admin edit, or a
            # message's start/end_date rolled over) -- restart from the
            # top of the new list rather than preserve a now-invalid
            # index/timer.
            self._promo_index = 0
            self._mode_started_at = now

        if self.mode == "nowplaying":
            if active_promos and (now - self._mode_started_at) >= self._nowplaying_min_seconds:
                self.mode = "promo"
                self._promo_index = 0
                self._mode_started_at = now
        else:  # mode == "promo"
            if not active_promos:
                self.mode = "nowplaying"
                self._mode_started_at = now
            else:
                _, display_seconds = active_promos[self._promo_index]
                if now - self._mode_started_at >= display_seconds:
                    self._promo_index += 1
                    if self._promo_index >= len(active_promos):
                        self.mode = "nowplaying"
                        self._mode_started_at = now
                    else:
                        self._mode_started_at = now

        if self.mode == "promo" and active_promos:
            name, _ = active_promos[self._promo_index]
            return "promo", name
        return "nowplaying", None
