#!/usr/bin/env python3
"""
Strava upload queue with persistence + retry.

Why this exists
---------------
treadmill_server.py used to spawn a daemon thread per save that called
`upload_tcx(tcx_path)` and discarded the return value. Any failure
(expired token, network blip, Strava 5xx) vanished into journalctl. Two
sessions in a row were silently lost that way.

Now: every `enqueue(tcx_path)` writes the path to a persistent JSON queue,
a background drain loop attempts uploads at startup and every 5 minutes,
and the user can force a drain via the API (UI Retry button). Failures
append to a human-readable error log instead of disappearing.

Files
-----
- `~/treadmill_exports/.upload_queue.json` : pending items only
- `~/treadmill_exports/.upload_errors.log` : append-only, last N entries

Threading model
---------------
A single daemon thread owns drain attempts. Concurrent `enqueue` calls
serialize through `_io_lock` on file reads/writes. `_drain_event` lets
callers wake the loop on demand without polling.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Optional

QUEUE_FILE = os.path.expanduser("~/treadmill_exports/.upload_queue.json")
ERROR_LOG = os.path.expanduser("~/treadmill_exports/.upload_errors.log")

# Cap the error log so a months-long Strava outage can't fill the SD card.
ERROR_LOG_MAX_BYTES = 256 * 1024  # 256 KB

# Drain at this cadence when idle. 5 min keeps cache-warm cost low and
# matches the user's typical "did it upload?" check interval.
DRAIN_INTERVAL_S = 300

# Cap retries per item so a permanently-broken TCX (deleted on disk,
# Strava-rejected for schema reasons) doesn't churn forever.
MAX_ATTEMPTS = 50

_io_lock = threading.Lock()
_drain_lock = threading.Lock()  # serializes drain() calls themselves
_drain_event = threading.Event()
# _state and the pending-count cache are read by HTTP threads (status()) and
# written by the drain thread — guard both with _state_lock. The cache lets
# status() answer /api/state (polled ~10x/s) without a disk read every time.
_state_lock = threading.Lock()
_state = {"last_error": "", "last_error_at": 0.0, "last_success_at": 0.0}
_pending_cache = {"count": 0, "oldest": 0.0, "loaded": False}


def _refresh_pending_cache(items: list[dict]) -> None:
    with _state_lock:
        _pending_cache["count"] = len(items)
        _pending_cache["oldest"] = min((it.get("added_at", 0) for it in items), default=0)
        _pending_cache["loaded"] = True


def _ensure_cache() -> None:
    with _state_lock:
        loaded = _pending_cache["loaded"]
    if not loaded:
        _refresh_pending_cache(_read_queue())


def _set_state(**kw) -> None:
    with _state_lock:
        _state.update(kw)


# ============================================================
# Internal helpers
# ============================================================

def _read_queue() -> list[dict]:
    if not os.path.exists(QUEUE_FILE):
        return []
    try:
        with open(QUEUE_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError):
        # Corrupt queue file → start fresh rather than wedge. The TCX files
        # themselves are still on disk; manual `enqueue` recovers them.
        return []


def _write_queue(items: list[dict]) -> None:
    os.makedirs(os.path.dirname(QUEUE_FILE), exist_ok=True)
    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=2)
    os.replace(tmp, QUEUE_FILE)
    _refresh_pending_cache(items)   # keep status() disk-read-free


def _append_error_log(tcx_path: str, msg: str) -> None:
    os.makedirs(os.path.dirname(ERROR_LOG), exist_ok=True)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {os.path.basename(tcx_path)}: {msg}\n"
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(line)
        if os.path.getsize(ERROR_LOG) > ERROR_LOG_MAX_BYTES:
            _trim_error_log()
    except OSError:
        pass


def _trim_error_log() -> None:
    try:
        with open(ERROR_LOG, "r") as f:
            lines = f.readlines()
        keep = lines[-200:]  # last 200 entries
        with open(ERROR_LOG, "w") as f:
            f.writelines(keep)
    except OSError:
        pass


# ============================================================
# Public API
# ============================================================

def enqueue(tcx_path: str, name: Optional[str] = None) -> None:
    """Add a TCX to the upload queue. Idempotent — duplicate paths are dropped.

    `name` is the Strava activity title (e.g. "Central Park · Soir · Treadmill").
    Persisted with the item so the title survives a reboot before the upload
    drains; without it the uploader falls back to "Treadmill Run <ts>".
    """
    abs_path = os.path.abspath(tcx_path)
    with _io_lock:
        items = _read_queue()
        if any(item.get("tcx_path") == abs_path for item in items):
            return
        items.append({
            "tcx_path": abs_path,
            "name": name,
            "added_at": time.time(),
            "attempts": 0,
            "last_error": "",
            "last_attempt_at": 0.0,
        })
        _write_queue(items)
    _drain_event.set()


def pending() -> list[dict]:
    """Snapshot of pending uploads (safe to read concurrently)."""
    with _io_lock:
        return _read_queue()


def pending_count() -> int:
    return len(pending())


def status() -> dict:
    """Compact status for /api/state. Serves from the in-memory cache so the
    ~10 Hz browser poll never triggers a disk read of the queue file."""
    _ensure_cache()
    with _state_lock:
        return {
            "pending_count": _pending_cache["count"],
            "last_error": _state["last_error"],
            "last_error_at": _state["last_error_at"],
            "last_success_at": _state["last_success_at"],
            "oldest_pending": _pending_cache["oldest"],
        }


def drain(uploader=None) -> tuple[int, int]:
    """
    Walk the queue once, attempt each upload. Return (succeeded, failed).

    `uploader` is `upload_tcx(path) -> (ok: bool, msg: str)`. Injected so we
    can unit-test the queue without monkey-patching the requests-using module.
    """
    if uploader is None:
        # Late import so this module is importable even if requests isn't
        # installed (e.g. on a dev box without strava deps).
        from treadmill_strava import upload_tcx as uploader  # type: ignore

    # Only one drain at a time. If the periodic loop and the UI Retry button
    # both fire, the second call returns (0, 0) instead of double-uploading.
    if not _drain_lock.acquire(blocking=False):
        return 0, 0

    try:
        with _io_lock:
            items = list(_read_queue())

        if not items:
            return 0, 0

        succeeded = 0
        failed = 0
        remaining: list[dict] = []
        # Set of paths we attempted, so we can later merge items added
        # *during* the drain — uploads can take minutes; an enqueue() during
        # that window would otherwise be overwritten by the final write.
        attempted_paths = {item.get("tcx_path", "") for item in items}

        for item in items:
            path = item.get("tcx_path", "")
            if not path or not os.path.exists(path):
                # TCX deleted locally — log once and drop.
                _append_error_log(path or "?", "TCX missing on disk, removed from queue")
                continue

            if item.get("attempts", 0) >= MAX_ATTEMPTS:
                _append_error_log(path, f"Dropped after {MAX_ATTEMPTS} attempts: {item.get('last_error', '?')}")
                continue

            name = item.get("name")
            try:
                try:
                    ok, msg = uploader(path, name)
                except TypeError:
                    # Test-injected or legacy single-arg uploader.
                    ok, msg = uploader(path)
            except Exception as e:  # uploader itself may raise
                ok = False
                msg = f"uploader exception: {type(e).__name__}: {e}"

            if ok:
                succeeded += 1
                _set_state(last_success_at=time.time())
                continue

            item["attempts"] = item.get("attempts", 0) + 1
            item["last_error"] = msg
            item["last_attempt_at"] = time.time()
            _set_state(last_error=msg, last_error_at=time.time())
            _append_error_log(path, msg)
            remaining.append(item)
            failed += 1

        # Re-read the queue under the lock and merge in anything that was
        # enqueue()'d while we were uploading. Without this, a save during
        # a long Strava upload silently disappears from the pending queue
        # (the TCX file is still on disk, just no longer scheduled).
        with _io_lock:
            current = _read_queue()
            added_during_drain = [it for it in current
                                  if it.get("tcx_path", "") not in attempted_paths]
            _write_queue(remaining + added_during_drain)
        return succeeded, failed
    finally:
        _drain_lock.release()


def request_drain() -> None:
    """Wake the background drain loop (non-blocking)."""
    _drain_event.set()


def start_drain_loop(uploader=None, interval_s: int = DRAIN_INTERVAL_S) -> threading.Thread:
    """Start the background daemon. Call once at server startup."""
    def loop():
        # Initial drain after a 5s settle so the network has time to come up
        # at boot. Without this, the first attempt routinely failed on a
        # cold Pi boot before NetworkManager finished associating.
        time.sleep(5)
        while True:
            # Clear BEFORE the work, not after wait(). If we cleared after
            # waking, an enqueue() firing between wait()-returning and
            # clear() would lose its signal — the next drain would only
            # happen after the full interval_s timeout. Clearing first
            # means any set() after this point is preserved for the next
            # iteration's wait().
            _drain_event.clear()
            try:
                drain(uploader=uploader)
            except Exception as e:
                # Never let the drain loop die. Log and keep going.
                _set_state(last_error=f"drain loop crashed: {e}", last_error_at=time.time())
                _append_error_log("?", f"drain loop exception: {type(e).__name__}: {e}")
            _drain_event.wait(timeout=interval_s)

    t = threading.Thread(target=loop, daemon=True, name="strava-drain")
    t.start()
    return t


# ============================================================
# CLI helper for ad-hoc debugging on the Pi
# ============================================================
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        items = pending()
        if not items:
            print("Queue is empty.")
        for it in items:
            age_h = (time.time() - it.get("added_at", 0)) / 3600.0
            print(f"  {os.path.basename(it['tcx_path']):40s}  "
                  f"attempts={it.get('attempts', 0)}  "
                  f"age={age_h:.1f}h  "
                  f"last_error={it.get('last_error', '')[:60]}")
    elif len(sys.argv) > 1 and sys.argv[1] == "drain":
        ok, fail = drain()
        print(f"Drained: {ok} succeeded, {fail} failed.")
    elif len(sys.argv) > 1 and sys.argv[1] == "enqueue":
        if len(sys.argv) < 3:
            print("Usage: treadmill_strava_queue.py enqueue <path.tcx>")
            sys.exit(1)
        enqueue(sys.argv[2])
        print(f"Enqueued: {sys.argv[2]}")
    else:
        print("Usage:")
        print("  treadmill_strava_queue.py list                # show pending")
        print("  treadmill_strava_queue.py drain               # attempt all pending")
        print("  treadmill_strava_queue.py enqueue <file.tcx>  # add to queue")
