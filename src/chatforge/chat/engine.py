"""The chat agent loop (PLAN §1.4 ``chat/engine.py``, §1.5 events, §1.6 streaming contract).

One request, as events (every event also carries ``type`` and ``request_id``)::

    chat.start      {provider, model, user_ts}       ``user_ts``: the ``_ts`` this message is
                                                     stored with
    chat.phase      {phase: loading_model}           local model not ready yet
    chat.context    {dropped_messages, first_kept_ts, message_cut}
                                                     the conversation does not fit the
                                                     model's context window: older turns
                                                     were left out of the prompt (it holds
                                                     the messages from the one stored at
                                                     ``first_kept_ts`` on) and/or this
                                                     message was cut (see below)
    chat.phase      {phase: generating}              each model round starts
    chat.phase      {phase: thinking}                first reasoning delta of a stretch
    chat.delta      {reasoning} | {content}
    chat.phase      {phase: calling_tool}
    chat.tool_call  {call_id, name, arguments}       arguments cut to 300 chars
    chat.tool_result{call_id, name, ok, summary, document?}
                                                     ``document`` {name, path, size, kind}:
                                                     a file the tool saved
                                                     (``create_document``)
    chat.reset      {reason}                         drop the text streamed so far
                                                     (``refusal``: the reply said it could
                                                     not look things up, so a tool runs and
                                                     the model answers again)
    chat.fallback   {from_provider, provider, model, reason}
                                                     the local model failed; the message
                                                     runs again on the fallback provider
    ... more rounds ...
    chat.done       {finish_reason, usage, elapsed_s, tok_per_s, content, model, rounds,
                     provider}
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

Regenerate (``regenerate(request_id, emit)``) runs the latest user message again, with
its attached files and original time, in place of the reply that followed it; the same
events follow. If the new reply fails, or is stopped before it says anything, the old
reply is put back and ``chat.error`` carries ``restored: true``.

Attached files (``send(..., attachments=[Extracted, ...])``) are stored on the user
message as ``_attachments`` with their text, next to the typed text; ``chat.history``
adds them to the prompt as ``<file name="...">`` blocks, cut to fit each model's budget.
``chat.max_prompt_chars`` limits the typed text only.

Context window (``chat.history.fit_prompt``, no summarising): every round's prompt keeps
the system prompt, this message and this turn's tool calls and results, and leaves out
the oldest whole turns that do not fit; if this message alone does not fit, its end is
cut. ``chat.context`` comes before the first round that leaves something out, again if a
later round of the turn leaves out something else (more tool results, or the fallback
provider's window), and once with ``dropped_messages: 0`` when a turn fits whole after
an earlier one did not, so the popup can move or remove its divider. A turn that ends
(done or stopped) stores where the model's view started as
``Conversation.context_start_ts``; a message that was cut is stored with ``_cut``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chatforge.attachments import MAX_FILES, as_record
from chatforge.chat import conversation as conv_store
from chatforge.chat import prompts, research
from chatforge.chat.conversation import Conversation
from chatforge.chat.history import Fitted, fit_prompt
from chatforge.llm.errors import LLMError, normalize_base_url
from chatforge.llm.events import Completed, ContentDelta, ReasoningDelta, ToolCall
from chatforge.logging_setup import get_logger
from chatforge.tools.registry import ToolNotAllowed, ToolResult

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
#: The local model's temperature cap once tool results are in the prompt.
GROUNDED_TEMPERATURE = 0.3
#: Added to the system prompt when ``create_document`` is offered.
DOCUMENTS_SENTENCE = (
    "When the user asks for a file or document, write it with create_document and mention its name."
)


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
    #: The attached files as ``_attachments`` records (``attachments.as_record``).
    files: list[dict[str, Any]] = field(default_factory=list)
    #: The user message's ``_ts`` (sent with ``chat.start``; a fallback keeps it).
    user_ts: float | None = None
    #: ``Conversation.context_start_ts`` when the turn started (what the popup shows).
    shown_start: float | None = None
    #: The last ``chat.context`` fields sent in this turn.
    context: dict[str, Any] | None = None
    #: What the latest round's prompt left out.
    fitted: Fitted | None = None
    #: Regenerate: the turn taken out of the conversation (user message first) and the
    #: index it came from, put back if the new reply fails or says nothing.
    restore: list[dict] | None = None
    restore_at: int = 0


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


def _plan_sig(plan: research.Plan) -> tuple[str, str]:
    """The duplicate-detection signature of an engine-made tool call."""
    return plan.name, _canonical_args(json.dumps(plan.arguments))


def _model_for(configured: str | None, spec: Any) -> str:
    """``configured`` if set, else the provider's default (or first) model."""
    return configured or spec.default_model or (spec.models[0] if spec.models else "")


def _with_documents_sentence(system: dict) -> dict:
    """``system`` with :data:`DOCUMENTS_SENTENCE` after the tool guidance (or before the
    date line when a custom prompt already carries its own guidance)."""
    content = str(system.get("content") or "")
    head, found, tail = content.partition(prompts.TOOLS_SENTENCE)
    if found:
        content = f"{head}{found} {DOCUMENTS_SENTENCE}{tail}"
    else:
        head, found, tail = content.partition("\nToday is ")
        content = (
            f"{head} {DOCUMENTS_SENTENCE}{found}{tail}"
            if found
            else f"{content}\n{DOCUMENTS_SENTENCE}"
        )
    return {**system, "content": content}


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
        """``[{role, content, ts, reasoning?, tools?, model?, stopped?, attachments?,
        documents?, cut?}]`` plus a ``{role: "notice", kind: "context_cut"}`` item where the
        model's view starts, for the popup (``Conversation.items``)."""
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

    async def send(
        self, text: str, request_id: str, emit: Emit, attachments: list[Any] | None = None
    ) -> None:
        """Run one user message to completion; progress and outcome arrive as events.
        ``attachments`` are ``attachments.Extracted`` files (or ``{name, kind, chars,
        truncated, text}`` dicts). Never raises (except when the task itself is cancelled
        from outside)."""
        await self._execute(request_id, emit, text, [as_record(f) for f in attachments or []])

    async def regenerate(self, request_id: str, emit: Emit) -> None:
        """Answer the latest user message again (Regenerate): its reply is replaced by a
        new one, asked with the same text and attached files. The old reply comes back if
        the new one fails, or is stopped before it says anything (``chat.error`` then
        carries ``restored: true``). With no user message: ``chat.error`` ``bad_request``.
        Never raises, like :meth:`send`."""
        await self._execute(request_id, emit, None, [])

    async def _execute(
        self, request_id: str, emit: Emit, text: str | None, files: list[dict[str, Any]]
    ) -> None:
        """``send`` (``text`` given) or ``regenerate`` (``text`` None)."""
        loop = asyncio.get_running_loop()
        req = _Request(id=request_id, emit=emit, task=asyncio.current_task(), loop=loop)
        req.files = files
        self._active[request_id] = req
        # The conversation and its rollback mark are taken only once this request holds
        # the lock: a request still queued behind another must never roll back, repair or
        # append to the conversation the running one is writing.
        conv = self._conv
        mark = len(conv)
        started = False
        try:
            async with self._send_lock:
                conv = self._conv
                mark = len(conv)
                started = True
                if text is None:
                    text = self._take_last_turn(req, conv)
                    mark = len(conv)
                await self._run(req, conv, text, self._mono())
        except asyncio.CancelledError:
            if not req.cancelled:
                raise  # shutdown, not a user Stop
            task = asyncio.current_task()
            if task is not None:
                while task.cancelling():
                    task.uncancel()
            if started:
                self._finish_cancelled(req, conv, text or "")
            else:
                self._emit_queued_cancel(req)
        except LLMError as exc:
            if not started:
                self._fail(req, conv, len(conv), exc)  # nothing of ours to roll back
            elif exc.code == "cancelled" or req.cancelled:
                self._finish_cancelled(req, conv, text or "")
            else:
                self._fail(req, conv, mark, exc)
        except Exception as exc:  # noqa: BLE001 - every failure becomes chat.error
            log.exception("chat_send_failed", request_id=request_id)
            self._fail(
                req,
                conv,
                mark if started else len(conv),
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

    def _user_message(self, req: _Request, text: str) -> dict:
        ts = req.user_ts if req.user_ts is not None else self._clock()
        msg: dict[str, Any] = {"role": "user", "content": text, "_ts": ts}
        if req.files:
            msg["_attachments"] = [dict(f) for f in req.files]
        return msg

    def _take_last_turn(self, req: _Request, conv: Conversation) -> str:
        """Regenerate: take the latest user message and everything after it out of
        ``conv`` (kept in ``req.restore``); its text is returned, its files and time go on
        ``req`` so ``_run`` records the message as it was."""
        idx = next(
            (i for i in range(len(conv) - 1, -1, -1) if conv.messages[i].get("role") == "user"),
            None,
        )
        if idx is None:
            raise LLMError("There is no message to answer again", code="bad_request")
        user = conv.messages[idx]
        req.files = [dict(f) for f in user.get("_attachments") or [] if isinstance(f, dict)]
        ts = user.get("_ts")
        req.user_ts = float(ts) if isinstance(ts, int | float) else None
        req.restore, req.restore_at = conv.messages[idx:], idx
        conv.rollback(idx)
        log.info("regenerate", request_id=req.id, dropped=len(req.restore) - 1)
        content = user.get("content")
        return content if isinstance(content, str) else ""

    def _restore(self, req: _Request, conv: Conversation) -> bool:
        """Put back the turn a failed or empty regenerate took out. True if it did."""
        if req.restore is None or conv is not self._conv:
            return False
        conv.rollback(req.restore_at)
        conv.messages.extend(req.restore)
        req.restore = None
        return True

    def _fail(self, req: _Request, conv: Conversation, mark: int, exc: LLMError) -> None:
        if conv is self._conv and len(conv) > mark:
            conv.rollback(mark)
        restored = self._restore(req, conv)
        log.info("chat_error", request_id=req.id, code=exc.code)
        fields: dict[str, Any] = {"restored": True} if restored else {}
        self._emit(
            req,
            "chat.error",
            code=exc.code or "server",
            message=exc.message,
            hint=exc.hint,
            action=getattr(exc, "action", None),
            # After a fallback this is the fallback provider, so a "no key" error asks
            # for the right key.
            provider=req.provider,
            **fields,
        )

    def _emit_queued_cancel(self, req: _Request) -> None:
        """A request stopped while still waiting for the lock: it never touched the
        conversation, so there is nothing to record or repair."""
        self._emit(
            req,
            "chat.error",
            code="cancelled",
            message="Stopped.",
            hint=None,
            action=None,
            partial=False,
        )

    def _finish_cancelled(self, req: _Request, conv: Conversation, text: str) -> None:
        partial = bool(req.round_content or req.round_reasoning)
        unloaded = req.cancel_reason == "unloaded"
        # A regenerate stopped before it said anything keeps the reply it was replacing.
        if not (partial or req.turn_parts) and self._restore(req, conv):
            self._persist()
            self._emit(
                req,
                "chat.error",
                code="cancelled",
                message="Stopped: the local model was unloaded." if unloaded else "Stopped.",
                hint=None,
                action=None,
                partial=False,
                restored=True,
            )
            return
        if conv is self._conv:
            if not req.user_recorded:
                conv.append(self._user_message(req, text))
                req.user_recorded = True
            self._store_context(req, conv)
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
        if (not text or not text.strip()) and not req.files:
            raise LLMError("Type a message first", code="bad_request")
        if len(req.files) > MAX_FILES:
            raise LLMError(f"Attach at most {MAX_FILES} files to one message", code="bad_request")
        if len(text) > limit:
            raise LLMError(
                f"The message is longer than {limit} characters",
                code="bad_request",
                hint="Shorten it, or raise the limit in Settings → General.",
            )
        pid = cfg.chat.provider
        provider = self._providers.get(pid)
        model = _model_for(cfg.chat.model, provider.spec)
        req.provider, req.model = pid, model
        if req.user_ts is None:  # a regenerate keeps the message's original time
            req.user_ts = self._clock()
        req.shown_start = conv.context_start_ts
        self._emit(req, "chat.start", provider=pid, model=model, user_ts=req.user_ts)

        mark = len(conv)
        try:
            await self._attempt(req, conv, text, cfg, provider, model)
        except LLMError as exc:
            fallback = self._fallback_for(cfg, provider.spec, exc, req)
            if fallback is None:
                raise
            fb_provider, fb_model = fallback
            log.info(
                "local_fallback",
                request_id=req.id,
                error=exc.code,
                provider=fb_provider.spec.id,
                model=fb_model,
            )
            if len(conv) > mark:
                conv.rollback(mark)
            self._reset_turn(req)
            req.provider, req.model = fb_provider.spec.id, fb_model
            self._emit(
                req,
                "chat.fallback",
                from_provider=pid,
                provider=fb_provider.spec.id,
                model=fb_model,
                reason=exc.message,
            )
            await self._attempt(req, conv, text, cfg, fb_provider, fb_model)

        content = "\n\n".join(p.strip() for p in req.turn_parts if p and p.strip())
        self._store_context(req, conv)
        self._emit(
            req,
            "chat.done",
            finish_reason=req.finish_reason,
            usage=req.usage,
            elapsed_s=round(self._mono() - t0, 3),
            tok_per_s=round(req.tok_per_s, 2) if req.tok_per_s is not None else None,
            content=content,
            model=req.served_model or req.model or model,
            rounds=req.rounds,
            provider=req.provider,
        )
        conv.trim()
        self._persist()

    async def _attempt(
        self, req: _Request, conv: Conversation, text: str, cfg: Any, provider: Any, model: str
    ) -> None:
        """Run the message on one provider: load/lease the model, then the tool rounds."""
        local = provider.spec.kind == "ovms"
        manager = getattr(provider, "manager", None) if local else None
        if local:
            status = manager.status() if manager is not None else {}
            if status.get("state") != "ready" or status.get("model_id") != model:
                self._phase(req, "loading_model")
        client = await provider.client_for(model)  # remote: LLMError(no_key); local: loads
        try:
            conv.append(self._user_message(req, text))
            req.user_recorded = True
            if local and manager is not None:
                async with self._lease(req, provider, manager, model) as base_url:
                    if normalize_base_url(base_url) != client.base_url:
                        await client.aclose()
                        client = provider.make_client(base_url)
                    await self._rounds(req, conv, provider, client, model, cfg, manager, text)
            else:
                await self._rounds(req, conv, provider, client, model, cfg, None, text)
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()

    def _fallback_for(
        self, cfg: Any, spec: Any, exc: LLMError, req: _Request
    ) -> tuple[Any, str] | None:
        """``(provider, model)`` to retry a failed local message on, or ``None``."""
        if req.cancelled or exc.code == "cancelled" or spec.kind != "ovms":
            return None
        fb = str(getattr(cfg.chat, "fallback_provider", "") or "").strip()
        if not fb or fb == spec.id:
            return None
        try:
            provider = self._providers.get(fb)
        except LLMError:
            log.warning("fallback_provider_unknown", provider=fb)
            return None
        if provider.spec.kind == "ovms":
            return None
        return provider, _model_for(getattr(cfg.chat, "fallback_model", ""), provider.spec)

    @staticmethod
    def _reset_turn(req: _Request) -> None:
        """Forget a failed attempt's progress before the fallback runs."""
        req.user_recorded = False
        req.phase = None
        req.round_content = req.round_reasoning = ""
        req.turn_parts = []
        req.usage = {}
        req.tok_per_s = None
        req.finish_reason = None
        req.served_model = None
        req.rounds = 0
        req.fitted = None

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
        text: str = "",
    ) -> None:
        max_rounds = int(cfg.chat.max_tool_rounds)
        local = provider.spec.kind == "ovms"
        use_tools = bool(provider.spec.supports_tools) and max_rounds > 0
        if use_tools and manager is not None and hasattr(manager, "model_supports_tools"):
            use_tools = bool(manager.model_supports_tools(model))
        enabled = list(cfg.tools.enabled) if use_tools else []
        schemas = self._schemas(enabled, local=local) if enabled else []
        offered = {s["function"]["name"] for s in schemas}
        budget = provider.budget(model)
        system = prompts.system_message(
            self._now(),
            cfg.chat.system_prompt,
            tools=bool(schemas),
            location=str(getattr(self._tools, "location", "") or ""),
            instructions=str(getattr(cfg.chat, "instructions", "") or ""),
        )
        if "create_document" in offered:
            system = _with_documents_sentence(system)

        tool_rounds = 0
        prev_sigs: set[tuple[str, str]] = set()
        final_round = False
        retried = False
        halve = False
        round_budget = budget
        # The small local model tends to refuse live-data questions instead of calling a
        # tool, so a clear live-data intent runs the tool before its first round.
        helped = False
        if local and offered and max_rounds > 0:
            plan = research.plan(text, offered, eager=True)
            if plan is not None:
                helped = True
                tool_rounds += 1
                prev_sigs = {_plan_sig(plan)}
                await self._auto_tool(req, conv, plan, enabled, budget.tool_result_chars, model)
        while True:
            round_tools = [] if final_round else schemas
            fitted = fit_prompt(system, conv.llm_history(), round_tools, round_budget, halve=halve)
            self._report_context(req, conv, fitted)
            request = provider.make_request(model, fitted.messages, round_tools or None)
            if local and tool_rounds and request.temperature is not None:
                # The small model embroiders tool results at its chat temperature.
                request.temperature = min(request.temperature, GROUNDED_TEMPERATURE)
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
                # A refusal ("I can't access real-time data") with tools on offer and none
                # used yet: drop it, look the answer up, and let the model answer again.
                if (
                    offered
                    and not final_round
                    and not helped
                    and tool_rounds == 0
                    and research.needs_lookup(msg.content)
                ):
                    plan = research.plan(text, offered, eager=False)
                    if plan is not None:
                        helped = True
                        tool_rounds += 1
                        prev_sigs = {_plan_sig(plan)}
                        self._drop_last_round(req, conv)
                        self._emit(req, "chat.reset", reason="refusal")
                        log.info("refusal_recovery", request_id=req.id, tool=plan.name)
                        await self._auto_tool(
                            req, conv, plan, enabled, budget.tool_result_chars, model
                        )
                        continue
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

    def _report_context(self, req: _Request, conv: Conversation, fitted: Fitted) -> None:
        """Remember what this round's prompt left out, and send ``chat.context`` when the
        popup has not been told yet (see the module docstring)."""
        req.fitted = fitted
        if fitted.message_cut:
            conv.mark_latest_cut()
        fields = {
            "dropped_messages": fitted.dropped,
            "first_kept_ts": fitted.first_kept_ts,
            "message_cut": fitted.message_cut,
        }
        if req.context is None:
            if not fitted.left_out and req.shown_start is None:
                return  # it all fits, as it did before
        elif fields == req.context:
            return
        req.context = fields
        log.info(
            "context_window",
            request_id=req.id,
            dropped=fitted.dropped,
            message_cut=fitted.message_cut,
        )
        self._emit(req, "chat.context", **fields)

    def _store_context(self, req: _Request, conv: Conversation) -> None:
        """A finished or stopped turn keeps where the model's view started with the
        conversation (``items()`` shows a notice there)."""
        if req.fitted is not None and conv is self._conv:
            conv.context_start_ts = req.fitted.first_kept_ts

    def _schemas(self, enabled: list[str], *, local: bool) -> list[dict]:
        try:
            return self._tools.schemas(enabled, local=local)
        except TypeError:  # a registry without the local subset
            return self._tools.schemas(enabled)

    def _drop_last_round(self, req: _Request, conv: Conversation) -> None:
        """Forget the round just recorded (its text was streamed; ``chat.reset`` clears it)."""
        if len(conv) and conv.messages[-1].get("role") == "assistant":
            conv.rollback(len(conv) - 1)
        if req.turn_parts:
            req.turn_parts.pop()

    async def _auto_tool(
        self,
        req: _Request,
        conv: Conversation,
        plan: research.Plan,
        enabled: list[str],
        max_chars: int,
        model: str,
    ) -> None:
        """Make ``plan``'s tool call on the model's behalf: recorded as an assistant
        tool call plus its result, exactly as if the model had asked for it."""
        call = ToolCall(
            id=f"call_auto_{uuid.uuid4().hex[:12]}",
            name=plan.name,
            arguments=json.dumps(plan.arguments, ensure_ascii=False),
        )
        conv.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [call.to_openai()],
                "_ts": self._clock(),
                "_model": model,
                "_auto": True,
            }
        )
        log.info("auto_tool", request_id=req.id, tool=plan.name)
        self._phase(req, "calling_tool")
        await self._run_tool(req, conv, call, enabled, max_chars)

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
        log.info("tool_call", request_id=req.id, tool=call.name, ok=bool(result.ok))
        document = getattr(result, "document", None)
        extra = {"document": dict(document)} if isinstance(document, dict) else {}
        self._emit(
            req,
            "chat.tool_result",
            call_id=call.id,
            name=call.name,
            ok=bool(result.ok),
            summary=result.summary,
            **extra,
        )
        message: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": call.id,
            "content": result.content,
            "_name": call.name,
            "_ok": bool(result.ok),
            "_summary": result.summary,
            "_ts": self._clock(),
        }
        if extra:
            message["_document"] = extra["document"]
        conv.append(message)

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
