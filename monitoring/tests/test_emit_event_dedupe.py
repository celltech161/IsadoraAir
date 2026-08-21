"""[P0] 1.3C physical-acceptance-failure fix -- required test 8. A real
production event storm (~560 "Studio Monitor output lost" SystemEvent rows
in ~2.5s off ONE physical UCA222 unplug) was caused by engine.py baking
SlotCoordinator's internal operation generation into the dedupe_key, so a
recovery-ownership bug that kept advancing that counter defeated
emit_event's normal 60-second coalescing on every single call. Fixed at
the root in engine.py (see library/tests/test_engine_output_recovery.py's
OutputStaleGenerationOwnershipTests/OutputFastOperationObserverTests), but
this file locks in the defense-in-depth half directly against emit_event
itself: hundreds of rapid calls sharing a STABLE dedupe_key must always
coalesce into repeat_count on one row, regardless of what's calling it --
so if some future, unrelated bug reintroduces rapid flapping, the
dashboard still can't be flooded.

No prior test coverage of emit_event's coalescing mechanism existed at
all before this phase."""
from django.test import TestCase

from monitoring.models import SystemEvent, emit_event


class EmitEventDedupeTests(TestCase):
    def test_rapid_repeated_calls_with_stable_key_coalesce_into_one_row(self):
        """The exact shape of the production storm, reproduced directly
        against emit_event: hundreds of calls in a tight loop, same
        stable key (matching engine.py's post-fix
        "hardware|output-lost|studio_monitor", no generation suffix),
        each call's detail carrying a different (simulated) generation
        -- must still coalesce into exactly one row with repeat_count
        tracking every call."""
        for i in range(200):
            emit_event(
                category="hardware", level="warning", title="Studio Monitor output lost",
                detail={"error": "disconnected", "generation": i, "loss_episode": 1},
                dedupe_key="hardware|output-lost|studio_monitor",
            )

        rows = list(SystemEvent.objects.filter(dedupe_key="hardware|output-lost|studio_monitor"))
        self.assertEqual(len(rows), 1, "200 rapid same-key calls must coalesce into exactly one row")
        self.assertEqual(rows[0].repeat_count, 200)  # default=1 on insert, +1 per coalesced bump (199 of them)
        # detail reflects the LATEST call, not the first -- matches
        # emit_event's own documented "bump; ... detail=detail" behavior.
        self.assertEqual(rows[0].detail["generation"], 199)

    def test_generation_suffixed_key_would_have_produced_hundreds_of_rows(self):
        """Negative-control regression guard: proves this test file
        would actually have CAUGHT the original bug -- the OLD,
        generation-suffixed key shape genuinely does defeat coalescing,
        confirming the stable-key test above isn't passing for a
        trivial/uninteresting reason."""
        for i in range(50):
            emit_event(
                category="hardware", level="warning", title="Studio Monitor output lost",
                detail={"error": "disconnected"},
                dedupe_key=f"hardware|output-lost|studio_monitor|gen{i}",
            )

        rows = list(SystemEvent.objects.filter(dedupe_key__startswith="hardware|output-lost|studio_monitor|gen"))
        self.assertEqual(len(rows), 50, "a generation-suffixed key must NOT coalesce -- "
                                         "each one defeats emit_event's dedupe window, exactly the storm")

    def test_different_stable_keys_never_coalesce_into_each_other(self):
        """Sanity: the fix must not have accidentally made "lost" and
        "recovered" (or Studio Monitor and Stereotool) share a row."""
        emit_event(category="hardware", level="warning", title="Studio Monitor output lost",
                    detail={}, dedupe_key="hardware|output-lost|studio_monitor")
        emit_event(category="hardware", level="info", title="Studio Monitor output recovered",
                    detail={}, dedupe_key="hardware|output-recovered|studio_monitor")
        emit_event(category="hardware", level="warning", title="Stereotool Input output lost",
                    detail={}, dedupe_key="hardware|output-lost|stereotool")

        self.assertEqual(SystemEvent.objects.count(), 3)
