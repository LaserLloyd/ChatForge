"""Catalog loading and search-result badges."""

from __future__ import annotations

from pathlib import Path

import pytest

from chatforge.models.catalog import NOTE_NOT_INT4, Catalog

CUSTOM = """
[[model]]
id = "OpenVINO/Qwen3-4B-int4-ov"
label = "Qwen3 4B INT4"
approx_gb = 2.29
npu = "recommended"
tool_parser = "hermes3"
reasoning_parser = "qwen3"
thinking_toggle = true
licence = "apache-2.0"
note = "Default."

[[model]]
id = "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"
label = "Qwen2.5 1.5B"
npu = "supported"
tool_parser = "hermes3"
note = "Fallback."

[[model]]
id = "OpenVINO/Qwen3-8B-int4-cw-ov"
label = "Listed despite avoid"
npu = "supported"

[[avoid]]
pattern = "Qwen3-8B"
reason = "Too large."
"""


@pytest.fixture
def custom(tmp_path: Path) -> Catalog:
    path = tmp_path / "catalog.toml"
    path.write_text(CUSTOM, encoding="utf-8")
    return Catalog.load(path)


@pytest.fixture
def packaged() -> Catalog:
    return Catalog.load()


def test_packaged_catalog_has_exactly_one_recommended(packaged: Catalog) -> None:
    recommended = [e for e in packaged.entries if e.npu == "recommended"]
    assert len(recommended) == 1
    assert packaged.recommended() == recommended[0]
    assert packaged.badge(recommended[0].id).badge == "recommended"


def test_packaged_catalog_carries_every_plan_avoid_pattern(packaged: Catalog) -> None:
    patterns = {r.pattern for r in packaged.avoid_rules}
    assert patterns >= {
        "Qwen3-8B",
        "Qwen3-1.7B-int4-ov",
        "Qwen3-0.6B-int4-ov",
        "Phi-4-mini",
        "Qwen3.5",
        "Qwen3.6",
        "LFM2",
    }


def test_catalog_entries_badge_with_hints(custom: Catalog) -> None:
    rec = custom.badge("OpenVINO/Qwen3-4B-int4-ov")
    assert rec.badge == "recommended"
    assert rec.note == "Default."
    assert rec.tool_parser == "hermes3" and rec.reasoning_parser == "qwen3"
    assert rec.thinking_toggle and rec.tools_enabled
    sup = custom.badge("openvino/qwen2.5-1.5b-instruct-int4-ov")  # case-insensitive
    assert sup.badge == "supported" and sup.note == "Fallback."
    assert custom.get("OpenVINO/Qwen3-4B-int4-ov").approx_gb == 2.29


def test_catalog_entry_wins_over_avoid(custom: Catalog) -> None:
    assert custom.badge("OpenVINO/Qwen3-8B-int4-cw-ov").badge == "supported"
    assert custom.badge("OpenVINO/Qwen3-8B-int4-ov").badge == "avoid"


@pytest.mark.parametrize(
    "model_id",
    [
        "OpenVINO/Qwen3-8B-int4-ov",
        "OpenVINO/Qwen3-1.7B-int4-ov",
        "OpenVINO/Qwen3-0.6B-int4-ov",
        "OpenVINO/Phi-4-mini-instruct-int4-ov",
        "OpenVINO/Qwen3.5-4B-int4-ov",
        "someone/qwen3.6-2b-int4-ov",
        "OpenVINO/LFM2-1.2B-int4-ov",
    ],
)
def test_avoid_badges(packaged: Catalog, model_id: str) -> None:
    verdict = packaged.badge(model_id)
    assert verdict.badge == "avoid"
    assert verdict.note
    assert not verdict.tools_enabled


def test_channel_wise_variant_is_not_caught_by_asymmetric_avoid(packaged: Catalog) -> None:
    assert packaged.badge("OpenVINO/Qwen3-1.7B-int4-cw-ov").badge == "untested"


def test_unknown_qwen_int4_is_untested_with_hermes3(packaged: Catalog) -> None:
    verdict = packaged.badge("OpenVINO/Qwen2.5-3B-Instruct-int4-ov")
    assert verdict.badge == "untested"
    assert verdict.tool_parser == "hermes3"
    assert NOTE_NOT_INT4 not in verdict.note


def test_unknown_non_int4_gets_symmetric_int4_note(packaged: Catalog) -> None:
    verdict = packaged.badge("OpenVINO/Mistral-7B-Instruct-fp16-ov")
    assert verdict.badge == "untested"
    assert "symmetric INT4" in verdict.note
    assert verdict.tool_parser is None
    assert "tools disabled" in verdict.note


def test_unknown_non_qwen_int4_runs_without_tools(packaged: Catalog) -> None:
    verdict = packaged.badge("OpenVINO/gemma-2b-int4-ov")
    assert verdict.badge == "untested"
    assert not verdict.tools_enabled
    assert "symmetric INT4" not in verdict.note


def test_annotate_adds_badge_and_note_without_mutating(custom: Catalog) -> None:
    results = [{"id": "OpenVINO/Qwen3-4B-int4-ov", "downloads": 3}, {"id": "x/Qwen3-8B-foo"}]
    annotated = custom.annotate(results)
    assert annotated[0]["badge"] == "recommended" and annotated[0]["downloads"] == 3
    assert annotated[1]["badge"] == "avoid" and annotated[1]["note"] == "Too large."
    assert annotated[0]["npu_ok"] is True and annotated[1]["npu_ok"] is False
    assert all(item["npu_ok"] is None for item in annotated if item["badge"] == "untested")
    assert "badge" not in results[0]


def test_invalid_catalog_entries_raise() -> None:
    with pytest.raises(ValueError):
        Catalog.from_dict({"model": [{"label": "no id"}]})
    with pytest.raises(ValueError):
        Catalog.from_dict({"model": [{"id": "a/b", "npu": "maybe"}]})
    with pytest.raises(ValueError):
        Catalog.from_dict({"avoid": [{"reason": "no pattern"}]})


def test_recommended_none_when_absent() -> None:
    assert Catalog.from_dict({}).recommended() is None
