"""Redirects the two files the sync writes under the station's runtime
directory into a test's own scratch directory."""
from unittest.mock import patch

from road_conditions import alerts, trigger


def patch_runtime_paths(root):
    """Starts the patchers and returns them; the caller stops them."""
    patchers = [
        patch.object(trigger, "TRIGGER_PATH", root / "road_generation.trigger"),
        patch.object(alerts, "STATE_PATH", root / "road_conditions_alert_state.json"),
    ]
    for patcher in patchers:
        patcher.start()
    return patchers
