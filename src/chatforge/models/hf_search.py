"""HuggingFace discovery for OpenVINO model repos: search, and a repo's file tree.

Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
The GGUF quant parsing, logical downloads, shards, mmproj and date-window walk
are cut. What is left is the plain JSON API client (two endpoints plus the repo
metadata call), its bounded 429 backoff, and the path-safety helpers that every
repo-supplied name passes through before it touches the disk.

Every ``repo_id`` and every file path in this module comes from a third party
(the user's search box or a repo's own listing), and both end up joined onto the
model directory. :func:`validate_repo_id` and :func:`safe_relpath` are the
security boundary: they refuse, never sanitise.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import email.utils
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Final
from urllib.parse import quote, urlsplit

import httpx

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_HF_ENDPOINT",
    "HfSearch",
    "ModelError",
    "RepoFile",
    "RepoMeta",
    "file_url",
    "parse_hf_timestamp",
    "safe_filename",
    "safe_relpath",
    "validate_repo_id",
]

DEFAULT_HF_ENDPOINT: Final = "https://huggingface.co"

_DEFAULT_TIMEOUT_S: Final = 30.0
_MAX_RATE_LIMIT_RETRIES: Final = 3
_MAX_RETRY_SLEEP_S: Final = 30.0
_MAX_TREE_PAGES: Final = 20
_MAX_SEARCH_LIMIT: Final = 100

#: PLAN §7 item 7. ASCII only: ``\w`` would otherwise admit any Unicode letter.
_REPO_ID_RE: Final = re.compile(r"^[A-Za-z0-9][\w.-]{0,95}/[A-Za-z0-9][\w.-]{0,95}$", re.ASCII)
_SHA256_RE: Final = re.compile(r"\A[0-9a-f]{64}\Z")
# RFC 8288 Link header entry: `<https://host/path?cursor=..>; rel="next"`.
_LINK_RE: Final = re.compile(r"<(?P<url>[^>]*)>\s*;\s*[^,]*\brel\s*=\s*\"?next\"?", re.I)

# Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
# Reserved DOS device names. Windows resolves these to devices regardless of
# extension, so a repo file called "aux.json" would open a device handle.
_WINDOWS_RESERVED: Final = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)


class ModelError(Exception):
    """A model-library failure with a user-facing message.

    Shaped like ``chatforge.errors.AppError`` (message, code, hint, action,
    details) so the bridge can render it the same way; kept local so this
    package does not depend on WS2's module landing first.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "model_error",
        hint: str | None = None,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.action = action
        self.details: dict[str, Any] = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "action": self.action,
        }


@dataclass(frozen=True)
class RepoFile:
    """One file in a repo's tree. ``sha256`` is the LFS oid, when there is one."""

    path: str
    size: int
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class RepoMeta:
    """What ``/api/models/{repo}`` says about a repo that the sidecar records."""

    repo_id: str
    sha: str | None = None
    license: str | None = None
    gated: bool = False
    private: bool = False
    tags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def validate_repo_id(repo_id: str) -> str:
    """Return ``repo_id`` if it is a plain ``publisher/name``, else raise.

    PLAN §7 item 7: the regex, plus an explicit refusal of any ``..`` so that a
    name like ``a/b..`` (which Windows would resolve oddly) never reaches a
    path join. Each half also has to pass :func:`safe_filename`, which catches
    reserved device names (``con/aux``) and trailing dots.
    """
    if not isinstance(repo_id, str):
        raise ModelError("repository id must be a string", code="invalid_repo_id")
    text = repo_id.strip()
    if not _REPO_ID_RE.match(text) or ".." in text:
        raise ModelError(
            f"refusing repository id {repo_id!r}: expected 'publisher/name' using letters, "
            "digits, '.', '_' and '-' only",
            code="invalid_repo_id",
            details={"repo_id": repo_id},
        )
    publisher, _, name = text.partition("/")
    safe_filename(publisher)
    safe_filename(name)
    return text


# Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
def safe_filename(name: str) -> str:
    """Validate a repo-supplied name as a single, safe path component.

    Refused outright rather than sanitised: silently rewriting a hostile name
    produces a file the user did not ask for under a name they cannot
    correlate with the repo.
    """
    if not isinstance(name, str) or not name or not name.strip():
        raise ModelError("repository file name is empty", code="unsafe_filename")
    if "\x00" in name:
        raise ModelError("repository file name contains a NUL byte", code="unsafe_filename")
    if "/" in name or "\\" in name:
        raise ModelError(
            f"refusing repository file name {name!r}: it contains a path separator",
            code="unsafe_filename",
        )
    if name in {".", ".."}:
        raise ModelError(
            f"refusing repository file name {name!r}: relative path component",
            code="unsafe_filename",
        )
    # Catches "C:x.bin" (drive-relative) as well as "C:\\Windows\\x.bin".
    if PureWindowsPath(name).drive or PureWindowsPath(name).is_absolute():
        raise ModelError(
            f"refusing repository file name {name!r}: absolute or drive-qualified path",
            code="unsafe_filename",
        )
    if PurePosixPath(name).is_absolute():
        raise ModelError(
            f"refusing repository file name {name!r}: absolute path", code="unsafe_filename"
        )
    if name != name.rstrip(". ") or ":" in name:
        # Win32 strips trailing dots and spaces, and a colon names an NTFS
        # alternate data stream: both resolve to a different file.
        raise ModelError(
            f"refusing repository file name {name!r}: trailing dot/space or ':' would "
            "resolve to a different file on Windows",
            code="unsafe_filename",
        )
    if name.partition(".")[0].strip().lower() in _WINDOWS_RESERVED:
        raise ModelError(
            f"refusing repository file name {name!r}: reserved device name",
            code="unsafe_filename",
        )
    return name


def safe_relpath(path: str) -> str:
    """Validate a repo-relative POSIX path (``a/b/c.json``) component by component.

    OpenVINO repos are flat, but the tree API is recursive, so a nested file is
    allowed as long as every component is itself a safe filename. Backslashes
    are refused (they are separators on Windows) and so is anything empty,
    absolute, or containing ``..``.
    """
    if not isinstance(path, str) or not path:
        raise ModelError("repository file path is empty", code="unsafe_filename")
    if "\\" in path or path.startswith("/"):
        raise ModelError(
            f"refusing repository file path {path!r}: absolute or backslash-separated",
            code="unsafe_filename",
        )
    for part in path.split("/"):
        safe_filename(part)
    return path


def file_url(repo_id: str, filename: str, *, endpoint: str, revision: str = "main") -> str:
    """Resolve URL for one repo file: ``{endpoint}/{repo}/resolve/{rev}/{path}``.

    StudioForge used ``huggingface_hub.hf_hub_url``; this is the same layout and
    percent-encoding without the dependency.
    """
    repo = validate_repo_id(repo_id)
    rel = safe_relpath(filename)
    return (
        f"{endpoint.rstrip('/')}/{repo}/resolve/{quote(revision, safe='')}/{quote(rel, safe='/')}"
    )


# ---------------------------------------------------------------------------
# Timestamps and headers
# ---------------------------------------------------------------------------


# Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
def parse_hf_timestamp(iso: str | None) -> dt.datetime | None:
    """HF's ``2026-08-18T12:59:16.000Z`` as an aware UTC datetime, else ``None``."""
    if not isinstance(iso, str) or not iso.strip():
        return None
    text = iso.strip()
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


# Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
def _retry_after_seconds(header: str | None, fallback: float) -> float:
    """Parse ``Retry-After`` (delta-seconds or HTTP-date), falling back on backoff."""
    if not header:
        return fallback
    header = header.strip()
    try:
        return max(0.0, float(header))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return fallback
    if parsed is None:  # pragma: no cover - older interpreters
        return fallback
    now = dt.datetime.now(tz=parsed.tzinfo or dt.UTC)
    return max(0.0, (parsed - now).total_seconds())


def _next_page_url(link_header: str | None, *, endpoint: str) -> str | None:
    """The ``rel="next"`` URL from a ``Link`` header, only if it is same-origin.

    Security boundary adapted from StudioForge: the URL comes from upstream and
    every request may carry a token, so a cross-origin "next" ends pagination.
    The origin is compared exactly (scheme + host + port), not by prefix.
    """
    if not link_header:
        return None
    ours = urlsplit(endpoint)
    for match in _LINK_RE.finditer(link_header):
        url = match.group("url").strip()
        if not url:
            continue
        theirs = urlsplit(url)
        if (theirs.scheme, theirs.netloc) == (ours.scheme, ours.netloc):
            return url
        log.warning("hf.pagination_cross_origin")
        return None
    return None


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _license_from(payload: dict[str, Any]) -> str | None:
    card = payload.get("cardData")
    if isinstance(card, dict) and isinstance(card.get("license"), str):
        return card["license"]
    for tag in payload.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            return tag.partition(":")[2] or None
    return None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HfSearch:
    """Async client for the HuggingFace endpoints the model library needs."""

    def __init__(
        self,
        *,
        endpoint: str = DEFAULT_HF_ENDPOINT,
        client: httpx.AsyncClient | None = None,
        token: str | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
    ) -> None:
        self._endpoint = (endpoint or DEFAULT_HF_ENDPOINT).rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT_S), follow_redirects=True
        )
        self._token = token
        self._sleep = sleep or asyncio.sleep

    @property
    def endpoint(self) -> str:
        return self._endpoint

    async def aclose(self) -> None:
        """Close the HTTP client, but only if we created it."""
        if self._owns_client:
            await self._client.aclose()

    # -- requests -------------------------------------------------------

    # Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
    def _headers(self) -> dict[str, str]:
        """Accept JSON; the token (if any) goes in a header, never a URL or a log."""
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
    async def _get_page(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        url: str | None = None,
    ) -> tuple[Any, str | None]:
        """One page of a JSON endpoint plus the same-origin ``rel="next"`` URL.

        429s are retried a bounded number of times, honouring ``Retry-After``
        (capped). ``path`` is what gets logged, never the full URL, because the
        query carries the user's search terms.
        """
        target = url or f"{self._endpoint}{path}"
        backoff = 1.0
        response: httpx.Response | None = None
        for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
            try:
                response = await self._client.get(
                    target, params=None if url else params, headers=self._headers()
                )
            except httpx.HTTPError as exc:
                raise ModelError(
                    f"HuggingFace request to {path} failed: {type(exc).__name__}: {exc}",
                    code="unreachable",
                    hint="Check your internet connection and try again.",
                    action="retry",
                ) from exc
            if response.status_code == 429 and attempt < _MAX_RATE_LIMIT_RETRIES:
                wait = min(
                    _retry_after_seconds(response.headers.get("Retry-After"), backoff),
                    _MAX_RETRY_SLEEP_S,
                )
                log.warning("hf.rate_limited path=%s attempt=%d wait_s=%.1f", path, attempt, wait)
                await self._sleep(wait)
                backoff = min(backoff * 2, _MAX_RETRY_SLEEP_S)
                continue
            break

        assert response is not None  # noqa: S101 - loop always runs at least once
        self._raise_for_status(response, path)
        try:
            payload = response.json()
        except ValueError as exc:
            raise ModelError(
                f"HuggingFace returned non-JSON for {path}", code="upstream_error"
            ) from exc
        return payload, _next_page_url(response.headers.get("Link"), endpoint=self._endpoint)

    # Adapted from StudioForge src/studioforge/core/hf_search.py (MIT, LaserLloyd).
    def _raise_for_status(self, response: httpx.Response, path: str) -> None:
        status = response.status_code
        if status < 400:
            return
        details = {"status": status, "path": path}
        if status in (401, 403):
            raise ModelError(
                f"HuggingFace refused access to {path} (HTTP {status}): the repository is "
                "gated or private.",
                code="gated_repo",
                hint="Open the model page on huggingface.co and accept its licence.",
                details=details,
            )
        if status == 404:
            raise ModelError(
                f"HuggingFace has no such repository or file: {path}",
                code="not_found",
                details=details,
            )
        if status == 429:
            raise ModelError(
                "HuggingFace rate-limited this client and kept doing so after "
                f"{_MAX_RATE_LIMIT_RETRIES} retries.",
                code="rate_limit",
                hint="Wait a few minutes and try again.",
                action="retry",
                details=details,
            )
        raise ModelError(
            f"HuggingFace returned HTTP {status} for {path}",
            code="upstream_error",
            action="retry",
            details=details,
        )

    # -- public API -----------------------------------------------------

    async def search(
        self, q: str, *, author: str | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """``GET /api/models?search=&author=&sort=downloads&limit=N``.

        Returns plain dicts ``{id, downloads, likes, last_modified, tags,
        pipeline_tag, gated}``; :meth:`chatforge.models.catalog.Catalog.annotate`
        adds the badge and note.
        """
        params: dict[str, Any] = {
            "search": (q or "").strip(),
            "sort": "downloads",
            "limit": max(1, min(_MAX_SEARCH_LIMIT, int(limit))),
        }
        if author:
            params["author"] = author.strip()
        payload, _next = await self._get_page("/api/models", params)
        if not isinstance(payload, list):
            raise ModelError(
                "HuggingFace returned an unexpected search payload", code="upstream_error"
            )
        results: list[dict[str, Any]] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id") or item.get("modelId")
            if not isinstance(model_id, str) or not model_id:
                continue
            results.append(
                {
                    "id": model_id,
                    "downloads": _as_int(item.get("downloads")),
                    "likes": _as_int(item.get("likes")),
                    "last_modified": item.get("lastModified") or item.get("createdAt"),
                    "tags": [t for t in item.get("tags") or [] if isinstance(t, str)],
                    "pipeline_tag": item.get("pipeline_tag"),
                    "gated": bool(item.get("gated")),
                }
            )
        log.info("hf.search query_len=%d results=%d", len(params["search"]), len(results))
        return results

    async def repo_files(self, repo_id: str) -> list[RepoFile]:
        """Every file in a repo, with its size and LFS sha256 when published.

        ``GET /api/models/{repo}/tree/main?recursive=true``. Directory entries
        are dropped. A single unsafe path refuses the whole repo: a repo that
        ships a traversal name is hostile, not merely untidy.
        """
        repo = validate_repo_id(repo_id)
        path = f"/api/models/{repo}/tree/main"
        payload, next_url = await self._get_page(path, {"recursive": "true"})
        entries: list[Any] = []
        pages = 0
        while True:
            if not isinstance(payload, list):
                raise ModelError(
                    f"HuggingFace returned an unexpected tree for {repo}", code="upstream_error"
                )
            entries.extend(payload)
            pages += 1
            if not next_url or pages >= _MAX_TREE_PAGES:
                break
            payload, next_url = await self._get_page(path, url=next_url)

        files: list[RepoFile] = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "file":
                continue
            rel = entry.get("path")
            if not isinstance(rel, str):
                continue
            safe_relpath(rel)
            lfs = entry.get("lfs") if isinstance(entry.get("lfs"), dict) else None
            sha = None
            size = _as_int(entry.get("size"))
            if lfs is not None:
                oid = str(lfs.get("oid") or "").lower()
                if oid.startswith("sha256:"):
                    oid = oid.partition(":")[2]
                sha = oid if _SHA256_RE.match(oid) else None
                size = _as_int(lfs.get("size")) or size
            files.append(RepoFile(path=rel, size=size, sha256=sha))
        return files

    async def repo_meta(self, repo_id: str) -> RepoMeta:
        """``GET /api/models/{repo}``: commit sha and licence for the sidecar."""
        repo = validate_repo_id(repo_id)
        payload, _next = await self._get_page(f"/api/models/{repo}")
        if not isinstance(payload, dict):
            raise ModelError(
                f"HuggingFace returned an unexpected payload for {repo}", code="upstream_error"
            )
        sha = payload.get("sha")
        return RepoMeta(
            repo_id=repo,
            sha=sha if isinstance(sha, str) and sha else None,
            license=_license_from(payload),
            gated=bool(payload.get("gated")),
            private=bool(payload.get("private")),
            tags=[t for t in payload.get("tags") or [] if isinstance(t, str)],
        )

    def url_for(self, repo_id: str, filename: str, *, revision: str = "main") -> str:
        return file_url(repo_id, filename, endpoint=self._endpoint, revision=revision)
