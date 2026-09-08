"""Regression coverage for the shared WEATHER_DATA_DIR contract."""

import ast
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "lib"))

import wxconfig  # noqa: E402


ENTRY_POINTS = (
    "amber_poll.py",
    "amber_alert.py",
    "wx_forecast.py",
    "wx_alert.py",
    "current_temp.py",
    "update_local_wx_data.py",
    "wx_alert_beep.py",
)


class WeatherDataDirResolverTests(unittest.TestCase):
    def test_configured_weather_data_dir_is_honored_and_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            configured = Path(temporary) / "nested" / "weather"
            resolved = wxconfig.resolve_weather_data_dir(
                {"weather_data_dir": str(configured)}
            )
            self.assertEqual(resolved, configured)
            self.assertTrue(configured.is_dir())

    def test_canonical_default_never_points_into_checkout(self):
        resolved = wxconfig.resolve_weather_data_dir({}, create=False)
        self.assertEqual(resolved, Path("/var/lib/isadoraair/weather"))
        self.assertFalse(resolved.is_relative_to(PROJECT_ROOT))

    def test_manual_invocation_fetches_django_owned_config(self):
        with patch.object(
            wxconfig,
            "load_weather_config",
            return_value={"weather_data_dir": "/srv/station/weather"},
        ) as load:
            resolved = wxconfig.resolve_weather_data_dir(create=False)
        self.assertEqual(resolved, Path("/srv/station/weather"))
        load.assert_called_once_with()

    def test_relative_configured_path_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must be absolute"):
            wxconfig.resolve_weather_data_dir(
                {"weather_data_dir": "weather-data"}, create=False
            )


class WeatherDataDirEntrypointTests(unittest.TestCase):
    def test_every_entrypoint_assigns_data_dir_from_shared_resolver(self):
        for relative in ENTRY_POINTS:
            with self.subTest(entrypoint=relative):
                tree = ast.parse((PROJECT_ROOT / relative).read_text(encoding="utf-8"))
                assignments = [
                    node for node in ast.walk(tree)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "DATA_DIR"
                            for target in node.targets)
                ]
                self.assertEqual(len(assignments), 1)
                value = assignments[0].value
                self.assertIsInstance(value, ast.Call)
                self.assertIsInstance(value.func, ast.Name)
                self.assertEqual(value.func.id, "resolve_weather_data_dir")

    def test_no_entrypoint_uses_source_tree_data_expression(self):
        for relative in ENTRY_POINTS:
            source = (PROJECT_ROOT / relative).read_text(encoding="utf-8")
            with self.subTest(entrypoint=relative):
                self.assertNotIn('BASE_DIR / "data"', source)
                self.assertNotIn("BASE_DIR / 'data'", source)


if __name__ == "__main__":
    unittest.main()
