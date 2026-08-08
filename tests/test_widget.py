import unittest
from datetime import datetime, timedelta, timezone

from modules.satellites import Satellite
from modules.widget import sort_satellites


class SortSatellitesTests(unittest.TestCase):
    def setUp(self):
        self.satellites = [
            Satellite(norad=3, name="Zebra", mean_altitude=400.0),
            Satellite(norad=1, name="apple", mean_altitude=800.0),
            Satellite(norad=2, name="Banana", mean_altitude=200.0),
        ]

    def test_returns_copy_when_no_column(self):
        result = sort_satellites(self.satellites, None, True)
        self.assertEqual(result, self.satellites)
        self.assertIsNot(result, self.satellites)

    def test_sorts_by_norad_ascending(self):
        result = sort_satellites(self.satellites, "norad", True)
        self.assertEqual([s.norad for s in result], [1, 2, 3])

    def test_sorts_by_norad_descending(self):
        result = sort_satellites(self.satellites, "norad", False)
        self.assertEqual([s.norad for s in result], [3, 2, 1])

    def test_sorts_by_name_case_insensitive(self):
        result = sort_satellites(self.satellites, "name", True)
        self.assertEqual([s.name for s in result], ["apple", "Banana", "Zebra"])

    def test_sorts_by_mean_altitude(self):
        result = sort_satellites(self.satellites, "mean_altitude", False)
        self.assertEqual([s.norad for s in result], [1, 3, 2])

    def test_next_pass_sorts_with_none_last_ascending(self):
        satellites = [
            Satellite(norad=1, name="A", next_pass=None),
            Satellite(
                norad=2, name="B",
                next_pass=datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            ),
            Satellite(
                norad=3, name="C",
                next_pass=datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
            ),
        ]
        result = sort_satellites(satellites, "next_pass", True)
        self.assertEqual([s.norad for s in result], [3, 2, 1])

    def test_next_pass_sorts_with_none_last_descending(self):
        satellites = [
            Satellite(norad=1, name="A", next_pass=None),
            Satellite(
                norad=2, name="B",
                next_pass=datetime(2026, 8, 10, 12, tzinfo=timezone.utc)
            ),
            Satellite(
                norad=3, name="C",
                next_pass=datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
            ),
        ]
        result = sort_satellites(satellites, "next_pass", False)
        self.assertEqual([s.norad for s in result], [2, 3, 1])
