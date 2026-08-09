#!/usr/bin/env python3
"""List nearby BLE devices to verify the ESP32 SatWidget is advertising."""
import asyncio

from bleak import BleakScanner

BLE_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"


async def main():
    print("Scanning for 15 seconds...")
    devices = await BleakScanner.discover(timeout=15, return_adv=True)
    if not devices:
        print("Nothing found. Check that Bluetooth is enabled on the PC "
              "and the ESP32 is powered and in range.")
        return
    print(f"\n{'Name':32} {'Address':20} BLE UART service")
    for address, (device, adv) in sorted(devices.items()):
        name = adv.local_name or device.name or "(no name)"
        services = [str(u).lower() for u in adv.service_uuids]
        marker = "yes" if BLE_SERVICE_UUID.lower() in services else ""
        print(f"{name[:32]:32} {address:20} {marker}")


if __name__ == "__main__":
    asyncio.run(main())
