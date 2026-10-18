"""Is there a working Intel NPU on this Windows PC? Probed once, in the background, cached.

OpenVINO Model Server on a machine without an NPU fails to compile for it, so the local
model would be a dead end. :func:`probe` asks Windows (through :func:`chatforge.doctor.
list_npu_devices`, a PowerShell PnP query that takes one to three seconds) and caches the
answer; :func:`absent` is what the launch path reads, and it is ``True`` only when the probe
*succeeded* and found no NPU in working order. A failed or pending probe is "unknown" and
keeps the old behaviour (launch for the NPU), so a flaky query can never move a working NPU
user onto the CPU.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from chatforge.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class NpuProbe:
    #: ``True``: an NPU in working order. ``False``: none. ``None``: unknown.
    present: bool | None
    #: One plain sentence: why ``present`` is what it is.
    reason: str = ""


_lock = threading.Lock()
_cached: NpuProbe | None = None
_started = False
#: Set when a probe has finished, whatever it found (or failed to find).
_done = threading.Event()


def cached() -> NpuProbe | None:
    """The cached result, or ``None`` while it has not been probed."""
    return _cached


def reset() -> None:
    """Forget the cached result (tests, and after the user installs a driver)."""
    global _cached, _started
    with _lock:
        _cached = None
        _started = False
        _done.clear()


def classify(devices: list[str] | None) -> NpuProbe:
    """Turn ``list_npu_devices()`` output (``"Intel(R) AI Boost [OK]"`` lines) into a verdict."""
    if devices is None:
        return NpuProbe(None, "the NPU query did not run")
    if not devices:
        return NpuProbe(False, "no Intel NPU (AI Boost) was found on this PC")
    if any(line.rstrip().endswith("[OK]") for line in devices):
        return NpuProbe(True, "an Intel NPU is present")
    return NpuProbe(False, "an Intel NPU is present but its driver is not working")


def probe(timeout_s: float = 20.0) -> NpuProbe:
    """Run the query now (blocking) and cache the answer. A failed query is not cached, so a
    later call tries again."""
    global _cached
    from chatforge import doctor

    try:
        result = classify(doctor.list_npu_devices(timeout_s))
        if result.present is not None:
            with _lock:
                _cached = result
            log.info("npu_probe", present=result.present, reason=result.reason)
        return result
    finally:
        _done.set()


def start_background() -> None:
    """Probe on a daemon thread, once. Never raises."""
    global _started
    with _lock:
        if _started or _cached is not None:
            return
        _started = True

    def run() -> None:
        try:
            probe()
        except Exception as exc:  # noqa: BLE001 - a probe must never break startup
            log.warning("npu_probe_failed", error=type(exc).__name__)
        finally:
            _done.set()

    threading.Thread(target=run, name="chatforge-npu-probe", daemon=True).start()


def wait(timeout_s: float = 15.0) -> bool:
    """Block until a probe has finished (starting one if none was started); ``True`` when it
    did, ``False`` after ``timeout_s``. The first model load calls this so it does not race
    the probe that decides between NPU and CPU."""
    if _done.is_set():
        return True
    start_background()
    return _done.wait(timeout_s)


def absent() -> bool:
    """True only when a finished probe found no working NPU."""
    result = _cached
    return result is not None and result.present is False


def absence_reason() -> str:
    result = _cached
    return result.reason if result is not None and result.present is False else ""
