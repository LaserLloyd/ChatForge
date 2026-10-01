"""OpenAI-compatible streaming chat client (one code path for local OVMS and cloud).

Adapted from CrucibleForge crucibleforge/api.py (MIT, LaserLloyd): the ``stream_chat``
SSE loop (``data:`` framing, ``[DONE]``, in-stream ``error`` frames, usage and tok/s,
the "no SSE data in 200 response" check, stall and wall-clock timeouts) and
``_merge_tool_call_deltas``, converted from a sync ``httpx.Client`` function returning
a ``ChatResult`` into an ``httpx.AsyncClient`` async generator of stream events with a
cancel event. Cut: WrongModelError (``verify_model=False`` semantics), priority holds,
VRAM classes, retries (errors carry ``retryable``/``retry_after_s`` for the caller).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from chatforge.config import Timeouts
from chatforge.llm.errors import (
    LLMError,
    decode_lenient,
    error_frame_error,
    is_loopback_url,
    join_url,
    normalize_base_url,
    status_error,
    transport_error,
)
from chatforge.llm.events import (
    AssistantMessage,
    Completed,
    ContentDelta,
    ReasoningDelta,
    StreamEvent,
    ToolCall,
)
from chatforge.llm.quirks import REASONING_KEYS, Quirks
from chatforge.llm.thinking import StreamSplitter, merge_reasoning

_MAX_NON_SSE_CHARS = 1_000_000


@dataclass
class ChatRequest:
    model: str
    messages: list[dict]
    tools: list[dict] | None = None
    temperature: float | None = None
    max_tokens: int = 1024
    extra_body: dict = field(default_factory=dict)


# Adapted from CrucibleForge crucibleforge/api.py (MIT, LaserLloyd): _merge_tool_call_deltas.
def _merge_tool_call_deltas(raw_deltas: list[dict]) -> list[dict]:
    calls: dict[int, dict] = {}
    last: int | None = None
    for d in raw_deltas:
        idx = d.get("index")
        if not isinstance(idx, int) or isinstance(idx, bool):
            # No index (some OpenAI-compatible servers): a new call id starts a new call;
            # a fragment without an id continues the call before it.
            new_id = d.get("id")
            if last is None or (new_id and calls[last]["id"] and new_id != calls[last]["id"]):
                idx = max(calls) + 1 if calls else 0
            else:
                idx = last
        last = idx
        slot = calls.setdefault(idx, {"id": None, "name": "", "arguments": ""})
        if d.get("id"):
            slot["id"] = d["id"]
        fn = d.get("function") or {}
        name = fn.get("name")
        # Some servers repeat the full name on every fragment; only append new text.
        if name and name != slot["name"]:
            slot["name"] += name
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]
    return [calls[k] for k in sorted(calls)]


def _to_tool_calls(merged: list[dict]) -> list[ToolCall]:
    return [
        ToolCall(id=m["id"] or f"call_{i}", name=m["name"], arguments=m["arguments"])
        for i, m in enumerate(merged)
    ]


class _StreamState:
    def __init__(self) -> None:
        self.raw_parts: list[str] = []
        self.reasoning_parts: list[dict] = []
        self.tool_deltas: list[dict] = []
        self.finish_reason: str | None = None
        self.served_model: str | None = None
        self.usage: dict = {}
        self.first_t: float | None = None
        self.last_t: float | None = None

    def touch(self) -> None:
        now = time.monotonic()
        if self.first_t is None:
            self.first_t = now
        self.last_t = now


class OpenAICompatClient:
    """``POST {base_url}/chat/completions`` with ``stream: true``.

    ``stream_chat`` yields :class:`ContentDelta` / :class:`ReasoningDelta` and ends with
    one :class:`Completed`; every failure is an :class:`LLMError`.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        *,
        quirks: Quirks,
        timeouts: Timeouts | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self._api_key = api_key or None
        self.quirks = quirks
        self.timeouts = timeouts or Timeouts()
        self._local = is_loopback_url(self.base_url)
        t = self.timeouts
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(t.stall_s, connect=t.connect_s),
            follow_redirects=False,
            # A system proxy must never capture loopback traffic to the local OVMS server.
            trust_env=not self._local,
        )

    def __repr__(self) -> str:  # never show the key
        return f"OpenAICompatClient({self.base_url!r}, quirks={self.quirks.name!r})"

    async def __aenter__(self) -> OpenAICompatClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, *, sse: bool) -> dict[str, str]:
        headers = {"Accept": "text/event-stream" if sse else "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def build_body(self, req: ChatRequest) -> dict:
        """The request body after quirks (exposed for tests and the probe)."""
        body: dict[str, Any] = {
            "model": req.model,
            "messages": req.messages,
            "max_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.tools:
            body["tools"] = req.tools
        for k, v in (req.extra_body or {}).items():
            body.setdefault(k, v)
        return self.quirks.prepare_body(body)

    # ------------------------------------------------------------------ #
    # list_models
    # ------------------------------------------------------------------ #

    async def list_models(self) -> list[str]:
        """Model ids from ``GET /models``; ``[]`` when the endpoint is 404/405."""
        return parse_model_ids(await self.list_model_entries())

    async def list_model_entries(self) -> list[dict]:
        """The raw ``GET /models`` entries (id plus whatever metadata the server adds,
        such as context lengths); ``[]`` when the endpoint is 404/405."""
        url = join_url(self.base_url, "models")
        try:
            resp = await self._http.get(url, headers=self._headers(sse=False))
        except httpx.HTTPError as e:
            raise transport_error(
                e, url, local=self._local, read_timeout=self.timeouts.stall_s
            ) from e
        if resp.status_code in (404, 405):
            return []
        payload = decode_lenient(resp.content)
        if isinstance(payload, dict):
            self.quirks.check_payload(payload)
        if resp.status_code >= 400:
            raise status_error(
                resp.status_code,
                payload,
                url=url,
                key_used=bool(self._api_key),
                retry_after=resp.headers.get("Retry-After"),
            )
        return parse_model_entries(payload)

    # ------------------------------------------------------------------ #
    # stream_chat
    # ------------------------------------------------------------------ #

    async def stream_chat(
        self, req: ChatRequest, cancel: asyncio.Event | None = None
    ) -> AsyncIterator[StreamEvent]:
        t0 = time.monotonic()
        url = join_url(self.base_url, "chat/completions")
        body = self.build_body(req)
        request = self._http.build_request("POST", url, json=body, headers=self._headers(sse=True))
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        pump = asyncio.create_task(self._pump(request, queue))
        watcher = asyncio.create_task(self._watch_cancel(cancel, queue)) if cancel else None
        st = _StreamState()
        splitter = StreamSplitter(self.quirks.stream_rules)
        saw_sse = False
        non_sse: list[str] = []
        non_sse_len = 0
        try:
            kind, resp = await self._next(queue, t0, cancel, url)
            if kind == "error_body":
                status, headers, raw = resp
                payload = decode_lenient(raw)
                if isinstance(payload, dict):
                    self.quirks.check_payload(payload)
                raise status_error(
                    status,
                    payload,
                    url=url,
                    model=req.model,
                    key_used=bool(self._api_key),
                    retry_after=headers.get("Retry-After"),
                )
            while True:
                kind, line = await self._next(queue, t0, cancel, url)
                if kind == "eof":
                    break
                line = line.strip()
                if not line:
                    continue
                if not line.startswith("data:"):
                    if line.startswith((":", "event:", "id:", "retry:")):
                        continue
                    if non_sse_len < _MAX_NON_SSE_CHARS:
                        non_sse.append(line)
                        non_sse_len += len(line)
                    continue
                saw_sse = True
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                for ev in self._handle_chunk(data, st, splitter):
                    yield ev
            if not saw_sse:
                for ev in self._handle_non_sse("\n".join(non_sse), st, splitter):
                    yield ev
            for channel, text in splitter.flush():
                yield ContentDelta(text) if channel == "content" else ReasoningDelta(text)
            yield self._completed(st, t0)
        finally:
            pump.cancel()
            if watcher is not None:
                watcher.cancel()
            await asyncio.gather(pump, *([watcher] if watcher else []), return_exceptions=True)

    # -- plumbing ---------------------------------------------------------- #

    async def _pump(self, request: httpx.Request, queue: asyncio.Queue) -> None:
        resp: httpx.Response | None = None
        try:
            resp = await self._http.send(request, stream=True)
            if resp.status_code >= 400:
                raw = await resp.aread()
                queue.put_nowait(("error_body", (resp.status_code, resp.headers, raw)))
                return
            queue.put_nowait(("response", resp))
            async for line in resp.aiter_lines():
                queue.put_nowait(("line", line))
            queue.put_nowait(("eof", None))
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 - forwarded to the consumer
            queue.put_nowait(("exc", e))
        finally:
            if resp is not None:
                with contextlib.suppress(Exception):
                    await resp.aclose()

    @staticmethod
    async def _watch_cancel(cancel: asyncio.Event, queue: asyncio.Queue) -> None:
        await cancel.wait()
        queue.put_nowait(("cancel", None))

    async def _next(
        self, queue: asyncio.Queue, t0: float, cancel: asyncio.Event | None, url: str
    ) -> tuple[str, Any]:
        if cancel is not None and cancel.is_set():
            raise _cancelled()
        wall = self.timeouts.wall_s
        stall = self.timeouts.stall_s
        wall_left = t0 + wall - time.monotonic()
        if wall_left <= 0:
            raise _wall_timeout(wall)
        try:
            async with asyncio.timeout(min(stall, wall_left)):
                kind, val = await queue.get()
        except TimeoutError:
            if time.monotonic() - t0 >= wall - 0.01:
                raise _wall_timeout(wall) from None
            raise LLMError(
                "The model stopped sending data",
                code="stalled",
                hint=f"No data from {url} for {stall:.0f}s.",
            ) from None
        if kind == "cancel":
            raise _cancelled()
        if kind == "exc":
            if isinstance(val, LLMError):
                raise val
            if isinstance(val, httpx.HTTPError):
                raise transport_error(val, url, local=self._local, read_timeout=stall) from val
            raise LLMError(
                "The connection to the provider failed",
                code="unreachable",
                hint=f"{type(val).__name__}: {str(val)[:200]}",
            ) from val
        return kind, val

    def _handle_chunk(
        self, data: dict, st: _StreamState, splitter: StreamSplitter
    ) -> list[StreamEvent]:
        self.quirks.check_payload(data)
        if data.get("error"):
            raise error_frame_error(data["error"])
        if data.get("model"):
            st.served_model = data["model"]
        if isinstance(data.get("usage"), dict):
            st.usage = data["usage"]
        events: list[StreamEvent] = []
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") or choice.get("message") or {}
            if not isinstance(delta, dict):
                delta = {}
            reasoning = self.quirks.reasoning_from_delta(delta)
            part = {k: delta[k] for k in REASONING_KEYS if delta.get(k) is not None}
            if part:
                st.reasoning_parts.append(part)
            if reasoning:
                st.touch()
                events.append(ReasoningDelta(reasoning))
            content = delta.get("content")
            if isinstance(content, str) and content:
                st.touch()
                st.raw_parts.append(content)
                for channel, text in splitter.feed(content):
                    events.append(
                        ContentDelta(text) if channel == "content" else ReasoningDelta(text)
                    )
            if delta.get("tool_calls"):
                st.touch()
                st.tool_deltas.extend(d for d in delta["tool_calls"] if isinstance(d, dict))
            if choice.get("finish_reason"):
                st.finish_reason = choice["finish_reason"]
        return events

    def _handle_non_sse(
        self, text: str, st: _StreamState, splitter: StreamSplitter
    ) -> list[StreamEvent]:
        """A 200 without SSE framing: a provider error body (MiniMax ``base_resp``),
        or a server that ignored ``stream: true`` and sent a whole completion."""
        try:
            obj = json.loads(text) if text else None
        except ValueError:
            obj = None
        if isinstance(obj, dict):
            self.quirks.check_payload(obj)
            if obj.get("error"):
                raise error_frame_error(obj["error"])
            choices = obj.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                msg = choices[0].get("message")
                if isinstance(msg, dict):
                    calls = msg.get("tool_calls") or []
                    msg = {**msg, "tool_calls": [{**c, "index": i} for i, c in enumerate(calls)]}
                    choice = {**choices[0], "message": msg}
                    return self._handle_chunk({**obj, "choices": [choice]}, st, splitter)
        raise LLMError(
            "The provider answered without a stream",
            code="server",
            hint="No SSE data in the 200 response. "
            + (f"It said: {text[:200]}" if text else "The body was empty."),
        )

    def _completed(self, st: _StreamState, t0: float) -> Completed:
        streamed_calls = _to_tool_calls(_merge_tool_call_deltas(st.tool_deltas))
        raw = "".join(st.raw_parts)
        msg: AssistantMessage = self.quirks.finalize(raw, st.reasoning_parts, streamed_calls)
        finish = st.finish_reason
        if msg.tool_calls and not streamed_calls and finish in (None, "stop"):
            finish = "tool_calls"  # recovered from <tool_call> text
        msg.content = merge_reasoning(
            msg.content, msg.reasoning, has_tool_calls=bool(msg.tool_calls), finish_reason=finish
        )
        elapsed = time.monotonic() - t0
        tok_per_s = None
        completion = st.usage.get("completion_tokens")
        spanned = st.first_t is not None and st.last_t is not None and st.last_t > st.first_t
        if spanned and isinstance(completion, int) and completion >= 2:
            # the first token lands at first_t, so the span covers the other n-1
            tok_per_s = (completion - 1) / (st.last_t - st.first_t)
        return Completed(
            message=msg,
            finish_reason=finish,
            usage=dict(st.usage),
            served_model=st.served_model,
            elapsed_s=elapsed,
            tok_per_s=tok_per_s,
        )


def parse_model_entries(payload: Any) -> list[dict]:
    """``GET /models`` as ``[{"id": ..., ...metadata}]`` (a bare id becomes ``{"id": id}``)."""
    data = payload.get("data") if isinstance(payload, dict) else payload
    out: list[dict] = []
    for item in data if isinstance(data, list) else []:
        entry = dict(item) if isinstance(item, dict) else {"id": item}
        mid = entry.get("id")
        if isinstance(mid, str) and mid.strip():
            entry["id"] = mid.strip()
            out.append(entry)
    return out


def parse_model_ids(payload: Any) -> list[str]:
    """Sorted unique model ids from a ``GET /models`` payload or its entry list."""
    entries = (
        payload
        if isinstance(payload, list) and all(isinstance(e, dict) for e in payload)
        else parse_model_entries(payload)
    )
    return sorted({e["id"] for e in entries if isinstance(e.get("id"), str) and e["id"].strip()})


def _cancelled() -> LLMError:
    return LLMError("Generation stopped", code="cancelled", hint=None)


def _wall_timeout(wall: float) -> LLMError:
    return LLMError(
        "The reply took too long",
        code="timeout",
        hint=f"The request exceeded its {wall:.0f}s limit.",
    )
