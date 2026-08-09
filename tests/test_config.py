import json
import tempfile
import unittest
from pathlib import Path

from modules.config import Config


class ConfigTests(unittest.TestCase):
    def test_reads_named_sections(self):
        data = {
            "display": {"width": 100},
            "observer": {"lat": 1},
            "tle": {"update_hours": 3},
            "satellites": [{"norad": 1}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)

        self.assertEqual(config.display["width"], 100)
        self.assertEqual(config.observer["lat"], 1)
        self.assertEqual(config.tle["update_hours"], 3)
        self.assertEqual(config.satellites[0]["norad"], 1)

    def test_missing_file_raises_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            Config("missing-config.json")

    def test_satellite_map_visibility_is_saved(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [{"norad": 1, "map_visible": True}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            config.set_satellite_map_visibility(1, False)
            reloaded = Config(path)

        self.assertFalse(reloaded.satellites[0]["map_visible"])

    def test_add_satellite_appends_and_saves(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            config.add_satellite(25544, "ISS (ZARYA)", "#00FF00")
            reloaded = Config(path)

        self.assertEqual(len(reloaded.satellites), 1)
        entry = reloaded.satellites[0]
        self.assertEqual(entry["norad"], 25544)
        self.assertEqual(entry["name"], "ISS (ZARYA)")
        self.assertEqual(entry["color"], "#00FF00")
        self.assertTrue(entry["enabled"])
        self.assertTrue(entry["map_visible"])
        self.assertFalse(entry["esp_transmit"])

    def test_set_satellite_esp_transmit_is_saved(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [{"norad": 25544, "esp_transmit": False}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            config.set_satellite_esp_transmit(25544, True)
            reloaded = Config(path)

        self.assertTrue(reloaded.satellites[0]["esp_transmit"])

    def test_set_satellite_esp_transmit_missing_raises(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            with self.assertRaises(ValueError):
                config.set_satellite_esp_transmit(25544, True)

    def test_add_satellite_ignores_duplicate(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [{"norad": 25544, "name": "ISS"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            config.add_satellite(25544, "ISS (ZARYA)", "#00FF00")
            reloaded = Config(path)

        self.assertEqual(len(reloaded.satellites), 1)
        self.assertEqual(reloaded.satellites[0]["name"], "ISS")

    def test_remove_satellite_removes_and_saves(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [
                {"norad": 25544, "name": "ISS"},
                {"norad": 25545, "name": "OTHER"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            self.assertTrue(config.remove_satellite(25544))
            reloaded = Config(path)

        self.assertEqual([s["norad"] for s in reloaded.satellites], [25545])

    def test_remove_missing_satellite_returns_false(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [{"norad": 25544}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            self.assertFalse(config.remove_satellite(99999))
            reloaded = Config(path)

        self.assertEqual(len(reloaded.satellites), 1)

    def test_update_satellite_changes_and_saves(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [{
                "norad": 25544,
                "name": "ISS",
                "color": "#00FF00",
                "show_track": True,
                "show_label": True,
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            config.update_satellite(
                25544,
                name="ISS (ZARYA)",
                color="#FF0000",
                show_track=False,
                show_label=True,
            )
            reloaded = Config(path)

        entry = reloaded.satellites[0]
        self.assertEqual(entry["name"], "ISS (ZARYA)")
        self.assertEqual(entry["color"], "#FF0000")
        self.assertFalse(entry["show_track"])
        self.assertTrue(entry["show_label"])
        self.assertEqual(entry["norad"], 25544)

    def test_update_missing_satellite_raises(self):
        data = {
            "display": {},
            "observer": {},
            "tle": {},
            "satellites": [{"norad": 25544}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            config = Config(path)
            with self.assertRaises(ValueError):
                config.update_satellite(99999, name="NOPE")
