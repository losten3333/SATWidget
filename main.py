import os
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

# В собранной версии ресурсы находятся во внутренней папке PyInstaller.
# Рабочая директория должна быть настроена до импорта модулей, загружающих
# изображения и эфемериды по относительным путям.
if getattr(sys, "frozen", False):
    os.chdir(Path(sys._MEIPASS))


class GpSignalBridge(QObject):
    """Мост между потоком HTTP-приёмника и основным (GUI) потоком."""
    gp_received = Signal(int)


def get_config_path() -> Path:
    """Возвращает пользовательский config.json для исходной и собранной версий."""
    if not getattr(sys, "frozen", False):
        return Path("config.json")

    bundled_dir = Path(sys._MEIPASS)
    config_path = Path(sys.executable).parent / "config.json"

    if not config_path.exists():
        bundled_config = bundled_dir / "config.json"
        if not bundled_config.exists():
            bundled_config = bundled_dir / "config.example.json"
        shutil.copy2(bundled_config, config_path)

    return config_path


from modules.config import Config
from modules.console_capture import ConsoleCapture, ConsoleWindow
from modules.gp_receiver import GpReceiver
from modules.satellites import SatelliteManager
from modules.widget import MainWidget


def main():

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app_icon = QIcon(str(Path("resources") / "logo.png"))
    app.setWindowIcon(app_icon)
    console_capture = ConsoleCapture()
    console_capture.install()
    console_window = ConsoleWindow(console_capture)
    config = Config(get_config_path())

    # Локальный HTTP-приёмник GP-данных от расширения Chrome.
    receiver_cfg = config.data.get("gp_receiver", {})
    receiver = None
    if receiver_cfg.get("enabled", True):
        receiver = GpReceiver(
            port=receiver_cfg.get("port", 9090),
            cache_path=receiver_cfg.get("cache", "data/gp.json"),
            host=receiver_cfg.get("host", "127.0.0.1"),
        )
        space_track_cfg = config.data.get("space_track", {})
        receiver.set_space_track_credentials(
            space_track_cfg.get("username", ""),
            space_track_cfg.get("password", ""),
        )
        receiver.start()

    manager = SatelliteManager(config)
    manager.initialize()
    window = MainWidget(
        config=config,
        satellites=manager,
        receiver=receiver,
        console_window=console_window,
    )
    window.setWindowIcon(app_icon)

    tray = QSystemTrayIcon(app_icon, app)
    tray_menu = QMenu()
    minimize_action = tray_menu.addAction("Свернуть")
    restore_action = tray_menu.addAction("Развернуть")
    tray_menu.addSeparator()
    close_action = tray_menu.addAction("Закрыть")

    minimize_action.triggered.connect(window.hide)

    def restore_window():
        window.showNormal()
        window.raise_()
        window.activateWindow()

    restore_action.triggered.connect(restore_window)
    close_action.triggered.connect(app.quit)
    tray.setContextMenu(tray_menu)

    def show_tray_menu(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            tray_menu.popup(QCursor.pos())

    tray.activated.connect(show_tray_menu)
    tray.show()
    window.showNormal()

    # Связка приёмника с GUI: новые GP от расширения мгновенно обновляют
    # спутники, историю орбит и виджет.
    bridge = GpSignalBridge()

    def on_gp_received(updated_count):
        print(f"[gp_receiver] уведомил GUI: {updated_count} записей")
        window.handle_gp_received(updated_count)

    bridge.gp_received.connect(on_gp_received)
    if receiver is not None:
        receiver.set_on_gp(lambda n: bridge.gp_received.emit(n))

    try:
        exit_code = app.exec()
    finally:
        if receiver is not None:
            receiver.stop()
            receiver.set_on_gp(None)
        console_capture.uninstall()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
