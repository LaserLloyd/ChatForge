"""Shared pieces of the Office Open XML writers (Word, Excel, PowerPoint): namespaces,
escaping, relationships, content types and the zip package. Standard library only."""

from __future__ import annotations

import io
import zipfile
from xml.sax.saxutils import escape, quoteattr

from chatforge.tools.doc_markdown import clean_xml_text

XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
OFFICE_DOC = f"{REL_NS}/officeDocument"
CORE_PROPS = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
CT_RELS = "application/vnd.openxmlformats-package.relationships+xml"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_OOXML = "application/vnd.openxmlformats-officedocument."


def text(s: str) -> str:
    """Element text: escaped, without characters XML cannot hold."""
    return escape(clean_xml_text(s))


def attr(s: str) -> str:
    """A quoted attribute value (``"..."``), escaped."""
    return quoteattr(clean_xml_text(s))


def rels(items: list[tuple]) -> str:
    """A ``.rels`` part from ``(id, type, target[, external])`` tuples."""
    out = [XML_HEAD, f'<Relationships xmlns="{PKG_REL_NS}">']
    for item in items:
        rid, rtype, target = item[0], item[1], item[2]
        external = ' TargetMode="External"' if len(item) > 3 and item[3] else ""
        out.append(f'<Relationship Id="{rid}" Type="{rtype}" Target={attr(target)}{external}/>')
    out.append("</Relationships>")
    return "".join(out)


def content_types(overrides: dict[str, str]) -> str:
    """``[Content_Types].xml``: ``rels`` and ``xml`` defaults plus one override per part
    (``{"/xl/workbook.xml": "application/..."}``)."""
    parts = "".join(
        f'<Override PartName="{name}" ContentType="{ctype}"/>' for name, ctype in overrides.items()
    )
    return (
        f'{XML_HEAD}<Types xmlns="{CT_NS}">'
        f'<Default Extension="rels" ContentType="{CT_RELS}"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        f"{parts}</Types>"
    )


def core_properties(title: str) -> str:
    """``docProps/core.xml`` holding just the title."""
    return (
        f"{XML_HEAD}<cp:coreProperties "
        'xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{text(title[:255])}</dc:title></cp:coreProperties>"
    )


def package(parts: dict[str, str | bytes]) -> bytes:
    """A zip of ``{part name: XML}``; ``[Content_Types].xml`` goes first."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        order = sorted(parts, key=lambda n: n != "[Content_Types].xml")
        for name in order:
            data = parts[name]
            zf.writestr(name, data.encode("utf-8") if isinstance(data, str) else data)
    return buf.getvalue()


__all__ = [
    "CORE_PROPS",
    "CT_CORE",
    "CT_OOXML",
    "OFFICE_DOC",
    "REL_NS",
    "XML_HEAD",
    "attr",
    "content_types",
    "core_properties",
    "package",
    "rels",
    "text",
]
