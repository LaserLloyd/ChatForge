"""Will a downloaded OpenVINO LLM run correctly on the NPU? Read from its files.

OpenVINO GenAI's NPU guide lists what an LLM export needs on the NPU: symmetric 4-bit
weights (``--weight-format int4 --sym``, or ``nf4``), channel-wise (``--group-size -1``)
or group-size-128 quantization, and ``--ratio 1.0``. optimum-intel records the NNCF
settings it used in the ``rt_info`` at the end of ``openvino_model.xml``::

    <nncf><weight_compression>
        <group_size value="128" /> <mode value="int4_sym" /> <ratio value="1.0" /> ...

A model can meet every format rule and still be wrong on this NPU: OpenVINO's own
``Qwen3-4B-int4-ov`` (int4_sym, group 128, ratio 1.0, AWQ) compiles on the Lunar Lake
NPU but generates garbage, while the same files are correct on CPU
(docs/RUNTIME-NOTES.md). Such findings live in the catalog's ``[[avoid]]`` list, so the
verdict combines both sources:

1. a catalog ``[[model]]`` entry: verified, ``ok=True``;
2. a catalog ``[[avoid]]`` match: ``ok=False`` with the catalog's reason;
3. the export's own ``weight_compression`` settings: ``ok=False`` when they break a rule;
4. otherwise ``ok=None`` (unknown, e.g. no NNCF info): the model is tried on the NPU.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "NPU_GROUP_SIZES",
    "NPU_WEIGHT_MODES",
    "NpuVerdict",
    "npu_verdict",
    "read_weight_compression",
    "verdict_from_compression",
]

#: NNCF weight modes the NPU LLM pipeline accepts.
NPU_WEIGHT_MODES = frozenset({"int4_sym", "nf4"})
#: ``-1`` is channel-wise; 128 is the group size the NPU guide recommends up to ~5B.
NPU_GROUP_SIZES = frozenset({-1, 128})
#: Bytes read from the end of ``openvino_model.xml`` (the rt_info is a few KB).
TAIL_BYTES = 64 * 1024
MODEL_XML = "openvino_model.xml"

_SECTION_RE = re.compile(r"<weight_compression>(.*?)</weight_compression>", re.S)
_VALUE_RE = re.compile(r'<([A-Za-z_][\w.-]*)\s+value="([^"]*)"\s*/>')


@dataclass(frozen=True)
class NpuVerdict:
    """``ok``: True (verified), False (known not to work on the NPU), None (unknown)."""

    ok: bool | None
    reason: str = ""
    source: str = "unknown"  # catalog | avoid | weights | unknown
    mode: str | None = None
    group_size: int | None = None
    ratio: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "source": self.source,
            "mode": self.mode,
            "group_size": self.group_size,
            "ratio": self.ratio,
        }


def _parse_compression(text: str) -> dict[str, str] | None:
    # The last section is the model's own (the rt_info sits at the end of the file).
    sections = _SECTION_RE.findall(text)
    if not sections:
        return None
    return dict(_VALUE_RE.findall(sections[-1]))


@lru_cache(maxsize=64)
def _read_tail(path: str, _mtime_ns: int, _size: int) -> dict[str, str] | None:
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, _size - TAIL_BYTES))
            text = handle.read().decode("utf-8", "replace")
    except OSError:
        return None
    return _parse_compression(text)


def read_weight_compression(model_dir: Path | str) -> dict[str, str] | None:
    """The NNCF ``weight_compression`` settings of a model folder, or ``None``.

    Cached per file (path, mtime, size): the runtime asks on every load.
    """
    path = Path(model_dir) / MODEL_XML
    try:
        st = os.stat(path)
    except OSError:
        return None
    return _read_tail(str(path), st.st_mtime_ns, st.st_size)


def _mode(value: str | None) -> str | None:
    if not value:
        return None
    # Older NNCF wrote the enum ("CompressWeightsMode.INT4_SYM").
    return value.rpartition(".")[2].strip().lower() or None


def _int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "", "None") else None
    except ValueError:
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "", "None") else None
    except ValueError:
        return None


def verdict_from_compression(info: dict[str, str] | None) -> NpuVerdict:
    """Check the export's NNCF settings against the NPU guide's rules."""
    if not info:
        return NpuVerdict(None, "no weight-compression info in the model files")
    mode = _mode(info.get("mode"))
    group = _int(info.get("group_size"))
    ratio = _float(info.get("ratio"))
    fields: dict[str, Any] = {"mode": mode, "group_size": group, "ratio": ratio}
    if mode is not None and mode not in NPU_WEIGHT_MODES:
        return NpuVerdict(
            False,
            f"its weights are {mode.upper()}; the NPU needs symmetric INT4 (int4_sym) or NF4",
            "weights",
            **fields,
        )
    if group is not None and group not in NPU_GROUP_SIZES:
        return NpuVerdict(
            False,
            f"it is quantized with group size {group}; the NPU needs channel-wise (-1) or 128",
            "weights",
            **fields,
        )
    if ratio is not None and ratio < 0.999:
        backup = _mode(info.get("backup_mode")) or "8-bit"
        return NpuVerdict(
            False,
            f"only {ratio:.0%} of its layers are 4-bit (the rest {backup.upper()}); "
            "the NPU needs ratio 1.0",
            "weights",
            **fields,
        )
    if mode is None:
        return NpuVerdict(None, "the weight-compression mode is not recorded", **fields)
    return NpuVerdict(True, "", "weights", **fields)


def npu_verdict(model_id: str, model_dir: Path | str | None, catalog: Any = None) -> NpuVerdict:
    """Combine the catalog and the export's settings (see the module docstring)."""
    if catalog is not None:
        try:
            badge = catalog.badge(model_id)
        except Exception:  # noqa: BLE001 - a broken catalog must not block a load
            badge = None
        if badge is not None and badge.badge in ("recommended", "supported"):
            return NpuVerdict(True, "", "catalog")
        if badge is not None and badge.badge == "avoid":
            return NpuVerdict(False, badge.note or "listed as not working on the NPU", "avoid")
    if model_dir is None:
        return NpuVerdict(None, "model folder unknown")
    return verdict_from_compression(read_weight_compression(model_dir))
