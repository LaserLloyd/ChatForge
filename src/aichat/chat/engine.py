"""The chat agent loop (PLAN §1.4 ``chat/engine.py``, §1.5 events, §1.6 streaming contract).

One request, as events (every event also carries ``type`` and ``request_id``)::

    chat.start      {provider, model}
    chat.phase      {phase: loading_model}           local model not ready yet
    chat.phase      {phase: generating}              each model round starts
    chat.phase      {phase: thinking}                first reasoning delta of a stretch
    chat.delta      {reasoning} | {content}
    chat.phase      {phase: calling_tool}
    chat.tool_call  {call_id, name, arguments}       arguments cut to 300 chars
    chat.tool_result{call_id, name, ok, summary}
    ... more rounds ...
    chat.done       {finish_reason, usage, elapsed_s, tok_per_s, content, model, rounds}
  or
    chat.error      {code, message, hint, action}    ``cancelled`` keeps the partial text

``chat.done.content`` is the turn's final visible text (rounds joined by a blank line).
It can differ from the streamed text: a reply whose content is empty but whose
reasoning is not shows the reasoning (``thinking.merge_reasoning``), and tags the
stream hid are gone. The UI should render ``content`` when the turn ends.

Conversation rules: a request refused before it runs (``no_key``, over-long text,
unknown provider, load failure) records nothing; any other error rolls the
conversation back to where it was, so Retry re-sends cleanly. A cancel keeps the user
message and the partial reply (marked ``stopped``) and answers any unanswered tool
call with "Not run (stopped).".
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from aichat.chat import conversation as conv_store
from aichat.chat import prompts
from aichat.chat.conversation import Conversation
from aichat.chat.history import fit_messages, halve_history
from aichat.llm.errors import LLMError, normalize_base_url
from aichat.llm.events import Completed, ContentDelta, ReasoningDelta, ToolCall
from aichat.logging_setup import get_logger
from aichat.tools.registry import ToolNotAllowed, ToolResult

log = get_logger(__name__)

Emit = Callable[[dict], None]

TOOL_ARGUMENTS_EVENT_CHARS = 300
#: The budget is tightened by this factor for the one retry after a server overflow.
OVERFLOW_RETRY_FACTOR = 0.75
NOTE_LIMIT = "Not run: the tool-call limit for this message was reached. Answer the user now."
NOTE_DUPLICATE = (
    "Not run: this exact call was already made; its result is above. Answer the user now."
)
NOTE_NO_TOOLS = "Not run: tools are not available for this answer."


@dataclass
class _Request:
    id: str
    emit: Emit
    task: asyncio.Task | None
    loop: asyncio.AbstractEventLoop
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False
    cancel_reason: str = "user"
    provider: str | None = None
    model: str | None = None
    phase: str | None = None
    user_recorded: bool = False
    round_content: str = ""
    round_reasoning: str = ""
    turn_parts: list[str] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    tok_per_s: float | None = None
    finish_reason: str | None = None
    served_model: str | None = None
    rounds: int = 0


#: What OVMS says (HTTP 400) when the prompt is over ``--max_prompt_len``.
OVMS_OVERFLOW_TEXT = "input length exceeds the maximum allowed length"


def _is_overflow(exc: LLMError) -> bool:
    """WS3 maps the OVMS overflow 400 to ``context_overflow``; the text is matched too,
    in case a server words its 400 so that it maps to ``bad_request``."""
    if exc.code == "context_overflow":
        return True
    said = f"{exc.message} {exc.hint or ''}".lower()
    return exc.code == "bad_request" and OVMS_OVERFLOW_TEXT in said


def _canonical_args(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments or "{}"), sort_keys=True, ensure_ascii=False)
    except ValueError:
        return (arguments or "").strip()


def _add_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
    for key, value in (usage or {}).items():
        if isinstance(value, bool):
            continue
        if isinstance(value, int | float):
            total[key] = total.get(key, 0) + value


class ChatEngine:
    """Runs one message at a time against the selected provider.

    * ``providers``: ``llm.providers.ProviderRegistry`` (its local provider carries the
      ``LocalModelManager`` as ``.manager``).
    * ``tools``: ``tools.registry.ToolRegistry`` (``schemas`` + ``call``).
    * ``get_config``: returns the live ``AppConfig`` (read at every send).
    * ``conversation_file``: ``Paths.conversation_file``; loaded now and saved after
      every turn while ``chat.persist_conversation`` is on (deleted when it is off).
    """

    def __init__(
        self,
        *,
        providers: Any,
        tools: Any,
        get_config: Callable[[], Any],
        conversation_file: Path | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._providers = providers
        self._tools = tools
        self._get_config = get_config
        self._file = Path(conversation_file) if conversation_file is not None else None
        self._clock = clock
        self._mono = monotonic
        self._now = now or (lambda: datetime.now().astimezone())
        self._active: dict[str, _Request] = {}
        self._send_lock = asyncio.Lock()
        self._conv = Conversation()
        if self._file is not None and self._persist_enabled():
            self._conv = conv_store.load(self._file)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def conversation(self) -> Conversation:
        return self._conv

    def conversation_items(self) -> list[dict]:
        """``[{role, content, ts, reasoning?, tools?, model?, stopped?}]`` for the popup."""
        return self._conv.items()

    def snapshot(self) -> dict:
        """For ``get_state``: ``{"conversation": [...items], "busy", "request_id"}``."""
        active = next(iter(self._active), None)
        return {
            "conversation": self._conv.items(),
            "busy": active is not None,
            "request_id": active,
        }

    def busy(self) -> bool:
        return bool(self._active)

    def cancel(self, request_id: str, reason: str = "user") -> None:
        """Stop ``request_id`` (unknown or finished ids are ignored). Thread-safe."""
        req = self._active.get(request_id)
        if req is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not req.loop:
            req.loop.call_soon_threadsafe(self._cancel_now, request_id, reason)
            return
        self._cancel_now(request_id, reason)

    def cancel_all(self, reason: str = "user") -> None:
        for rid in list(self._active):
            self.cancel(rid, reason)

    def new_chat(self) -> None:
        """Stop anything running and start an empty conversation."""
        self.cancel_all("new_chat")
        self._conv = Conversation()
        self._persist()

    async def send(self, text: str, request_id: str, emit: Emit) -> None:
        """Run one user message to completion; progress and outcome arrive as events.
        Never raises (except when the task itself is cancelled from outside)."""
        loop = asyncio.get_running_loop()
        req = _Request(id=request_id, emit=emit, task=asyncio.current_task(), loop=loop)
        self._active[request_id] = req
        conv = self._conv
        mark = len(conv)
        t0 = self._mono()
        try:
            async with self._send_lock:
                await self._run(req, conv, text, t0)
        except asyncio.CancelledError:
            if not req.cancelled:
                raise  # shutdown, not a user Stop
            task = asyncio.current_task()
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            self._finish_cancelled(req, conv, text)
        except LLMError as exc:
            if exc.code == "cancelled" or req.cancelled:
                self._finish_cancelled(req, conv, text)
            else:
                self._fail(req, conv, mark, exc)
        except Exception as exc:  # noqa: BLE001 - every failure becomes chat.error
            log.exception("chat_send_failed", request_id=request_id)
            self._fail(
                req,
                conv,
                mark,
                LLMError(
                    "Something went wrong",
                    code="server",
                    hint=f"{type(exc).__name__}. See Settings → Logs.",
                ),
            )
        finally:
            self._active.pop(request_id, None)

    # ------------------------------------------------------------------ #
    # Internals: request lifecycle
    # ------------------------------------------------------------------ #

    def _cancel_now(self, request_id: str, reason: str) -> None:
        req = self._active.get(request_id)
        if req is None or req.cancelled:
            return
        req.cancelled = True
        req.cancel_reason = reason
        req.cancel_event.set()
        if req.task is not None and not req.task.done():
            req.task.cancel()

    def _emit(self, req: _Request, etype: str, **fields: Any) -> None:
        event = {"type": etype, "request_id": req.id, **fields}
        try:
            req.emit(event)
        except Exception:  # noqa: BLE001 - a broken sink must not break the turn
            log.exception("chat_emit_failed", event_type=etype)

    def _phase(self, req: _Request, phase: str) -> None:
        if req.phase != phase:
            req.phase = phase
            self._emit(req, "chat.phase", phase=phase)

    def _user_message(self, text: str) -> dict:
        return {"role": "user", "content": text, "_ts": self._clock()}

    def _fail(self, req: _Request, conv: Conversation, mark: int, exc: LLMError) -> None:
        if conv is self._conv and len(conv) > mark:
            conv.rollback(mark)
        log.info("chat_error", request_id=req.id, code=exc.code)
        self._emit(
            req,
            "chat.error",
            code=exc.code or "server",
            message=exc.message,
            hint=exc.hint,
            action=getattr(exc, "action", None),
        )

    def _finish_cancelled(self, req: _Request, conv: Conversation, text: str) -> None:
        partial = bool(req.round_content or req.round_reasoning)
        if conv is self._conv:
            if not req.user_recorded:
                conv.append(self._user_message(text))
                req.user_recorded = True
            if partial:
                msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": req.round_content,
                    "_ts": self._clock(),
                    "_stopped": True,
                }
                if req.round_reasoning:
                    msg["_reasoning"] = req.round_reasoning
                if req.model:
                    msg["_model"] = req.model
                conv.append(msg)
            conv.repair_tool_calls(ts=self._clock())
            self._persist()
        unloaded = req.cancel_reason == "unloaded"
        self._emit(
            req,
            "chat.error",
            code="cancelled",
            message="Stopped: the local model was unloaded." if unloaded else "Stopped.",
            hint=None,
            action=None,
            partial=partial or bool(req.turn_parts),
        )

    # ------------------------------------------------------------------ #
    # Internals: one message
    # ------------------------------------------------------------------ #

    async def _run(self, req: _Request, conv: Conversation, text: str, t0: float) -> None:
        cfg = self._get_config()
        limit = int(cfg.chat.max_prompt_chars)
        if not text or not text.strip():
            raise LLMError("Type a message first", code="bad_request")
        if len(text) > limit:
            raise LLMError(
                f"The message is longer than {limit} characters",
                code="bad_request",
                hint="Shorten it, or raise the limit in Settings → General.",
            )
        pid = cfg.chat.provider
        provider = self._providers.get(pid)
        spec = provider.spec
        model = cfg.chat.model or spec.default_model or (spec.models[0] if spec.models else "")
        req.provider, req.model = pid, model
        self._emit(req, "chat.start", provider=pid, model=model)

        local = spec.kind == "ovms"
        manager = getattr(provider, "manager", None) if local else None
        if local:
            status = manager.status() if manager is not None else {}
            if status.get("state") != "ready" or status.get("model_id") != model:
                self._phase(req, "loading_model")
        client = await provider.client_for(model)  # remote: LLMError(no_key); local: loads
        try:
            conv.append(self._user_message(text))
            req.user_recorded = True
            if local and manager is not None:
                async with self._lease(req, provider, manager, model) as base_url:
                    if normalize_base_url(base_url) != client.base_url:
                        await client.aclose()
                        client = provider.make_client(base_url)
                    await self._rounds(req, conv, provider, client, model, cfg, manager)
            else:
                await self._rounds(req, conv, provider, client, model, cfg, None)
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

        content = "\n\n".join(p.strip() for p in req.turn_parts if p and p.strip())
        self._emit(
            req,
            "chat.done",
            finish_reason=req.finish_reason,
            usage=req.usage,
            elapsed_s=round(self._mono() - t0, 3),
            tok_per_s=round(req.tok_per_s, 2) if req.tok_per_s is not None else None,
            content=content,
            model=req.served_model or model,
            rounds=req.rounds,
        )
        conv.trim()
        self._persist()

    def _lease(self, req: _Request, provider: Any, manager: Any, model: str) -> Any:
        def on_cancel() -> None:
            self._cancel_now(req.id, "unloaded")

        try:
            return manager.lease(model, on_cancel=on_cancel)
        except TypeError:  # a manager without cancel callbacks
            return provider.lease(model)

    async def _rounds(
        self,
        req: _Request,
        conv: Conversation,
        provider: Any,
        client: Any,
        model: str,
        cfg: Any,
        manager: Any,
    ) -> None:
        max_rounds = int(cfg.chat.max_tool_rounds)
        use_tools = bool(provider.spec.supports_tools) and max_rounds > 0
        if use_tools and manager is not None and hasattr(manager, "model_supports_tools"):
            use_tools = bool(manager.model_supports_tools(model))
        enabled = list(cfg.tools.enabled) if use_tools else []
        schemas = self._tools.schemas(enabled) if enabled else []
        budget = provider.budget(model)
        system = prompts.system_message(self._now(), cfg.chat.system_prompt, tools=bool(schemas))

        tool_rounds = 0
        prev_sigs: set[tuple[str, str]] = set()
        final_round = False
        retried = False
        halve = False
        round_budget = budget
        while True:
            history = conv.llm_history()
            if halve:
                history = halve_history(history)
            offered = [] if final_round else schemas
            messages = fit_messages(system, history, offered, round_budget)
            request = provider.make_request(model, messages, offered or None)
            try:
                completed = await self._stream_round(req, client, request)
            except LLMError as exc:
                fresh = not (req.round_content or req.round_reasoning)
                if _is_overflow(exc) and not retried and fresh:
                    retried, halve = True, True
                    round_budget = budget.scaled(OVERFLOW_RETRY_FACTOR)
                    log.info("context_overflow_retry", request_id=req.id)
                    continue
                raise
            msg = completed.message
            self._record_round(req, conv, client, completed, model)
            if not msg.tool_calls:
                return
            sigs = {(c.name, _canonical_args(c.arguments)) for c in msg.tool_calls}
            duplicate = bool(sigs & prev_sigs)
            if final_round or duplicate or tool_rounds >= max_rounds:
                note = NOTE_NO_TOOLS if final_round else NOTE_DUPLICATE if duplicate else NOTE_LIMIT
                for call in msg.tool_calls:
                    conv.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": note,
                            "_name": call.name,
                            "_ok": False,
                            "_summary": "not run",
                            "_hidden": True,
                            "_ts": self._clock(),
                        }
                    )
                if final_round:
                    return
                log.info(
                    "tool_loop_stop",
                    request_id=req.id,
                    reason="duplicate" if duplicate else "max_rounds",
                    rounds=tool_rounds,
                )
                final_round = True
                continue
            tool_rounds += 1
            prev_sigs = sigs
            self._phase(req, "calling_tool")
            for call in msg.tool_calls:
                await self._run_tool(req, conv, call, enabled, budget.tool_result_chars)

    def _record_round(
        self, req: _Request, conv: Conversation, client: Any, completed: Completed, model: str
    ) -> None:
        msg = completed.message
        record = client.quirks.history_message(msg)
        record["_ts"] = self._clock()
        record["_model"] = completed.served_model or model
        if msg.reasoning:
            record["_reasoning"] = msg.reasoning
        conv.append(record)
        req.round_content = req.round_reasoning = ""
        req.turn_parts.append(msg.content or "")
        req.rounds += 1
        _add_usage(req.usage, completed.usage)
        if completed.tok_per_s is not None:
            req.tok_per_s = completed.tok_per_s
        req.finish_reason = completed.finish_reason
        req.served_model = completed.served_model or req.served_model

    async def _stream_round(self, req: _Request, client: Any, request: Any) -> Completed:
        self._phase(req, "generating")
        req.round_content = req.round_reasoning = ""
        separate = any(p.strip() for p in req.turn_parts)
        first_content = True
        async for ev in client.stream_chat(request, req.cancel_event):
            if isinstance(ev, ReasoningDelta):
                if not ev.text:
                    continue
                self._phase(req, "thinking")
                req.round_reasoning += ev.text
                self._emit(req, "chat.delta", reasoning=ev.text)
            elif isinstance(ev, ContentDelta):
                if not ev.text:
                    continue
                self._phase(req, "generating")
                out = ev.text
                if first_content and separate:
                    out = "\n\n" + out
                first_content = False
                req.round_content += ev.text
                self._emit(req, "chat.delta", content=out)
            elif isinstance(ev, Completed):
                return ev
        raise LLMError("The reply ended unexpectedly", code="server", hint="Try again.")

    async def _run_tool(
        self,
        req: _Request,
        conv: Conversation,
        call: ToolCall,
        enabled: list[str],
        max_chars: int,
    ) -> None:
        self._emit(
            req,
            "chat.tool_call",
            call_id=call.id,
            name=call.name,
            arguments=(call.arguments or "")[:TOOL_ARGUMENTS_EVENT_CHARS],
        )
        try:
            result = await self._tools.call(
                call.name, call.arguments, enabled=enabled, max_chars=max_chars
            )
        except ToolNotAllowed:
            result = ToolResult(
                False,
                f"The tool '{call.name}' is not enabled. Answer without it.",
                f"{call.name} is disabled",
            )
        except Exception as exc:  # noqa: BLE001 - a tool bug must not end the turn
            log.warning("tool_failed", tool=call.name, error=type(exc).__name__)
            result = ToolResult(False, f"Tool {call.name} failed.", f"{call.name} failed")
        self._emit(
            req,
            "chat.tool_result",
            call_id=call.id,
            name=call.name,
            ok=bool(result.ok),
            summary=result.summary,
        )
        conv.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": result.content,
                "_name": call.name,
                "_ok": bool(result.ok),
                "_summary": result.summary,
                "_ts": self._clock(),
            }
        )

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _persist_enabled(self) -> bool:
        try:
            return bool(self._get_config().chat.persist_conversation)
        except Exception:  # noqa: BLE001
            return False

    def _persist(self) -> None:
        if self._file is None:
            return
        try:
            if self._persist_enabled():
                conv_store.save(self._file, self._conv)
            else:
                conv_store.delete(self._file)
        except OSError as exc:
            log.warning("conversation_save_failed", error=str(exc)[:200])


__all__ = ["ChatEngine"]
