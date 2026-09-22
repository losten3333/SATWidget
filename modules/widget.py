import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from math import radians

import numpy as np
from PySide6.QtCore import QEvent, Qt, QPoint, QPointF, QRect, QRectF
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QScrollBar, QWidget, QLineEdit, QPushButton,
    QMenu, QWidgetAction, QLabel, QCheckBox, QHBoxLayout, QApplication
)

from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QPixmap, QPainterPath, QPolygonF, QImage,
    QIntValidator, QGuiApplication
)

from modules.astronomy import Astronomy
from modules.esp32_sender import Esp32Sender

# Ограничение прошивки ESP32 (MAX_SATELLITES в ESP32_SatWidget.ino).
MAX_ESP_SATELLITES = 8


def sort_satellites(rows, column, ascending):
    """
    Возвращает строки таблицы, отсортированные по полю column.

    Значения None в колонке «След. пролет» всегда помещаются в конец
    независимо от направления сортировки.
    """

    if column is None:
        return list(rows)

    def key(sat):
        if column == "name":
            return (sat.name or "").lower()
        return getattr(sat, column)

    if column == "next_pass":
        present = [s for s in rows if s.next_pass is not None]
        missing = [s for s in rows if s.next_pass is None]
        present.sort(key=key, reverse=not ascending)
        return present + missing

    return sorted(rows, key=key, reverse=not ascending)


class Esp32SendWorker(QThread):
    """Отправляет снимок спутников на ESP32 в фоновом потоке."""

    completed = Signal(object, object)

    def __init__(self, sender, records, parent=None):
        super().__init__(parent)
        self.sender = sender
        self.records = records

    def run(self):
        result, logs = self.sender.send(self.records)
        self.completed.emit(result, logs)


class MainWidget(QWidget):

    def __init__(self, config, satellites, receiver=None):

        super().__init__()
        self.astronomy = Astronomy()
        self.config = config
        try:
            self.timezone = ZoneInfo(
                self.config.observer["timezone"]
            )
        except ZoneInfoNotFoundError:
            # На Windows база IANA может отсутствовать до установки tzdata.
            # Встроенный UTC доступен всегда и не должен блокировать запуск.
            self.timezone = timezone.utc
        self.satellites = satellites
        self.receiver = receiver
        self.update_notice_visible = False
        self.update_notice_timer = QTimer(self)
        self.update_notice_timer.setSingleShot(True)
        self.update_notice_timer.timeout.connect(self.hide_update_notice)
        self.night_mask = None
        display = config.display
        self.map_options = display.get("map", {})
        self.map_width = display["width"]
        self.map_height = display["height"]
        self.drag_position = None
        self.base_map_width = self.map_width
        self.base_map_height = self.map_height

        self.scale = display.get("scale", 1.0)
        self.resizing = False
        self.enforcing_aspect_ratio = False
        self.resize_start_pos = None
        self.resize_start_scale = self.scale
        HEADER_HEIGHT = 28
        ROW_HEIGHT = 24

        # visible = sum(
        #     sat.enabled
        #     for sat in self.satellites.get_satellites()
        # )
        # self.max_table_rows = max(1, visible)
        self.max_table_rows = 10
        self.table_header_height = 26
        self.table_row_height = 22
        self.input_row_height = 30

        # (заголовок, x, ключ сортировки или None)
        # Колонки разнесены равномерно на всю ширину таблицы.
        self.table_columns = [
            ("", 10, None),
            ("Название", 55, "name"),
            ("NORAD ID", 160, "norad"),
            ("Орбита (Δ72ч)", 235, "mean_altitude"),
            ("Высота", 350, "altitude"),
            ("Период", 440, "period"),
            ("Наклон. (Δ72ч)", 530, "inclination"),
            ("RAAN (Δсут)", 650, "raan"),
            ("LTAN", 755, None),
            ("След. пролет", 830, "next_pass"),
        ]
        self.sort_column = None
        self.sort_ascending = True


        self.table_scroll_offset = 0

        self.status_height = (
                HEADER_HEIGHT +
                ROW_HEIGHT * (self.max_table_rows - 1) +
                self.input_row_height +
                8
        )

        self.base_status_height = self.status_height

        self.resize(
            int(self.base_map_width * self.scale),
            int((self.base_map_height + self.base_status_height) * self.scale)
        )

        self.setWindowOpacity(
            display["opacity"]
        )

        self.setWindowFlags(
            Qt.Window |
            Qt.WindowMinimizeButtonHint |
            Qt.WindowMaximizeButtonHint |
            Qt.WindowCloseButtonHint |
            Qt.WindowStaysOnBottomHint
        )

        self.table_scrollbar = QScrollBar(Qt.Vertical, self)
        self.table_scrollbar.setSingleStep(1)
        self.table_scrollbar.valueChanged.connect(self.set_table_scroll_offset)
        self.layout_table_scrollbar()

        self.notice_text = "Данные спутников обновлены"
        self.notice_color = QColor(25, 100, 55, 225)

        self.norad_input = QLineEdit(self)
        self.norad_input.setPlaceholderText("NORAD ID")
        self.norad_input.setValidator(QIntValidator(0, 999999, self))
        self.norad_input.returnPressed.connect(self.add_satellite_from_input)

        self.add_button = QPushButton("Добавить", self)
        self.add_button.clicked.connect(self.add_satellite_from_input)

        self.delete_button = QPushButton("Удалить", self)
        self.delete_button.clicked.connect(self.remove_satellite_from_input)

        self.controls_style = """
            QLineEdit {
                background: #2a2a2a;
                color: #ffffff;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 1px 4px;
            }
            QLineEdit:focus {
                border: 1px solid #7a7a7a;
            }
            QPushButton {
                background: #3a3a3a;
                color: #ffffff;
                border: 1px solid #555555;
                border-radius: 3px;
                padding: 2px 6px;
            }
            QPushButton:hover {
                background: #4a4a4a;
            }
            QPushButton:pressed {
                background: #2f2f2f;
            }
        """
        self.norad_input.setStyleSheet(self.controls_style)
        self.add_button.setStyleSheet(self.controls_style)
        self.delete_button.setStyleSheet(self.controls_style)

        self.layout_controls()

        self.setWindowTitle("SAT Widget")

        self._opened = False
        self._initial_position = (
            display.get("x", 20),
            display.get("y", 20)
        )
        self.move(*self._initial_position)

        # print(self.pos())

        self.timer = QTimer(self)

        self.timer.timeout.connect(
            self.update_scene
        )

        self.timer.start(
            display["update_sec"] * 1000
        )

        self.refresh_timer = QTimer(self)

        self.refresh_timer.timeout.connect(
            self.refresh_sources
        )

        # Проверяем раз в 5 минут
        self.refresh_timer.start(
            5 * 60 * 1000
        )

        # Проверяем сразу после запуска
        self.refresh_sources()

        self.last_update = datetime.now(timezone.utc)

        self.earth_day = QPixmap("resources/earth_day.png")
        self.earth_night = QPixmap("resources/earth_night.png")

        self.esp_sender = Esp32Sender(minutes=1)
        self.esp_send_worker = None
        self.esp_send_pending = False
        self.esp_prev_passes = {}

        self.satellites.update()
        self._update_esp_pass_tracking()
        self.request_esp_send()
        if self.map_options.get("show_day_night", True):
            self.night_mask = self.create_night_mask()

    def mousePressEvent(self, event):

        if event.button() == Qt.LeftButton and self.toggle_table_sort(event.position()):
            event.accept()
            return

        if event.button() == Qt.LeftButton and self.toggle_satellite_map_visibility(event.position()):
            event.accept()
            return

        if event.button() == Qt.LeftButton and self.open_satellite_settings(event.position()):
            event.accept()
            return

        if event.button() == Qt.LeftButton and self.in_resize_zone(event.position().toPoint()):
            self.resizing = True
            self.resize_start_pos = event.globalPosition().toPoint()
            self.resize_start_scale = self.scale

            event.accept()
            return

        if event.button() == Qt.LeftButton:
            self.drag_position = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event):
        if self.in_resize_zone(event.position().toPoint()):
            self.setCursor(Qt.SizeFDiagCursor)
        else:
            self.setCursor(Qt.ArrowCursor)

        if self.resizing:
            delta = (
                    event.globalPosition().toPoint()
                    - self.resize_start_pos
            )

            change = delta.x() / self.base_map_width

            self.set_scale(
                self.resize_start_scale + change
            )

            event.accept()
            return

        if (
                event.buttons() & Qt.LeftButton
                and self.drag_position is not None
        ):
            self.move(
                event.globalPosition().toPoint() - self.drag_position
            )
            event.accept()

    def mouseReleaseEvent(self, event):
        self.resizing = False
        self.drag_position = None
        event.accept()

    def wheelEvent(self, event):

        if self.table_scrollbar.isVisible():
            panel_top = self.map_height * self.scale
            if event.position().y() >= panel_top:
                steps = event.angleDelta().y() // 120
                self.table_scrollbar.setValue(
                    self.table_scrollbar.value() - steps
                )
                event.accept()
                return

        super().wheelEvent(event)

    def in_resize_zone(self, pos):

        margin = 20

        return (
                pos.x() >= self.width() - margin and
                pos.y() >= self.height() - margin
        )

    def set_scale(self, scale: float):

        self.scale = max(0.5, min(scale, 2.0))

        self.resize(
            int(self.base_map_width * self.scale),
            int(
                (self.base_map_height + self.base_status_height)
                * self.scale
            )
        )

        self.update()

    def _work_area_for_point(self, point: QPoint | None):
        """Возвращает рабочую область (без панели задач) экрана, на котором
        лежит точка point. Если точка не указана или не попадает ни на один
        экран — берётся экран, где находится окно, затем основной экран."""
        screen = QGuiApplication.screenAt(point) if point is not None else None
        if screen is None:
            screen = self.screen()
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return None
        return screen.availableGeometry()

    def _frame_offsets(self):
        """Возвращает (frame_w, frame_h) — насколько рамка окна (заголовок и
        бордеры) больше клиентской области. Если окно ещё не показано,
        считаем рамку нулевой (открытие поправит после первого показа)."""
        frame = self.frameGeometry()
        if frame.isEmpty() or frame.width() <= self.width():
            return 0, 0
        return frame.width() - self.width(), frame.height() - self.height()

    def _fit_to_work_area(self, work: QRect):
        """Масштабирует виджет под рабочую область: максимальный масштаб,
        при котором окно (клиентская область + рамка) целиком помещается в
        рабочую область — без захода под панель задач."""
        base_height = self.base_map_height + self.base_status_height
        base_width = self.base_map_width
        if base_height <= 0 or base_width <= 0 or work.width() <= 0:
            return
        frame_w, frame_h = self._frame_offsets()
        avail_h = work.height() - frame_h
        avail_w = work.width() - frame_w
        if avail_h <= 0 or avail_w <= 0:
            return
        scale = min(
            avail_h / base_height,
            avail_w / base_width,
        )
        scale = max(0.5, scale)
        self.scale = scale
        self.resize(
            int(base_width * scale),
            int(base_height * scale),
        )

    def _snap_top_right(self, work: QRect):
        """Прижимает рамку окна к правому верхнему углу рабочей области."""
        for _ in range(3):
            frame = self.frameGeometry()
            dx = work.right() - frame.right()
            dy = work.top() - frame.top()
            if dx == 0 and dy == 0:
                return
            self.move(self.x() + dx, self.y() + dy)

    def _position_and_fit_on_open(self):
        """При открытии: прижимает виджет к правому верхнему углу рабочей
        области экрана, указанного начальной позицией в config, и
        масштабирует его под доступную вертикаль."""
        point = QPoint(
            int(self._initial_position[0]),
            int(self._initial_position[1]),
        )
        work = self._work_area_for_point(point)
        if work is None:
            return
        self._fit_to_work_area(work)
        self._snap_top_right(work)

    def _ensure_pinned_top_right(self):
        """Проверяет, что виджет прижат к правому верхнему углу рабочей
        области экрана, на котором он находится (поддержка нескольких
        мониторов с разными разрешениями)."""
        work = self._work_area_for_point(None)
        if work is None:
            return
        self._snap_top_right(work)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._opened:
            self._opened = True
            self._position_and_fit_on_open()

    def refresh_sources(self):
        """Проверяет свежесть GP/TLE-данных.

        Виджет не скачивает данные сам. Если gp.json устарел (старше
        tle.update_hours в config.json), он:
        - выводит отладочную информацию в консоль,
        - показывает красное уведомление в виджете,
        - выставляет флаг needs_update в HTTP-приёмнике.

        Расширение Chrome опрашивает GET /status, видит needs_update=true,
        скачивает свежие GP-данные и отправляет их обратно (POST /gp).
        """
        outdated = self.satellites.sources_are_outdated()

        if self.receiver is not None:
            self.receiver.set_needs_update(outdated)

        if not outdated:
            return

        print(
            "[SATWidget] GP/TLE устарели — обновление выполнит "
            "расширение Chrome (tle.update_hours)"
        )
        for path in self.satellites.enabled_source_files():
            if path.exists():
                print(f"[SATWidget] {path}: mtime"
                      f" {datetime.fromtimestamp(path.stat().st_mtime)}")
            else:
                print(f"[SATWidget] {path}: файл отсутствует")
        self.show_message(
            "TLE-данные устарели: ожидается обновление "
            "расширением Chrome",
            ok=False,
        )

    def resizeEvent(self, event):

        if self.base_map_width and not self.enforcing_aspect_ratio:
            scale = self.width() / self.base_map_width
            target_height = int(
                (self.base_map_height + self.base_status_height) * scale
            )

            # Нативное растягивание рамки Windows сохраняет соотношение
            # сторон, как и ручное масштабирование виджета.
            if abs(self.height() - target_height) > 1:
                self.enforcing_aspect_ratio = True
                self.resize(self.width(), target_height)
                self.enforcing_aspect_ratio = False

            self.scale = scale

        super().resizeEvent(event)
        if hasattr(self, "table_scrollbar"):
            self.layout_table_scrollbar()
        if hasattr(self, "norad_input"):
            self.layout_controls()
        self.update()

    def enabled_table_satellites(self):
        return [
            sat
            for sat in self.satellites.get_satellites()
            if sat.enabled
        ]

    def set_table_scroll_offset(self, value):
        self.table_scroll_offset = value
        self.update()

    def layout_table_scrollbar(self):

        total_rows = len(self.enabled_table_satellites())
        maximum = max(0, total_rows - self.max_table_rows)
        self.table_scrollbar.setRange(0, maximum)
        self.table_scrollbar.setPageStep(self.max_table_rows)
        self.table_scrollbar.setVisible(maximum > 0)

        if maximum:
            width = 14
            self.table_scrollbar.setGeometry(
                self.width() - width,
                int(self.map_height * self.scale),
                width,
                int(self.status_height * self.scale)
            )

    def layout_controls(self):

        scale = self.scale
        row_top = (
                self.map_height +
                self.status_height -
                self.input_row_height
        ) * scale
        height = (self.input_row_height - 6) * scale
        y = row_top + 3 * scale

        self.norad_input.setGeometry(
            int(10 * scale),
            int(y),
            int(90 * scale),
            int(height)
        )
        self.add_button.setGeometry(
            int(110 * scale),
            int(y),
            int(80 * scale),
            int(height)
        )
        self.delete_button.setGeometry(
            int(196 * scale),
            int(y),
            int(80 * scale),
            int(height)
        )

    def _norad_from_input(self) -> int | None:

        text = self.norad_input.text().strip()

        if not text.isdigit():
            return None

        return int(text)

    def _next_satellite_color(self) -> str:
        """Выбирает первый цвет палитры, ещё не занятый спутниками."""

        used = {
            sat.color.name().upper()
            for sat in self.satellites.get_satellites()
        }

        palette = [
            "#e6194B", "#3cb44b", "#ffe119", "#4363d8", "#f58231",
            "#911eb4", "#42d4f4", "#f032e6", "#bfef45", "#fabed4",
            "#469990", "#dcbeff", "#9A6324", "#800000", "#aaffc3",
            "#808000", "#ffd8b1", "#000075", "#a9a9a9",
        ]

        for candidate in palette:
            if candidate.upper() not in used:
                return candidate

        return palette[len(used) % len(palette)]

    def add_satellite_from_input(self):

        norad = self._norad_from_input()

        if norad is None:
            self.show_message("Введите NORAD ID", ok=False)
            return

        existing = {
            sat.norad
            for sat in self.satellites.get_satellites()
        }

        if norad in existing:
            self.show_message(f"Спутник {norad} уже добавлен", ok=False)
            return

        color = self._next_satellite_color()
        satellite = self.satellites.add_satellite(norad, color)

        if satellite is None:
            self.show_message(f"Спутник {norad} не найден в данных", ok=False)
            return

        self.config.add_satellite(norad, satellite.name, color)
        self.satellites.update()

        self.layout_table_scrollbar()
        self.table_scrollbar.setValue(self.table_scrollbar.maximum())

        self.norad_input.clear()
        self.show_message(f"Добавлен: {satellite.name}")
        self.update()

    def remove_satellite_from_input(self):

        norad = self._norad_from_input()

        if norad is None:
            self.show_message("Введите NORAD ID", ok=False)
            return

        removed = self.satellites.remove_satellite(norad)
        removed_from_config = self.config.remove_satellite(norad)

        if not removed and not removed_from_config:
            self.show_message(f"Спутник {norad} не найден", ok=False)
            return

        self.layout_table_scrollbar()
        self.norad_input.clear()
        self.show_message(f"Удалён: {norad}")
        self.update()

    def show_message(self, text, ok=True):

        self.notice_text = text
        self.notice_color = (
            QColor(25, 100, 55, 225)
            if ok
            else QColor(160, 50, 50, 225)
        )
        self.update_notice_visible = True
        self.update_notice_timer.start(5_000)
        self.update()

    def toggle_satellite_map_visibility(self, position):

        if self.scale == 0:
            return False

        x = position.x() / self.scale
        y = position.y() / self.scale
        row_top = self.map_height + self.table_header_height
        row_index = int((y - row_top) // self.table_row_height)
        checkbox = QRectF(
            10,
            row_top + row_index * self.table_row_height + 4,
            14,
            14
        )

        if row_index < 0 or not checkbox.contains(QPointF(x, y)):
            return False

        satellites = self._sorted_table_satellites()
        sat_index = self.table_scroll_offset + row_index
        if sat_index >= len(satellites) or row_index >= self.max_table_rows:
            return False

        satellite = satellites[sat_index]
        satellite.map_visible = not satellite.map_visible
        self.config.set_satellite_map_visibility(
            satellite.norad,
            satellite.map_visible
        )
        self.update()
        return True

    def _can_enable_esp_transmit(self, exclude_norad=None) -> bool:
        """Разрешает включение передачи, пока выбрано меньше
        MAX_ESP_SATELLITES КА."""
        count = sum(
            1
            for sat in self.enabled_table_satellites()
            if sat.esp_transmit and sat.norad != exclude_norad
        )
        return count < MAX_ESP_SATELLITES

    def open_satellite_settings(self, position):

        if self.scale == 0:
            return False

        x = position.x() / self.scale
        y = position.y() / self.scale
        row_top = self.map_height + self.table_header_height
        row_index = int((y - row_top) // self.table_row_height)
        gear = QRectF(
            26,
            row_top + row_index * self.table_row_height + 3,
            14,
            14
        )

        if row_index < 0 or not gear.contains(QPointF(x, y)):
            return False

        satellites = self._sorted_table_satellites()
        sat_index = self.table_scroll_offset + row_index
        if sat_index >= len(satellites) or row_index >= self.max_table_rows:
            return False

        satellite = satellites[sat_index]

        cfg = self._config_entry_for_norad(satellite.norad)

        if cfg is None:
            return False

        self._show_satellite_settings_menu(satellite, cfg, gear)
        return True

    def _config_entry_for_norad(self, norad):
        for entry in self.config.satellites:
            if entry.get("norad") == norad:
                return entry

        return None

    FIELD_LABELS = {
        "name": "Название",
        "color": "Цвет",
        "show_track": "Показывать трек",
        "show_orbit": "Показывать орбиту",
        "show_label": "Показывать подпись",
        "esp_transmit": "Передача на ESP32",
    }

    def _settings_menu_style(self):
        return """
            QMenu {
                background-color: #2a2a2a;
                color: #ffffff;
                border: 1px solid #555555;
            }
            QMenu::item {
                background: transparent;
                padding: 2px;
            }
            QMenu::item:selected {
                background: #3a3a3a;
            }
        """

    def _settings_menu_row(self, label, value, is_bool):

        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(10)

        label_widget = QLabel(label)
        label_widget.setStyleSheet(
            "color: #ffffff; background: transparent;"
        )
        label_widget.setMinimumWidth(140)
        layout.addWidget(label_widget)

        if is_bool:
            editor = QCheckBox(container)
            editor.setChecked(bool(value))
            editor.setStyleSheet(
                "color: #ffffff; background: transparent;"
            )
        else:
            editor = QLineEdit(value)
            editor.setStyleSheet(self.controls_style)
            editor.setMinimumWidth(120)

        layout.addWidget(editor)

        return container, editor

    def _build_satellite_settings_menu(self, satellite, cfg):

        menu = QMenu(self)
        menu.setStyleSheet(self._settings_menu_style())

        editors = []

        for key, value in cfg.items():

            if key in ("enabled", "norad", "map_visible"):
                continue

            is_bool = isinstance(value, bool)
            label = self.FIELD_LABELS.get(key, key)
            container, editor = self._settings_menu_row(
                label,
                value,
                is_bool
            )

            action = QWidgetAction(menu)
            action.setDefaultWidget(container)
            menu.addAction(action)

            editors.append((key, editor, is_bool))

        menu.addSeparator()

        apply_container = QWidget()
        apply_layout = QHBoxLayout(apply_container)
        apply_layout.setContentsMargins(8, 4, 8, 6)

        apply_button = QPushButton("Применить", apply_container)
        apply_button.setStyleSheet(self.controls_style)
        apply_layout.addStretch()
        apply_layout.addWidget(apply_button)
        apply_layout.addStretch()

        apply_action = QWidgetAction(menu)
        apply_action.setDefaultWidget(apply_container)
        menu.addAction(apply_action)

        apply_button.clicked.connect(
            lambda: self._apply_satellite_settings(
                satellite,
                cfg,
                editors,
                menu
            )
        )

        return menu

    def _show_satellite_settings_menu(self, satellite, cfg, gear_rect):

        menu = self._build_satellite_settings_menu(satellite, cfg)

        global_pos = self.mapToGlobal(
            QPoint(
                int(gear_rect.left() * self.scale),
                int(gear_rect.bottom() * self.scale)
            )
        )

        menu.exec(global_pos)
        menu.deleteLater()

    def _apply_satellite_settings(self, satellite, cfg, editors, menu):

        changes = {}

        for key, editor, is_bool in editors:

            if is_bool:
                changes[key] = editor.isChecked()
                continue

            value = editor.text().strip()

            if key == "color" and not re.fullmatch(
                    r"#[0-9a-fA-F]{6}",
                    value
            ):
                self.show_message("Некорректный цвет", ok=False)
                return

            changes[key] = value

        if changes.get("esp_transmit") and not self._can_enable_esp_transmit(
                satellite.norad
        ):
            self.show_message(
                f"ESP32: можно передавать не более {MAX_ESP_SATELLITES} КА. "
                "Снимите отметку хотя бы с одного выбранного КА",
                ok=False,
            )
            return

        if "name" in changes:
            satellite.name = changes["name"]

        if "color" in changes:
            satellite.color = QColor(changes["color"])

        if "show_track" in changes:
            satellite.show_track = changes["show_track"]

        if "show_orbit" in changes:
            satellite.show_orbit = changes["show_orbit"]

            if satellite.show_orbit:
                self.satellites.calculate_orbit(
                    satellite,
                    force=True
                )

        if "show_label" in changes:
            satellite.show_label = changes["show_label"]

        if "esp_transmit" in changes:
            satellite.esp_transmit = changes["esp_transmit"]

        self.config.update_satellite(
            satellite.norad,
            **changes
        )

        self.layout_table_scrollbar()
        self.show_message("Настройки сохранены")
        self.update()
        menu.close()

        # A snapshot must be sent for both transitions.  In particular, when
        # the last selected satellite is unchecked, the empty snapshot clears
        # the card on the ESP32 instead of leaving the previous data visible.
        if "esp_transmit" in changes:
            self.request_esp_send()

    def request_esp_send(self):

        if (
                self.esp_send_worker is not None and
                self.esp_send_worker.isRunning()
        ):
            self.esp_send_pending = True
            return

        self._start_esp_send_worker()

    def _start_esp_send_worker(self):

        self.esp_send_pending = False

        try:
            records = self._esp_satellite_records()
        except Exception as error:
            self.show_message(f"ESP32: {error}", ok=False)
            return

        worker = Esp32SendWorker(self.esp_sender, records, self)
        self.esp_send_worker = worker
        worker.completed.connect(self._on_esp_send_finished)
        worker.start()

    def _on_esp_send_finished(self, result, logs):

        for log in logs:
            print("ESP32:", log)

        if result is not None:
            self.show_message(f"ESP32: отправлено ({result})")
        else:
            self.show_message("ESP32: отправка не удалась", ok=False)

        worker = self.esp_send_worker
        self.esp_send_worker = None
        if worker is not None:
            worker.deleteLater()

        if self.esp_send_pending:
            QTimer.singleShot(0, self.request_esp_send)

    def _update_esp_pass_tracking(self) -> bool:
        """Отслеживает изменение времени следующего пролёта для передачи."""

        changed = False

        for sat in self.enabled_table_satellites():

            if not sat.esp_transmit:
                continue

            key = sat.norad
            current = sat.next_pass

            if self.esp_prev_passes.get(key) != current:
                changed = True
                self.esp_prev_passes[key] = current

        return changed

    def _esp_satellite_records(self):
        """Готовит записи SAT|... для ESP32 в порядке ближайших пролётов."""

        selected = [
            sat
            for sat in self.enabled_table_satellites()
            if sat.esp_transmit
        ]

        def pass_key(sat):
            return (
                sat.next_pass.timestamp()
                if sat.next_pass is not None
                else float("inf")
            )

        selected.sort(key=pass_key)

        # Прошивка ESP32 принимает не более MAX_SATELLITES КА.
        selected = selected[:MAX_ESP_SATELLITES]

        records = []

        for sat in selected:
            records.append({
                "name": sat.name,
                "norad": str(sat.norad),
                "sma": self._esp_sma(sat),
                "period": self._esp_period(sat),
                "incl": self._esp_inclination(sat),
                "raan": self._esp_raan(sat),
                "pass": self._esp_pass(sat),
                "rotations": self._esp_rotations(sat),
                "ltan": self._esp_ltan(sat),
                "color": sat.color.name(),
            })

        return records

    @staticmethod
    def _rus_decimal(value: float, decimals: int) -> str:
        """Форматирует число в русском стиле: разделитель тысяч — пробел,
        десятичная запятая."""
        text = f"{value:.{decimals}f}"
        int_part, _, frac = text.partition(".")
        sign = ""
        if int_part.startswith("-"):
            sign = "-"
            int_part = int_part[1:]
        int_part = f"{int(int_part):,}".replace(",", " ")
        return f"{sign}{int_part},{frac}"

    @staticmethod
    def _delta_text(delta, precision: int) -> str:
        if delta is None:
            return ""
        sign = "+" if delta > 0 else ""
        return f"({sign}{MainWidget._rus_decimal(delta, precision)})"

    def _esp_sma(self, sat) -> str:
        return (
            f"{self._rus_decimal(sat.mean_altitude, 1)} km"
            f"{self._delta_text(sat.orbit_change_72h, 2)}"
        )

    def _esp_period(self, sat) -> str:
        minutes = int(sat.period)
        seconds = int(round((sat.period - minutes) * 60))
        if seconds == 60:
            minutes += 1
            seconds = 0
        return f"{minutes} min {seconds} s"

    def _esp_inclination(self, sat) -> str:
        return (
            f"{self._rus_decimal(sat.inclination, 1)}°"
            f"{self._delta_text(sat.inclination_change_72h, 2)}"
        )

    def _esp_raan(self, sat) -> str:
        return (
            f"{self._rus_decimal(sat.raan, 1)}°"
            f"{self._delta_text(sat.raan_change_per_day, 2)}"
        )

    def _esp_pass(self, sat) -> str:
        if sat.next_pass is None:
            return "—"
        return sat.next_pass.astimezone(self.timezone).strftime("%H:%M")

    def _esp_ltan(self, sat) -> str:
        """Местное солнечное время восходящего узла (LTAN).

        LTAN = 12 ч + (RAAN - RA_Солнца) / 15°/ч по модулю 24 ч.
        """
        sun_ra = self.astronomy.solar_ra()
        ltan_hours = (12 + (sat.raan - sun_ra) / 15.0) % 24.0
        total_minutes = int(round(ltan_hours * 60)) % (24 * 60)
        return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"

    def _esp_rotations(self, sat) -> str:
        return str(self.satellites.total_rotations(sat))

    def _sorted_table_satellites(self):
        """Возвращает видимые спутники в порядке сортировки таблицы."""
        return sort_satellites(
            self.enabled_table_satellites(),
            self.sort_column,
            self.sort_ascending
        )

    def _column_at_x(self, x) -> int | None:
        """Возвращает индекс колонки по логической координате x."""

        starts = [
            column[1]
            for column in self.table_columns
        ]

        for index, start in enumerate(starts):
            end = (
                starts[index + 1]
                if index + 1 < len(starts)
                else self.map_width
            )
            if start <= x < end:
                return index

        return None

    def toggle_table_sort(self, position) -> bool:

        if self.scale == 0:
            return False

        x = position.x() / self.scale
        y = position.y() / self.scale

        header_top = self.map_height
        header_bottom = self.map_height + self.table_header_height

        if not (header_top <= y <= header_bottom):
            return False

        index = self._column_at_x(x)

        if index is None:
            return False

        column_key = self.table_columns[index][2]

        # Колонка чекбоксов не сортируется
        if column_key is None:
            return False

        if self.sort_column == column_key:
            self.sort_ascending = not self.sort_ascending
        else:
            self.sort_column = column_key
            self.sort_ascending = True

        self.table_scroll_offset = 0
        self.table_scrollbar.setValue(0)
        self.layout_table_scrollbar()
        self.update()
        return True

    def changeEvent(self, event):

        super().changeEvent(event)

        # Разворачивание окна изменяет его пропорции. Возвращаем прежний
        # размер, чтобы кнопка не включала растянутый полноэкранный режим.
        if event.type() == QEvent.WindowStateChange and self.isMaximized():
            QTimer.singleShot(0, self.showNormal)

    def show_update_notice(self, text=None):

        if text is not None:
            self.notice_text = text

        self.notice_color = QColor(25, 100, 55, 225)
        self.update_notice_visible = True
        self.update_notice_timer.start(60_000)
        self.update()

    def hide_update_notice(self):

        self.update_notice_visible = False
        self.update()

    def handle_gp_received(self, updated_count):
        """Вызывается из основного потока GUI после того, как расширение Chrome
        прислало новые GP-данные (записался новый gp.json). Пересобирает модели
        спутников (вместе с историей орбит) и обновляет виджет."""
        if self.receiver is not None:
            self.receiver.set_needs_update(False)
        if self.satellites.get_satellites():
            self.satellites.update_tles()
        else:
            self.satellites.load_satellites()
        self.satellites.update()
        self.update()
        self.show_update_notice(
            f"GP-данные обновлены расширением "
            f"({updated_count} записей)"
        )
        self._update_esp_pass_tracking()
        self.request_esp_send()

    def update_scene(self):

        now = datetime.now(timezone.utc)

        # Каждое обновление экрана проверяем прижатие к правому верхнему
        # углу рабочей области текущего монитора.
        self._ensure_pinned_top_right()

        # Компьютер мог спать
        if (now - self.last_update).total_seconds() > 900:
            self.refresh_sources()
            self.satellites.reset_history()

        self.last_update = now

        self.satellites.update()
        if self.map_options.get("show_day_night", True):
            self.night_mask = self.create_night_mask()

        if self._update_esp_pass_tracking():
            self.request_esp_send()

        self.update()

    def update_tles(self):
        self.refresh_sources()

    def paintEvent(self, event):

        painter = QPainter(self)

        painter.setRenderHint(QPainter.Antialiasing)

        painter.save()
        painter.scale(self.scale, self.scale)

        self.draw_background(painter)

        if self.night_mask:
            painter.drawImage(
                QRect(
                    0,
                    0,
                    self.map_width,
                    self.map_height
                ),
                self.night_mask
            )

        if self.map_options.get("show_day_night", True):
            self.draw_sun(painter)
        if self.map_options.get("show_grid", True):
            self.draw_grid(painter)
        self.draw_observer(painter)
        if self.map_options.get("show_orbits", True):
            self.draw_orbits(painter)
        if self.map_options.get("show_tracks", True):
            self.draw_tracks(painter)
        self.draw_satellites(painter)
        self.draw_status_panel(painter)
        self.draw_update_notice(painter)

        painter.restore()

        painter.end()

    def draw_background(self, painter: QPainter):
        self.draw_day_map(painter)
        # self.draw_night_map(painter)

    def draw_day_map(self, painter):

        if self.earth_day.isNull():
            painter.fillRect(
                self.rect(),
                QColor(20, 20, 20)
            )

            return

        painter.drawPixmap(
            QRect(
                0,
                0,
                self.map_width,
                self.map_height
            ),
            self.earth_day
        )

    def create_night_mask(self):

        width = self.map_width
        height = self.map_height

        image = (
            self.earth_night
            .scaled(
                width,
                height,
                Qt.IgnoreAspectRatio,
                Qt.SmoothTransformation
            )
            .toImage()
            .convertToFormat(QImage.Format_ARGB32)
        )

        solar_lat, solar_lon = self.astronomy.solar_subpoint()
        latitudes = np.deg2rad(90.0 - 180.0 * np.arange(height) / height)
        longitudes = np.deg2rad(360.0 * np.arange(width) / width - 180.0)
        solar_lat = radians(solar_lat)
        solar_lon = radians(solar_lon)

        dot = (
            np.cos(latitudes)[:, None] * np.cos(longitudes - solar_lon)
            * np.cos(solar_lat)
            + np.sin(latitudes)[:, None] * np.sin(solar_lat)
        )
        twilight = 0.12
        alpha = np.clip(
            255 * (twilight - dot) / (2 * twilight),
            0,
            255
        ).astype(np.uint8)
        alpha_image = QImage(
            alpha.tobytes(),
            width,
            height,
            width,
            QImage.Format_Alpha8
        ).copy()
        image.setAlphaChannel(alpha_image)

        return image

    def draw_sun(self, painter):

        lat, lon = self.astronomy.solar_subpoint()

        x, y = self.satellites.latlon_to_xy(
            lat,
            lon,
            self.map_width,
            self.map_height
        )

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 230, 0))

        painter.drawEllipse(
            QPointF(x, y),
            6,
            6
        )

        # painter.setPen(QColor("white"))
        # painter.drawText(
        #     x + 10,
        #     y - 10,
        #     "Sun"
        # )

    def draw_grid(self, painter: QPainter):

        pen = QPen(QColor(255, 255, 255, 50))
        pen.setWidth(1)
        painter.setPen(pen)

        w = self.map_width
        h = self.map_height

        # Параллели
        for lat in range(-60, 90, 30):
            y = int((90 - lat) / 180 * h)
            painter.drawLine(0, y, w, y)

        # Меридианы
        for lon in range(-150, 180, 30):
            x = int((lon + 180) / 360 * w)
            painter.drawLine(x, 0, x, h)

        # Экватор
        pen = QPen(QColor(255, 255, 255, 120))
        pen.setWidth(2)
        painter.setPen(pen)

        y = h // 2
        painter.drawLine(0, y, w, y)

        # Нулевой меридиан
        x = w // 2
        painter.drawLine(x, 0, x, h)

    def draw_satellites(self, painter):

        painter.setPen(Qt.NoPen)

        for sat in self.satellites.get_satellites():

            if not sat.enabled or not sat.map_visible:
                continue

            x, y = self.satellites.latlon_to_xy(
                sat.latitude,
                sat.longitude,
                self.map_width,
                self.map_height
            )

            painter.setBrush(QColor(sat.color))

            painter.drawEllipse(
                int(x) - 4,
                int(y) - 4,
                8,
                8
            )

            if self.map_options.get("show_labels", True) and sat.show_label:

                painter.setPen(Qt.white)

                metrics = painter.fontMetrics()

                text_width = metrics.horizontalAdvance(sat.name)

                margin = 10

                # По умолчанию подпись справа
                text_x = int(x) + 8

                # Если справа не помещается — переносим влево
                if text_x + text_width > self.map_width - margin:
                    text_x = int(x) - text_width - 8

                # Защита от выхода за левую границу
                if text_x < margin:
                    text_x = margin

                painter.drawText(
                    text_x,
                    int(y) - 8,
                    sat.name
                )

                painter.setPen(Qt.NoPen)

    def draw_tracks(self, painter: QPainter):

        for sat in self.satellites.satellites:

            if not sat.enabled or not sat.map_visible or not sat.show_track:
                continue

            if len(sat.track) < 2:
                continue

            previous = None
            count = len(sat.track)

            for i, point in enumerate(sat.track):

                if previous is None:
                    previous = point
                    continue

                lat1, lon1 = previous
                lat2, lon2 = point

                # разрыв на линии смены дат
                if abs(lon2 - lon1) > 180:
                    previous = point
                    continue

                x1, y1 = self.satellites.latlon_to_xy(
                    lat1,
                    lon1,
                    self.map_width,
                    self.map_height
                )

                x2, y2 = self.satellites.latlon_to_xy(
                    lat2,
                    lon2,
                    self.map_width,
                    self.map_height
                )

                color = QColor(sat.color)
                color.setAlpha(int(255 * i / count))

                pen = QPen(color)
                width = 0.5 + 3.5 * i / count

                pen.setWidthF(width)
                pen.setCapStyle(Qt.RoundCap)

                painter.setPen(pen)

                painter.drawLine(
                    QPointF(x1, y1),
                    QPointF(x2, y2)
                )

                previous = point

    def draw_orbits(self, painter):
        for sat in self.satellites.satellites:

            if not sat.enabled or not sat.map_visible:
                continue

            if not sat.show_orbit:
                continue

            if len(sat.orbit) < 2:
                continue

            center = len(sat.orbit) // 2

            past = sat.orbit[:center + 1]
            future = sat.orbit[center:]

            # Прошедшая часть
            past_pen = QPen(sat.color)
            past_pen.setWidth(1)
            past_pen.setStyle(Qt.DashLine)

            past_color = QColor(sat.color)
            past_color.setAlpha(80)
            past_pen.setColor(past_color)

            # Будущая часть
            future_pen = QPen(sat.color)
            future_pen.setWidth(1)
            future_pen.setStyle(Qt.SolidLine)

            painter.setBrush(Qt.NoBrush)

            painter.setPen(past_pen)
            self.draw_orbit_segment(
                painter,
                past,
                past_pen
            )

            painter.setPen(future_pen)
            self.draw_orbit_segment(
                painter,
                future,
                future_pen
            )

    def draw_orbit_segment(self, painter, points, color):

        if len(points) < 2:
            return

        path = QPainterPath()

        started = False
        previous_lon = None

        for lat, lon in points:

            x, y = self.satellites.latlon_to_xy(
                lat,
                lon,
                self.map_width,
                self.map_height
            )

            if not started:
                path.moveTo(x, y)
                started = True

            elif abs(lon - previous_lon) > 180:
                path.moveTo(x, y)

            else:
                path.lineTo(x, y)

            previous_lon = lon

        painter.setPen(color)
        painter.drawPath(path)

    def draw_polyline(self, painter, points):

        if len(points) < 2:
            return

        width = self.map_width
        height = self.map_height

        for i in range(len(points) - 1):

            lat1, lon1 = points[i]
            lat2, lon2 = points[i + 1]

            x1, y1 = self.satellites.latlon_to_xy(
                lat1,
                lon1,
                width,
                height
            )

            x2, y2 = self.satellites.latlon_to_xy(
                lat2,
                lon2,
                width,
                height
            )

            # Разрыв на линии смены дат
            if abs(x2 - x1) > width / 2:
                continue

            painter.drawLine(
                QPointF(x1, y1),
                QPointF(x2, y2)
            )

    def draw_status_panel(self, painter: QPainter):

        y0 = self.map_height

        # Фон панели
        painter.fillRect(
            0,
            y0,
            self.map_width,
            self.status_height,
            QColor(35, 35, 35, 220)
        )

        # Верхняя граница
        painter.setPen(QPen(QColor(90, 90, 90)))
        painter.drawLine(
            0,
            y0,
            self.map_width,
            y0
        )

        header_h = self.table_header_height

        painter.setPen(Qt.white)

        font = painter.font()
        font.setBold(True)
        font.setPointSize(9)
        painter.setFont(font)

        for index, (text, x, column_key) in enumerate(self.table_columns):
            painter.drawText(
                x,
                y0 + 18,
                text
            )

            if column_key is not None and column_key == self.sort_column:
                marker = "▲" if self.sort_ascending else "▼"
                painter.drawText(
                    x + painter.fontMetrics().horizontalAdvance(text) + 4,
                    y0 + 18,
                    marker
                )

        painter.setPen(QColor(80, 80, 80))

        painter.drawLine(
            5,
            y0 + header_h,
            self.map_width - 5,
            y0 + header_h
        )

        font.setBold(False)
        painter.setFont(font)

        row_y = y0 + header_h + 18
        rows = self._sorted_table_satellites()
        visible_rows = rows[
            self.table_scroll_offset:
            self.table_scroll_offset + self.max_table_rows
        ]

        for sat in visible_rows:

            painter.setPen(Qt.white)

            checkbox = QRectF(10, row_y - 15, 14, 14)
            painter.setBrush(QColor(25, 25, 25))
            painter.drawRect(checkbox)
            if sat.map_visible:
                painter.setPen(QPen(QColor(80, 220, 120), 2))
                painter.drawLine(13, row_y - 8, 16, row_y - 5)
                painter.drawLine(16, row_y - 5, 22, row_y - 12)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(Qt.white)

            self.draw_gear_icon(
                painter,
                QPointF(33, row_y - 8),
                5.0
            )
            painter.setBrush(Qt.NoBrush)
            painter.setPen(Qt.white)

            name_rect = QRectF(55, row_y - 16, 160 - 55 - 8, 20)
            painter.drawText(
                name_rect,
                Qt.AlignLeft | Qt.AlignVCenter,
                painter.fontMetrics().elidedText(sat.name, Qt.ElideRight, int(name_rect.width()))
            )

            painter.drawText(160, row_y, str(sat.norad))

            x = 235

            # Основная высота
            text = f"{sat.mean_altitude:.1f} км"

            painter.setPen(Qt.white)
            metrics = painter.fontMetrics()
            painter.drawText(x, row_y, text)

            self.draw_history_delta(
                painter,
                x + metrics.horizontalAdvance(text) + 6,
                row_y,
                sat.orbit_change_72h,
                precision=2,
                colored=True
            )

            painter.setPen(Qt.white)

            painter.drawText(
                350,
                row_y,
                f"{sat.altitude:.1f} км"
            )

            painter.drawText(
                440,
                row_y,
                f"{sat.period:.2f} мин"
            )

            inclination_text = f"{sat.inclination:.2f}°"
            painter.drawText(
                530,
                row_y,
                inclination_text
            )
            self.draw_history_delta(
                painter,
                530 + metrics.horizontalAdvance(inclination_text) + 6,
                row_y,
                sat.inclination_change_72h,
                precision=2,
                colored=True
            )

            raan_text = f"{sat.raan:.2f}°"
            painter.setPen(Qt.white)
            painter.drawText(650, row_y, raan_text)
            self.draw_history_delta(
                painter,
                650 + metrics.horizontalAdvance(raan_text) + 4,
                row_y,
                sat.raan_change_per_day,
                precision=2,
                colored=False
            )

            painter.drawText(755, row_y, self._esp_ltan(sat))

            if sat.next_pass:
                value = self.format_next_pass(sat)
            else:
                value = "—"

            painter.drawText(
                830,
                row_y,
                value
            )

            row_y += 22

    def draw_gear_icon(self, painter, center, radius):

        painter.save()
        painter.translate(center.x(), center.y())
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(210, 210, 210))

        teeth = 8

        for i in range(teeth):
            painter.save()
            painter.rotate(i * 360 / teeth)
            painter.drawRect(
                QRectF(
                    radius * 0.6,
                    -radius * 0.24,
                    radius * 0.6,
                    radius * 0.48
                )
            )
            painter.restore()

        painter.drawEllipse(
            QPointF(0, 0),
            radius * 0.78,
            radius * 0.78
        )

        painter.restore()

    @staticmethod
    def draw_history_delta(painter, x, y, delta, precision, colored):
        """Рисует изменение орбитального параметра в скобках."""
        if delta is None:
            return

        metrics = painter.fontMetrics()
        painter.drawText(x, y, "(")
        x += metrics.horizontalAdvance("(")


        if colored and delta > 0:
            painter.setPen(QColor(0, 220, 0))
        elif colored and delta < 0:
            painter.setPen(QColor(220, 80, 80))
        else:
            painter.setPen(QColor(180, 180, 180))

        sign = "+" if delta > 0 else ""
        value = f"{sign}{delta:.{precision}f}"
        painter.drawText(x, y, value)
        x += metrics.horizontalAdvance(value)

        painter.setPen(Qt.white)
        painter.drawText(x, y, ")")

    def draw_update_notice(self, painter: QPainter):

        if not self.update_notice_visible:
            return

        text = self.notice_text
        padding_x = 12
        padding_y = 8
        rect = painter.fontMetrics().boundingRect(text)
        box = QRectF(
            12,
            12,
            rect.width() + padding_x * 2,
            rect.height() + padding_y * 2
        )

        painter.setPen(Qt.NoPen)
        painter.setBrush(self.notice_color)
        painter.drawRoundedRect(box, 6, 6)
        painter.setPen(Qt.white)
        painter.drawText(
            int(box.x()) + padding_x,
            int(box.y()) + padding_y + painter.fontMetrics().ascent(),
            text
        )

    def format_next_pass(self, sat):

        if sat.next_pass is None:
            return "—"

        now = datetime.now(timezone.utc)

        delta = sat.next_pass - now

        local_time = sat.next_pass.astimezone(self.timezone)

        minutes = int(delta.total_seconds() // 60)

        if minutes < 0:
            return local_time.strftime("%H:%M")

        if minutes < 60:
            return f"{local_time:%H:%M} ({minutes} мин)"

        hours = minutes // 60
        mins = minutes % 60

        return f"{local_time:%H:%M} ({hours} ч {mins} м)"

    def format_orbit(self, sat) -> str:

        if sat.orbit_delta is None:
            return f"{sat.mean_altitude:.1f} км"

        delta = sat.orbit_delta

        # Игнорируем изменения меньше 100 метров
        if abs(delta) < 0.1:
            return f"{sat.mean_altitude:.1f} км"

        sign = "+" if delta > 0 else ""

        return f"{sat.mean_altitude:.1f} км ({sign}{delta:.1f})"

    def draw_observer(self, painter: QPainter):

        observer = self.config.observer

        x, y = self.satellites.latlon_to_xy(
            observer["lat"],
            observer["lon"],
            self.map_width,
            self.map_height
        )

        painter.save()

        # Белая точка
        painter.setPen(Qt.NoPen)
        painter.setBrush(Qt.white)
        painter.drawEllipse(QPointF(x, y), 3.5, 3.5)

        # Тонкая чёрная окантовка для читаемости
        painter.setPen(QPen(Qt.black, 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(QPointF(x, y), 3.5, 3.5)

        painter.restore()
