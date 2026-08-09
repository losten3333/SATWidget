import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from modules.satellites import Satellite
from modules.widget import MainWidget, sort_satellites


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


class EspFormattingTests(unittest.TestCase):
    """Проверяет форматирование записей SAT|... для ESP32."""

    def make_widget(self, sun_ra=138.0):
        widget = MainWidget.__new__(MainWidget)
        widget.astronomy = SimpleNamespace(solar_ra=lambda: sun_ra)
        widget.satellites = SimpleNamespace(
            total_rotations=lambda sat: 57976
        )
        widget.timezone = timezone.utc
        return widget

    def test_rus_decimal(self):
        self.assertEqual(MainWidget._rus_decimal(6907.1, 1), "6 907,1")
        self.assertEqual(MainWidget._rus_decimal(420.0, 1), "420,0")
        self.assertEqual(MainWidget._rus_decimal(-0.25, 2), "-0,25")

    def test_delta_text(self):
        self.assertEqual(MainWidget._delta_text(None, 1), "")
        self.assertEqual(MainWidget._delta_text(0.4, 1), "(+0,4)")
        self.assertEqual(MainWidget._delta_text(-0.1, 1), "(-0,1)")

    def test_esp_sma(self):
        widget = self.make_widget()
        sat = Satellite(norad=1, name="A", mean_altitude=420.0)
        self.assertEqual(widget._esp_sma(sat), "420,0 km")

        sat.orbit_change_72h = 0.4
        self.assertEqual(widget._esp_sma(sat), "420,0 km(+0,40)")

    def test_esp_period(self):
        widget = self.make_widget()
        sat = Satellite(norad=1, name="A", period=92.9)
        self.assertEqual(widget._esp_period(sat), "92 min 54 s")

        sat.period = 95.0
        self.assertEqual(widget._esp_period(sat), "95 min 0 s")

    def test_esp_inclination(self):
        widget = self.make_widget()
        sat = Satellite(norad=1, name="A", inclination=51.6)
        self.assertEqual(widget._esp_inclination(sat), "51,6°")

        sat.inclination_change_72h = -0.1
        self.assertEqual(widget._esp_inclination(sat), "51,6°(-0,10)")

    def test_esp_raan(self):
        widget = self.make_widget()
        sat = Satellite(norad=1, name="A", raan=136.8)
        sat.raan_change_per_day = None
        self.assertEqual(widget._esp_raan(sat), "136,8°")

        sat.raan_change_per_day = 0.9
        self.assertEqual(widget._esp_raan(sat), "136,8°(+0,90)")

    def test_esp_ltan(self):
        widget = self.make_widget(sun_ra=138.0)
        sat = Satellite(norad=1, name="A", raan=136.8)
        self.assertEqual(widget._esp_ltan(sat), "11:55")

    def test_esp_ltan_is_zero_padded(self):
        widget = self.make_widget(sun_ra=100.0)
        sat = Satellite(norad=1, name="A", raan=50.0)
        self.assertEqual(widget._esp_ltan(sat), "08:40")

    def test_esp_pass(self):
        widget = self.make_widget()
        sat = Satellite(norad=1, name="A")

        sat.next_pass = datetime(2026, 8, 9, 14, 7, tzinfo=timezone.utc)
        self.assertEqual(widget._esp_pass(sat), "14:07")

        sat.next_pass = None
        self.assertEqual(widget._esp_pass(sat), "—")

    def test_esp_rotations(self):
        widget = self.make_widget()
        sat = Satellite(norad=1, name="A")
        self.assertEqual(widget._esp_rotations(sat), "57976")
