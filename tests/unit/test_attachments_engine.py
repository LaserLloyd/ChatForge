"""Attached files and saved documents through chat.history and the chat engine."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

from chatforge.attachments import NOTE_CUT, NOTE_CUT_LOCAL, Extracted
from chatforge.chat import prompts
from chatforge.chat.engine import DOCUMENTS_SENTENCE, _with_documents_sentence
from chatforge.chat.history import PromptBudget, estimate_tokens, fit_messages
from tests.fakes.openai_server import fake_openai_server, sse, text_chunks, tool_call_chunks
from tests.unit.test_engine import Harness
from tests.unit.test_manager import QWEN

SYSTEM = {"role": "system", "content": "You are terse."}


def record(name: str, text: str, kind: str = "text", truncated: bool = False) -> dict:
    return {"name": name, "kind": kind, "chars": len(text), "truncated": truncated, "text": text}


def user(text: str, *files: dict) -> dict:
    msg = {"role": "user", "content": text, "_ts": 1.0}
    if files:
        msg["_attachments"] = list(files)
    return msg


# --------------------------------------------------------------------------- #
# chat.history.fit_messages
# --------------------------------------------------------------------------- #


def test_small_files_are_sent_whole() -> None:
    history = [user("Summarize", record("notes.txt", "line one\nline two"))]
    out = fit_messages(SYSTEM, history, [], PromptBudget())
    assert out[-1]["content"] == 'Summarize\n\n<file name="notes.txt">\nline one\nline two\n</file>'
    assert history[0]["content"] == "Summarize"  # the stored message is untouched


def test_local_window_cuts_long_files_and_says_so() -> None:
    short = record("short.txt", "s" * 100)
    long = record("long.txt", "word " * 4000)
    history = [user("Compare these", short, long)]
    before = copy.deepcopy(history)
    budget = PromptBudget(max_prompt_tokens=1000)
    out = fit_messages(SYSTEM, history, [], budget)
    content = out[-1]["content"]
    assert estimate_tokens(out, None, budget) <= 1000
    assert "s" * 100 + "\n</file>" in content  # the short file fits whole
    assert content.count(NOTE_CUT_LOCAL) == 1 and content.rstrip().endswith("</file>")
    assert content.startswith('Compare these\n\n<file name="short.txt">')
    assert history == before


def test_cloud_window_note() -> None:
    history = [user("Read", record("big.md", "x" * 20_000))]
    budget = PromptBudget(max_prompt_tokens=2000, local=False)
    out = fit_messages(SYSTEM, history, [], budget)
    assert NOTE_CUT in out[-1]["content"] and NOTE_CUT_LOCAL not in out[-1]["content"]
    assert estimate_tokens(out, None, budget) <= 2000


def test_follow_up_keeps_the_file_turn_longest() -> None:
    history = [
        user("old " + "a" * 1500),
        {"role": "assistant", "content": "b" * 1500},
        user("Read this", record("spec.txt", "the spec says 42")),
        {"role": "assistant", "content": "Read it."},
        user("later " + "c" * 1500),
        {"role": "assistant", "content": "d" * 1500},
        user("What number does the spec give?"),
    ]
    budget = PromptBudget(max_prompt_tokens=700)
    out = fit_messages(SYSTEM, history, [], budget)
    contents = [m["content"] for m in out[1:]]
    assert contents == [
        'Read this\n\n<file name="spec.txt">\nthe spec says 42\n</file>',
        "Read it.",
        "What number does the spec give?",
    ]


def test_a_file_turn_that_cannot_fit_is_dropped() -> None:
    history = [
        user("q" * 3000, record("a.txt", "x" * 5000)),
        {"role": "assistant", "content": "ok"},
        user("Next question"),
    ]
    out = fit_messages(SYSTEM, history, [], PromptBudget(max_prompt_tokens=300))
    assert [m["content"] for m in out[1:]] == ["Next question"]


# --------------------------------------------------------------------------- #
# Engine: attachments
# --------------------------------------------------------------------------- #


def notes(text: str = "line one\nline two", name: str = "notes.txt") -> Extracted:
    return Extracted(name, "text", text, len(text))


async def test_attached_file_reaches_the_model_and_survives_a_restart(tmp_path) -> None:
    with fake_openai_server(sse(text_chunks("Summary.", 1)), sse(text_chunks("Two.", 1))) as srv:
        h = Harness(tmp_path, srv.base_url)
        await h.engine.send("Summarize", "r1", h.events.append, attachments=[notes()])
        first = srv.chat_requests[0].json["messages"]
        h.engine = h.new_engine()  # a restart reads conversation.json
        await h.engine.send("And the second line?", "r2", h.events.append)
        follow = srv.chat_requests[1].json["messages"]
    block = '<file name="notes.txt">\nline one\nline two\n</file>'
    assert first[-1] == {"role": "user", "content": f"Summarize\n\n{block}"}
    assert follow[1] == first[-1]  # the follow-up still carries the file
    stored = h.engine.conversation.messages[0]
    assert stored["content"] == "Summarize"
    assert stored["_attachments"] == [
        {
            "name": "notes.txt",
            "kind": "text",
            "chars": 17,
            "truncated": False,
            "text": "line one\nline two",
        }
    ]
    items = h.engine.conversation_items()
    assert items[0] == {
        "role": "user",
        "content": "Summarize",
        "ts": 1_700_000_000.0,
        "attachments": [{"name": "notes.txt", "kind": "text", "chars": 17, "truncated": False}],
    }
    assert "attachments" not in items[2]  # a message without files has none


async def test_files_only_and_the_typed_text_limit(tmp_path) -> None:
    long_file = notes("z" * 5000)
    with fake_openai_server(sse(text_chunks("ok", 1))) as srv:
        h = Harness(tmp_path, srv.base_url, chat={"max_prompt_chars": 10})
        await h.engine.send("", "r1", h.events.append, attachments=[long_file])
        sent = srv.chat_requests[0].json["messages"][-1]["content"]
        events = await h.send("x" * 11, "r2")
    # chat.max_prompt_chars limits what was typed, not the attached text.
    assert sent.startswith('<file name="notes.txt">\n' + "z" * 100)
    assert h.of("chat.done")[0]["content"] == "ok"
    assert events[-1]["type"] == "chat.error" and events[-1]["code"] == "bad_request"


async def test_more_than_ten_files_are_refused(tmp_path) -> None:
    with fake_openai_server() as srv:
        h = Harness(tmp_path, srv.base_url)
        files = [notes(name=f"f{i}.txt") for i in range(11)]
        await h.engine.send("Read all", "r1", h.events.append, attachments=files)
        assert srv.chat_requests == []
    err = h.events[-1]
    assert err["type"] == "chat.error" and err["code"] == "bad_request" and "10" in err["message"]
    assert h.engine.conversation_items() == []


async def test_local_model_gets_a_cut_file_with_a_note(tmp_path) -> None:
    big = notes("The quick brown fox. " * 3000, name="book.txt")
    with fake_openai_server(sse(text_chunks("A fox.", 1)), prefix="/v3") as srv:
        h = Harness(
            tmp_path, srv.base_url, provider="local-npu", chat={"model": QWEN}, manager=True
        )
        await h.engine.send("Summarize the book", "r1", h.events.append, attachments=[big])
        body = srv.chat_requests[0].json
    assert h.events[-1]["type"] == "chat.done"
    budget = h.providers.get("local-npu").budget(QWEN)
    assert estimate_tokens(body["messages"], body.get("tools"), budget) <= budget.limit_tokens
    content = body["messages"][-1]["content"]
    assert content.startswith('Summarize the book\n\n<file name="book.txt">\nThe quick brown fox.')
    assert NOTE_CUT_LOCAL in content
    # Local models are not offered create_document, so the prompt does not mention it.
    assert DOCUMENTS_SENTENCE not in body["messages"][0]["content"]
    assert "create_document" not in json.dumps(body.get("tools") or [])
    # The stored file keeps its full text for a later cloud model.
    stored = h.engine.conversation.messages[0]["_attachments"][0]
    assert stored["text"] == big.text and stored["chars"] == big.chars


async def test_cancel_keeps_the_attached_files(tmp_path) -> None:
    slow = sse(text_chunks("one two three four five six", 6), event_delay_s=0.15)
    with fake_openai_server(slow) as srv:
        h = Harness(tmp_path, srv.base_url)
        task = asyncio.create_task(
            h.engine.send("Read", "r9", h.events.append, attachments=[notes()])
        )
        async with asyncio.timeout(10):
            while not h.of("chat.delta"):
                await asyncio.sleep(0.01)
        h.engine.cancel("r9")
        await task
    assert h.events[-1]["code"] == "cancelled"
    items = h.engine.conversation_items()
    assert items[0]["attachments"][0]["name"] == "notes.txt" and items[1]["stopped"] is True


# --------------------------------------------------------------------------- #
# Engine: create_document
# --------------------------------------------------------------------------- #


async def test_create_document_reaches_the_event_and_the_items(tmp_path) -> None:
    docs = tmp_path / "docs"
    call = sse(
        tool_call_chunks(
            [("d1", "create_document", json.dumps({"filename": "plan.md", "content": "# Plan\n"}))]
        )
    )
    with fake_openai_server(call, sse(text_chunks("I saved plan.md.", 1))) as srv:
        h = Harness(tmp_path, srv.base_url, tools={"documents_dir": str(docs)})
        events = await h.send("Write me a plan as a file")
        first = srv.chat_requests[0].json
    expected = {"name": "plan.md", "path": str(docs / "plan.md"), "size": 7, "kind": "text"}
    result = h.of("chat.tool_result", events)[0]
    assert result["ok"] is True and result["document"] == expected
    assert (docs / "plan.md").read_text(encoding="utf-8") == "# Plan\n"
    item = h.engine.conversation_items()[1]
    assert item["documents"] == [expected] and item["content"] == "I saved plan.md."
    tool_msg = next(m for m in h.engine.conversation.messages if m["role"] == "tool")
    assert tool_msg["_document"] == expected
    # The system prompt asks for create_document right after the tool guidance.
    system = first["messages"][0]["content"]
    assert f"{prompts.TOOLS_SENTENCE} {DOCUMENTS_SENTENCE}" in system
    assert "create_document" in [t["function"]["name"] for t in first["tools"]]


async def test_no_documents_sentence_without_the_tool(tmp_path) -> None:
    with fake_openai_server(sse(text_chunks("ok", 1))) as srv:
        h = Harness(tmp_path, srv.base_url, tools={"enabled": ["calculator"]})
        events = await h.send("Hi")
        system = srv.chat_requests[0].json["messages"][0]["content"]
    assert DOCUMENTS_SENTENCE not in system
    assert all("document" not in e for e in h.of("chat.tool_result", events))


def test_documents_sentence_with_a_custom_prompt_that_has_its_own_guidance() -> None:
    custom = "Pirate. For anything current or local, use tools."
    system = prompts.system_message(None, custom, tools=True)
    out = _with_documents_sentence(system)["content"]
    assert out.startswith(f"{custom} {DOCUMENTS_SENTENCE}\nToday is ")
    assert _with_documents_sentence({"role": "system", "content": "X"})["content"] == (
        f"X\n{DOCUMENTS_SENTENCE}"
    )


def test_conversation_items_without_paths_ignore_documents(tmp_path: Path) -> None:
    from chatforge.chat.conversation import Conversation

    conv = Conversation(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "create_document", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "failed", "_document": {"name": "x"}},
            {"role": "assistant", "content": "Sorry."},
        ]
    )  # fmt: skip
    assert "documents" not in conv.items()[1]
