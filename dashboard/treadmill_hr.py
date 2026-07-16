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
    # False once the background manager thread has died, so the dashboard can
    # tell "searching" apart from "manager crashed, HR is dead until restart".
    "manager_alive": True,
}
hr_lock = threading.Lock()

# Substrings that mark a *transient* BLE failure — the kind that almost always
# succeeds on a second, immediate attempt to the same device. We fast-retry
# these instead of paying a full rescan cycle.
_TRANSIENT_MARKERS = (
    "inprogress", "in progress",
    "le-connection-abort", "connection abort", "software caused connection abort",
    "notready", "not ready",
    "timeout", "timed out",
    "atterror", "gatt",
)


def _is_transient(exc) -> bool:
    s = repr(exc).lower()
    return any(m in s for m in _TRANSIENT_MARKERS)

# Diagnostic logging — every state transition prints `[HR] t=<sec>s cycle=<n>
# <event> key=value …`. Lets journalctl reconstruct exactly where reconnects
# get stuck without adding fragile shell-side correlation.
_hr_start_t = 0.0
_hr_cycle = 0


def _log(event: str, **kv) -> None:
    elapsed = time.time() - _hr_start_t if _hr_start_t else 0
    parts = [f"[HR] t={elapsed:.0f}s cycle={_hr_cycle} {event}"]
    for k, v in kv.items():
        parts.append(f"{k}={v}")
    print(" ".join(parts), flush=True)


def parse_hr_measurement(data):
    """Parse BLE Heart Rate Measurement characteristic value.
    Length-guarded: a malformed/empty notification must not raise inside the
    bleak callback (that would only be recovered by the 10s silence timer)."""
    if not data:
        return 0
    flags = data[0]
    # Bit 0: HR format (0 = uint8, 1 = uint16)
    if flags & 0x01:
        if len(data) < 3:
            return 0
        hr = int.from_bytes(data[1:3], byteorder='little')
    else:
        if len(data) < 2:
            return 0
        hr = data[1]
    return hr


async def _drop_stale_link(BleakClient):
    """Best-effort: drop any ACL link BlueZ still holds to the known strap.

    A connected BLE peripheral stops advertising, so after a server
    restart/crash that left the link open, every scan would miss the strap
    until its supervision timeout expired. Kicking the link first restores
    advertising."""
    if not KNOWN_HR_ADDRESS:
        return
    try:
        stale = BleakClient(KNOWN_HR_ADDRESS)
        await asyncio.wait_for(stale.disconnect(), timeout=5)
        _log("STALE_LINK_DROP mac=" + KNOWN_HR_ADDRESS)
    except Exception:
        pass  # nothing to drop (the common case) — fine


async def _scan_for_device(BleakScanner):
    """Scan for the HR strap, returning (device, devices_found).

    Exits as soon as the known MAC is seen with a *live* advertisement (RSSI
    present, i.e. not a stale BlueZ cache entry) instead of always waiting the
    full 10s. scanner.stop() runs in a finally so a leaked discovery session
    can't poison every later scan with InProgress."""
    devices_found = {}
    found_event = asyncio.Event()

    def detection_callback(device, adv_data):
        devices_found[device.address] = (device, adv_data)
        if (KNOWN_HR_ADDRESS
                and device.address.upper() == KNOWN_HR_ADDRESS.upper()
                and getattr(adv_data, "rssi", None) is not None):
            found_event.set()

    scanner = BleakScanner(detection_callback=detection_callback)
    await asyncio.wait_for(scanner.start(), timeout=15)
    try:
        try:
            await asyncio.wait_for(found_event.wait(), timeout=10)
        except asyncio.TimeoutError:
            pass
    finally:
        try:
            await asyncio.wait_for(scanner.stop(), timeout=5)
        except Exception:
            _log("SCAN_STOP_FAIL")

    hr_device = None
    match_via = None
    # Known MAC always wins — the Fenix watch also advertises 0x180D and would
    # otherwise hijack the loop.
    if KNOWN_HR_ADDRESS:
        for addr, (device, adv_data) in devices_found.items():
            if addr.upper() == KNOWN_HR_ADDRESS.upper():
                hr_device, match_via = device, "known_mac"
                break
    if hr_device is None and not KNOWN_HR_ADDRESS:
        for addr, (device, adv_data) in devices_found.items():
            uuids_str = " ".join(str(u).lower() for u in (adv_data.service_uuids or []))
            if "180d" in uuids_str:
                hr_device, match_via = device, "service_180d"
                break
            name_lower = (device.name or "").lower()
            if any(k in name_lower for k in ["garmin", "fenix", "hrm", "forerunner", "venu"]):
                hr_device, match_via = device, "name_keyword"
                break

    return hr_device, match_via, devices_found


async def _stream_from_device(BleakClient, hr_device):
    """Connect to hr_device, subscribe to HR notifications, and stay until the
    strap goes silent. Transient connect errors are fast-retried on the SAME
    device (abort-by-local almost always succeeds on attempt 2). The client is
    always disconnected in a finally, which also cancels a pending BlueZ
    Connect() that would otherwise block the next scan."""
    for attempt in range(3):
        client = BleakClient(hr_device, timeout=20.0)
        try:
            await asyncio.wait_for(client.connect(), timeout=25)
        except Exception as e:
            if _is_transient(e) and attempt < 2:
                _log("CONNECT_RETRY", attempt=attempt + 1, repr=repr(e))
                await asyncio.sleep(2 * (attempt + 1))
                continue
            _log("CONNECT_FAIL", repr=repr(e))
            return

        try:
            if not client.is_connected:
                _log("CONNECT_FAIL is_connected=False")
                return

            with hr_lock:
                hr_data["connected"] = True
                hr_data["device_name"] = hr_device.name or "HRM-Pro+"
            _log("CONNECT_OK", name=(hr_device.name or "?"))

            hr_char = None
            for s in client.services:
                if "180d" in s.uuid:
                    for c in s.characteristics:
                        if "2a37" in c.uuid:
                            hr_char = c
                            break
            if not hr_char:
                _log("HR_CHAR_MISSING")
                return

            _log("SUBSCRIBE", char=hr_char.uuid)
            connect_t = time.time()
            first_data_logged = False

            def hr_callback(sender, data):
                nonlocal first_data_logged
                bpm = parse_hr_measurement(data)
                with hr_lock:
                    hr_data["bpm"] = bpm
                    hr_data["last_update"] = time.time()
                if not first_data_logged:
                    first_data_logged = True
                    _log("NOTIFY_FIRST_DATA", bpm=bpm)

            await asyncio.wait_for(client.start_notify(hr_char.uuid, hr_callback), timeout=10)
            _log("NOTIFY_STARTED")

            while client.is_connected:
                await asyncio.sleep(1)
                with hr_lock:
                    last_update = hr_data["last_update"]
                if first_data_logged and time.time() - last_update > 10:
                    _log("NOTIFY_SILENT_10S reconnect=true")
                    break
                if not first_data_logged and time.time() - connect_t > 20:
                    _log("NOTIFY_ZOMBIE_20S reconnect=true")
                    break
            _log("DISCONNECT")
            return
        finally:
            # Always tear down — cancels any pending Connect() too.
            try:
                await asyncio.wait_for(client.disconnect(), timeout=10)
            except Exception:
                pass
            with hr_lock:
                hr_data["connected"] = False
                hr_data["bpm"] = 0


async def _scan_and_connect():
    """Scan for BLE HR device and connect."""
    global _hr_start_t, _hr_cycle
    _hr_start_t = time.time()

    try:
        from bleak import BleakScanner, BleakClient
    except ImportError:
        _log("FATAL bleak_not_installed", hint="pip3 install bleak --break-system-packages")
        return

    # Brief settle delay so BlueZ has time to bring up hci0 after `After=bluetooth.service`.
    # The previous 20s sleep + in-process `sudo systemctl restart bluetooth` was both:
    #   (a) silently failing when NOPASSWD sudo wasn't configured (bare except: pass)
    #   (b) racing the Garmin's broadcast-HR auto-stop timer
    # Now we rely on the systemd ordering and let bleak retry on errors.
    _log("BOOT settle_wait_3s")
    await asyncio.sleep(3)

    scan_failures = 0
    empty_scans = 0  # consecutive scans that saw *zero* devices → adapter suspect

    # Drop any link BlueZ still holds from a previous run before the first scan.
    await _drop_stale_link(BleakClient)

    while True:
        _hr_cycle += 1
        try:
            with hr_lock:
                hr_data["connected"] = False

            _log("SCAN_START known_mac=" + (KNOWN_HR_ADDRESS or "(none)"))
            hr_device, match_via, devices_found = await _scan_for_device(BleakScanner)
            _log("SCAN_END", seen=len(devices_found),
                 matched=("yes" if hr_device else "no"))

            # Zero devices in a lived-in home usually means the adapter is
            # down (rfkill soft-block, not powered), not "strap absent".
            if len(devices_found) == 0:
                empty_scans += 1
                if empty_scans >= 3:
                    _log("ADAPTER_SUSPECT empty_scans=" + str(empty_scans)
                         + " hint=check_rfkill_and_bluetooth_powered")
            else:
                empty_scans = 0

            if not hr_device:
                scan_failures += 1
                if scan_failures == 1 or scan_failures % 10 == 0:
                    seen_sorted = sorted(
                        devices_found.items(),
                        key=lambda kv: getattr(kv[1][1], "rssi", -999) or -999,
                        reverse=True,
                    )[:8]
                    for addr, (dev, adv) in seen_sorted:
                        rssi = getattr(adv, "rssi", None)
                        uuids = ",".join(str(u) for u in (adv.service_uuids or [])) or "-"
                        _log("SCAN_SEEN", mac=addr, name=(dev.name or "?"),
                             rssi=rssi, uuids=uuids)
                # A stale BlueZ link can keep the strap from advertising; try
                # dropping it every few misses so a wedged link self-heals.
                if scan_failures % 3 == 0:
                    await _drop_stale_link(BleakClient)
                _log("NO_MATCH fail_count=" + str(scan_failures) + " sleep=8s")
                await asyncio.sleep(8)
                continue

            scan_failures = 0
            _log("FOUND", name=(hr_device.name or "?"),
                 mac=hr_device.address, via=match_via)

            await _stream_from_device(BleakClient, hr_device)

        except Exception as e:
            import traceback
            _log("ERROR", type=type(e).__name__, repr=repr(e))
            traceback.print_exc()
            # No in-process bluetooth restart. If BlueZ is wedged, a process
            # restart (systemd, After=bluetooth.service) recovers ordering.

        with hr_lock:
            hr_data["connected"] = False
            hr_data["bpm"] = 0

        _log("RETRY_SLEEP duration=5s")
        await asyncio.sleep(5)


def _hr_thread():
    """Run the async BLE loop in a thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Catch BaseException (CancelledError, import-time AttributeError from a
        # broken dbus_fast, etc.) — otherwise the daemon thread dies silently
        # and HR stays dead until a full service restart, with only a stderr
        # traceback as evidence.
        loop.run_until_complete(_scan_and_connect())
    except BaseException as e:  # noqa: BLE001 — deliberate catch-all + report
        import traceback
        _log("MANAGER_DIED", type=type(e).__name__, repr=repr(e))
        traceback.print_exc()
    finally:
        with hr_lock:
            hr_data["manager_alive"] = False
            hr_data["connected"] = False
            hr_data["bpm"] = 0


def start_hr_monitor(mac: "str | None" = None):
    """Start the HR monitor in a background thread. Call once at startup.
    `mac` overrides the target strap address from Pi config (empty/None keeps
    the module default), so swapping straps is a config edit, not a code edit."""
    global KNOWN_HR_ADDRESS
    if mac:
        KNOWN_HR_ADDRESS = mac.strip()
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
