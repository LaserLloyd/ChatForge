"""LLM error type and the HTTP/transport → ``LLMError`` mapping.

``normalize_base_url``, ``provider_message``, ``status_error`` and ``transport_error``
are Adapted from DisPatch backend/app/llm_api.py (MIT, LaserLloyd): the same wording
and decisions, re-targeted from ``ApiError(message, detail)`` onto ``LLMError`` codes.
"""

from __future__ import annotations

import email.utils
import json
import time
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import httpx

from chatforge.errors import AppError

LLMCode = Literal[
    "no_key",
    "auth",
    "region_or_key",
    "rate_limit",
    "quota",
    "balance",
    "context_overflow",
    "bad_request",
    "not_found",
    "unreachable",
    "timeout",
    "stalled",
    "server",
    "model_loading_failed",
    "cancelled",
]
LLMAction = Literal["add_key", "open_settings", "retry"] | None

# code -> (retryable, default action)
_DEFAULTS: dict[str, tuple[bool, str | None]] = {
    "no_key": (False, "add_key"),
    "auth": (False, "open_settings"),
    "region_or_key": (False, "open_settings"),
    "rate_limit": (True, "retry"),
    "quota": (False, "retry"),
    "balance": (False, None),
    "context_overflow": (False, None),
    "bad_request": (False, None),
    "not_found": (False, "open_settings"),
    "unreachable": (True, "retry"),
    "timeout": (True, "retry"),
    "stalled": (True, "retry"),
    "server": (True, "retry"),
    "model_loading_failed": (False, "retry"),
    "cancelled": (False, None),
}

_UNSET: Any = object()


class LLMError(AppError):
    """A provider/transport failure the UI can explain. ``code`` is one of
    :data:`LLMCode`; ``retryable`` says whether an automatic retry can help."""

    code: str | None = "server"

    def __init__(
        self,
        message: str,
        *,
        code: LLMCode = "server",
        hint: str | None = None,
        action: LLMAction = _UNSET,
        retryable: bool | None = None,
        status: int | None = None,
        retry_after_s: float | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        default_retryable, default_action = _DEFAULTS.get(code, (False, None))
        details = dict(details or {})
        if status is not None:
            details.setdefault("status", status)
        if retry_after_s is not None:
            details.setdefault("retry_after_s", retry_after_s)
        super().__init__(message, code=code, hint=hint, details=details)
        self.action = default_action if action is _UNSET else action
        self.retryable = default_retryable if retryable is None else retryable
        self.status = status
        self.retry_after_s = retry_after_s

    def to_payload(self) -> dict[str, Any]:
        payload = super().to_payload()
        payload["retryable"] = self.retryable
        return payload

    def __repr__(self) -> str:
        return f"LLMError(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #


# Adapted from DisPatch backend/app/llm_api.py (MIT, LaserLloyd): normalize_base_url.
def normalize_base_url(url: str | None) -> str:
    """Validate + tidy an operator-supplied base URL (http/https with a host,
    no trailing slash). Raises ``LLMError(code="bad_request")``."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise LLMError(
            "A base URL is required",
            code="bad_request",
            hint="Enter the address of the server, e.g. http://127.0.0.1:1234/v1",
            action="open_settings",
        )
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise LLMError(
            "The base URL must start with http:// or https://",
            code="bad_request",
            hint=(
                f"Got {parsed.scheme or 'no'} scheme. Only http and https are supported; "
                "a bare host:port is read as a scheme, so write the http:// prefix out in full."
            ),
            action="open_settings",
        )
    if not parsed.hostname:
        raise LLMError(
            "The base URL has no host",
            code="bad_request",
            hint=f"{url!r} does not name a server to connect to.",
            action="open_settings",
        )
    return urlunparse(parsed._replace(path=parsed.path.rstrip("/")))


def join_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def is_loopback_url(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1") or host.endswith(".localhost")


# --------------------------------------------------------------------------- #
# Body helpers
# --------------------------------------------------------------------------- #


def decode_lenient(body: bytes) -> Any:
    """An error body as parsed JSON, or its first 300 characters of text."""
    text = body.decode("utf-8", "replace").strip()
    try:
        return json.loads(text)
    except ValueError:
        return text[:300]


# Adapted from DisPatch backend/app/llm_api.py (MIT, LaserLloyd): _provider_message.
def provider_message(payload: Any) -> str:
    """Pull the human-readable sentence out of a provider's error body."""
    if isinstance(payload, str):
        return payload.strip()[:300]
    if isinstance(payload, dict):
        err = payload.get("error", payload)
        if isinstance(err, str):
            return err.strip()[:300]
        if isinstance(err, dict):
            for k in ("message", "detail", "error", "msg"):
                v = err.get(k)
                if isinstance(v, str) and v.strip():
                    return v.strip()[:300]
        for k in ("message", "detail"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()[:300]
        base = payload.get("base_resp")
        if isinstance(base, dict) and isinstance(base.get("status_msg"), str):
            return base["status_msg"].strip()[:300]
    return ""


def parse_retry_after(value: str | None) -> float | None:
    """``Retry-After`` as seconds (delta-seconds or an HTTP date)."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


_OVERFLOW_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "context window",
    "too many tokens",
    "prompt is too long",
    "max_prompt_len",
    "exceeds the maximum",
    "input is too long",
    "token limit",
)
_QUOTA_MARKERS = ("insufficient_quota", "quota", "exceeded your current")


def _looks(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


# Adapted from DisPatch backend/app/llm_api.py (MIT, LaserLloyd): _status_error.
def status_error(
    status: int,
    payload: Any,
    *,
    url: str,
    model: str = "",
    key_used: bool = False,
    retry_after: str | None = None,
) -> LLMError:
    """Map an HTTP status (plus the provider's own words) onto an LLMError."""
    said = provider_message(payload)
    raw = said or (json.dumps(payload)[:500] if not isinstance(payload, str) else payload)
    tail = f" The provider said: {said}" if said else ""
    if status in (401, 403):
        return LLMError(
            "The provider rejected the API key",
            code="auth",
            status=status,
            hint=(
                "No API key was sent; this provider requires one."
                if not key_used
                else "The key was not accepted. Check it has not been revoked, and that it "
                "belongs to the provider (and region) you selected."
            )
            + tail,
            action="open_settings" if key_used else "add_key",
        )
    if status == 402:
        return LLMError(
            "The provider account is out of credit",
            code="balance",
            status=status,
            hint="Top up the account balance." + tail,
        )
    if status == 404:
        return LLMError(
            "The provider could not find that model or endpoint",
            code="not_found",
            status=status,
            hint=(
                f"{url} returned 404. Either the model {model!r} does not exist on this "
                "account, or the base URL is missing its version prefix (most "
                "OpenAI-compatible servers want one ending in /v1)." + tail
            )
            if model
            else f"{url} returned 404; check the base URL.{tail}",
        )
    if status == 408:
        return LLMError(
            "The provider timed out", code="timeout", status=status, hint=f"{url} returned 408."
        )
    if status == 413 or (status in (400, 422) and _looks(raw, _OVERFLOW_MARKERS)):
        return LLMError(
            "The conversation is too long for this model",
            code="context_overflow",
            status=status,
            hint="Shorten the message, start a new chat, or switch to a cloud model." + tail,
        )
    if status in (400, 422):
        return LLMError(
            "The provider rejected the request",
            code="bad_request",
            status=status,
            hint=f"{url} returned {status}.{tail}",
        )
    if status == 429:
        wait = parse_retry_after(retry_after)
        if _looks(raw, _QUOTA_MARKERS):
            return LLMError(
                "The provider quota is used up",
                code="quota",
                status=status,
                retry_after_s=wait,
                hint="The plan's usage window is exhausted; wait for it to reset." + tail,
            )
        when = f" Try again in {wait:.0f}s." if wait is not None else " Try again shortly."
        return LLMError(
            "The provider is rate-limiting this key",
            code="rate_limit",
            status=status,
            retry_after_s=wait,
            hint="Too many requests." + when + tail,
        )
    if 500 <= status < 600:
        return LLMError(
            "The provider had an error",
            code="server",
            status=status,
            retry_after_s=parse_retry_after(retry_after),
            hint=f"{url} returned {status}.{tail} This is the provider's end; try again in a moment.",
        )
    return LLMError(
        f"The provider returned HTTP {status}", code="server", status=status, hint=f"{url}{tail}"
    )


# Adapted from DisPatch backend/app/llm_api.py (MIT, LaserLloyd): _transport_error.
def transport_error(e: BaseException, url: str, *, local: bool, read_timeout: float) -> LLMError:
    """Connection-level failures, phrased for local vs remote causes."""
    if isinstance(e, httpx.ConnectTimeout | httpx.ConnectError):
        return LLMError(
            "Could not reach the provider",
            code="unreachable",
            hint=(
                f"Nothing answered at {url}. The local model server may have stopped; "
                "try again to reload it."
                if local
                else f"Could not connect to {url}. Check the address, and that this "
                "machine has network access to it."
            )
            + f" ({type(e).__name__})",
        )
    if isinstance(e, httpx.ReadTimeout | httpx.WriteTimeout | httpx.PoolTimeout):
        return LLMError(
            "The provider took too long to answer",
            code="stalled",
            hint=f"No reply from {url} within {int(read_timeout)}s.",
        )
    return LLMError(
        "The connection to the provider failed",
        code="unreachable",
        hint=f"{type(e).__name__} talking to {url}: {str(e)[:200]}",
    )


def error_frame_error(err: Any) -> LLMError:
    """An in-stream ``data: {"error": ...}`` frame."""
    said = provider_message({"error": err}) or json.dumps(err)[:300]
    code_field = err.get("code") if isinstance(err, dict) else None
    type_field = err.get("type") if isinstance(err, dict) else None
    blob = f"{said} {code_field} {type_field}"
    status = None
    if isinstance(code_field, int):
        status = code_field
    elif isinstance(code_field, str) and code_field.isdigit():
        status = int(code_field)
    if _looks(blob, _OVERFLOW_MARKERS):
        return LLMError(
            "The conversation is too long for this model",
            code="context_overflow",
            hint="Shorten the message, start a new chat, or switch to a cloud model. "
            f"The provider said: {said}",
        )
    if status == 429 or _looks(blob, ("rate_limit", "rate limit", "too many requests")):
        return LLMError(
            "The provider is rate-limiting this key",
            code="rate_limit",
            hint=f"The provider said: {said}",
        )
    if status is not None and 400 <= status < 600:
        return status_error(status, {"error": err}, url="the stream")
    return LLMError("The provider reported an error mid-stream", code="server", hint=said)
