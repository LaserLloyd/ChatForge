"""structlog setup with secret redaction.

# Adapted from StudioForge src/studioforge/logging.py (MIT, LaserLloyd)

API keys pass through config objects and request headers that get logged wholesale in
places, so redaction is done as a processor (structlog) and as a handler filter (stdlib
records, e.g. from httpx or keyring) rather than at each call site -- a missed call site
is a leaked key, a missed processor is impossible.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
from collections.abc import Hashable, MutableMapping
from pathlib import Path
from typing import Any

import structlog

from aichat.logfiles import DEFAULT_BACKUP_COUNT, DEFAULT_MAX_BYTES, SafeRotatingFileHandler

# Adapted from StudioForge src/studioforge/logging.py `_SECRET_KEYS` (MIT, LaserLloyd)
_SECRET_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "x-api-key",
    "token",
    "hf_token",
    "authorization",
    "password",
    "secret",
}
_REDACTED = "***REDACTED***"

# A bearer credential embedded in free text (a header dump, an httpx repr).
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")

# Registered at runtime so log lines that embed a secret in a longer string
# (a launch command line, an error message) still get scrubbed.
_secret_values: set[str] = set()

#: Name of the app's log file inside ``log_dir``.
APP_LOG_NAME = "aichat.log"


def register_secret(value: str | None) -> None:
    """Record a secret value so it is scrubbed from all future log output."""
    if value and len(value) >= 6:
        _secret_values.add(value)


def _scrub_text(text: str) -> str:
    # Snapshot: register_secret can run from another thread while this iterates.
    for secret in tuple(_secret_values):
        if secret in text:
            text = text.replace(secret, _REDACTED)
    return _BEARER_RE.sub(rf"\1 {_REDACTED}", text)


def _redact_value(value: Any) -> Any:
    """Scrub one value, whatever its shape (str, dict, list/tuple of those)."""
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return _redact_dict(value)
    if isinstance(value, (list, tuple)):
        scrubbed = [_redact_value(item) for item in value]
        return tuple(scrubbed) if isinstance(value, tuple) else scrubbed
    return value


def _is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in _SECRET_KEYS


def _redact(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if _is_secret_key(key) and value is not None:
            event_dict[key] = _REDACTED
        else:
            event_dict[key] = _redact_value(value)
    return event_dict


def _redact_dict(data: dict[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if _is_secret_key(key) and value is not None:
            out[key] = _REDACTED
        else:
            out[key] = _redact_value(value)
    return out


class _ScrubFilter(logging.Filter):
    """Scrub the formatted message of plain stdlib records before any handler writes it."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad %-format must not stop the record
            return True
        scrubbed = _scrub_text(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = None
        return True


class RingBufferHandler(logging.Handler):
    """Keeps the last N formatted records in memory for the settings window's log view."""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.capacity = capacity
        self.records: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "level": record.levelname,
                "logger": record.name,
                "message": _scrub_text(record.getMessage()),
            }
        except Exception:  # pragma: no cover - never let logging break the app
            return
        self.records.append(entry)
        if len(self.records) > self.capacity:
            del self.records[: len(self.records) - self.capacity]

    def tail(self, n: int = 200, level: str | None = None) -> list[dict[str, Any]]:
        records = self.records
        if level:
            wanted = level.upper()
            order = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
            if wanted in order:
                threshold = order.index(wanted)
                records = [r for r in records if r["level"] in order[threshold:]]
        return records[-n:]


RING_BUFFER = RingBufferHandler()


class _SafeStreamHandler(logging.StreamHandler):  # type: ignore[type-arg,unused-ignore]
    """A stderr handler that cannot take the process down.

    ``logging.Handler.handleError`` catches ``OSError`` only; a closed or detached stream
    raises ``ValueError: I/O operation on closed file`` from ``stream.write``, and that
    would propagate out of ``log.info(...)`` at the call site.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if self.stream is None:
            return
        super().emit(record)  # a failure lands in handleError, below

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib name
        # Detach for good: every later emit would fail the same way, and the ring buffer
        # and log file still carry the record.
        self.stream = None  # type: ignore[assignment,unused-ignore]


#: The file handler the last :func:`configure_logging` installed, so the next call can
#: close it rather than leave it holding the file.
_file_handler: logging.Handler | None = None


def configure_logging(
    level: str = "INFO",
    *,
    json_logs: bool = False,
    log_dir: Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> None:
    """Configure structlog + stdlib logging. Safe to call more than once.

    With ``log_dir`` set, ``<log_dir>/aichat.log`` is written by a size-rotating handler
    (``max_bytes`` 0 = never rotate). ``pythonw`` has no stderr, so the console handler is
    only installed when there is one.
    """
    global _file_handler
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    level_no = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Redaction runs LAST before rendering, so the exception text that
            # format_exc_info just inserted (an httpx Request repr, a subprocess error
            # carrying an argv) is scrubbed too.
            _redact,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level_no),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    if _file_handler is not None:
        # Closed, not just detached: a replaced handler still holding the file would
        # block the new one's rename exactly as another process does.
        with contextlib.suppress(Exception):
            _file_handler.close()
        _file_handler = None
    root.setLevel(level_no)

    scrub = _ScrubFilter()
    if sys.stderr is not None:
        stream = _SafeStreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter("%(message)s"))
        stream.addFilter(scrub)
        root.addHandler(stream)
    root.addHandler(RING_BUFFER)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = SafeRotatingFileHandler(
            log_dir / APP_LOG_NAME, max_bytes=max_bytes, backup_count=backup_count
        )
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        file_handler.addFilter(scrub)
        root.addHandler(file_handler)
        _file_handler = file_handler

    # Chatty libraries: keep them, but quieter. httpx logs full request URLs at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


def flush_logging() -> None:
    """Flush the file handler (tests, and a clean shutdown)."""
    if _file_handler is not None:
        with contextlib.suppress(Exception):
            _file_handler.flush()


#: Keys :func:`first_time` has answered ``True`` for, process-wide. Bounded: past the cap
#: the set starts over, so a caller minting endless keys costs a repeated line, never memory.
_FIRST_TIME_CAP = 4096
_first_time_seen: set[tuple[Hashable, ...]] = set()
_first_time_lock = threading.Lock()


def first_time(*key: Hashable) -> bool:
    """``True`` the first time this process sees ``key``, ``False`` after.

    For a log line that states a fact about a *state* rather than an event; such a fact is
    worth one WARNING per process::

        emit = log.warning if first_time("kind", model_id) else log.debug
    """
    with _first_time_lock:
        if key in _first_time_seen:
            return False
        if len(_first_time_seen) >= _FIRST_TIME_CAP:
            _first_time_seen.clear()
        _first_time_seen.add(key)
        return True


def reset_first_time() -> None:
    """Forget every :func:`first_time` key (tests, and nothing else)."""
    with _first_time_lock:
        _first_time_seen.clear()
