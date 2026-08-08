import unittest

from modules.astronomy import Astronomy


class AstronomyTests(unittest.TestCase):
    def test_sun_dot_is_one_at_solar_subpoint(self):
        value = Astronomy.sun_dot(None, 10, 20, 10, 20)
        self.assertAlmostEqual(value, 1.0)

    def test_sun_dot_is_negative_on_opposite_meridian(self):
        value = Astronomy.sun_dot(None, 0, 0, 0, 180)
        self.assertAlmostEqual(value, -1.0)
