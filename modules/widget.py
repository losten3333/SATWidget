from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from math import radians

import numpy as np
from PySide6.QtCore import QEvent, Qt, QPointF, QRect, QRectF
from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtWidgets import QScrollBar, QWidget

from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QPixmap, QPainterPath, QPolygonF, QImage
)

from modules.astronomy import Astronomy


class SourceRefreshWorker(QThread):
    """Downloads GP/TLE without blocking the Qt event loop."""

    downloaded = Signal(bool)

    def __init__(self, satellites, parent=None):
        super().__init__(parent)
        self.satellites = satellites

    def run(self):
        self.downloaded.emit(self.satellites.download_sources())


class MainWidget(QWidget):

    def __init__(self, config, satellites):

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
        self.source_refresh_worker = None
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


        self.table_scroll_offset = 0

        self.status_height = (
                HEADER_HEIGHT +
                ROW_HEIGHT * (self.max_table_rows - 1) +
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
        self.setWindowTitle("SAT Widget")

        self.move(
            display.get("x", 20),
            display.get("y", 20)
        )

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

        self.satellites.update()
        if self.map_options.get("show_day_night", True):
            self.night_mask = self.create_night_mask()

    def mousePressEvent(self, event):

        if event.button() == Qt.LeftButton and self.toggle_table_satellite(event.position()):
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

    def refresh_sources(self):

        if self.source_refresh_worker is not None:
            return

        if not self.satellites.sources_are_outdated():
            return

        self.source_refresh_worker = SourceRefreshWorker(
            self.satellites,
            self
        )
        self.source_refresh_worker.downloaded.connect(
            self.apply_downloaded_sources
        )
        self.source_refresh_worker.finished.connect(
            self.clear_source_refresh_worker
        )
        self.source_refresh_worker.start()

    def apply_downloaded_sources(self, downloaded):

        if not downloaded:
            return

        if self.satellites.get_satellites():
            self.satellites.update_tles()
        else:
            self.satellites.load_satellites()

        self.satellites.update()
        self.update()
        self.show_update_notice()

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

    def toggle_table_satellite(self, position):

        if self.scale == 0:
            return False

        x = position.x() / self.scale
        y = position.y() / self.scale
        row_top = self.map_height + self.table_header_height
        row_index = int((y - row_top) // self.table_row_height)
        checkbox = QRectF(10, row_top + row_index * self.table_row_height + 4, 14, 14)

        if row_index < 0 or not checkbox.contains(QPointF(x, y)):
            return False

        satellites = self.enabled_table_satellites()
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

    def changeEvent(self, event):

        super().changeEvent(event)

        # Разворачивание окна изменяет его пропорции. Возвращаем прежний
        # размер, чтобы кнопка не включала растянутый полноэкранный режим.
        if event.type() == QEvent.WindowStateChange and self.isMaximized():
            QTimer.singleShot(0, self.showNormal)

    def show_update_notice(self):

        self.update_notice_visible = True
        self.update_notice_timer.start(60_000)
        self.update()

    def hide_update_notice(self):

        self.update_notice_visible = False
        self.update()

    def clear_source_refresh_worker(self):

        worker = self.source_refresh_worker
        self.source_refresh_worker = None

        if worker is not None:
            worker.deleteLater()

    def update_scene(self):

        now = datetime.now(timezone.utc)

        # Компьютер мог спать
        if (now - self.last_update).total_seconds() > 900:
            self.refresh_sources()
            self.satellites.reset_history()

        self.last_update = now

        self.satellites.update()

        if self.map_options.get("show_day_night", True):
            self.night_mask = self.create_night_mask()

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

        columns = [
            ("", 10),
            ("Название", 32),
            ("NORAD ID", 155),
            ("Орбита (Δ72ч)", 245),
            ("Высота", 390),
            ("Период", 475),
            ("Наклон. (Δ72ч)", 565),
            ("RAAN (Δсут)", 715),
            ("След. пролет", 830)
        ]

        for text, x in columns:
            painter.drawText(
                x,
                y0 + 18,
                text
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
        rows = self.enabled_table_satellites()
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

            painter.drawText(32, row_y, sat.name)

            painter.drawText(155, row_y, str(sat.norad))

            x = 245

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
                390,
                row_y,
                f"{sat.altitude:.1f} км"
            )

            painter.drawText(
                475,
                row_y,
                f"{sat.period:.2f} мин"
            )

            inclination_text = f"{sat.inclination:.2f}°"
            painter.drawText(
                565,
                row_y,
                inclination_text
            )
            self.draw_history_delta(
                painter,
                565 + metrics.horizontalAdvance(inclination_text) + 6,
                row_y,
                sat.inclination_change_72h,
                precision=2,
                colored=True
            )

            raan_text = f"{sat.raan:.2f}°"
            painter.setPen(Qt.white)
            painter.drawText(715, row_y, raan_text)
            self.draw_history_delta(
                painter,
                715 + metrics.horizontalAdvance(raan_text) + 4,
                row_y,
                sat.raan_change_per_day,
                precision=2,
                colored=False
            )

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

        text = "Данные спутников обновлены"
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
        painter.setBrush(QColor(25, 100, 55, 225))
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
