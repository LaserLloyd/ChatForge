"""Fit a conversation into a model's prompt window (PLAN §1.4 ``chat/history.py``, §1.6, §3).

The local NPU runs with a static prompt window (``local.max_prompt_len``, 4096 by
default), so every request is trimmed before it is sent. Nothing is summarised: the
only cost is the size estimate. The order is fixed:

1. old tool results (before the latest user message) are cut to 300 characters;
2. the oldest whole turn groups are dropped (a group starts at a user message, so an
   assistant ``tool_calls`` message is never separated from its tool messages, and what
   is kept starts at a user message); the newest group with attached files goes last, so
   follow-up questions about a file work;
3. the attached files of that group are cut to share what is left (each cut file ends
   with a note saying so; on the local model the note suggests a cloud model);
4. the current turn's tool results are cut to share what is left of the budget;
5. the end of the latest user message is cut, and it ends with :data:`MESSAGE_CUT_NOTE`;
6. only when the system prompt and the tool schemas leave no room for even that is
   ``LLMError(context_overflow)`` raised (or, in the rare case that this turn's own tool
   calls and results do not fit even when cut short, with a "start a new chat" hint).

The system prompt, the latest user message and the current turn's tool calls and results
are never dropped. When earlier turns were dropped, :data:`CONTEXT_NOTE` becomes the last
line of the system message; its words never change, so a provider's prefix cache still
matches the next request. :func:`fit_prompt` also says what was left out (``Fitted``), for
the engine's ``chat.context`` event.

Room for the reply is left by the budgets themselves: a remote provider's is its context
window minus ``max_output_tokens`` and a 256-token margin (``llm.providers.prompt_tokens_for``).
On the NPU the budget is ``local.max_prompt_len`` minus a 128-token margin for estimate
error; that is the prompt limit OVMS enforces (longer prompts get a 400), and the reply is
not counted against it. Without a known window the cloud cap applies.

Attached files are stored on the user message as ``_attachments`` (``[{name, kind, chars,
truncated, text}]``) next to the typed text in ``content``; the copies returned here carry
the typed text followed by one ``<file name="...">`` block per file.

Attached pictures (``kind: "image"``, see ``chatforge.images``) reach a model that can see
them (``PromptBudget.vision``) as OpenAI-style content parts: the message's ``content``
becomes ``[{"type": "text", "text": ...}, {"type": "image_url", ...}, ...]``, the text part
holding the typed text, the file blocks and one ``[Image "name" (W×H) attached below.]``
line per picture. Only the pictures of the latest :data:`IMAGE_TURNS` user messages that
have some are shown (at most :data:`MAX_PROMPT_IMAGES`); older ones, and every picture for
a model that cannot see them, become a short note (with any text recognised in the
picture, ``ocr``). A picture is estimated at ``images.image_tokens`` tokens. When the
latest message does not fit, its pictures are left out (last first, each leaving a note)
before its text is cut (step 5). The parts point at the stored files
(``chatforge-image:<file>``); ``images.inline`` makes them ``data:`` URLs before sending.

Token counts are estimated at ``chars_per_token`` (3.0, conservative for Qwen). A
cloud budget (``max_prompt_tokens=None``) only trims past a generous cap.

History messages may carry private ``_``-prefixed keys (``_origin``, ``_visible``,
``_ts`` ...). They are kept on the returned copies (quirks strip them before sending)
and ignored when estimating size.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from chatforge import images
from chatforge.attachments import NOTE_CUT, NOTE_CUT_LOCAL, file_block, user_content
from chatforge.llm.errors import LLMError

#: Old tool results are cut to this many characters (step 1).
OLD_TOOL_RESULT_CHARS = 300
#: The smallest share a current tool result is cut to before giving up (step 4).
MIN_TOOL_RESULT_CHARS = 120
#: Estimated budget for providers without a known window (cloud), in tokens.
CLOUD_CAP_TOKENS = 100_000
#: Per-message chat-template overhead (role markers, separators), in tokens. WS1
#: measured about 5 on Qwen2.5 (docs/RUNTIME-NOTES.md, /v3/tokenize).
MESSAGE_OVERHEAD_TOKENS = 5
#: The tool-calling preamble the chat template adds when tools are offered (WS1: two
#: small schemas cost ~190 prompt tokens, more than their JSON alone).
TOOLS_TEMPLATE_OVERHEAD_TOKENS = 100
TRUNCATION_MARK = " …[truncated]"
#: The last line of the system message when earlier turns were dropped. Always the same
#: words, so the provider's prefix cache keeps matching.
CONTEXT_NOTE = (
    "(Too long for context: earlier messages in this conversation were cut to fit. "
    "Don't assume what they said; ask the user if you need them.)"
)
#: Ends the latest user message when its end was cut (step 5).
MESSAGE_CUT_NOTE = "\n\n… (Too long for context: the rest of this message was cut.)"
#: A model that sees pictures is shown those of the latest this-many user messages that
#: have pictures; older ones become a note.
IMAGE_TURNS = 3
#: The most pictures one prompt shows (the newest).
MAX_PROMPT_IMAGES = 10

#: What happens to one attached picture in a prompt.
SHOW = "show"  # an image_url part
BLIND = "blind"  # a note: the model cannot see pictures (with any recognised text)
EARLIER = "earlier"  # a note: an older picture, no longer shown
LEFT_OUT = "left_out"  # a note: it did not fit the context window
MISSING = "missing"  # a note: the stored file is gone

HINT_SYSTEM = (
    "Shorten the personality or instructions in Settings → General, or switch to a model "
    "with a larger context window."
)
HINT_TOO_LONG = (
    "The tool results and replies of this turn do not fit. Start a new chat, ask for "
    "less, or switch to a cloud model."
)


@dataclass
class PromptBudget:
    """How much prompt a provider/model accepts. ``max_prompt_tokens=None`` = cloud."""

    max_prompt_tokens: int | None = None
    chars_per_token: float = 3.0
    tool_result_chars: int = 1500
    #: ``None``: local exactly when ``max_prompt_tokens`` is set (the original rule). A
    #: remote provider with a known context window passes ``local=False``.
    local: bool | None = None
    #: The model can see pictures (``image_url`` content parts).
    vision: bool = False

    @property
    def limit_tokens(self) -> int:
        return self.max_prompt_tokens if self.max_prompt_tokens is not None else CLOUD_CAP_TOKENS

    @property
    def is_local(self) -> bool:
        return self.local if self.local is not None else self.max_prompt_tokens is not None

    def tokens(self, chars: int) -> int:
        return math.ceil(max(0, chars) / max(0.5, self.chars_per_token))

    def scaled(self, factor: float) -> PromptBudget:
        """A tighter copy (used for the one retry after a server-side overflow)."""
        if self.max_prompt_tokens is None:
            return PromptBudget(
                None, self.chars_per_token, self.tool_result_chars, self.local, self.vision
            )
        return PromptBudget(
            max(64, int(self.max_prompt_tokens * factor)),
            self.chars_per_token,
            self.tool_result_chars,
            self.local,
            self.vision,
        )


@dataclass
class Fitted:
    """The prompt :func:`fit_prompt` built, and what it left out of it."""

    messages: list[dict]
    #: How many stored messages were left out (whole turn groups).
    dropped: int = 0
    #: ``_ts`` of the first message of the unbroken stretch the model sees, up to the
    #: latest message; ``None`` when nothing was dropped. A file turn kept for follow-up
    #: questions (step 2) can sit before it.
    first_kept_ts: float | None = None
    #: The latest user message (its typed text or its attached files) was cut to fit.
    message_cut: bool = False

    @property
    def left_out(self) -> bool:
        return bool(self.dropped or self.message_cut)


# --------------------------------------------------------------------------- #
# Estimation helpers
# --------------------------------------------------------------------------- #


def public(msg: dict) -> dict:
    """The message without private ``_`` keys (what a provider may see)."""
    return {k: v for k, v in msg.items() if not str(k).startswith("_")}


def _sized_part(part: object) -> object:
    """A content part as its size is estimated: a picture counts by its own estimate
    (:func:`images.part_tokens`), not by the length of its URL."""
    if not isinstance(part, dict):
        return part
    if part.get("type") == "image_url":
        return {"type": "image_url"}
    return {k: v for k, v in part.items() if not str(k).startswith("_")}


def message_chars(msg: dict) -> int:
    """The JSON length of what a provider sees of ``msg`` (pictures not included)."""
    out = public(msg)
    content = out.get("content")
    if isinstance(content, list):
        out["content"] = [_sized_part(p) for p in content]
    return len(json.dumps(out, ensure_ascii=False, default=str))


def message_tokens(msg: dict, budget: PromptBudget) -> int:
    content = msg.get("content")
    pictures = sum(images.part_tokens(p) for p in content) if isinstance(content, list) else 0
    return budget.tokens(message_chars(msg)) + MESSAGE_OVERHEAD_TOKENS + pictures


def tools_tokens(tools: list[dict] | None, budget: PromptBudget) -> int:
    if not tools:
        return 0
    return budget.tokens(len(json.dumps(tools, ensure_ascii=False))) + (
        TOOLS_TEMPLATE_OVERHEAD_TOKENS
    )


def estimate_tokens(messages: list[dict], tools: list[dict] | None, budget: PromptBudget) -> int:
    return sum(message_tokens(m, budget) for m in messages) + tools_tokens(tools, budget)


def truncate_text(text: str, limit: int) -> str:
    if limit < 0 or len(text) <= limit:
        return text
    keep = max(0, limit - len(TRUNCATION_MARK))
    return text[:keep].rstrip() + TRUNCATION_MARK


def _truncate_tool(msg: dict, limit: int) -> dict:
    content = msg.get("content")
    if not isinstance(content, str) or len(content) <= limit:
        return msg
    out = dict(msg)
    out["content"] = truncate_text(content, limit)
    return out


def with_context_note(system: dict) -> dict:
    """A copy of the system message with :data:`CONTEXT_NOTE` as its last line."""
    content = system.get("content")
    text = content if isinstance(content, str) else ""
    return {**system, "content": f"{text}\n{CONTEXT_NOTE}" if text else CONTEXT_NOTE}


def text_of(content: object) -> str:
    """The text of a message's content: the string, or a part list's first text part."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                return str(part.get("text") or "")
    return ""


def with_text(msg: dict, text: str) -> dict:
    """A copy of ``msg`` whose text (see :func:`text_of`) is ``text``; pictures are kept."""
    content = msg.get("content")
    if not isinstance(content, list):
        return {**msg, "content": text}
    parts = list(content)
    for i, part in enumerate(parts):
        if isinstance(part, dict) and part.get("type") == "text":
            parts[i] = {**part, "text": text}
            break
    else:
        parts.insert(0, {"type": "text", "text": text})
    return {**msg, "content": parts}


def cut_message(msg: dict, keep: int) -> dict:
    """A copy of ``msg`` with only the first ``keep`` characters of its text, followed by
    :data:`MESSAGE_CUT_NOTE` (its pictures, if any, are kept)."""
    head = text_of(msg.get("content"))[: max(0, keep)].rstrip()
    return with_text(msg, head + MESSAGE_CUT_NOTE if head else MESSAGE_CUT_NOTE.lstrip())


# --------------------------------------------------------------------------- #
# Attached files
# --------------------------------------------------------------------------- #


def attached_files(msg: dict) -> list[dict]:
    """The ``_attachments`` of a user message (``[]`` for any other message)."""
    files = msg.get("_attachments") if msg.get("role") == "user" else None
    return [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []


def image_modes(history: list[dict], vision: bool) -> dict[int, dict[int, str]]:
    """``{message index: {attachment index: mode}}`` for every picture in ``history``
    (:data:`SHOW`, :data:`BLIND`, :data:`EARLIER` or :data:`MISSING`; see the module
    docstring)."""
    out: dict[int, dict[int, str]] = {}
    turns = shown = 0
    for k in range(len(history) - 1, -1, -1):
        files = attached_files(history[k])
        pictures = [i for i, f in enumerate(files) if images.is_image_record(f)]
        if not pictures:
            continue
        recent = vision and turns < IMAGE_TURNS
        turns += 1
        modes: dict[int, str] = {}
        for i in pictures:
            if not vision:
                modes[i] = BLIND
            elif not images.stored_file(files[i]):
                modes[i] = MISSING
            elif recent and shown < MAX_PROMPT_IMAGES:
                modes[i] = SHOW
                shown += 1
            else:
                modes[i] = EARLIER
        out[k] = modes
    return out


def _picture_note(record: dict, mode: str, limit: int | None, note: str) -> str:
    """The text a picture that is not shown leaves in the prompt: a note, then any text
    recognised in it (``ocr``) as a file block, cut to ``limit`` characters."""
    head = {
        EARLIER: images.NOTE_EARLIER,
        LEFT_OUT: images.NOTE_LEFT_OUT,
        MISSING: images.NOTE_MISSING,
    }.get(mode, images.NOTE_BLIND).format(images.describe(record))
    recognised = str(record.get("ocr") or "").strip()
    if not recognised or mode == MISSING:
        return head
    body, mark = recognised, None
    if limit is not None and len(body) > limit:
        body, mark = body[: max(0, limit)].rstrip(), note or NOTE_CUT
    name = str(record.get("name") or "image")
    return f"{head[:-1]} Text recognised in it:]{file_block(name, body, mark)}"


def _body_lengths(msg: dict, modes: dict[int, str] | None) -> list[int]:
    """The length of what each attached file puts in the prompt that can be cut: a text
    file's text, the recognised text of a picture that is not shown."""
    out: list[int] = []
    for i, f in enumerate(attached_files(msg)):
        if not images.is_image_record(f):
            out.append(len(str(f.get("text") or "")))
        elif (modes or {}).get(i, BLIND) in (SHOW, MISSING):
            out.append(0)
        else:
            out.append(len(str(f.get("ocr") or "").strip()))
    return out


def with_files(
    msg: dict,
    limits: list[int] | None = None,
    note: str = "",
    modes: dict[int, str] | None = None,
) -> dict:
    """A copy of a user message whose content is the typed text plus its file blocks
    (file ``i`` cut to ``limits[i]`` characters, ending with ``note``) and its pictures:
    ``modes[i]`` says what becomes of picture ``i`` (default :data:`BLIND`). With a
    picture to show the content is a list of parts (see the module docstring)."""
    out = dict(msg)
    typed = msg.get("content")
    texts: list[dict] = []
    text_limits: list[int] = []
    notes: list[str] = []
    shown: list[dict] = []
    for i, f in enumerate(attached_files(msg)):
        limit = limits[i] if limits is not None else None
        if not images.is_image_record(f):
            texts.append(f)
            if limit is not None:
                text_limits.append(limit)
            continue
        mode = (modes or {}).get(i, BLIND)
        if mode == SHOW:
            shown.append(f)
            notes.append(images.NOTE_SHOWN.format(images.describe(f)))
        else:
            notes.append(_picture_note(f, mode, limit, note))
    cuts = text_limits if limits is not None else None
    content = user_content(typed if isinstance(typed, str) else "", texts, cuts, note)
    if notes:
        joined = "\n\n".join(notes)
        content = f"{content}\n\n{joined}" if content.strip() else joined
    if shown:
        out["content"] = [{"type": "text", "text": content}, *map(images.ref_part, shown)]
    else:
        out["content"] = content
    return out


def share_out(lengths: list[int], total: int) -> list[int]:
    """Split ``total`` characters over files of ``lengths``: short files keep all of
    theirs and the longer ones share the rest equally."""
    limits = [0] * len(lengths)
    left = max(0, total)
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for k, i in enumerate(order):
        limits[i] = min(lengths[i], left // (len(order) - k))
        left -= limits[i]
    return limits


# --------------------------------------------------------------------------- #
# Turn groups
# --------------------------------------------------------------------------- #


def last_user_index(history: list[dict]) -> int:
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            return i
    return -1


def split_groups(history: list[dict]) -> list[list[dict]]:
    """Split into turn groups, each starting at a user message. Messages before the
    first user message form a leading group of their own."""
    groups: list[list[dict]] = []
    for msg in history:
        if msg.get("role") == "user" or not groups:
            groups.append([msg])
        else:
            groups[-1].append(msg)
    return groups


def drop_oldest_groups(history: list[dict], count: int) -> list[dict]:
    """Drop up to ``count`` of the oldest groups, never the current (latest-user) one."""
    if count <= 0:
        return list(history)
    groups = split_groups(history)
    current = groups[-1:] if last_user_index(history) >= 0 else groups
    old = groups[:-1] if last_user_index(history) >= 0 else []
    kept = old[min(count, len(old)) :]
    return [m for g in (*kept, *current) for m in g]


def _half(old_groups: int) -> int:
    """How many old groups the overflow retry drops: the older half."""
    return max(1, math.ceil(old_groups / 2)) if old_groups > 0 else 0


def halve_history(history: list[dict]) -> list[dict]:
    """Drop the older half of the old turn groups (what ``fit_prompt(halve=True)`` does
    first)."""
    old = len(split_groups(history)) - (1 if last_user_index(history) >= 0 else 0)
    return drop_oldest_groups(history, _half(old)) if old > 0 else list(history)


# --------------------------------------------------------------------------- #
# fit_prompt
# --------------------------------------------------------------------------- #


def fit_prompt(
    system: dict,
    history: list[dict],
    tools: list[dict],
    budget: PromptBudget,
    *,
    halve: bool = False,
) -> Fitted:
    """``[system, *history']`` trimmed to ``budget`` (see the module docstring), with
    what was left out. ``halve`` (the retry after a server-side overflow) drops the older
    half of the earlier turns first.

    Never mutates its inputs. Raises ``LLMError(code="context_overflow")``.
    """
    limit = budget.limit_tokens
    noted = with_context_note(system)
    tools_cost = tools_tokens(tools, budget)
    plain_fixed = message_tokens(system, budget) + tools_cost
    noted_fixed = message_tokens(noted, budget) + tools_cost
    # Attached files join their user message's content (at full length for now), and
    # pictures become parts or notes.
    modes = image_modes(history, budget.vision)
    msgs = [
        with_files(m, modes=modes.get(k)) if attached_files(m) else dict(m)
        for k, m in enumerate(history)
    ]
    stored = {id(m): h for m, h in zip(msgs, history, strict=True) if attached_files(h)}
    modes_of = {id(m): modes.get(k) for k, m in enumerate(msgs) if id(m) in stored}
    cut = last_user_index(msgs)
    note = NOTE_CUT_LOCAL if budget.is_local else NOTE_CUT
    current_limits: list[int] | None = None  # what step 3 cut the latest message's files to
    gone: list[list[dict]] = []  # the dropped turn groups
    message_cut = False

    sizes: dict[int, tuple[dict, int]] = {}  # id -> (message, tokens): each is sized once

    def size(m: dict) -> int:
        hit = sizes.get(id(m))
        if hit is None or hit[0] is not m:
            hit = sizes[id(m)] = (m, message_tokens(m, budget))
        return hit[1]

    def fixed() -> int:
        # Once a turn is dropped, the system message carries CONTEXT_NOTE.
        return noted_fixed if gone else plain_fixed

    def over(items: list[dict]) -> bool:
        return fixed() + sum(size(m) for m in items) > limit

    def flat(groups: list[list[dict]]) -> list[dict]:
        return [m for g in groups for m in g]

    def share_files(at: int, original: dict, file_modes: dict[int, str] | None) -> list[int]:
        """Cut the files of ``msgs[at]`` (stored as ``original``) to share what is left;
        returns the limits it settled on."""
        lengths = _body_lengths(original, file_modes)
        emptied = [
            *msgs[:at],
            with_files(original, [0] * len(lengths), note, file_modes),
            *msgs[at + 1 :],
        ]
        used = fixed() + sum(size(m) for m in emptied)
        total = int(max(0, limit - used) * budget.chars_per_token)
        while True:
            limits = share_out(lengths, total)
            msgs[at] = with_files(original, limits, note, file_modes)
            # JSON escaping (newlines, quotes) makes the cut text count a little longer.
            if not over(msgs) or total <= 0:
                return limits
            total = int(total * 0.85) if total > 64 else 0

    # Step 1: old tool results -> 300 chars (always on a local window and in the overflow
    # retry; cloud only if over).
    if cut > 0 and (budget.is_local or halve or over(msgs)):
        msgs = [
            _truncate_tool(m, OLD_TOOL_RESULT_CHARS) if i < cut and m.get("role") == "tool" else m
            for i, m in enumerate(msgs)
        ]

    # Turn groups, each knowing where it starts in ``history``.
    if cut >= 0:
        groups = split_groups(msgs)
        old, current = groups[:-1], groups[-1]
    else:
        old, current = [], msgs
    starts: dict[int, int] = {}
    pos = 0
    for g in old:
        starts[id(g)] = pos
        pos += len(g)
    if halve and old:
        n = _half(len(old))
        gone.extend(old[:n])
        del old[:n]

    # Step 2: drop the oldest whole turn groups; the newest group with files goes last.
    anchor = next((g for g in reversed([*old, current]) if id(g[0]) in stored), None)
    while old and over(flat(old) + current):
        victim = next((k for k, g in enumerate(old) if g is not anchor), None)
        if victim is None:
            break  # only the file group is left: its files are cut in step 3
        gone.append(old.pop(victim))
    msgs = flat(old) + current

    # Step 3: the files of that group share what is left (dropped whole if still too big).
    if anchor is not None and over(msgs):
        at = next(k for k, m in enumerate(msgs) if m is anchor[0])
        original = stored[id(anchor[0])]
        anchor_modes = modes_of[id(anchor[0])]
        limits = share_files(at, original, anchor_modes)
        if anchor is current:
            lengths = _body_lengths(original, anchor_modes)
            message_cut = any(k < n for k, n in zip(limits, lengths, strict=True))
            current_limits = limits
        elif over(msgs):
            old = [g for g in old if g is not anchor]
            gone.append(anchor)
            msgs = flat(old) + current

    # Step 4: the current turn's tool results share what is left.
    start = len(msgs) - len(current)
    if over(msgs):
        tool_idx = [i for i in range(start, len(msgs)) if msgs[i].get("role") == "tool"]
        if tool_idx:
            emptied = [{**m, "content": ""} if i in tool_idx else m for i, m in enumerate(msgs)]
            used = fixed() + sum(size(m) for m in emptied)
            spare_chars = int(max(0, limit - used) * budget.chars_per_token)
            share = max(MIN_TOOL_RESULT_CHARS, spare_chars // len(tool_idx))
            originals = {i: msgs[i] for i in tool_idx}
            while True:
                for i in tool_idx:
                    msgs[i] = _truncate_tool(originals[i], share)
                # JSON escaping can make a cut result a little longer than ``share``.
                if not over(msgs) or share <= MIN_TOOL_RESULT_CHARS:
                    break
                share = max(MIN_TOOL_RESULT_CHARS, int(share * 0.85))

    # Step 4b: the latest message's pictures are left out, the last first (files that
    # step 3 cut share the room each one leaves).
    if cut >= 0 and start < len(msgs) and msgs[start].get("role") == "user" and over(msgs):
        latest_modes = dict(modes.get(cut) or {})
        for i in sorted((i for i, m in latest_modes.items() if m == SHOW), reverse=True):
            latest_modes[i] = LEFT_OUT
            message_cut = True
            if current_limits is not None:
                current_limits = share_files(start, history[cut], latest_modes)
            else:
                msgs[start] = with_files(history[cut], None, note, latest_modes)
            if not over(msgs):
                break

    # Step 5: the end of the latest user message is cut.
    latest = msgs[start] if cut >= 0 and start < len(msgs) else None
    if latest is not None and latest.get("role") != "user":
        latest = None
    if latest is not None and over(msgs):
        others = sum(size(m) for k, m in enumerate(msgs) if k != start)
        room = limit - fixed() - others - message_tokens(with_text(latest, ""), budget)
        keep = int(max(0, room) * budget.chars_per_token) - len(MESSAGE_CUT_NOTE)
        while True:
            msgs[start] = cut_message(latest, keep)
            # JSON escaping makes the cut text count a little longer than ``keep``.
            if not over(msgs) or keep <= 0:
                break
            keep = int(keep * 0.85) if keep > 64 else 0
        if over(msgs):
            msgs[start] = latest  # cutting it is not enough: step 6 says why
        else:
            message_cut = True

    # Step 6: give up with a useful message.
    if over(msgs):
        core = [cut_message(latest, 0)] if latest is not None else []
        if fixed() + sum(message_tokens(m, budget) for m in core) > limit:
            raise LLMError(
                "The system prompt is too long for this model",
                code="context_overflow",
                hint=HINT_SYSTEM,
                details={"limit_tokens": limit},
            )
        raise LLMError(
            "The conversation is too long for this model",
            code="context_overflow",
            hint=HINT_TOO_LONG,
            details={"limit_tokens": limit},
        )

    first_kept_ts = None
    if gone:
        # Everything after the newest dropped group is kept.
        tail = max(starts[id(g)] + len(g) for g in gone)
        first_kept_ts = history[tail].get("_ts")
    return Fitted(
        [noted if gone else dict(system), *msgs],
        dropped=sum(len(g) for g in gone),
        first_kept_ts=first_kept_ts,
        message_cut=message_cut,
    )


def fit_messages(
    system: dict, history: list[dict], tools: list[dict], budget: PromptBudget
) -> list[dict]:
    """``fit_prompt(...).messages``: the prompt alone."""
    return fit_prompt(system, history, tools, budget).messages


__all__ = [
    "CLOUD_CAP_TOKENS",
    "CONTEXT_NOTE",
    "IMAGE_TURNS",
    "MAX_PROMPT_IMAGES",
    "MESSAGE_CUT_NOTE",
    "OLD_TOOL_RESULT_CHARS",
    "Fitted",
    "PromptBudget",
    "attached_files",
    "cut_message",
    "drop_oldest_groups",
    "estimate_tokens",
    "fit_messages",
    "fit_prompt",
    "halve_history",
    "image_modes",
    "last_user_index",
    "share_out",
    "split_groups",
    "text_of",
    "truncate_text",
    "with_context_note",
    "with_files",
    "with_text",
]
