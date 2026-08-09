from skyfield.api import load, wgs84
from math import sin, cos, radians, degrees, atan2, asin, pi

ts = load.timescale()
eph = load("de421.bsp")
earth = eph["earth"]
sun = eph["sun"]


class Astronomy:

    def __init__(self):
        self.ts = ts
        self.eph = eph
        self.earth = earth
        self.sun = sun

    def solar_subpoint(self):
        t = self.ts.now()

        position = self.earth.at(t).observe(self.sun).apparent()

        subpoint = wgs84.subpoint(position)

        return (
            subpoint.latitude.degrees,
            subpoint.longitude.degrees
        )

    def solar_ra(self) -> float:
        """Прямое восхождение Солнца в градусах (0..360)."""
        t = self.ts.now()

        position = self.earth.at(t).observe(self.sun).apparent()

        ra, _, _ = position.radec()

        return ra.degrees

    def terminator_points(self, step=2):
        solar_lat, solar_lon = self.solar_subpoint()

        lat0 = radians(solar_lat)
        lon0 = radians(solar_lon)

        points = []

        for angle in range(0, 361, step):
            a = radians(angle)

            x = -sin(lon0) * cos(a) - sin(lat0) * cos(lon0) * sin(a)

            y = cos(lon0) * cos(a) - sin(lat0) * sin(lon0) * sin(a)

            z = cos(lat0) * sin(a)

            lat = degrees(asin(z))
            lon = degrees(atan2(y, x))

            points.append((lat, lon))

        return points

    def is_daylight(self, lat, lon, solar_lat, solar_lon):
        return self.sun_dot(lat, lon, solar_lat, solar_lon) > 0

    def sun_dot(self, lat, lon, solar_lat, solar_lon):
        lat = radians(lat)
        lon = radians(lon)

        solar_lat = radians(solar_lat)
        solar_lon = radians(solar_lon)

        x = cos(lat) * cos(lon)
        y = cos(lat) * sin(lon)
        z = sin(lat)

        sx = cos(solar_lat) * cos(solar_lon)
        sy = cos(solar_lat) * sin(solar_lon)
        sz = sin(solar_lat)

        return x * sx + y * sy + z * sz