#!/usr/bin/env python3
"""Send a complete demonstration snapshot to ESP32 SatWidget over USB CDC or BLE."""
import argparse
import asyncio
import base64
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

SATELLITES = [
    {
        "name": "KHAYYAM", "norad": "52940", "sma": "6 907,1 km (+0,4)",
        "period": "97 min 44 s", "incl": "97,5 deg (+0,1)", "raan": "159,2 deg",
        "pass": "00 h 27 min", "rotations": "19 842", "ltan": "10:30",
        "color": "#BF805B",
    },
    {
        "name": "KANOPUS-V-6", "norad": "45783", "sma": "6 884,5 km (-0,2)",
        "period": "95 min 53 s", "incl": "97,4 deg (-0,1)", "raan": "221,8 deg",
        "pass": "03 h 12 min", "rotations": "32 614", "ltan": "09:45",
        "color": "#4777B8",
    },
]

IMAGE_WIDTH = 150
IMAGE_HEIGHT = 150
IMAGE_BYTES = IMAGE_WIDTH * IMAGE_HEIGHT * 2
IMAGE_DIRECTORY = Path(__file__).with_name("image")

BLE_NAME = "ESP32_SatWidget"
BLE_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
BLE_RX_UUID = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
BLE_TX_UUID = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
# A single GATT write is limited by the negotiated ATT MTU. bleak on Windows
# keeps the default 23-byte MTU, so every write must stay at 20 bytes or less.
BLE_CHUNK_SIZE = 20
IMAGE_CHUNK_BYTES = 96
BLE_IMAGE_BATCH_LINES = 12
BLE_IMAGE_PACKET_PAUSE_SECONDS = 0.003


class BleTransport:
    """Synchronous transport interface over BLE (Nordic UART Service).

    Exposes the same write()/flush()/readline()/close() surface as a pyserial
    Serial object so the protocol code below works for both transports.
    bleak runs on a dedicated asyncio loop in a background thread.
    """

    def __init__(self, name, timeout=2.0):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            sys.exit("Install dependency once: python -m pip install --user bleak")
        self._BleakClient = BleakClient
        self._BleakScanner = BleakScanner
        self.name = name
        self.timeout = timeout
        self._client = None
        self._image_write_size = BLE_CHUNK_SIZE
        self._rx = queue.Queue()
        self._line = b""
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, args=(self.loop,),
                                       daemon=True, name="bleak-loop")
        self.thread.start()
        self._call(self._connect, timeout=30)

    @staticmethod
    def _run_loop(loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    def _call(self, coro, *args, timeout=None):
        future = asyncio.run_coroutine_threadsafe(coro(*args), self.loop)
        return future.result(timeout if timeout is not None else self.timeout + 5)

    async def _connect(self):
        device = await self._BleakScanner.find_device_by_name(self.name, timeout=10)
        if device is None:
            raise RuntimeError(
                f"BLE device '{self.name}' not found. "
                "Power the ESP32 on and check that it is in range.")
        self._client = self._BleakClient(device)
        await self._client.connect()
        characteristic = self._client.services.get_characteristic(BLE_RX_UUID)
        if characteristic is not None:
            self._image_write_size = max(
                BLE_CHUNK_SIZE,
                min(512, characteristic.max_write_without_response_size),
            )
        await self._client.start_notify(BLE_TX_UUID, self._on_notify)

    async def _disconnect(self):
        if self._client is not None:
            await self._client.disconnect()

    async def _write_all(self, data):
        for offset in range(0, len(data), self._image_write_size):
            chunk = bytes(data[offset:offset + self._image_write_size])
            await self._client.write_gatt_char(BLE_RX_UUID, chunk, response=True)

    async def _write_image_data(self, data):
        for offset in range(0, len(data), BLE_CHUNK_SIZE):
            chunk = bytes(data[offset:offset + BLE_CHUNK_SIZE])
            await self._client.write_gatt_char(BLE_RX_UUID, chunk, response=False)
            if self._image_write_size <= BLE_CHUNK_SIZE:
                await asyncio.sleep(BLE_IMAGE_PACKET_PAUSE_SECONDS)

    def _on_notify(self, _handle, data):
        self._rx.put(bytes(data))

    def write(self, data):
        self._call(self._write_all, data)

    def write_image_data(self, data):
        """Fast, bounded image-data transfer using Write Without Response."""
        self._call(self._write_image_data, data)

    def flush(self):
        pass

    def readline(self, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                chunk = self._rx.get(timeout=max(0.05, min(0.2, remaining)))
            except queue.Empty:
                continue
            self._line += chunk
            if b"\n" in self._line:
                line, _, self._line = self._line.partition(b"\n")
                return line + b"\n"
        return b""

    def close(self):
        try:
            self._call(self._disconnect, timeout=5)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


def serial_module():
    try:
        import serial
        from serial.tools import list_ports
        return serial, list_ports
    except ImportError:
        sys.exit("Install dependency once: python -m pip install --user pyserial")


def available_ports():
    _, list_ports = serial_module()
    return list(list_ports.comports())


def choose_port(requested):
    if requested:
        return requested
    ports = available_ports()
    if len(ports) == 1:
        return ports[0].device
    if not ports:
        sys.exit("No COM ports found. Connect the ESP32-C6 and pass --port COMx.")
    print("Available COM ports:")
    for port in ports:
        print(f"  {port.device}: {port.description}")
    sys.exit("Pass the ESP32 port explicitly, for example: python satellite_sender.py --port COM5")


def sat_record(sat):
    values = [sat[key] for key in ("name", "norad", "sma", "period", "incl", "raan",
                                    "pass", "rotations", "ltan", "color")]
    if any("|" in value or "\n" in value for value in values):
        raise ValueError("Satellite values cannot contain | or a newline")
    return "SAT|" + "|".join(values)


def wait_for_reply(transport, expected_prefix, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reply = transport.readline().decode("utf-8", errors="replace").strip()
        if reply:
            if reply != "IMG_NEXT":
                print("ESP32:", reply)
            if reply.startswith("IMG_ERROR|"):
                raise RuntimeError(reply)
            if reply.startswith(expected_prefix):
                return reply
    raise TimeoutError(f"ESP32 did not answer {expected_prefix}")


def send_image(transport, norad):
    image_path = IMAGE_DIRECTORY / f"{norad}.rgb565"
    if not image_path.exists():
        print(f"Image not found, keeping current ESP32 image: {image_path.name}")
        return
    image_data = image_path.read_bytes()
    if len(image_data) != IMAGE_BYTES:
        raise ValueError(f"{image_path} must be exactly {IMAGE_BYTES} bytes "
                         f"({IMAGE_WIDTH}x{IMAGE_HEIGHT} RGB565)")

    transport.write(
        f"IMG_BEGIN|{norad}|{IMAGE_BYTES}|{BLE_IMAGE_BATCH_LINES}\n".encode("ascii")
    )
    transport.flush()
    wait_for_reply(transport, f"IMG_READY|{norad}")
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
            write_image_data(b"".join(batch))
        transport.flush()
        wait_for_reply(transport, "IMG_NEXT")
        progress_percent = min(100, (start + len(batch)) * 100 // len(lines))
        if progress_percent >= next_progress_percent or progress_percent == 100:
            print(f"ESP32: image {norad}: {progress_percent}%")
            next_progress_percent += 10
    transport.write(b"IMG_END\n")
    transport.flush()
    wait_for_reply(transport, f"IMG_OK|{norad}")


def stored_image_ids(transport):
    transport.write(b"IMG_STATUS\n")
    transport.flush()
    reply = wait_for_reply(transport, "IMG_STATUS|")
    fields = reply.split("|", 2)
    if len(fields) < 2 or fields[1] != "OK":
        raise RuntimeError(f"ESP32 image storage is unavailable: {reply}")
    return set(fields[2].split(",")) if len(fields) == 3 and fields[2] else set()


def local_image_ids():
    return {satellite["norad"] for satellite in SATELLITES
            if (IMAGE_DIRECTORY / f"{satellite['norad']}.rgb565").exists()}


def send_snapshot(transport, minutes):
    lines = ["BEGIN", f"CONFIG|{minutes}", f"TIME|{datetime.now():%H:%M}"]
    lines.extend(sat_record(sat) for sat in SATELLITES)
    lines.append("END")
    transport.write(("\n".join(lines) + "\n").encode("utf-8"))
    transport.flush()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        reply = transport.readline().decode("utf-8", errors="replace").strip()
        if reply:
            print("ESP32:", reply)
            if reply.startswith("OK|"):
                return
    raise TimeoutError("ESP32 did not acknowledge the snapshot")


def send_update(transport, minutes):
    local_ids = local_image_ids()
    device_ids = stored_image_ids(transport)
    if local_ids.issubset(device_ids):
        print("All local images are already stored on ESP32; sending text only.")
    else:
        print("ESP32 image set is incomplete; uploading local images.")
        for satellite in SATELLITES:
            send_image(transport, satellite["norad"])
    send_snapshot(transport, minutes)


def run_serial(args):
    serial, _ = serial_module()
    port = choose_port(args.port)
    print(f"Sending {len(SATELLITES)} satellites to {port}...")
    with serial.Serial(port, 115200, timeout=2, rtscts=False, dsrdtr=False) as device:
        # Many ESP32 USB-UART/CDC boards wire DTR/RTS to auto-reset circuitry.
        # Keep both lines inactive before and after the transfer, otherwise closing
        # the COM port may reboot the board and erase its RAM-only snapshot.
        device.dtr = False
        device.rts = False
        time.sleep(1.5)
        device.reset_input_buffer()
        send_update(device, args.minutes)


def run_ble(args):
    print(f"Sending {len(SATELLITES)} satellites to BLE device '{args.device}'...")
    transport = BleTransport(args.device)
    try:
        send_update(transport, args.minutes)
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", help="ESP32 USB CDC COM port, e.g. COM5 (serial transport)")
    parser.add_argument("--transport", choices=["serial", "ble"], default="serial",
                        help="data transport: serial (USB CDC) or ble (Bluetooth Low Energy)")
    parser.add_argument("--device", default=BLE_NAME,
                        help=f"BLE device name to connect to (default: {BLE_NAME})")
    parser.add_argument("--minutes", type=int, default=1, choices=range(1, 1441),
                        metavar="1..1440", help="display duration for each satellite")
    args = parser.parse_args()
    if args.transport == "ble":
        run_ble(args)
    else:
        run_serial(args)


if __name__ == "__main__":
    main()
