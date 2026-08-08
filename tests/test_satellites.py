import unittest
import tempfile
import os
import json
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

from skyfield.api import load

from modules.satellites import Satellite, SatelliteManager


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


class SpaceTrackDownloadTests(unittest.TestCase):
    def setUp(self):
        self.provider = {
            "name": "Space-Track GP",
            "type": "space_track",
            "format": "gp",
            "cache": "data/gp.json",
            "url": "https://www.space-track.org",
            "params": {
                "query": "basicspacedata/query/class/gp/"
                         "OBJECT_TYPE/PAYLOAD/EPOCH/%3Enow-30/"
                         "ORDERBY/NORAD_CAT_ID/format/json"
            },
            "auth": {"user_env": "ST_USER", "password_env": "ST_PASS"},
        }

    def test_download_requires_credentials(self):
        with patch.dict(os.environ, {}, clear=True):
            manager = SatelliteManager.__new__(SatelliteManager)
            self.assertFalse(manager._download_space_track(self.provider))

    def test_download_writes_cache(self):
        payload = [{"NORAD_CAT_ID": 25544, "OBJECT_NAME": "ISS"}]
        session = FakeSession(
            login_text="Success",
            get_text=json.dumps(payload),
            get_payload=payload,
        )

        with patch.dict(
                os.environ,
                {"ST_USER": "user", "ST_PASS": "pass"},
                clear=True
        ):
            with patch(
                    "modules.satellites.requests.Session",
                    return_value=session
            ):
                with tempfile.TemporaryDirectory() as directory:
                    provider = dict(self.provider)
                    provider["cache"] = str(Path(directory) / "gp.json")

                    manager = SatelliteManager.__new__(SatelliteManager)

                    self.assertTrue(manager._download_space_track(provider))
                    self.assertTrue(session.closed)

                    written = json.loads(
                        Path(provider["cache"]).read_text(encoding="utf-8")
                    )
                    self.assertEqual(written, payload)

    def test_download_fails_on_bad_login(self):
        session = FakeSession(
            login_text="Failed to authenticate",
            get_text="",
            get_payload=None,
        )

        with patch.dict(
                os.environ,
                {"ST_USER": "user", "ST_PASS": "wrong"},
                clear=True
        ):
            with patch(
                    "modules.satellites.requests.Session",
                    return_value=session
            ):
                manager = SatelliteManager.__new__(SatelliteManager)

                self.assertFalse(manager._download_space_track(self.provider))

    def test_download_sources_skips_lower_priority_same_format(self):
        providers = [
            {
                "name": "Space-Track GP",
                "type": "space_track",
                "format": "gp",
                "enabled": True,
                "priority": 1,
                "cache": "data/gp.json",
                "url": "https://www.space-track.org",
                "params": {"query": "basicspacedata/query/class/gp/format/json"},
                "auth": {"user_env": "ST_USER", "password_env": "ST_PASS"},
            },
            {
                "name": "CelesTrak GP",
                "type": "http",
                "format": "gp",
                "enabled": True,
                "priority": 2,
                "cache": "data/gp.json",
                "url": "https://celestrak.org/NORAD/elements/gp.php",
                "params": {"GROUP": "active", "FORMAT": "json"},
            },
        ]

        payload = [{"NORAD_CAT_ID": 25544}]
        session = FakeSession(
            login_text="Success",
            get_text=json.dumps(payload),
            get_payload=payload,
        )

        manager = SatelliteManager.__new__(SatelliteManager)
        manager.config = SimpleNamespace(tle={"providers": providers})

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "gp.json"
            providers[0]["cache"] = str(cache)
            providers[1]["cache"] = str(cache)

            with patch.dict(
                    os.environ,
                    {"ST_USER": "user", "ST_PASS": "pass"},
                    clear=True
            ):
                with patch(
                        "modules.satellites.requests.Session",
                        return_value=session
                ):
                    with patch(
                            "modules.satellites.requests.get",
                            side_effect=AssertionError(
                                "CelesTrak не должен вызываться "
                                "после успешного Space-Track"
                            )
                    ) as mock_get:
                        self.assertTrue(manager.download_sources())
                        mock_get.assert_not_called()

            self.assertEqual(
                json.loads(cache.read_text(encoding="utf-8")),
                payload
            )


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
