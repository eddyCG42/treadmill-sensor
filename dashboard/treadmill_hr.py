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

# Fallback when the known strap is not advertising: any *other* strap that
# advertises the Heart Rate service (e.g. the COROS arm band). On 2026-11-17
# the COROS was heard at -47 dBm on every scan and ignored for a whole run
# because only the known MAC was accepted. Watches broadcasting HR are still
# excluded by name, so the Fenix cannot hijack the loop.
WATCH_NAME_KEYWORDS = ("fenix", "forerunner", "venu", "epix", "enduro",
                       "instinct", "watch", "pace", "apex", "vertix")
# How long the known strap keeps priority once a fallback strap has been heard.
FALLBACK_GRACE_S = 8

# Address of the strap we last connected to (known or fallback), so the
# stale-link cleanup targets the right device.
_last_hr_address = ""

# A strap that is heard but refuses the link (HCI 0x3e, slots full...) is
# sidelined for a while after repeated connect failures, so it cannot starve
# the other strap: addr -> [consecutive_failures, sidelined_until_monotonic].
CONNECT_FAILS_BEFORE_SIDELINE = 2
SIDELINE_S = 120
_connect_fail = {}


def _is_sidelined(addr: str) -> bool:
    rec = _connect_fail.get(addr.upper())
    return bool(rec) and time.monotonic() < rec[1]


# Escalation: a strap that is heard but cannot be connected for this long gets
# a full restart of the BlueZ daemon (clears its cache and controller state),
# at most once per BT_RESTART_MIN_GAP_S. The adapter power-cycle alone did not
# clear the 2026-11-17 failures.
REFUSED_BEFORE_BT_RESTART_S = 150
BT_RESTART_MIN_GAP_S = 600
_refused_since = 0.0    # monotonic time of the first failure since last success
_last_bt_restart = 0.0


def _note_connect_result(addr: str, ok: bool, name: str = "") -> None:
    global _refused_since
    addr = addr.upper()
    with hr_lock:
        hr_data["detail"] = "" if ok else "refused"
        hr_data["heard_name"] = "" if ok else (name or addr)
    if ok:
        _refused_since = 0.0
        _connect_fail.pop(addr, None)
        return
    if not _refused_since:
        _refused_since = time.monotonic()
    rec = _connect_fail.setdefault(addr, [0, 0.0])
    rec[0] += 1
    if rec[0] >= CONNECT_FAILS_BEFORE_SIDELINE:
        rec[0] = 0
        rec[1] = time.monotonic() + SIDELINE_S
        _log("SIDELINED", mac=addr, seconds=SIDELINE_S)


def _is_fallback_strap(device, adv_data) -> bool:
    uuids = " ".join(str(u).lower() for u in (adv_data.service_uuids or []))
    if "180d" not in uuids:
        return False
    name = (device.name or getattr(adv_data, "local_name", "") or "").lower()
    return not any(k in name for k in WATCH_NAME_KEYWORDS)

# Shared state
hr_data = {
    "bpm": 0,
    "connected": False,
    "device_name": "",
    "last_update": 0,
    # False once the background manager thread has died, so the dashboard can
    # tell "searching" apart from "manager crashed, HR is dead until restart".
    "manager_alive": True,
    # What the manager is doing, for the dashboard: starting | scanning |
    # connecting | connected | adapter_error | dead.
    "state": "starting",
    # Why there is no HR while not connected, for the dashboard chip:
    #   ""         nothing to report (connected, or still on the first scan)
    #   "no_strap" the last scan heard no HR strap at all (strap off / asleep)
    #   "refused"  a strap is on air but the link keeps failing (radio, slots)
    "detail": "",
    # Name of the strap heard but not connectable, when detail == "refused".
    "heard_name": "",
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


def _set_state(state: str) -> None:
    with hr_lock:
        hr_data["state"] = state


async def _bluetoothctl(*args, timeout=8.0) -> str:
    """Run one bluetoothctl command, return its stdout ("" on any failure).
    Works unprivileged for members of the `bluetooth` group."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return out.decode("utf-8", errors="ignore")
    except Exception:
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return ""


async def _drop_stale_link():
    """Drop an ACL link BlueZ still holds to the known strap.

    A connected BLE peripheral stops advertising, so after a server
    restart/crash that left the link open, every scan would miss the strap
    until its supervision timeout expired. The previous version called
    BleakClient(mac).disconnect() on a never-connected client, which bleak
    treats as a no-op (while still logging STALE_LINK_DROP); go through BlueZ
    itself instead, and only when it really reports the link up."""
    for mac in dict.fromkeys(m for m in (KNOWN_HR_ADDRESS, _last_hr_address) if m):
        info = await _bluetoothctl("info", mac)
        if "Connected: yes" not in info:
            continue
        await _bluetoothctl("disconnect", mac)
        _log("STALE_LINK_DROP mac=" + mac)


async def _forget_corrupt_entry():
    """Remove a corrupted BlueZ record of the known strap.

    Found on 2026-11-17: /var/lib/bluetooth held the HRM-Pro+ with an empty
    SupportedTechnologies, so BlueZ loaded it at boot as a *public* address
    device with no services. The strap advertises with a *random* address, and
    every advert then failed with "Unable to create object for found device"
    in bluetoothd — bleak never saw the strap although it was on air (this is
    what made HR discovery fail for weeks). Such a record shows up as
    "(public)", unpaired, not connected and without any UUID; removing it is
    harmless, the next advert recreates a correct one."""
    if not KNOWN_HR_ADDRESS:
        return
    info = await _bluetoothctl("info", KNOWN_HR_ADDRESS)
    first_line = info.splitlines()[0] if info.strip() else ""
    if ("(public)" in first_line and "UUID:" not in info
            and "Connected: yes" not in info and "Paired: yes" not in info):
        await _bluetoothctl("remove", KNOWN_HR_ADDRESS)
        _log("CORRUPT_ENTRY_REMOVED mac=" + KNOWN_HR_ADDRESS)


async def _restart_bluetooth_if_stuck():
    """Restart bluetooth.service when straps have been heard-but-refused for
    REFUSED_BEFORE_BT_RESTART_S. The server runs with NoNewPrivileges, so no
    sudo: a polkit rule (installed by deploy.sh) lets this user restart that
    one unit through plain `systemctl`."""
    global _refused_since, _last_bt_restart
    now = time.monotonic()
    if not _refused_since or now - _refused_since < REFUSED_BEFORE_BT_RESTART_S:
        return
    if _last_bt_restart and now - _last_bt_restart < BT_RESTART_MIN_GAP_S:
        return
    _last_bt_restart = now
    _refused_since = now
    _log("BT_SERVICE_RESTART reason=strap_heard_but_refused")
    rc = -1
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "restart", "bluetooth.service",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        rc = await asyncio.wait_for(proc.wait(), timeout=25)
    except Exception as e:
        _log("BT_SERVICE_RESTART_ERROR", repr=repr(e))
    _log("BT_SERVICE_RESTART_DONE", rc=rc)
    _connect_fail.clear()
    await asyncio.sleep(6)   # let bluetoothd bring hci0 back up


async def _power_cycle_adapter():
    """Recover a wedged hci0 (Pi 4 combo chip) without root: BlueZ lets the
    `bluetooth` group toggle Powered. Clears leaked discovery sessions too."""
    _log("ADAPTER_POWER_CYCLE")
    await _bluetoothctl("power", "off")
    await asyncio.sleep(2)
    await _bluetoothctl("power", "on")
    await asyncio.sleep(3)


def _is_live_adv(adv_data) -> bool:
    """True only for a real advertisement. bleak's BlueZ backend fills
    adv_data.rssi with -127 when the D-Bus object has no RSSI, and re-fires the
    callback on any Device1 property change (Connected, ServicesResolved...),
    so a cached-but-silent strap used to look "found" and cost ~90 s of doomed
    connect attempts. BlueZ only carries RSSI while it is hearing adverts."""
    pd = getattr(adv_data, "platform_data", None)
    if isinstance(pd, tuple) and len(pd) >= 2 and isinstance(pd[1], dict):
        return "RSSI" in pd[1]
    rssi = getattr(adv_data, "rssi", None)
    return rssi is not None and rssi > -127


# One long listening window instead of 10 s scan / 8 s pause: the strap was
# missed while the Pi was deaf ~45 % of the time.
SCAN_WINDOW_S = 30
SCAN_PAUSE_S = 1


async def _scan_for_device(BleakScanner):
    """Scan for the HR strap, returning (device, devices_found).

    Exits as soon as the known MAC is seen with a *live* advertisement instead
    of always waiting the full window. start() and stop() both sit inside the
    try/finally so a slow start can't leak a discovery session that would
    poison every later scan with InProgress."""
    devices_found = {}
    found_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    fallback_seen_t = []  # loop time of the first fallback-strap advert

    def detection_callback(device, adv_data):
        if not _is_live_adv(adv_data):
            return
        devices_found[device.address] = (device, adv_data)
        if _is_sidelined(device.address):
            return
        if (KNOWN_HR_ADDRESS
                and device.address.upper() == KNOWN_HR_ADDRESS.upper()):
            found_event.set()
        elif not fallback_seen_t and _is_fallback_strap(device, adv_data):
            fallback_seen_t.append(loop.time())

    scanner = BleakScanner(detection_callback=detection_callback)
    try:
        await asyncio.wait_for(scanner.start(), timeout=15)
        deadline = loop.time() + SCAN_WINDOW_S
        grace = (FALLBACK_GRACE_S
                 if KNOWN_HR_ADDRESS and not _is_sidelined(KNOWN_HR_ADDRESS)
                 else 0)
        while not found_event.is_set() and loop.time() < deadline:
            # A fallback strap is on air: give the known one a short head
            # start, then stop listening instead of burning the full window.
            if fallback_seen_t and loop.time() - fallback_seen_t[0] >= grace:
                break
            await asyncio.sleep(0.25)
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
            if (addr.upper() == KNOWN_HR_ADDRESS.upper()
                    and not _is_sidelined(addr)):
                hr_device, match_via = device, "known_mac"
                break
    if hr_device is None:
        # Known strap silent (or none configured): strongest strap advertising
        # the HR service wins. Watches are filtered out by _is_fallback_strap.
        straps = [(device, adv_data) for device, adv_data in devices_found.values()
                  if _is_fallback_strap(device, adv_data)
                  and not _is_sidelined(device.address)]
        if straps:
            straps.sort(key=lambda da: getattr(da[1], "rssi", -999) or -999,
                        reverse=True)
            hr_device, match_via = straps[0][0], "service_180d"
    if hr_device is None and not KNOWN_HR_ADDRESS:
        for addr, (device, adv_data) in devices_found.items():
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
    # 2 attempts, not 3: the device was just heard advertising, so a second
    # immediate try covers the transient aborts; beyond that we are better
    # off back in the scan than spending ~90 s on a strap that went quiet.
    _set_state("connecting")
    for attempt in range(2):
        client = BleakClient(hr_device, timeout=15.0)
        try:
            await asyncio.wait_for(client.connect(), timeout=20)
        except Exception as e:
            if _is_transient(e) and attempt < 1:
                _log("CONNECT_RETRY", attempt=attempt + 1, repr=repr(e))
                await asyncio.sleep(2 * (attempt + 1))
                continue
            _log("CONNECT_FAIL", repr=repr(e))
            _note_connect_result(hr_device.address, False, hr_device.name or "")
            return

        try:
            if not client.is_connected:
                _log("CONNECT_FAIL is_connected=False")
                _note_connect_result(hr_device.address, False, hr_device.name or "")
                return
            _note_connect_result(hr_device.address, True)

            with hr_lock:
                hr_data["connected"] = True
                hr_data["state"] = "connected"
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
                # Leave "connected" right away so the dashboard chip doesn't
                # stay green until the next scan starts.
                if hr_data.get("state") == "connected":
                    hr_data["state"] = "scanning"


async def _scan_and_connect():
    """Scan for BLE HR device and connect."""
    global _hr_start_t, _hr_cycle, _last_hr_address, _refused_since
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
    errors = 0       # consecutive exceptions (InProgress, no adapter...)

    # Drop any link BlueZ still holds from a previous run before the first scan.
    await _drop_stale_link()
    await _forget_corrupt_entry()

    while True:
        _hr_cycle += 1
        try:
            with hr_lock:
                hr_data["connected"] = False
                if hr_data["state"] != "adapter_error":
                    hr_data["state"] = "scanning"

            _log("SCAN_START known_mac=" + (KNOWN_HR_ADDRESS or "(none)"))
            hr_device, match_via, devices_found = await _scan_for_device(BleakScanner)
            _log("SCAN_END", seen=len(devices_found),
                 matched=("yes" if hr_device else "no"))
            errors = 0
            _set_state("scanning")
            if match_via != "known_mac":
                await _forget_corrupt_entry()

            # Zero devices in a lived-in home usually means the adapter is
            # down (rfkill soft-block, not powered), not "strap absent".
            if len(devices_found) == 0:
                empty_scans += 1
                if empty_scans >= 3:
                    _log("ADAPTER_SUSPECT empty_scans=" + str(empty_scans)
                         + " hint=check_rfkill_and_bluetooth_powered")
                    _set_state("adapter_error")
                    await _power_cycle_adapter()
                    empty_scans = 0
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
                    await _drop_stale_link()
                _log("NO_MATCH fail_count=" + str(scan_failures)
                     + f" sleep={SCAN_PAUSE_S}s")
                # Tell "no strap on air" from "strap heard but sidelined".
                heard = [dev.name or addr
                         for addr, (dev, adv) in devices_found.items()
                         if (KNOWN_HR_ADDRESS
                             and addr.upper() == KNOWN_HR_ADDRESS.upper())
                         or _is_fallback_strap(dev, adv)]
                with hr_lock:
                    hr_data["detail"] = "refused" if heard else "no_strap"
                    hr_data["heard_name"] = heard[0] if heard else ""
                if not heard:
                    _refused_since = 0.0
                await _restart_bluetooth_if_stuck()
                await asyncio.sleep(SCAN_PAUSE_S)
                continue

            scan_failures = 0
            _log("FOUND", name=(hr_device.name or "?"),
                 mac=hr_device.address, via=match_via)
            _last_hr_address = hr_device.address

            await _stream_from_device(BleakClient, hr_device)
            await _restart_bluetooth_if_stuck()

        except Exception as e:
            import traceback
            errors += 1
            _log("ERROR", type=type(e).__name__, repr=repr(e), consecutive=errors)
            traceback.print_exc()
            # Repeated errors (InProgress from a leaked discovery, "No powered
            # Bluetooth adapters", D-Bus timeouts) never heal by retrying:
            # power-cycle the adapter, which resets BlueZ's discovery state.
            if errors >= 3:
                _set_state("adapter_error")
                await _power_cycle_adapter()
                errors = 0

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
            hr_data["state"] = "dead"
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


def release_hr_link() -> None:
    """Synchronously drop BlueZ's link to the strap (call on shutdown).
    BlueZ keeps the ACL link when the process dies, which ate one of the
    strap's 2-3 BLE slots until its supervision timeout."""
    import subprocess
    for mac in dict.fromkeys(m for m in (_last_hr_address, KNOWN_HR_ADDRESS) if m):
        try:
            subprocess.run(["bluetoothctl", "disconnect", mac],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=3)
        except Exception:
            pass


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
