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
