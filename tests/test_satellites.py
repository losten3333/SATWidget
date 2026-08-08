import unittest
import tempfile
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from modules.satellites import Satellite, SatelliteManager


class SatelliteTests(unittest.TestCase):
    def test_coordinate_projection(self):
        self.assertEqual(
            SatelliteManager.latlon_to_xy(0, 0, 1000, 500),
            (500.0, 250.0)
        )
        self.assertEqual(
            SatelliteManager.latlon_to_xy(90, -180, 1000, 500),
            (0.0, 0.0)
        )

    def test_track_keeps_only_requested_history(self):
        satellite = Satellite(norad=1, name="Test")
        for index in range(4):
            satellite.add_track_point(index, index, limit=3)

        self.assertEqual(satellite.track, [(1, 1), (2, 2), (3, 3)])

    def test_map_visibility_is_independent_from_configuration_enabled_flag(self):
        satellite = Satellite(norad=1, name="Test", enabled=True, map_visible=False)

        self.assertTrue(satellite.enabled)
        self.assertFalse(satellite.map_visible)

    def test_cache_for_format_uses_provider_value_or_default(self):
        providers = [{"format": "GP", "cache": "cache/gp.json"}]
        self.assertEqual(
            SatelliteManager._cache_for_format(providers, "gp", "default.json"),
            "cache/gp.json"
        )
        self.assertEqual(
            SatelliteManager._cache_for_format(providers, "tle", "default.txt"),
            "default.txt"
        )

    def test_disabled_source_cache_is_not_selected_or_checked(self):
        providers = [
            {"format": "gp", "enabled": True, "cache": "data/gp.json"},
            {"format": "tle", "enabled": False, "cache": "data/tle.txt"},
        ]
        self.assertEqual(
            SatelliteManager._cache_for_format(providers, "tle", "default.txt"),
            "default.txt"
        )

        with tempfile.TemporaryDirectory() as directory:
            gp_file = Path(directory) / "gp.json"
            gp_file.touch()
            manager = SatelliteManager.__new__(SatelliteManager)
            manager.config = SimpleNamespace(tle={"providers": [
                {"format": "gp", "enabled": True, "cache": str(gp_file)},
                {"format": "tle", "enabled": False, "cache": str(Path(directory) / "tle.txt")},
            ]})
            manager.tle_update_interval = timedelta(hours=1)

            self.assertFalse(manager.sources_are_outdated())

    def test_mean_altitude_uses_keplerian_mean_motion(self):
        manager = SatelliteManager.__new__(SatelliteManager)
        model = SimpleNamespace(model=SimpleNamespace(no_kozai=0.0676))
        altitude = manager.calculate_mean_altitude(model)

        self.assertGreater(altitude, 100)
        self.assertLess(altitude, 1_000)

    def test_orbit_history_survives_restart_and_calculates_daily_change(self):
        now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
        first = Satellite(norad=1, name="Test", mean_altitude=500.0)
        second = Satellite(norad=1, name="Test", mean_altitude=500.25)

        with tempfile.TemporaryDirectory() as directory:
            history_file = Path(directory) / "orbit_history.json"
            manager = SatelliteManager.__new__(SatelliteManager)
            manager.orbit_history_file = history_file
            manager.orbit_history = {}
            manager.record_orbit_altitudes([first], now - timedelta(hours=12))

            restarted_manager = SatelliteManager.__new__(SatelliteManager)
            restarted_manager.orbit_history_file = history_file
            restarted_manager.orbit_history = {}
            restarted_manager.load_orbit_history()
            restarted_manager.record_orbit_altitudes([second], now)

        self.assertAlmostEqual(second.orbit_change_72h, 0.25)

    def test_orbit_history_keeps_a_separate_72_hour_change(self):
        now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
        first = Satellite(norad=1, name="Test", mean_altitude=500.0)
        second = Satellite(norad=1, name="Test", mean_altitude=500.5)

        with tempfile.TemporaryDirectory() as directory:
            manager = SatelliteManager.__new__(SatelliteManager)
            manager.orbit_history_file = Path(directory) / "orbit_history.json"
            manager.orbit_history = {}
            manager.record_orbit_altitudes([first], now - timedelta(hours=60))
            manager.record_orbit_altitudes([second], now)

        self.assertAlmostEqual(second.orbit_change_72h, 0.5)

    def test_orbit_history_tracks_inclination_changes(self):
        now = datetime(2026, 7, 25, 12, tzinfo=timezone.utc)
        first = Satellite(norad=1, name="Test", mean_altitude=500.0, inclination=51.60)
        second = Satellite(norad=1, name="Test", mean_altitude=500.5, inclination=51.65)

        with tempfile.TemporaryDirectory() as directory:
            manager = SatelliteManager.__new__(SatelliteManager)
            manager.orbit_history_file = Path(directory) / "orbit_history.json"
            manager.orbit_history = {}
            manager.record_orbit_altitudes([first], now - timedelta(hours=48))
            manager.record_orbit_altitudes([second], now)

        self.assertAlmostEqual(second.inclination_change_72h, 0.05)
