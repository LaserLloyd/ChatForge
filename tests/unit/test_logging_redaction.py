"""logging_setup.py: secrets never reach the log file or the ring buffer."""

import logging

import pytest
import structlog

from aichat import logging_setup
from aichat.logging_setup import (
    RING_BUFFER,
    configure_logging,
    first_time,
    flush_logging,
    get_logger,
    register_secret,
    reset_first_time,
)
from aichat.secrets import set_api_key

SECRET = "sk-live-Zx9Qw8Er7Ty6Ui5Op"
HF = "hf_AbCdEfGhIjKlMnOpQrSt"


@pytest.fixture
def log_dir(tmp_path):
    directory = tmp_path / "logs"
    configure_logging("DEBUG", log_dir=directory)
    yield directory
    # Release the file (Windows) and restore quiet defaults.
    configure_logging("WARNING")
    structlog.reset_defaults()
    RING_BUFFER.records.clear()


def _log_text(directory) -> str:
    flush_logging()
    return (directory / "aichat.log").read_text(encoding="utf-8")


def test_registered_secret_is_scrubbed_from_messages(log_dir):
    register_secret(SECRET)
    log = get_logger("test")
    log.info(f"calling provider with key {SECRET} now")
    log.info("argv", argv=["ovms", "--token", SECRET, "--x"])
    log.info("nested", cfg={"a": {"b": [SECRET, ("t", SECRET)]}})
    logging.getLogger("thirdparty").warning("plain stdlib: %s", SECRET)
    logging.getLogger("thirdparty").error("also stdlib " + SECRET)
    text = _log_text(log_dir)
    assert SECRET not in text
    assert text.count("***REDACTED***") >= 5
    assert "calling provider with key" in text


def test_secret_keys_are_redacted_whatever_the_value(log_dir):
    log = get_logger("test")
    log.info(
        "headers",
        api_key="anything-1",
        apikey="anything-2",
        token="anything-3",
        hf_token=HF,
        authorization="Bearer abcdefgh12345678",
        password="hunter2hunter2",
        secret="anything-4",
        headers={"x-api-key": "anything-5", "api-key": "anything-6", "Content-Type": "json"},
    )
    text = _log_text(log_dir)
    for leaked in (
        "anything-1",
        "anything-2",
        "anything-3",
        HF,
        "abcdefgh12345678",
        "hunter2hunter2",
        "anything-4",
        "anything-5",
        "anything-6",
    ):
        assert leaked not in text
    assert "Content-Type" in text  # non-secret headers survive


def test_bearer_tokens_in_free_text_are_scrubbed(log_dir):
    get_logger("test").info("request headers: Authorization: Bearer abcdef0123456789xyz")
    text = _log_text(log_dir)
    assert "abcdef0123456789xyz" not in text


def test_exception_tracebacks_are_scrubbed(log_dir):
    register_secret(SECRET)
    log = get_logger("test")
    try:
        raise RuntimeError(f"upstream rejected key {SECRET}")
    except RuntimeError:
        log.exception("call failed")
    text = _log_text(log_dir)
    assert "call failed" in text
    assert SECRET not in text


def test_set_api_key_registers_key_for_redaction(log_dir, fake_keyring):
    key = "sk-registered-through-set-000"
    set_api_key("minimax", key)
    get_logger("test").info(f"oops printed {key}")
    assert key not in _log_text(log_dir)
    assert key not in "".join(r["message"] for r in RING_BUFFER.records)


def test_short_values_are_not_registered():
    before = set(logging_setup._secret_values)
    register_secret("abc")
    register_secret("")
    register_secret(None)
    assert logging_setup._secret_values == before


def test_ring_buffer_is_redacted_and_tailable(log_dir):
    register_secret(SECRET)
    logging.getLogger("thirdparty").warning("leak %s", SECRET)
    logging.getLogger("thirdparty").debug("debug line")
    tail = RING_BUFFER.tail(50)
    assert all(SECRET not in r["message"] for r in tail)
    assert any(r["message"] == "leak ***REDACTED***" for r in tail)
    warnings = RING_BUFFER.tail(50, level="WARNING")
    assert all(r["level"] in {"WARNING", "ERROR", "CRITICAL"} for r in warnings)


def test_log_file_location_and_level(tmp_path):
    directory = tmp_path / "logs2"
    configure_logging("WARNING", log_dir=directory)
    try:
        log = get_logger("lvl")
        log.info("quiet")
        log.warning("loud")
        text = _log_text(directory)
        assert "loud" in text
        assert "quiet" not in text
    finally:
        configure_logging("WARNING")
        structlog.reset_defaults()


def test_reconfigure_closes_previous_file_handler(tmp_path):
    first = tmp_path / "a"
    second = tmp_path / "b"
    configure_logging("INFO", log_dir=first)
    handler = logging_setup._file_handler
    configure_logging("INFO", log_dir=second)
    try:
        assert handler is not None
        assert handler.stream is None  # closed
        assert logging_setup._file_handler is not handler
        # the first file can be renamed (would fail on Windows if still held)
        (first / "aichat.log").rename(first / "moved.log")
    finally:
        configure_logging("WARNING")
        structlog.reset_defaults()


def test_rotation_keeps_backup_count(tmp_path):
    directory = tmp_path / "rot"
    configure_logging("INFO", log_dir=directory, max_bytes=2000, backup_count=2)
    try:
        log = get_logger("rot")
        for i in range(300):
            log.info(f"line {i} " + "x" * 50)
        flush_logging()
    finally:
        configure_logging("WARNING")
        structlog.reset_defaults()
    backups = [p for p in directory.iterdir() if p.name != "aichat.log"]
    assert 1 <= len(backups) <= 2
    assert (directory / "aichat.log").is_file()


def test_first_time():
    reset_first_time()
    assert first_time("k", 1) is True
    assert first_time("k", 1) is False
    assert first_time("k", 2) is True
    reset_first_time()
    assert first_time("k", 1) is True
