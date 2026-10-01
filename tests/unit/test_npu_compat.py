"""models.npu_compat: reading the NNCF rt_info and the NPU verdict."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aichat.models import npu_compat
from aichat.models.catalog import Catalog
from aichat.models.npu_compat import (
    npu_verdict,
    read_weight_compression,
    verdict_from_compression,
)

CATALOG = Catalog.from_dict(
    {
        "model": [{"id": "Good/Verified-int4-ov", "label": "V", "npu": "recommended"}],
        "avoid": [{"pattern": "Garbage-4B", "reason": "Garbage output on the NPU."}],
    }
)


def rt_info(mode: str = "int4_sym", group: str = "128", ratio: str = "1.0") -> str:
    """The tail of an optimum-intel export's ``openvino_model.xml``."""
    return (
        "<?xml version='1.0'?>\n<net name='Model0' version='11'>\n<layers/>\n"
        + "<!-- padding -->\n" * 50
        + "\t<rt_info>\n\t\t<nncf>\n\t\t\t<weight_compression>\n"
        + '\t\t\t\t<awq value="True" />\n'
        + '\t\t\t\t<backup_mode value="int8_asym" />\n'
        + f'\t\t\t\t<group_size value="{group}" />\n'
        + f'\t\t\t\t<mode value="{mode}" />\n'
        + f'\t\t\t\t<ratio value="{ratio}" />\n'
        + "\t\t\t</weight_compression>\n\t\t</nncf>\n\t</rt_info>\n</net>\n"
    )


def model_dir(tmp_path: Path, xml: str | None, name: str = "m") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    if xml is not None:
        (d / "openvino_model.xml").write_text(xml, encoding="utf-8")
    return d


def test_reads_weight_compression_from_the_xml_tail(tmp_path) -> None:
    info = read_weight_compression(model_dir(tmp_path, rt_info()))
    assert info is not None
    assert info["mode"] == "int4_sym" and info["group_size"] == "128" and info["ratio"] == "1.0"
    assert info["backup_mode"] == "int8_asym"
    assert read_weight_compression(model_dir(tmp_path, "<net/>", "none")) is None
    assert read_weight_compression(tmp_path / "missing") is None


def test_reread_after_the_file_changes(tmp_path) -> None:
    d = model_dir(tmp_path, rt_info(mode="int4_sym"))
    assert read_weight_compression(d)["mode"] == "int4_sym"
    xml = d / "openvino_model.xml"
    xml.write_text(rt_info(mode="int4_asym") + " ", encoding="utf-8")  # new size
    st = xml.stat()
    os.utime(xml, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    assert read_weight_compression(d)["mode"] == "int4_asym"


@pytest.mark.parametrize(
    ("mode", "group", "ratio", "ok", "words"),
    [
        ("int4_sym", "128", "1.0", True, ""),
        ("int4_sym", "-1", "1.0", True, ""),  # channel-wise
        ("nf4", "-1", "1.0", True, ""),
        ("CompressWeightsMode.INT4_SYM", "128", "1.0", True, ""),  # older NNCF
        ("int4_asym", "128", "1.0", False, "INT4_ASYM"),
        ("int8_asym", "-1", "1.0", False, "symmetric INT4"),
        ("int4_sym", "64", "1.0", False, "group size 64"),
        ("int4_sym", "128", "0.8", False, "80%"),
    ],
)
def test_verdict_from_compression(mode, group, ratio, ok, words) -> None:
    info = read_weight_compression_text(rt_info(mode, group, ratio))
    verdict = verdict_from_compression(info)
    assert verdict.ok is ok
    assert words in verdict.reason
    if ok:
        assert verdict.source == "weights" and verdict.reason == ""


def read_weight_compression_text(text: str) -> dict[str, str] | None:
    return npu_compat._parse_compression(text)  # noqa: SLF001 - pure parser


def test_unknown_without_compression_info() -> None:
    assert verdict_from_compression(None).ok is None
    assert verdict_from_compression({"group_size": "128"}).ok is None  # no mode recorded


def test_catalog_entry_and_avoid_list_win_over_the_files(tmp_path) -> None:
    sym = model_dir(tmp_path, rt_info(), "sym")
    asym = model_dir(tmp_path, rt_info(mode="int4_asym"), "asym")
    # A verified catalog entry is trusted even if the files look odd.
    assert npu_verdict("Good/Verified-int4-ov", asym, CATALOG).ok is True
    # An avoid entry (e.g. Qwen3-4B: int4_sym gs128, still garbage on Lunar Lake).
    avoided = npu_verdict("OpenVINO/Garbage-4B-int4-ov", sym, CATALOG)
    assert avoided.ok is False and avoided.source == "avoid"
    assert avoided.reason == "Garbage output on the NPU."
    # Otherwise the files decide.
    assert npu_verdict("Acme/other-int4-ov", sym, CATALOG).ok is True
    assert npu_verdict("Acme/other-int4-ov", asym, CATALOG).ok is False
    assert npu_verdict("Acme/other-int4-ov", None, CATALOG).ok is None
    assert npu_verdict("Acme/other-int4-ov", sym, None).ok is True


def test_packaged_catalog_avoids_qwen3_4b_on_the_npu() -> None:
    verdict = npu_verdict("OpenVINO/Qwen3-4B-int4-ov", None, Catalog.load())
    assert verdict.ok is False and "garbage" in verdict.reason
    assert npu_verdict("OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov", None, Catalog.load()).ok
