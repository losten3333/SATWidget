import json
from datetime import timedelta, datetime, timezone
from enum import Enum

from pathlib import Path
from math import sqrt, pi, degrees, cos
import requests
from PySide6.QtGui import QColor

from skyfield.api import EarthSatellite
from skyfield.api import load
from skyfield.api import wgs84

from dataclasses import dataclass, field

MU = 398600.4418  # км³/с²
EARTH_RADIUS = 6378.137
EARTH_J2 = 1.08262668e-3


class SatelliteSource(Enum):
    GP = "gp"
    TLE = "tle"


@dataclass(slots=True)
class Satellite:
    """Информация о спутнике."""

    norad: int
    name: str

    color: QColor = "#00FF00"

    enabled: bool = True
    map_visible: bool = True

    show_track: bool = True
    show_orbit: bool = True
    show_label: bool = True

    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0

    speed: float = 0.0
    inclination: float = 0.0
    raan: float = 0.0
    raan_change_per_day: float = 0.0
    period: float = 0.0

    track: list[tuple[float, float]] = field(default_factory=list)
    orbit: list = field(default_factory=list)
    orbit_timestamp: datetime | None = None

    previous_altitude: float = 0.0
    mean_altitude: float = 0.0
    previous_mean_altitude: float | None = None
    orbit_change_72h: float | None = None
    inclination_change_72h: float | None = None
    altitude_trend: int = 0

    reference_altitude: float = 0.0

    next_pass: datetime | None = None

    source: SatelliteSource = SatelliteSource.GP

    model: EarthSatellite | None = None
    tle_line1: str = ""
    tle_line2: str = ""

    def add_track_point(
            self,
            latitude: float,
            longitude: float,
            limit: int = 180
    ) -> None:
        self.track.append((latitude, longitude))

        if len(self.track) > limit:
            self.track.pop(0)

    @property
    def orbit_delta(self) -> float | None:
        if self.previous_mean_altitude is None:
            return None

        return self.mean_altitude - self.previous_mean_altitude


class SatelliteManager:

    def __init__(self, config):

        self.config = config
        self.ts = load.timescale()
        providers = self.config.tle.get("providers", [])
        self.tle_file = Path(self._cache_for_format(providers, "tle", "data/tle.txt"))
        self.gp_file = Path(self._cache_for_format(providers, "gp", "data/gp.json"))
        self.orbit_history_file = Path(
            self.config.tle.get("orbit_history_cache", "data/orbit_history.json")
        )
        self.orbit_history: dict[str, list[dict]] = {}
        self.load_orbit_history()
        self.satellites: list[Satellite] = []
        self.tle_update_interval = timedelta(
            hours=self.config.tle.get("update_hours", 6)
        )

    @staticmethod
    def _cache_for_format(providers, source_format, default):
        for provider in providers:
            if (
                    provider.get("enabled", True) and
                    provider.get("format", "").lower() == source_format
            ):
                return provider.get("cache", default)
        return default

    def source_is_enabled(self, source_format: str) -> bool:
        return any(
            provider.get("enabled", True) and
            provider.get("format", "").lower() == source_format
            for provider in self.config.tle.get("providers", [])
        )

    def enabled_source_files(self) -> list[Path]:
        """Возвращает кэши только включённых источников без повторов."""
        files = []
        for provider in self.config.tle.get("providers", []):
            if not provider.get("enabled", True):
                continue

            path = Path(provider.get("cache", ""))
            if path not in files:
                files.append(path)
        return files

    def load_orbit_history(self):

        if not self.orbit_history_file.exists():
            return

        try:
            data = json.loads(self.orbit_history_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.orbit_history = data
        except (OSError, json.JSONDecodeError) as error:
            print(f"Не удалось прочитать историю орбит: {error}")

    def save_orbit_history(self):

        try:
            self.orbit_history_file.parent.mkdir(parents=True, exist_ok=True)
            self.orbit_history_file.write_text(
                json.dumps(self.orbit_history, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except OSError as error:
            print(f"Не удалось сохранить историю орбит: {error}")

    @staticmethod
    def _history_time(value):

        try:
            timestamp = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

        if timestamp.tzinfo is None:
            return timestamp.replace(tzinfo=timezone.utc)

        return timestamp.astimezone(timezone.utc)

    def restore_orbit_change(self, sat: Satellite, now=None, before=None):

        now = now or datetime.now(timezone.utc)
        cutoff_72h = now - timedelta(days=3)
        entries = self.orbit_history.get(str(sat.norad), [])
        valid_entries = []

        for entry in entries:
            timestamp = self._history_time(entry.get("timestamp"))
            altitude = entry.get("mean_altitude")
            if (
                    timestamp is None or
                    timestamp < cutoff_72h or
                    (before is not None and timestamp >= before) or
                    not isinstance(altitude, (int, float))
            ):
                continue
            inclination = entry.get("inclination")
            valid_entries.append((
                timestamp,
                float(altitude),
                float(inclination)
                if isinstance(inclination, (int, float)) else None,
            ))

        valid_entries.sort(key=lambda item: item[0])
        sat.orbit_change_72h = (
            sat.mean_altitude - valid_entries[0][1]
            if valid_entries else None
        )
        inclination_entries = [
            entry for entry in valid_entries if entry[2] is not None
        ]
        sat.inclination_change_72h = (
            sat.inclination - inclination_entries[0][2]
            if inclination_entries else None
        )

    def record_orbit_altitudes(self, satellites, now=None):

        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=3)

        for sat in satellites:
            key = str(sat.norad)
            entries = self.orbit_history.get(key, [])
            valid_entries = []

            for entry in entries:
                timestamp = self._history_time(entry.get("timestamp"))
                altitude = entry.get("mean_altitude")
                if timestamp is None or timestamp < cutoff or not isinstance(altitude, (int, float)):
                    continue
                valid_entry = {
                    "timestamp": timestamp.isoformat(),
                    "mean_altitude": float(altitude),
                }
                inclination = entry.get("inclination")
                if isinstance(inclination, (int, float)):
                    valid_entry["inclination"] = float(inclination)
                valid_entries.append(valid_entry)

            valid_entries.append({
                "timestamp": now.isoformat(),
                "mean_altitude": sat.mean_altitude,
                "inclination": sat.inclination,
            })
            self.orbit_history[key] = valid_entries
            self.restore_orbit_change(sat, now, before=now)

        self.save_orbit_history()

    def download_sources(self):

        providers = sorted(
            self.config.tle["providers"],
            key=lambda p: p["priority"]
        )

        success = False

        for provider in providers:

            if not provider["enabled"]:
                continue

            cache = Path(provider["cache"])

            if provider["type"] == "file":

                if cache.exists():
                    success = True

                continue

            try:

                response = requests.get(
                    provider["url"],
                    params=provider.get("params"),
                    timeout=20
                )

                response.raise_for_status()

                cache.parent.mkdir(
                    parents=True,
                    exist_ok=True
                )

                fmt = provider["format"].lower()

                if fmt == "tle":
                    text = response.text
                    lines = [line.strip() for line in text.splitlines() if line.strip()]

                    if len(lines) < 3 or not lines[1].startswith("1 "):
                        print(f'{provider["name"]}: неверный TLE')
                        continue

                    cache.write_text(text, encoding="utf-8")

                elif fmt == "gp":
                    data = response.json()
                    if not isinstance(data, list):
                        print(f'{provider["name"]}: неверный GP')
                        continue
                    cache.write_text(response.text, encoding="utf-8")
                else:
                    print(f'Неизвестный формат {fmt}')
                    continue
                print(f'{provider["name"]}: OK')
                success = True

            except Exception as e:
                print(provider["name"])
                print(e)

        return success

    def sources_are_outdated(self) -> bool:
        """
        Нужно ли обновить GP/TLE.
        """

        files = self.enabled_source_files()

        now = datetime.now()

        for path in files:

            if not path.exists():
                return True

            modified = datetime.fromtimestamp(
                path.stat().st_mtime
            )
            # print(f'{path} - {modified=}')
            # print(f'{path} - {now}')

            if now - modified >= self.tle_update_interval:
                print(f'Файл {path} устарел на {now - modified} (предел {self.tle_update_interval})')
                return True

        return False

    def read_tle(self):

        if not self.source_is_enabled("tle") or not self.tle_file.exists():
            return []

        return [
            line.strip()
            for line in self.tle_file.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]

    def read_tle_map(self) -> dict[int, tuple[str, str, str]]:
        """
        Возвращает словарь:
        NORAD -> (name, line1, line2)
        """

        lines = self.read_tle()

        result = {}

        for i in range(0, len(lines) - 2, 3):

            name = lines[i]
            line1 = lines[i + 1]
            line2 = lines[i + 2]

            try:
                norad = int(line1[2:7])
            except ValueError:
                continue

            result[norad] = (name, line1, line2)

        return result

    def read_gp(self) -> dict[int, dict]:
        """
        Возвращает словарь:

            NORAD -> GP (OMM) запись
        """

        if not self.source_is_enabled("gp") or not self.gp_file.exists():
            return {}

        try:

            with self.gp_file.open(
                    "r",
                    encoding="utf-8"
            ) as f:
                satellites = json.load(f)
                # print(type(satellites))
                # print(len(satellites))
                # print(satellites[0])

        except Exception as e:

            print("Ошибка чтения GP:", e)
            return {}

        result = {}

        for sat in satellites:

            try:
                norad = int(sat["NORAD_CAT_ID"])
            except Exception:
                continue

            result[norad] = sat

        return result

    def read_gp_map(self) -> dict[int, dict]:
        """
        Возвращает словарь:

            NORAD -> запись GP (OMM)
        """

        return self.read_gp()

    def create_object_from_gp(self, gp: dict) -> EarthSatellite:
        """
        Создаёт EarthSatellite из GP (OMM).
        """

        return EarthSatellite.from_omm(
            self.ts,
            gp
        )

    def calculate_mean_altitude(self, obj):

        n = obj.model.no_kozai

        # переводим рад/мин → рад/сек
        n /= 60.0

        a = (MU / (n * n)) ** (1 / 3)

        return a - EARTH_RADIUS

    @staticmethod
    def calculate_raan_change_per_day(obj) -> float:
        """Возвращает вызванную J2 прецессию RAAN в градусах за сутки."""
        mean_motion = obj.model.no_kozai / 60.0  # рад/с
        semi_major_axis = (MU / (mean_motion * mean_motion)) ** (1 / 3)
        semi_latus_rectum = semi_major_axis * (1 - obj.model.ecco ** 2)
        nodal_rate = (
                -1.5
                * EARTH_J2
                * mean_motion
                * (EARTH_RADIUS / semi_latus_rectum) ** 2
                * cos(obj.model.inclo)
        )
        return degrees(nodal_rate * 86400.0)

    def load_satellites(self):

        self.satellites.clear()

        gp_map = self.read_gp_map()
        tle_map = self.read_tle_map()

        wanted = {
            sat["norad"]: sat
            for sat in self.config.satellites
        }

        for norad, cfg in wanted.items():

            obj = None
            name = cfg["name"]
            line1 = ""
            line2 = ""

            #
            # Сначала пробуем GP
            #
            gp = gp_map.get(norad)

            if gp is not None:

                try:

                    obj = self.create_object_from_gp(gp)

                    # name = gp.get(
                    #     "OBJECT_NAME",
                    #     name
                    # )

                except Exception as e:

                    print(
                        f"GP ошибка для {norad}: {e}"
                    )

            #
            # Если GP нет — используем TLE
            #
            if obj is None:

                tle = tle_map.get(norad)

                if tle is None:
                    print(
                        f"Спутник {norad} не найден "
                        "ни в GP, ни в TLE."
                    )

                    continue

                name, line1, line2 = tle

                try:

                    obj = EarthSatellite(
                        line1,
                        line2,
                        name,
                        self.ts
                    )

                except Exception as e:

                    print(
                        f"TLE ошибка для {norad}: {e}"
                    )

                    continue

            satellite = Satellite(
                norad=norad,
                name=name,
                color=QColor(cfg["color"]),
                enabled=cfg["enabled"],
                map_visible=cfg.get("map_visible", cfg["enabled"]),
                show_track=cfg["show_track"],
                show_orbit=cfg["show_orbit"],
                show_label=cfg["show_label"]
            )

            satellite.tle_line1 = line1
            satellite.tle_line2 = line2

            satellite.period = (
                                       2 * pi
                               ) / obj.model.no_kozai

            satellite.mean_altitude = (
                self.calculate_mean_altitude(obj)
            )

            satellite.inclination = degrees(
                obj.model.inclo
            )
            satellite.raan = degrees(obj.model.nodeo)
            satellite.raan_change_per_day = self.calculate_raan_change_per_day(obj)
            satellite.previous_mean_altitude = None
            self.restore_orbit_change(satellite)

            satellite.model = obj

            position = obj.at(
                self.ts.now()
            )

            subpoint = wgs84.subpoint(
                position
            )

            satellite.reference_altitude = (
                subpoint.elevation.km
            )

            satellite.altitude = (
                satellite.reference_altitude
            )

            self.satellites.append(
                satellite
            )

            self.calculate_next_pass(
                satellite
            )

    def update_satellite_from_tle(
            self,
            sat: Satellite,
            name: str,
            line1: str,
            line2: str
    ) -> bool:
        """
        Обновляет спутник по новому TLE.

        Возвращает True, если TLE изменился.
        """

        if (
                sat.tle_line1 == line1 and
                sat.tle_line2 == line2
        ):
            return False

        obj = EarthSatellite(
            line1,
            line2,
            name,
            self.ts
        )

        new_mean_altitude = self.calculate_mean_altitude(obj)

        # Первичная загрузка
        if sat.mean_altitude == 0:
            sat.mean_altitude = new_mean_altitude
        else:
            sat.previous_mean_altitude = sat.mean_altitude
            sat.mean_altitude = new_mean_altitude

        sat.model = obj
        if sat.show_orbit:
            self.calculate_orbit(sat, force=True)

        self.calculate_next_pass(sat)

        sat.period = (2 * pi) / obj.model.no_kozai
        sat.inclination = degrees(obj.model.inclo)
        sat.raan = degrees(obj.model.nodeo)
        sat.raan_change_per_day = self.calculate_raan_change_per_day(obj)

        sat.tle_line1 = line1
        sat.tle_line2 = line2

        return True

    def update_tles(self) -> int:
        """
        Обновляет все спутники из GP или TLE.

        GP имеет приоритет.
        """

        gp_map = self.read_gp_map()
        tle_map = self.read_tle_map()

        updated = 0
        updated_satellites = []

        for sat in self.satellites:

            #
            # Сначала GP
            #
            gp = gp_map.get(sat.norad)

            if gp is not None:

                try:

                    obj = self.create_object_from_gp(gp)

                    new_mean = self.calculate_mean_altitude(obj)

                    if sat.mean_altitude != 0:
                        sat.previous_mean_altitude = sat.mean_altitude

                    sat.mean_altitude = new_mean
                    sat.period = (2 * pi) / obj.model.no_kozai
                    sat.inclination = degrees(obj.model.inclo)
                    sat.raan = degrees(obj.model.nodeo)
                    sat.raan_change_per_day = self.calculate_raan_change_per_day(obj)

                    sat.model = obj

                    if sat.show_orbit:
                        self.calculate_orbit(sat, force=True)

                    self.calculate_next_pass(sat)

                    updated += 1
                    updated_satellites.append(sat)

                    continue

                except Exception as e:

                    print(
                        f"GP update {sat.norad}: {e}"
                    )

            #
            # Если GP нет — используем старый механизм TLE
            #
            tle = tle_map.get(sat.norad)

            if tle is None:
                continue

            name, line1, line2 = tle

            if self.update_satellite_from_tle(
                    sat,
                    name,
                    line1,
                    line2
            ):
                updated += 1
                updated_satellites.append(sat)

        if updated_satellites:
            self.record_orbit_altitudes(updated_satellites)

        return updated

    def refresh_tles(self) -> int:

        if not self.sources_are_outdated():
            return 0

        print("Обновление GP/TLE...")

        if not self.download_sources():
            return 0

        return self.update_tles()

    def update(self):
        """
        Обновить положение всех спутников.
        """

        for sat in self.satellites:

            if not sat.enabled:
                continue

            obj = sat.model

            if obj is None:
                continue

            position = obj.at(self.ts.now())
            # print(position.position.km)

            subpoint = wgs84.subpoint(position)

            # print(
            #     subpoint.latitude.degrees,
            #     subpoint.longitude.degrees,
            #     subpoint.elevation.km
            # )

            sat.latitude = subpoint.latitude.degrees
            sat.longitude = subpoint.longitude.degrees
            sat.speed = self.calculate_speed(obj)
            sat.altitude = subpoint.elevation.km
            delta = sat.altitude - sat.reference_altitude

            EPS = 0.2  # км

            if delta > EPS:
                sat.altitude_trend = 1
            elif delta < -EPS:
                sat.altitude_trend = -1
            else:
                sat.altitude_trend = 0

            sat.add_track_point(
                sat.latitude,
                sat.longitude,
                limit=300
            )

            if sat.show_orbit:
                self.calculate_orbit(sat)

            self.update_next_pass(sat)

    def calculate_speed(
            self,
            satellite: EarthSatellite
    ) -> float:

        velocity = satellite.at(
            self.ts.now()
        ).velocity.km_per_s

        vx, vy, vz = velocity

        return sqrt(
            vx * vx +
            vy * vy +
            vz * vz
        )

    @staticmethod
    def latlon_to_xy(
            latitude: float,
            longitude: float,
            width: int,
            height: int
    ):

        x = (longitude + 180.0) / 360.0 * width

        y = (90.0 - latitude) / 180.0 * height

        return x, y

    def get_satellites(self):
        return self.satellites

    def add_track_point(
            self,
            latitude,
            longitude,
            limit=180
    ):

        self.track.append(
            (latitude, longitude)
        )

        if len(self.track) > limit:
            self.track.pop(0)

    def reset_history(self):

        for sat in self.satellites:
            sat.track.clear()
            sat.orbit.clear()
            sat.next_pass = None

    def initialize(self):

        if self.sources_are_outdated():
            print("Обновление источников...")

        self.load_satellites()

        self.update()

    def orbit_points(self, sat, minutes=90, step=1):

        obj = sat.model

        if obj is None:
            return []

        now = self.ts.now().utc_datetime()

        points = []

        for minute in range(-minutes, minutes + 1, step):
            t = self.ts.from_datetime(
                now + timedelta(minutes=minute)
            )

            subpoint = obj.at(t).subpoint()

            points.append((
                subpoint.latitude.degrees,
                subpoint.longitude.degrees
            ))

        return points

    def calculate_orbit(self, sat, points=360, force=False):

        obj = sat.model

        if obj is None:
            sat.orbit.clear()
            return

        now = self.ts.now().utc_datetime().replace(
            second=0,
            microsecond=0
        )

        if not force and sat.orbit and sat.orbit_timestamp == now:
            return

        sat.orbit.clear()

        period = sat.period  # минут
        half = period / 2

        dt = period / (points - 1)
        # print(sat.name, sat.period)
        for i in range(points):
            minute = -half + i * dt

            t = self.ts.from_datetime(
                now + timedelta(minutes=minute)
            )

            subpoint = wgs84.subpoint(obj.at(t))

            sat.orbit.append((
                subpoint.latitude.degrees,
                subpoint.longitude.degrees
            ))

        sat.orbit_timestamp = now

    def calculate_next_pass(self, sat: Satellite):

        observer = wgs84.latlon(
            self.config.observer["lat"],
            self.config.observer["lon"],
            elevation_m=self.config.observer["alt"]
        )

        obj = sat.model

        if obj is None:
            sat.orbit.clear()
            return

        t0 = self.ts.now()
        t1 = self.ts.from_datetime(
            t0.utc_datetime() + timedelta(days=1)
        )

        try:

            times, events = obj.find_events(
                observer,
                t0,
                t1,
                altitude_degrees=10.0
            )

            for t, event in zip(times, events):

                # 0 = восход
                if event == 0:
                    sat.next_pass = t.utc_datetime()
                    return

        except Exception:
            pass

        sat.next_pass = None

    def update_next_pass(self, sat: Satellite):

        if sat.next_pass is None:
            self.calculate_next_pass(sat)
            return

        now = datetime.now(timezone.utc)

        if now >= sat.next_pass:
            self.calculate_next_pass(sat)
