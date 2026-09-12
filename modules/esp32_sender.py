"""Отправка снимка спутников на ESP32 SatWidget (USB CDC или BLE).

Порт протокола из ESP32_SatWidget/satellite_sender.py: виджет передаёт
список строк SAT|... между BEGIN/CONFIG и END. Перед текстовым снимком
при необходимости загружаются изображения КА из resources/satellites
(файл <NORAD>.rgb565, RGB565LE 150x150) по протоколу IMG_*.
"""
import asyncio
import base64
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

BLE_NAME = "ESP32_SatWidget"
BLE_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
BLE_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
BLE_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
BLE_CHUNK_SIZE = 20

IMAGE_WIDTH = 150
IMAGE_HEIGHT = 150
IMAGE_BYTES = IMAGE_WIDTH * IMAGE_HEIGHT * 2
IMAGE_CHUNK_BYTES = 96
# The firmware accepts this acknowledgement window and has a 4 KiB BLE ring.
# One IMG_NEXT per 12 data lines removes almost all BLE round-trip overhead.
BLE_IMAGE_BATCH_LINES = 12
BLE_IMAGE_PACKET_PAUSE_SECONDS = 0.003


def image_directory() -> Path:
    """Папка с изображениями КА (resources/satellites) в исходной и
    собранной (PyInstaller) версиях."""
    candidates = [
        Path(__file__).resolve().parent.parent / "resources" / "satellites",
        Path.cwd() / "resources" / "satellites",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


IMAGE_DIRECTORY = image_directory()
TEMPLATE_IMAGE_NAME = "template.rgb565"


# Манифест хранит NORAD ID временных картинок на устройстве.  Старый формат
# (JSON-список) обозначал сгенерированные заглушки; новый формат отдельно
# помнит template.rgb565, чтобы один раз заменить старые заглушки шаблоном.
def placeholder_manifest_path() -> Path:
    return Path.home() / ".satwidget" / "esp_placeholder_uploads.json"


def _hex_to_rgb(color_hex: str):
    text = (color_hex or "").strip()
    if text.startswith("#") and len(text) == 7:
        try:
            return tuple(int(text[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            pass
    return 128, 128, 128


def _brighten(cr, cg, cb, factor=1.35):
    return tuple(min(255, int(c * factor)) for c in (cr, cg, cb))


def placeholder_image_bytes(color_hex: str) -> bytes:
    """Генерирует 150x150 RGB565LE карточку-заглушку цвета КА.

    Рисует силуэт спутника с солнечными панелями и кольцом орбиты, чтобы
    на устройстве у каждого КА была своя различимая иконка до появления
    реального фото в resources/satellites.
    """
    r, g, b = _hex_to_rgb(color_hex)
    width, height = IMAGE_WIDTH, IMAGE_HEIGHT
    data = bytearray(IMAGE_BYTES)

    def set_pixel(x, y, cr, cg, cb):
        value = ((cr & 0xF8) << 8) | ((cg & 0xFC) << 3) | (cb >> 3)
        index = (y * width + x) * 2
        data[index] = value & 0xFF
        data[index + 1] = (value >> 8) & 0xFF

    cx, cy = width // 2, height // 2

    for y in range(height):
        for x in range(width):
            # Фон панели.
            set_pixel(x, y, 0x22, 0x22, 0x22)
            # Кольцо орбиты вокруг спутника.
            distance = math.hypot(x - cx, y - cy)
            if 42 <= distance <= 48:
                set_pixel(x, y, r, g, b)

    # Солнечные панели.
    pr, pg, pb = _brighten(r, g, b)
    for y in range(60, 71):
        for x in list(range(22, 58)) + list(range(73, 109)):
            set_pixel(x, y, pr, pg, pb)

    # Корпус — сплюснутый эллипс.
    for y in range(cy - 20, cy + 21):
        for x in range(cx - 26, cx + 27):
            dx = (x - cx) / 26.0
            dy = (y - cy) / 20.0
            if dx * dx + dy * dy <= 1.0:
                set_pixel(x, y, r, g, b)

    # Блик-«объектив» в центре корпуса.
    for y in range(cy - 4, cy + 5):
        for x in range(cx - 4, cx + 5):
            if (x - cx) ** 2 + (y - cy) ** 2 <= 16:
                set_pixel(x, y, 0xF0, 0xF0, 0xF0)

    return bytes(data)

# Подстроки в описании COM-порта, указывающие на ESP32 (USB CDC/UART).
SERIAL_HINT_KEYWORDS = (
    "esp32", "esp", "usb", "cp210", "ch340", "ch341",
    "cdc", "uart", "serial",
)


class BleTransport:
    """Синхронный транспорт поверх BLE (Nordic UART Service).

    Имеет тот же интерфейс write()/readline()/close(), что и pyserial.
    bleak работает на выделенном asyncio-цикле в фоновом потоке.
    """

    def __init__(self, name, timeout=2.0):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            raise ImportError(
                "bleak не установлен. Установите: pip install bleak"
            )

        self.__client = None
        self.__loop = None
        self.__thread = None
        self.scanner = BleakScanner
        self.BleakClient = BleakClient
        self.timeout = timeout
        self.name = name

        self.__rx_buffer = bytearray()
        self.__rx_lock = threading.Lock()
        self.__image_write_size = BLE_CHUNK_SIZE

    def _notify_callback(self, sender, data):
        with self.__rx_lock:
            self.__rx_buffer.extend(data)

    def connect(self, timeout=10.0):
        loop = asyncio.new_event_loop()
        self.__loop = loop

        def run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self.__thread = threading.Thread(target=run, daemon=True)
        self.__thread.start()

        self.__connect_event = threading.Event()
        self.__connect_error = None

        loop.call_soon_threadsafe(self.__connect_async, timeout)

        if not self.__connect_event.wait(timeout=timeout):
            raise TimeoutError(f"BLE device '{self.name}' не найден")

        if self.__client is None:
            if self.__connect_error is not None:
                raise self.__connect_error
            raise TimeoutError(f"BLE device '{self.name}' не найден")

    def __connect_async(self, timeout):
        async def _connect():
            try:
                device = await self.scanner.find_device_by_name(
                    self.name,
                    timeout=timeout
                )
                if device is None:
                    raise TimeoutError(f"BLE device '{self.name}' не найден")
                client = self.BleakClient(device, timeout=self.timeout)
                await client.connect()
                characteristic = client.services.get_characteristic(BLE_RX_UUID)
                if characteristic is not None:
                    # Windows exposes the value negotiated with the ESP32;
                    # retain 20 bytes as a safe fallback on older adapters.
                    self.__image_write_size = max(
                        BLE_CHUNK_SIZE,
                        min(512, characteristic.max_write_without_response_size),
                    )
                # Характеристика TX поддерживает только уведомления,
                # поэтому подписываемся на них сразу после подключения.
                await client.start_notify(
                    BLE_TX_UUID,
                    self._notify_callback
                )
                self.__client = client
            except Exception as error:
                self.__connect_error = error
            finally:
                self.__connect_event.set()

        asyncio.ensure_future(_connect())

    async def __disconnect(self):
        if self.__client is not None:
            try:
                await self.__client.stop_notify(BLE_TX_UUID)
            except Exception:
                pass
            await self.__client.disconnect()
            self.__client = None

    def write(self, data):
        if self.__client is None:
            raise RuntimeError("BLE client is not connected")

        chunks = [
            data[i:i + self.__image_write_size]
            for i in range(0, len(data), self.__image_write_size)
        ]
        loop = self.__loop
        event = threading.Event()
        errors = []

        def do_write():
            async def _write_chunks():
                try:
                    for chunk in chunks:
                        await self.__client.write_gatt_char(
                            BLE_RX_UUID,
                            chunk,
                            response=True
                        )
                except Exception as error:
                    errors.append(error)
                finally:
                    event.set()

            asyncio.ensure_future(_write_chunks())

        loop.call_soon_threadsafe(do_write)

        if not event.wait(timeout=self.timeout * len(chunks) + 2):
            raise TimeoutError("BLE: превышено время записи")

        if errors:
            raise errors[0]

    def write_image_data(self, data):
        """Sends one IMG_DATA line using BLE write commands.

        Image lines are explicitly paced by the IMG_NEXT reply, so waiting for
        a GATT response for each 20-byte fragment only makes the first upload
        appear frozen (roughly 3,400 requests per 150x150 image).
        """
        if self.__client is None:
            raise RuntimeError("BLE client is not connected")

        chunks = [
            data[i:i + BLE_CHUNK_SIZE]
            for i in range(0, len(data), BLE_CHUNK_SIZE)
        ]
        loop = self.__loop
        event = threading.Event()
        errors = []

        def do_write():
            async def _write_chunks():
                try:
                    for chunk in chunks:
                        await self.__client.write_gatt_char(
                            BLE_RX_UUID,
                            chunk,
                            response=False,
                        )
                        # Without response, Windows can queue packets much
                        # faster than the ESP32 application drains them.
                        if self.__image_write_size <= BLE_CHUNK_SIZE:
                            await asyncio.sleep(BLE_IMAGE_PACKET_PAUSE_SECONDS)
                except Exception as error:
                    errors.append(error)
                finally:
                    event.set()

            asyncio.ensure_future(_write_chunks())

        loop.call_soon_threadsafe(do_write)
        if not event.wait(timeout=self.timeout + 2):
            raise TimeoutError("BLE: превышено время записи изображения")
        if errors:
            raise errors[0]

    def flush(self):
        # write() has already awaited the queued GATT operation.  Sleeping
        # here once per image line added about 35 seconds to every image.
        pass

    def readline(self):
        if self.__client is None:
            raise RuntimeError("BLE client is not connected")

        deadline = time.monotonic() + self.timeout

        while time.monotonic() < deadline:
            with self.__rx_lock:
                index = self.__rx_buffer.find(b"\n")
                if index >= 0:
                    line = bytes(self.__rx_buffer[:index])
                    del self.__rx_buffer[:index + 1]
                    return line
            time.sleep(0.05)

        return b""

    def close(self):
        """Completes the BLE disconnect before stopping its event loop.

        The ESP32 starts advertising again from its disconnect callback.  The
        previous fire-and-forget shutdown could stop asyncio before that
        callback ran, so an automatic refresh immediately after a send could
        not reconnect while a manual retry later worked.
        """
        if self.__loop is not None and self.__client is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.__disconnect(), self.__loop
                )
                future.result(timeout=5)
            except Exception:
                # The connection may already have dropped; stopping the local
                # loop remains safe and the next attempt will scan again.
                self.__client = None
        if self.__loop is not None:
            self.__loop.call_soon_threadsafe(self.__loop.stop)
        if self.__thread is not None:
            self.__thread.join(timeout=2.0)


class Esp32Sender:
    """Отправляет снимок спутников на ESP32 через BLE или USB CDC."""

    def __init__(self, minutes=1):
        self.minutes = minutes
        (
            self._placeholder_uploads,
            self._template_uploads,
        ) = self._load_placeholder_manifest()
        self._ble_retry_delay = 0.75

    def _snapshot_lines(self, records):
        lines = [
            "BEGIN",
            f"CONFIG|{self.minutes}",
            f"TIME|{datetime.now():%H:%M}",
        ]
        for record in records:
            values = [record.get(key, "") for key in (
                "name", "norad", "sma", "period", "incl", "raan",
                "pass", "rotations", "ltan", "color",
            )]
            if any("|" in value or "\n" in value for value in values):
                raise ValueError(
                    "Значения спутника не могут содержать | или перевод строки"
                )
            lines.append("SAT|" + "|".join(values))
        lines.append("END")
        return lines

    def _wait_for_reply(self, transport, prefix, timeout=10):
        """Ждёт строку, начинающуюся с prefix, игнорируя промежуточные."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = (
                transport.readline()
                .decode("utf-8", errors="replace")
                .strip()
            )
            if not reply:
                continue
            if reply.startswith("IMG_ERROR|"):
                raise RuntimeError(reply)
            if reply.startswith(prefix):
                return reply
        raise TimeoutError(f"ESP32 не ответил: {prefix}")

    def _local_image_path(self, norad) -> Path:
        return IMAGE_DIRECTORY / f"{norad}.rgb565"

    @staticmethod
    def _template_image_path() -> Path:
        return IMAGE_DIRECTORY / TEMPLATE_IMAGE_NAME

    @staticmethod
    def _load_placeholder_manifest() -> tuple[set, set]:
        path = placeholder_manifest_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return (
                    {str(norad) for norad in raw.get("generated", [])},
                    {str(norad) for norad in raw.get("template", [])},
                )
            # Compatibility with manifests written before template.rgb565.
            return {str(norad) for norad in raw}, set()
        except (OSError, ValueError):
            return set(), set()

    @staticmethod
    def _save_placeholder_manifest(generated_ids: set, template_ids: set) -> None:
        path = placeholder_manifest_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({
                    "generated": sorted(generated_ids),
                    "template": sorted(template_ids),
                }, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    def _template_image_bytes(self):
        path = self._template_image_path()
        if not path.exists():
            return None
        image_data = path.read_bytes()
        if len(image_data) == IMAGE_BYTES:
            return image_data
        print(f"ESP32: {path} неверного размера — игнорирую шаблон")
        return None

    def _image_bytes_for_record(self, record):
        """Возвращает (данные, временное ли это изображение, это template)."""
        norad = record.get("norad", "")
        path = self._local_image_path(norad)
        if path.exists():
            image_data = path.read_bytes()
            if len(image_data) == IMAGE_BYTES:
                return image_data, False, False
            print(
                f"ESP32: {path} неверного размера — использую заглушку"
            )
        template_image = self._template_image_bytes()
        if template_image is not None:
            return template_image, True, True
        return placeholder_image_bytes(record.get("color", "#808080")), True, False

    def _stored_image_ids(self, transport) -> set:
        """Возвращает NORAD ID изображений, уже сохранённых на ESP32."""
        transport.write(b"IMG_STATUS\n")
        transport.flush()
        reply = self._wait_for_reply(transport, "IMG_STATUS|")
        fields = reply.split("|", 2)
        if len(fields) < 2 or fields[1] != "OK":
            raise RuntimeError(f"ESP32: хранилище изображений недоступно: {reply}")
        return set(fields[2].split(",")) if len(fields) == 3 and fields[2] else set()

    def _send_image(self, transport, norad, image_data=None):
        """Загружает изображение <NORAD>.rgb565 на ESP32 по протоколу IMG_*."""
        if image_data is None:
            image_path = self._local_image_path(norad)
            image_data = image_path.read_bytes()
        if len(image_data) != IMAGE_BYTES:
            raise ValueError(
                f"{norad} должен быть ровно {IMAGE_BYTES} байт "
                f"({IMAGE_WIDTH}x{IMAGE_HEIGHT} RGB565)"
            )

        transport.write(
            f"IMG_BEGIN|{norad}|{IMAGE_BYTES}|{BLE_IMAGE_BATCH_LINES}\n".encode("ascii")
        )
        transport.flush()
        self._wait_for_reply(transport, f"IMG_READY|{norad}")

        lines = [
            b"IMG_DATA|" + base64.b64encode(
                image_data[offset:offset + IMAGE_CHUNK_BYTES]
            ) + b"\n"
            for offset in range(0, len(image_data), IMAGE_CHUNK_BYTES)
        ]
        write_image_data = getattr(transport, "write_image_data", None)
        next_progress_percent = 10
        for start in range(0, len(lines), BLE_IMAGE_BATCH_LINES):
            batch = lines[start:start + BLE_IMAGE_BATCH_LINES]
            if write_image_data is None:
                for line in batch:
                    transport.write(line)
            else:
                # BLE Write Without Response stays fast, but a bounded batch
                # prevents overflowing the ESP32 receive ring.
                write_image_data(b"".join(batch))
            transport.flush()
            self._wait_for_reply(transport, "IMG_NEXT")
            progress_percent = min(100, (start + len(batch)) * 100 // len(lines))
            if progress_percent >= next_progress_percent or progress_percent == 100:
                print(f"ESP32: изображение {norad}: {progress_percent}%")
                next_progress_percent += 10

        transport.write(b"IMG_END\n")
        transport.flush()
        self._wait_for_reply(transport, f"IMG_OK|{norad}")

    def _send_update(self, transport, records):
        """Дозагружает недостающие изображения и отправляет текстовый снимок."""
        try:
            stored = self._stored_image_ids(transport)
        except Exception as error:
            print("ESP32:", error)
            stored = None

        if stored is not None:
            for record in records:
                norad = record.get("norad", "")
                placeholder_uploaded = norad in self._placeholder_uploads
                template_uploaded = norad in self._template_uploads
                real_image_path = self._local_image_path(norad)
                real_file_exists = (
                    real_image_path.is_file()
                    and real_image_path.stat().st_size == IMAGE_BYTES
                )
                template_available = self._template_image_bytes() is not None

                # Перезаливаем временную картинку, когда появилось реальное
                # фото, либо мигрируем старую сгенерированную заглушку на
                # template.rgb565. Неизвестные файлы на устройстве сохраняем.
                if norad in stored and not (
                        placeholder_uploaded and real_file_exists
                ) and not (
                        placeholder_uploaded and template_available
                        and not template_uploaded
                ):
                    continue

                image_data, is_placeholder, is_template = self._image_bytes_for_record(record)
                print(
                    f"ESP32: загрузка изображения {norad}"
                    + (" (заглушка)" if is_placeholder else "")
                )
                self._send_image(transport, norad, image_data)
                stored.add(norad)

                if is_placeholder:
                    self._placeholder_uploads.add(norad)
                    if is_template:
                        self._template_uploads.add(norad)
                    else:
                        self._template_uploads.discard(norad)
                else:
                    self._placeholder_uploads.discard(norad)
                    self._template_uploads.discard(norad)
                self._save_placeholder_manifest(
                    self._placeholder_uploads, self._template_uploads
                )

        lines = self._snapshot_lines(records)
        transport.write(("\n".join(lines) + "\n").encode("utf-8"))
        transport.flush()
        return self._wait_for_reply(transport, "OK|", timeout=3)

    @staticmethod
    def _serial_ports():
        try:
            from serial.tools import list_ports
        except ImportError:
            return []
        return list(list_ports.comports())

    def _send_serial(self, records):
        import serial

        ports = self._serial_ports()

        if not ports:
            return None

        def looks_like_esp32(port):
            description = (port.description or "").lower()
            return any(
                keyword in description
                for keyword in SERIAL_HINT_KEYWORDS
            )

        candidates = [p for p in ports if looks_like_esp32(p)]

        # Если подходящих по описанию портов нет, но подключён ровно
        # один — пробуем его.
        if not candidates and len(ports) == 1:
            candidates = ports

        for port in candidates:
            try:
                with serial.Serial(
                        port.device,
                        115200,
                        timeout=1,
                        rtscts=False,
                        dsrdtr=False
                ) as device:
                    # DTR/RTS задействованы в автосбросе ESP32 — оставляем
                    # неактивными, иначе закрытие COM-порта перезагрузит
                    # плату и сотрёт снимок из RAM.
                    device.dtr = False
                    device.rts = False
                    time.sleep(0.8)
                    device.reset_input_buffer()
                    return self._send_update(device, records)
            except Exception:
                continue

        return None

    def _send_ble_once(self, records):
        transport = BleTransport(BLE_NAME)
        try:
            transport.connect(timeout=10.0)
            return self._send_update(transport, records)
        finally:
            transport.close()

    def _send_ble(self, records):
        """Sends over BLE with one retry for the ESP32 advertising handoff."""
        last_error = None
        for attempt in range(2):
            try:
                return self._send_ble_once(records)
            except Exception as error:
                last_error = error
                if attempt == 0:
                    time.sleep(self._ble_retry_delay)
        raise RuntimeError(
            f"BLE connection failed after retry: {last_error!r}"
        )

    def send(self, records):
        """Отправляет снимок. Возвращает (результат, список логов)."""
        logs = []

        try:
            reply = self._send_ble(records)
            logs.append(f"BLE: {reply}")
            return "ble", logs
        except Exception as error:
            logs.append(f"BLE: {error}")

        try:
            reply = self._send_serial(records)
            if reply is None:
                logs.append("Serial: COM-порт ESP32 не найден")
                return None, logs
            logs.append(f"Serial: {reply}")
            return "serial", logs
        except Exception as error:
            logs.append(f"Serial: {error}")

        return None, logs
