"""The built-in model catalog (``catalog.toml``) and search-result badges.

A search result gets one of four badges:

- ``recommended`` / ``supported``: the id is a ``[[model]]`` entry.
- ``avoid``: the id matches an ``[[avoid]]`` pattern (case-insensitive substring).
- ``untested``: anything else. If the name does not say ``int4`` the note warns
  that the NPU needs a symmetric INT4 export. Unknown ``qwen*`` models default to
  the hermes3 tool parser; any other unknown model runs with tools disabled.

The recommended model is data, not code (PLAN §7, NPU model gate): callers use
:meth:`Catalog.recommended` rather than a literal id.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CATALOG_PATH",
    "AvoidRule",
    "Badge",
    "BadgeName",
    "Catalog",
    "CatalogEntry",
]

DEFAULT_CATALOG_PATH: Final = Path(__file__).with_name("catalog.toml")

BadgeName = Literal["recommended", "supported", "untested", "avoid"]

NOTE_NOT_INT4: Final = (
    "The NPU needs a symmetric INT4 OpenVINO export; this name does not say int4, "
    "so it may not compile on NPU."
)
NOTE_QWEN_UNTESTED: Final = "Untested on NPU. Tools use the hermes3 parser."
NOTE_OTHER_UNTESTED: Final = "Untested on NPU. Runs with tools disabled."


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    label: str
    npu: Literal["recommended", "supported"]
    approx_gb: float | None = None
    tool_parser: str | None = None
    reasoning_parser: str | None = None
    thinking_toggle: bool = False
    licence: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "npu": self.npu,
            "approx_gb": self.approx_gb,
            "tool_parser": self.tool_parser,
            "reasoning_parser": self.reasoning_parser,
            "thinking_toggle": self.thinking_toggle,
            "licence": self.licence,
            "note": self.note,
        }


@dataclass(frozen=True)
class AvoidRule:
    pattern: str
    reason: str

    def matches(self, model_id: str) -> bool:
        return self.pattern.casefold() in model_id.casefold()


@dataclass(frozen=True)
class Badge:
    """Verdict for one repo id, plus the launch hints the runtime needs."""

    badge: BadgeName
    note: str
    tool_parser: str | None = None
    reasoning_parser: str | None = None
    thinking_toggle: bool = False
    entry: CatalogEntry | None = field(default=None, compare=False)

    @property
    def tools_enabled(self) -> bool:
        return self.tool_parser is not None


class Catalog:
    """Parsed ``catalog.toml``."""

    def __init__(self, entries: list[CatalogEntry], avoid: list[AvoidRule]) -> None:
        self._entries = list(entries)
        self._by_id = {e.id.casefold(): e for e in entries}
        self._avoid = list(avoid)

    @classmethod
    def load(cls, path: Path | None = None) -> Catalog:
        """Read and validate a catalog file (the packaged one by default)."""
        source = Path(path) if path is not None else DEFAULT_CATALOG_PATH
        with source.open("rb") as handle:
            data = tomllib.load(handle)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        entries: list[CatalogEntry] = []
        for raw in data.get("model", []) or []:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
                raise ValueError(f"catalog [[model]] entry without an id: {raw!r}")
            npu = raw.get("npu", "supported")
            if npu not in ("recommended", "supported"):
                raise ValueError(f"catalog entry {raw['id']}: npu must be recommended|supported")
            approx = raw.get("approx_gb")
            entries.append(
                CatalogEntry(
                    id=raw["id"],
                    label=str(raw.get("label") or raw["id"]),
                    npu=npu,
                    approx_gb=float(approx) if approx is not None else None,
                    tool_parser=raw.get("tool_parser"),
                    reasoning_parser=raw.get("reasoning_parser"),
                    thinking_toggle=bool(raw.get("thinking_toggle", False)),
                    licence=raw.get("licence"),
                    note=str(raw.get("note") or ""),
                )
            )
        avoid: list[AvoidRule] = []
        for raw in data.get("avoid", []) or []:
            pattern = raw.get("pattern") if isinstance(raw, dict) else None
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(f"catalog [[avoid]] entry without a pattern: {raw!r}")
            avoid.append(AvoidRule(pattern=pattern.strip(), reason=str(raw.get("reason") or "")))
        return cls(entries, avoid)

    # -- queries --------------------------------------------------------

    @property
    def entries(self) -> list[CatalogEntry]:
        return list(self._entries)

    @property
    def avoid_rules(self) -> list[AvoidRule]:
        return list(self._avoid)

    def get(self, model_id: str) -> CatalogEntry | None:
        return self._by_id.get(model_id.casefold())

    def recommended(self) -> CatalogEntry | None:
        """The catalog's recommended model, whichever it currently is."""
        for entry in self._entries:
            if entry.npu == "recommended":
                return entry
        return None

    def badge(self, model_id: str) -> Badge:
        entry = self.get(model_id)
        if entry is not None:
            return Badge(
                badge=entry.npu,
                note=entry.note,
                tool_parser=entry.tool_parser,
                reasoning_parser=entry.reasoning_parser,
                thinking_toggle=entry.thinking_toggle,
                entry=entry,
            )
        for rule in self._avoid:
            if rule.matches(model_id):
                return Badge(badge="avoid", note=rule.reason)
        name = model_id.rpartition("/")[2].casefold()
        is_qwen = name.startswith("qwen")
        notes = [NOTE_QWEN_UNTESTED if is_qwen else NOTE_OTHER_UNTESTED]
        if "int4" not in name:
            notes.append(NOTE_NOT_INT4)
        return Badge(
            badge="untested",
            note=" ".join(notes),
            tool_parser="hermes3" if is_qwen else None,
        )

    def annotate(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Copy search results, adding ``badge`` and ``note`` to each."""
        out: list[dict[str, Any]] = []
        for item in results:
            verdict = self.badge(str(item.get("id", "")))
            out.append({**item, "badge": verdict.badge, "note": verdict.note})
        return out
