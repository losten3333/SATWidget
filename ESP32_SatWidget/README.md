# ESP32 SatWidget

The sketch shows an orbital-data card on the 280 x 456 portrait AMOLED display.
Data arrives as an atomic snapshot over the ESP32-C6 native USB CDC COM port or
over Bluetooth Low Energy.

## First use on Windows

1. In the Arduino IDE select the ESP32-C6 board and set **Tools → USB CDC On Boot → Enabled**.
2. Upload `ESP32_SatWidget.ino`, reconnect the board if Windows does not expose a new COM port yet, then note its `COMx` number in Device Manager.
3. Install the only PC dependency without administrator rights:

   ```powershell
   python -m pip install --user pyserial bleak
   ```

4. Send the demonstration data:

   ```powershell
   python .\satellite_sender.py --port COM5 --minutes 1
   ```

## Bluetooth Low Energy (BLE)

The sketch exposes the same text protocol over BLE as a Nordic UART Service
(device name `ESP32_SatWidget`). USB CDC and BLE work side by side, and every
reply is sent to both transports that are currently available.

1. Install the dependency: `python -m pip install --user bleak`
2. Send the demonstration data:

   ```powershell
   python .\satellite_sender.py --transport ble --minutes 1
   ```

The PC connects to the first BLE device named `ESP32_SatWidget`; pass
`--device <name>` to target another one. BLE attributes are transferred in
20-byte packets, so the first image upload over BLE is noticeably slower than
over USB. On later runs the ESP32 image set is already complete and only the
short text snapshot is transferred.

The widget keeps its last valid data on screen while no transport is
connected, exactly as when USB is unplugged.

## Satellite images

Images are stored in the ESP32 internal flash (FFat); no SD card is used. Put the source files next to the sender in this layout:

```text
ESP32_SatWidget/
  image/
    52940.rgb565
    45783.rgb565
```

The file name is the satellite NORAD ID. Each file must be an uncompressed **150 x 150 px RGB565 little-endian (RGB565LE)** image, exactly **45,000 bytes**. The receiver swaps the two bytes of every pixel while saving it because the active LVGL configuration uses `LV_COLOR_16_SWAP = 1`.

On every run, `satellite_sender.py` first sends `IMG_STATUS` and receives the NORAD IDs of images that are already stored in FFat. If every image found locally in `image/` is present on the ESP32, it sends only orbital text data. Otherwise it uploads the local image set and then sends the snapshot.

The sketch stores received images as `/image/<NORAD>.rgb565` in FFat and displays the matching one automatically. The selected ESP32 partition scheme must include a formatted internal FAT/FFat data partition; the Serial Monitor prints `FFAT READY` when it mounts successfully.

Windows' built-in USB CDC serial driver is used; no custom driver or elevated permission is needed. Close Arduino Serial Monitor before running the script because a COM port can have only one client.
The sender explicitly keeps DTR/RTS inactive so closing its COM port does not reset the ESP32 and discard the current RAM-only snapshot.

## Protocol for the future tracking script

Each update replaces the complete set only after `END`, so the display keeps its previous valid data if the transfer is interrupted.
An empty snapshot (`BEGIN`, `CONFIG`, `END`) clears the selected satellites from the display.

```text
BEGIN
CONFIG|1
TIME|18:36
SAT|KHAYYAM|52940|6 907,1 km (+0,4 km)|97 min 44 s|97,5 deg (+0,01 deg)|159,2 deg|00 h 27 min|19 842|10:30|#BF805B
SAT|KANOPUS-V-6|45783|6 884,5 km (-0,2 km)|95 min 53 s|97,4 deg (-0,01 deg)|221,8 deg|03 h 12 min|32 614|09:45|#4777B8
END
```

`CONFIG` is the shared display duration per satellite in minutes (1…1440). `TIME` synchronizes the current local time displayed in the top-left corner; the ESP32 then advances it autonomously. The device answers `OK|<satellite_count>|<minutes>` after accepting the snapshot. A field may not contain `|` or a line break.

The value after `LTAN` is a fallback colour (`#RRGGBB`) used only when no image for that NORAD ID is stored on the ESP32.

## Offline behaviour and persistence

The green status indicator changes to red when no complete valid snapshot has arrived for 24 hours. Valid data and uploaded images remain on the screen while the ESP32 stays powered.
