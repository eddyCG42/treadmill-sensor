#!/usr/bin/env python3
"""
BLE Heart Rate Monitor for Treadmill Dashboard
Connects to Garmin Fenix 8 (or any BLE HR strap) via Pi's Bluetooth.

Prerequisites:
  pip3 install bleak --break-system-packages

Usage:
  - Enable "Broadcast Heart Rate" on Garmin Fenix 8:
    Settings > Sensors > Wrist Heart Rate > Broadcast Heart Rate > ON
  - The Pi will auto-discover and connect

Standalone test:
  python3 treadmill_hr.py
"""

import asyncio
import threading
import time

# Heart Rate Service UUID
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
# Heart Rate Measurement Characteristic UUID
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"

# Known device MAC (Garmin HRM-Pro Plus) - set to your device
KNOWN_HR_ADDRESS = "F2:FD:75:D5:48:99"

# Shared state
hr_data = {
    "bpm": 0,
    "connected": False,
    "device_name": "",
    "last_update": 0,
}
hr_lock = threading.Lock()


def parse_hr_measurement(data):
    """Parse BLE Heart Rate Measurement characteristic value."""
    flags = data[0]
    # Bit 0: HR format (0 = uint8, 1 = uint16)
    if flags & 0x01:
        hr = int.from_bytes(data[1:3], byteorder='little')
    else:
        hr = data[1]
    return hr


async def _scan_and_connect():
    """Scan for BLE HR device and connect."""
    try:
        from bleak import BleakScanner, BleakClient
    except ImportError:
        print("[HR] bleak not installed. Run: pip3 install bleak --break-system-packages")
        return

    while True:
        try:
            # Scan for HR devices
            with hr_lock:
                hr_data["connected"] = False

            print("[HR] Scanning for heart rate devices...")
            hr_device = None
            
            # Use detection callback to access advertisement data
            devices_found = {}
            
            def detection_callback(device, adv_data):
                devices_found[device.address] = (device, adv_data)
            
            scanner = BleakScanner(detection_callback=detection_callback)
            await scanner.start()
            await asyncio.sleep(10)
            await scanner.stop()
            
            for addr, (device, adv_data) in devices_found.items():
                # 1. Check known MAC address
                if KNOWN_HR_ADDRESS and addr.upper() == KNOWN_HR_ADDRESS.upper():
                    hr_device = device
                    print(f"[HR] Matched by known MAC: {addr}")
                    break
                # 2. Check service UUIDs in advertisement
                uuids_str = " ".join(str(u).lower() for u in (adv_data.service_uuids or []))
                if "180d" in uuids_str:
                    hr_device = device
                    break
                # 3. Check by name (Garmin devices)
                name_lower = (device.name or "").lower()
                if any(k in name_lower for k in ["garmin", "fenix", "hrm", "forerunner", "venu"]):
                    hr_device = device
                    break

            if not hr_device:
                print(f"[HR] No HR device found ({len(devices_found)} devices seen), retrying in 15s...")
                await asyncio.sleep(15)
                continue

            print(f"[HR] Found: {hr_device.name or 'HRM-Pro+'} ({hr_device.address})")

            async with BleakClient(hr_device, timeout=15.0) as client:
                if not client.is_connected:
                    print("[HR] Connection failed")
                    continue

                with hr_lock:
                    hr_data["connected"] = True
                    hr_data["device_name"] = hr_device.name or "Fenix 8"

                print(f"[HR] Connected to {hr_device.name or 'HRM-Pro+'}")

                # Verify HR characteristic exists
                hr_char = None
                for s in client.services:
                    if "180d" in s.uuid:
                        for c in s.characteristics:
                            if "2a37" in c.uuid:
                                hr_char = c
                                break

                if not hr_char:
                    print("[HR] HR characteristic not found!")
                    continue

                print(f"[HR] Subscribing to {hr_char.uuid} ...")

                # Subscribe to HR notifications
                def hr_callback(sender, data):
                    bpm = parse_hr_measurement(data)
                    with hr_lock:
                        hr_data["bpm"] = bpm
                        hr_data["last_update"] = time.time()

                await client.start_notify(hr_char.uuid, hr_callback)
                print("[HR] Notifications started, waiting for data...")

                # Stay connected as long as we receive data
                while client.is_connected:
                    await asyncio.sleep(1)
                    # Check for stale data (no update in 10s = disconnected)
                    with hr_lock:
                        if time.time() - hr_data["last_update"] > 10 and hr_data["bpm"] > 0:
                            break

                print("[HR] Disconnected, will reconnect...")

        except Exception as e:
            print(f"[HR] Error: {e}")

        with hr_lock:
            hr_data["connected"] = False
            hr_data["bpm"] = 0

        await asyncio.sleep(5)


def _hr_thread():
    """Run the async BLE loop in a thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_scan_and_connect())


def start_hr_monitor():
    """Start the HR monitor in a background thread. Call once at startup."""
    t = threading.Thread(target=_hr_thread, daemon=True)
    t.start()
    return t


def get_hr():
    """Get current HR data. Thread-safe."""
    with hr_lock:
        return dict(hr_data)


# ==================================
# Standalone test
# ==================================
if __name__ == "__main__":
    print("=== BLE Heart Rate Monitor Test ===")
    print("Put on your Garmin HRM-Pro Plus chest strap")
    print("Scanning...\n")

    start_hr_monitor()

    try:
        while True:
            d = get_hr()
            if d["connected"]:
                print(f"\r  {d['device_name']}: {d['bpm']} bpm    ", end="", flush=True)
            else:
                print(f"\r  Searching...                        ", end="", flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nBye!")
