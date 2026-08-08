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

    def save(self) -> None:
        """Записывает актуальную конфигурацию на диск."""
        with self.path.open("w", encoding="utf-8") as file:
            json.dump(self.data, file, ensure_ascii=False, indent=2)
            file.write("\n")
