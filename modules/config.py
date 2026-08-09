import json
from pathlib import Path


class Config:
    """Загрузка и хранение настроек приложения."""

    def __init__(self, filename: str):

        self.path = Path(filename)

        if not self.path.exists():
            raise FileNotFoundError(filename)

        # utf-8-sig также корректно читает обычный UTF-8, но игнорирует BOM,
        # который мог быть добавлен редактором конфигурации.
        with self.path.open("r", encoding="utf-8-sig") as f:
            self.data = json.load(f)

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def display(self):
        return self.data["display"]

    @property
    def observer(self):
        return self.data["observer"]

    @property
    def tle(self):
        return self.data["tle"]

    @property
    def satellites(self):
        return self.data["satellites"]

    def set_satellite_map_visibility(self, norad: int, visible: bool) -> None:
        """Сохраняет настройку отображения спутника на карте."""
        for satellite in self.satellites:
            if satellite.get("norad") == norad:
                satellite["map_visible"] = visible
                self.save()
                return

        raise ValueError(f"Спутник с NORAD ID {norad} отсутствует в конфигурации")

    def update_satellite(self, norad: int, **changes) -> None:
        """Обновляет настройки спутника и сохраняет конфигурацию."""
        for satellite in self.satellites:
            if satellite.get("norad") == norad:
                satellite.update(changes)
                self.save()
                return

        raise ValueError(f"Спутник с NORAD ID {norad} отсутствует в конфигурации")

    def set_satellite_esp_transmit(self, norad: int, transmit: bool) -> None:
        """Сохраняет настройку передачи спутника на ESP32."""
        self.update_satellite(norad, esp_transmit=transmit)

    def add_satellite(
            self,
            norad: int,
            name: str,
            color: str,
            map_visible: bool = True
    ) -> None:
        """Добавляет спутник в перечень и сохраняет конфигурацию."""
        for satellite in self.satellites:
            if satellite.get("norad") == norad:
                return

        self.data["satellites"].append({
            "norad": norad,
            "name": name,
            "color": color,
            "enabled": True,
            "show_track": True,
            "show_orbit": True,
            "show_label": True,
            "map_visible": map_visible,
            "esp_transmit": False,
        })
        self.save()

    def remove_satellite(self, norad: int) -> bool:
        """Удаляет спутник из перечня и сохраняет конфигурацию."""
        before = len(self.data["satellites"])

        self.data["satellites"] = [
            satellite
            for satellite in self.data["satellites"]
            if satellite.get("norad") != norad
        ]

        if len(self.data["satellites"]) != before:
            self.save()
            return True

        return False

    def save(self) -> None:
        """Записывает актуальную конфигурацию на диск."""
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
            file.write("\n")
