"""Pictures in the prompt (chat.history) and through each provider's quirks."""

from __future__ import annotations

import copy

import pytest

from chatforge import images
from chatforge.chat.history import (
    IMAGE_TURNS,
    MAX_PROMPT_IMAGES,
    PromptBudget,
    cut_message,
    estimate_tokens,
    fit_messages,
    fit_prompt,
    message_chars,
    message_tokens,
)
from chatforge.llm.minimax import MiniMaxQuirks
from chatforge.llm.quirks import DeepSeekQuirks, GenericQuirks, OpenAIQuirks, OvmsQuirks

SYSTEM = {"role": "system", "content": "You are terse."}
VISION = PromptBudget(vision=True)
BLIND = PromptBudget()


def pic(name: str = "photo.jpg", n: int = 1, w: int = 1600, h: int = 1200, **extra) -> dict:
    """A stored picture's ``_attachments`` record (the file need not exist here)."""
    return {
        "name": name,
        "kind": "image",
        "chars": 0,
        "truncated": False,
        "text": "",
        "file": f"{n:064x}.jpg",
        "media_type": "image/jpeg",
        "width": w,
        "height": h,
        "thumb": "data:image/jpeg;base64,AAAA",
        **extra,
    }


def doc(name: str, text: str) -> dict:
    return {"name": name, "kind": "text", "chars": len(text), "truncated": False, "text": text}


def user(text: str, *files: dict, ts: float = 1.0) -> dict:
    msg = {"role": "user", "content": text, "_ts": ts}
    if files:
        msg["_attachments"] = list(files)
    return msg


def reply(text: str = "ok", ts: float = 1.5) -> dict:
    return {"role": "assistant", "content": text, "_ts": ts}


def refs(msg: dict) -> list[str]:
    """The stored files a prompt message shows, in order."""
    content = msg["content"]
    if not isinstance(content, list):
        return []
    return [
        p["image_url"]["url"].removeprefix(images.REF_SCHEME)
        for p in content
        if p.get("type") == "image_url"
    ]


def text(msg: dict) -> str:
    content = msg["content"]
    return content if isinstance(content, str) else content[0]["text"]


# --------------------------------------------------------------------------- #
# Vision and not
# --------------------------------------------------------------------------- #


def test_a_vision_model_gets_the_picture_as_a_part_after_the_text() -> None:
    history = [user("What is this?", doc("notes.txt", "a note"), pic())]
    before = copy.deepcopy(history)
    out = fit_messages(SYSTEM, history, [], VISION)
    content = out[-1]["content"]
    assert isinstance(content, list) and len(content) == 2
    assert content[0] == {
        "type": "text",
        "text": 'What is this?\n\n<file name="notes.txt">\na note\n</file>\n\n'
        '[Image "photo.jpg" (1600×1200) attached below.]',
    }
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"] == {"url": f"{images.REF_SCHEME}{1:064x}.jpg"}
    assert history == before  # the stored message is untouched


def test_a_picture_alone_still_has_a_text_part() -> None:
    out = fit_messages(SYSTEM, [user("", pic())], [], VISION)
    assert out[-1]["content"][0] == {
        "type": "text",
        "text": '[Image "photo.jpg" (1600×1200) attached below.]',
    }


def test_a_model_that_cannot_see_gets_a_note_and_any_recognised_text() -> None:
    out = fit_messages(
        SYSTEM, [user("Read it", pic("a.png"), pic("b.png", 2, ocr="Total: 42"))], [], BLIND
    )
    assert out[-1]["content"] == (
        "Read it\n\n"
        '[Image "a.png" (1600×1200) attached — this model cannot see images.]\n\n'
        '[Image "b.png" (1600×1200) attached — this model cannot see images. Text recognised '
        'in it:]\n\n<file name="b.png">\nTotal: 42\n</file>'
    )


def test_only_the_latest_picture_turns_are_shown() -> None:
    history = []
    for k in range(IMAGE_TURNS + 2):
        history += [user(f"look {k}", pic(f"p{k}.jpg", k + 1), ts=float(k)), reply(ts=k + 0.5)]
    history.append(user("and now?", ts=99.0))
    out = fit_messages(SYSTEM, history, [], VISION)
    users = [m for m in out if m["role"] == "user"]
    shown = [refs(m) for m in users]
    assert shown[:2] == [[], []]
    assert all(len(s) == 1 for s in shown[2 : 2 + IMAGE_TURNS])
    assert text(users[0]).endswith(
        '[Image "p0.jpg" (1600×1200) was attached earlier; it is no longer shown to the model.]'
    )


def test_at_most_max_prompt_images_are_shown() -> None:
    many = [pic(f"p{i}.jpg", i + 1) for i in range(MAX_PROMPT_IMAGES)]
    history = [user("older", pic("old.jpg", 99)), reply(), user("all of these", *many)]
    out = fit_messages(SYSTEM, history, [], VISION)
    assert len(refs(out[-1])) == MAX_PROMPT_IMAGES
    assert refs(out[1]) == [] and "no longer shown" in text(out[1])


def test_a_picture_without_its_file_is_noted() -> None:
    lost = pic()
    del lost["file"]
    out = fit_messages(SYSTEM, [user("hi", lost)], [], VISION)
    assert out[-1]["content"] == 'hi\n\n[Image "photo.jpg" (1600×1200) is no longer available.]'


# --------------------------------------------------------------------------- #
# Budget
# --------------------------------------------------------------------------- #


def test_pictures_count_by_their_estimate_not_their_url() -> None:
    shown = fit_messages(SYSTEM, [user("hi", pic())], [], VISION)[-1]
    as_text = fit_messages(SYSTEM, [user("hi")], [], VISION)[-1]
    cost = images.image_tokens(1600, 1200)
    extra_text = len('\n\n[Image "photo.jpg" (1600×1200) attached below.]')
    # The cost is the estimate plus the listing line; a data: URL's length never counts.
    inlined = {
        **shown,
        "content": [
            shown["content"][0],
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + "A" * 500_000},
                "_image": shown["content"][1]["_image"],
            },
        ],
    }
    assert message_tokens(inlined, VISION) == message_tokens(shown, VISION)
    assert message_chars(shown) < message_chars(as_text) + extra_text + 120
    assert message_tokens(shown, VISION) >= message_tokens(as_text, VISION) + cost


def test_old_turns_go_before_the_latest_pictures() -> None:
    history = [
        user("old " + "x" * 3000, ts=1.0),
        reply("y" * 3000, ts=2.0),
        user("look", pic(), ts=3.0),
    ]
    budget = PromptBudget(max_prompt_tokens=3000, local=False, vision=True)
    fitted = fit_prompt(SYSTEM, history, [], budget)
    assert fitted.dropped == 2 and not fitted.message_cut
    assert refs(fitted.messages[-1]) == [f"{1:064x}.jpg"]
    assert estimate_tokens(fitted.messages, None, budget) <= 3000


def test_the_latest_pictures_are_left_out_when_they_do_not_fit() -> None:
    history = [user("compare", pic("a.jpg", 1), pic("b.jpg", 2), pic("c.jpg", 3))]
    budget = PromptBudget(max_prompt_tokens=6000, local=False, vision=True)  # ~2 pictures
    fitted = fit_prompt(SYSTEM, history, [], budget)
    latest = fitted.messages[-1]
    assert refs(latest) == [f"{1:064x}.jpg", f"{2:064x}.jpg"]  # the last one went first
    assert '[Image "c.jpg" (1600×1200) attached, but left out' in text(latest)
    assert fitted.message_cut is True
    assert estimate_tokens(fitted.messages, None, budget) <= 6000
    # None fit: all are notes, and the text is still sent.
    tiny = PromptBudget(max_prompt_tokens=1200, local=False, vision=True)
    fitted = fit_prompt(SYSTEM, history, [], tiny)
    assert refs(fitted.messages[-1]) == [] and text(fitted.messages[-1]).startswith("compare")


def test_files_get_the_room_a_left_out_picture_frees() -> None:
    report = doc("report.txt", "word " * 6000)
    history = [user("summarise", report, pic("a.jpg", 1), pic("b.jpg", 2))]
    budget = PromptBudget(max_prompt_tokens=4000, local=False, vision=True)
    fitted = fit_prompt(SYSTEM, history, [], budget)
    latest = fitted.messages[-1]
    assert fitted.message_cut is True
    assert estimate_tokens(fitted.messages, None, budget) <= 4000
    assert refs(latest) == [f"{1:064x}.jpg"]  # b.jpg went, a.jpg fits
    body = text(latest)
    assert '[Image "b.jpg" (1600×1200) attached, but left out' in body
    assert body.count("word") > 500  # the report shares the room b.jpg left
    assert "[file truncated to fit this model's prompt window]" in body


def test_recognised_text_is_cut_like_a_file_on_a_small_window() -> None:
    history = [user("what does it say", pic(ocr="word " * 3000))]
    budget = PromptBudget(max_prompt_tokens=600)  # the local NPU window
    out = fit_messages(SYSTEM, history, [], budget)
    assert "[file truncated to fit the local model" in out[-1]["content"]
    assert estimate_tokens(out, None, budget) <= 600


def test_cut_message_keeps_the_pictures() -> None:
    msg = fit_messages(SYSTEM, [user("a" * 100, pic())], [], VISION)[-1]
    cut = cut_message(msg, 10)
    assert cut["content"][0]["text"].startswith("a" * 10 + "\n\n…")
    assert cut["content"][1] == msg["content"][1]


# --------------------------------------------------------------------------- #
# Quirks
# --------------------------------------------------------------------------- #


def picture_request() -> list[dict]:
    """A prompt with a picture after ``images.inline`` (plus a leftover private key)."""
    content = [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}, "_image": {}},
    ]
    return [SYSTEM, {"role": "user", "content": content, "_ts": 1.0}]


@pytest.mark.parametrize(
    "quirks", [GenericQuirks(), OpenAIQuirks(), DeepSeekQuirks(), MiniMaxQuirks()]
)
def test_cloud_quirks_send_parts_as_they_are(quirks) -> None:
    body = quirks.prepare_body({"model": "m", "messages": picture_request(), "stream": True})
    content = body["messages"][1]["content"]
    assert content == [
        {"type": "text", "text": "What is this?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]
    assert "_ts" not in body["messages"][1]


def test_ovms_keeps_a_minimal_history_with_plain_strings() -> None:
    quirks = OvmsQuirks()
    text_only = [
        SYSTEM,
        {
            "role": "user",
            "content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}],
        },
    ]
    body = quirks.prepare_body({"model": "m", "messages": text_only})
    assert body["messages"][1] == {"role": "user", "content": "one\n\ntwo"}
    # A picture (a vision model served by OVMS) keeps its parts.
    body = quirks.prepare_body({"model": "m", "messages": picture_request()})
    assert body["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")
    assert set(body["messages"][1]) == {"role", "content"}
