import unittest
import tempfile
import os
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skyfield.api import load

from modules.satellites import Satellite, SatelliteManager
from modules.config import Config


ISS_GP = {
    "CCSDS_OMM_VERS": "3.0",
    "OBJECT_NAME": "ISS (ZARYA)",
    "OBJECT_ID": "1998-067A",
    "CENTER_NAME": "EARTH",
    "REF_FRAME": "TEME",
    "TIME_SYSTEM": "UTC",
    "MEAN_ELEMENT_THEORY": "SGP4",
    "EPOCH": "2026-08-07T20:38:15.804096",
    "MEAN_MOTION": "15.49380334",
    "ECCENTRICITY": "0.00073377",
    "INCLINATION": "51.6325",
    "RA_OF_ASC_NODE": "44.3665",
    "ARG_OF_PERICENTER": "23.4740",
    "MEAN_ANOMALY": "336.6582",
    "EPHEMERIS_TYPE": "0",
    "CLASSIFICATION_TYPE": "U",
    "NORAD_CAT_ID": "25544",
    "ELEMENT_SET_NO": "999",
    "REV_AT_EPOCH": "57976",
    "BSTAR": "0.00010676931000",
    "MEAN_MOTION_DOT": "0.00005515",
    "MEAN_MOTION_DDOT": "0.0000000000000",
}


def make_manager(directory, gp_records=None, tle_text=None):
    gp_file = Path(directory) / "gp.json"
    gp_file.write_text(
        json.dumps(gp_records or []),
        encoding="utf-8"
    )
    tle_file = Path(directory) / "tle.txt"
    tle_file.write_text(tle_text or "", encoding="utf-8")

    manager = SatelliteManager.__new__(SatelliteManager)
    manager.config = SimpleNamespace(
        tle={"providers": [
            {"format": "gp", "enabled": True, "cache": str(gp_file)},
        ]},
        observer={"lat": 55.75, "lon": 37.62, "alt": 180},
    )
    manager.ts = load.timescale()
    manager.gp_file = gp_file
    manager.tle_file = tle_file
    manager.orbit_history_file = Path(directory) / "orbit_history.json"
    manager.orbit_history = {}
    manager.tle_update_interval = timedelta(hours=1)
    manager.satellites = []
    return manager


class FakeResponse:
    def __init__(self, text, payload=None, status_code=200):
        self.text = text
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, login_text, get_text, get_payload):
        self.login_text = login_text
        self.get_text = get_text
        self.get_payload = get_payload
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def post(self, url, data=None, timeout=None):
        return FakeResponse(self.login_text)

    def get(self, url, timeout=None):
        return FakeResponse(self.get_text, payload=self.get_payload)


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


class FileProviderDownloadTests(unittest.TestCase):
    """Обновление GP/TLE теперь выполняет расширение Chrome, а приложение
    просто проверяет наличие свежего кэш-файла (провайдер type=file)."""

    def make_manager(self, providers, directory):
        manager = SatelliteManager.__new__(SatelliteManager)
        manager.config = SimpleNamespace(tle={"providers": providers})
        return manager

    def test_download_sources_success_when_file_exists(self):
        providers = [
            {
                "name": "Chrome-расширение GP",
                "type": "file",
                "format": "gp",
                "enabled": True,
                "priority": 1,
                "cache": "data/gp.json",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "gp.json"
            cache.write_text('[{"NORAD_CAT_ID": 25544}]', encoding="utf-8")
            providers[0]["cache"] = str(cache)
            manager = self.make_manager(providers, directory)
            self.assertTrue(manager.download_sources())
            self.assertEqual(
                json.loads(cache.read_text(encoding="utf-8")),
                [{"NORAD_CAT_ID": 25544}],
            )

    def test_download_sources_failure_when_file_missing(self):
        providers = [
            {
                "name": "Chrome-расширение GP",
                "type": "file",
                "format": "gp",
                "enabled": True,
                "priority": 1,
                "cache": "data/gp.json",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            providers[0]["cache"] = str(Path(directory) / "gp.json")
            manager = self.make_manager(providers, directory)
            self.assertFalse(manager.download_sources())

    def test_download_sources_skips_disabled_provider(self):
        providers = [
            {
                "name": "Chrome-расширение GP",
                "type": "file",
                "format": "gp",
                "enabled": False,
                "priority": 1,
                "cache": "data/gp.json",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            providers[0]["cache"] = str(Path(directory) / "gp.json")
            manager = self.make_manager(providers, directory)
            self.assertFalse(manager.download_sources())

    def test_download_sources_skips_lower_priority_same_format(self):
        providers = [
            {
                "name": "Основной",
                "type": "file",
                "format": "gp",
                "enabled": True,
                "priority": 1,
                "cache": "data/gp.json",
            },
            {
                "name": "Резервный",
                "type": "file",
                "format": "gp",
                "enabled": True,
                "priority": 2,
                "cache": "data/backup_gp.json",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "gp.json"
            cache.write_text('[{"NORAD_CAT_ID": 25544}]', encoding="utf-8")
            providers[0]["cache"] = str(cache)
            providers[1]["cache"] = str(Path(directory) / "backup_gp.json")
            manager = self.make_manager(providers, directory)
            self.assertTrue(manager.download_sources())
            # Резервный формат gp не рассматриваем (уже удовлетворён).
            self.assertFalse(Path(providers[1]["cache"]).exists())

    def test_sources_are_outdated_depends_on_mtime(self):
        providers = [
            {
                "name": "Chrome-расширение GP",
                "type": "file",
                "format": "gp",
                "enabled": True,
                "priority": 1,
                "cache": "data/gp.json",
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "gp.json"
            cache.write_text('[{"NORAD_CAT_ID": 25544}]', encoding="utf-8")
            providers[0]["cache"] = str(cache)

            manager = SatelliteManager.__new__(SatelliteManager)
            manager.config = SimpleNamespace(tle={"providers": providers})
            manager.tle_update_interval = timedelta(hours=2)

            self.assertFalse(manager.sources_are_outdated())

            # Откатываем mtime в прошлое — файл становится устаревшим.
            old = datetime.now() - timedelta(hours=3)
            os.utime(cache, (old.timestamp(), old.timestamp()))
            self.assertTrue(manager.sources_are_outdated())


class SatelliteAddRemoveTests(unittest.TestCase):
    def test_add_satellite_builds_satellite_from_gp(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory, gp_records=[ISS_GP])
            satellite = manager.add_satellite(25544, "#123456")

        self.assertIsNotNone(satellite)
        self.assertEqual(satellite.norad, 25544)
        self.assertEqual(satellite.name, "ISS (ZARYA)")
        self.assertEqual(satellite.color.name().upper(), "#123456")
        self.assertIsNotNone(satellite.model)
        self.assertIn(satellite, manager.satellites)
        self.assertGreater(satellite.mean_altitude, 100)
        self.assertGreater(satellite.period, 80)

    def test_add_satellite_returns_none_for_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory, gp_records=[ISS_GP])
            self.assertIsNone(manager.add_satellite(99999, "#123456"))
            self.assertEqual(manager.satellites, [])

    def test_add_satellite_returns_none_for_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory, gp_records=[ISS_GP])
            first = manager.add_satellite(25544, "#123456")
            duplicate = manager.add_satellite(25544, "#abcdef")

        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertEqual(len(manager.satellites), 1)

    def test_add_satellite_restores_orbit_history(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory, gp_records=[ISS_GP])
            satellite = manager.add_satellite(25544, "#123456")
            now = datetime.now(timezone.utc)
            manager.record_orbit_altitudes([satellite], now - timedelta(hours=24))
            manager.remove_satellite(25544)

            self.assertEqual(manager.satellites, [])
            self.assertIn("25544", manager.orbit_history)

            satellite = manager.add_satellite(25544, "#123456")

        self.assertIsNotNone(satellite.orbit_change_72h)
        self.assertAlmostEqual(satellite.orbit_change_72h, 0.0, delta=0.01)

    def test_remove_satellite_removes_from_list(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory, gp_records=[ISS_GP])
            manager.add_satellite(25544, "#123456")
            self.assertTrue(manager.remove_satellite(25544))
            self.assertEqual(manager.satellites, [])
            self.assertFalse(manager.remove_satellite(25544))

    def test_total_rotations_counts_revolutions_since_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = make_manager(directory, gp_records=[ISS_GP])
            satellite = manager.add_satellite(25544, "#123456")
            rotations = manager.total_rotations(satellite)

        self.assertIsInstance(rotations, int)
        self.assertGreaterEqual(rotations, 57976)

    def test_esp_transmit_flag_loaded_from_config(self):
        with tempfile.TemporaryDirectory() as directory:
            gp_file = Path(directory) / "gp.json"
            gp_file.write_text(json.dumps([ISS_GP]), encoding="utf-8")

            config_file = Path(directory) / "config.json"
            config_file.write_text(json.dumps({
                "display": {},
                "observer": {"lat": 55.75, "lon": 37.62, "alt": 180},
                "tle": {"providers": [
                    {"format": "gp", "enabled": True, "cache": str(gp_file)},
                ]},
                "satellites": [{
                    "norad": 25544,
                    "name": "ISS (ZARYA)",
                    "color": "#00FF00",
                    "enabled": True,
                    "map_visible": True,
                    "show_track": True,
                    "show_orbit": True,
                    "show_label": True,
                    "esp_transmit": True,
                }],
            }), encoding="utf-8")

            manager = SatelliteManager.__new__(SatelliteManager)
            manager.config = Config(config_file)
            manager.ts = load.timescale()
            manager.gp_file = gp_file
            manager.tle_file = Path(directory) / "tle.txt"
            manager.orbit_history_file = Path(directory) / "orbit_history.json"
            manager.orbit_history = {}
            manager.tle_update_interval = timedelta(hours=1)
            manager.satellites = []
            manager.load_satellites()

        self.assertEqual(len(manager.satellites), 1)
        self.assertTrue(manager.satellites[0].esp_transmit)
