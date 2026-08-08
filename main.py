import os
import shutil
import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication

# В собранной версии ресурсы находятся во внутренней папке PyInstaller.
# Рабочая директория должна быть настроена до импорта модулей, загружающих
# изображения и эфемериды по относительным путям.
if getattr(sys, "frozen", False):
    os.chdir(Path(sys._MEIPASS))


def get_config_path() -> Path:
    """Возвращает пользовательский config.json для исходной и собранной версий."""
    if not getattr(sys, "frozen", False):
        return Path("config.json")

    bundled_dir = Path(sys._MEIPASS)
    config_path = Path(sys.executable).parent / "config.json"

    if not config_path.exists():
        shutil.copy2(bundled_dir / "config.json", config_path)

    return config_path


from modules.config import Config
from modules.satellites import SatelliteManager
from modules.widget import MainWidget


def main():

    app = QApplication(sys.argv)
    config = Config(get_config_path())
    manager = SatelliteManager(config)
    manager.initialize()
    window = MainWidget(
        config=config,
        satellites=manager
    )
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
