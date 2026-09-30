"""In-memory HuggingFace API for ``httpx.MockTransport`` tests.

Serves the endpoints the model library uses:

- ``GET /api/models`` (search)
- ``GET /api/models/{pub}/{repo}`` (meta: sha, licence)
- ``GET /api/models/{pub}/{repo}/tree/main?recursive=true``
- ``GET /{pub}/{repo}/resolve/{rev}/{path}`` with ``Range`` support

Per-file knobs on :class:`FakeFile` simulate the failure modes the downloader
must survive. Every request is recorded in :attr:`FakeHfApi.requests`.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

_RANGE_RE = re.compile(r"bytes=(\d+)-")


@dataclass
class FakeFile:
    content: bytes
    lfs: bool = True
    #: Size reported by the tree listing (defaults to ``len(content)``).
    listed_size: int | None = None
    #: sha256 reported as the LFS oid (defaults to the real hash).
    listed_sha: str | None = None
    #: Bytes actually served instead of ``content`` (sha-mismatch simulation).
    serve: bytes | None = None
    #: Answer every Range request with a full 200.
    ignore_range: bool = False
    #: Answer the first N resolve requests with 429.
    rate_limit: int = 0
    retry_after: str | None = "1"
    #: Answer the first N resolve requests with 500.
    server_errors: int = 0
    #: Serve this many bytes, then block until ``release`` is set.
    hang_after: int | None = None
    hung: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def body(self) -> bytes:
        return self.serve if self.serve is not None else self.content

    @property
    def sha256(self) -> str:
        return self.listed_sha or hashlib.sha256(self.content).hexdigest()


@dataclass
class FakeRepo:
    files: dict[str, FakeFile]
    sha: str = "0123456789abcdef0123456789abcdef01234567"
    license: str | None = "apache-2.0"
    downloads: int = 100
    likes: int = 5
    #: Extra raw tree entries (e.g. hostile paths).
    extra_tree: list[dict[str, Any]] = field(default_factory=list)


class _HangingStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes, fake: FakeFile) -> None:
        self._data = data
        self._fake = fake

    async def __aiter__(self):
        yield self._data
        # Reaching here means the consumer asked for more: the first chunk is written.
        self._fake.hung.set()
        await self._fake.release.wait()

    async def aclose(self) -> None:
        return None


class FakeHfApi:
    def __init__(self, endpoint: str = "https://huggingface.co") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.repos: dict[str, FakeRepo] = {}
        self.requests: list[httpx.Request] = []
        self.search_results: list[dict[str, Any]] | None = None
        #: Answer the first N API (non-resolve) requests with 429.
        self.api_rate_limit = 0
        self.api_retry_after: str | None = "2"

    def add_repo(self, repo_id: str, files: dict[str, bytes | FakeFile], **kwargs: Any) -> FakeRepo:
        repo = FakeRepo(
            files={
                path: value if isinstance(value, FakeFile) else FakeFile(value)
                for path, value in files.items()
            },
            **kwargs,
        )
        self.repos[repo_id] = repo
        return repo

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport(), follow_redirects=True)

    def resolve_requests(self, path: str | None = None) -> list[httpx.Request]:
        return [
            r
            for r in self.requests
            if "/resolve/" in r.url.path and (path is None or r.url.path.endswith("/" + path))
        ]

    # -- dispatch -------------------------------------------------------

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.startswith("/api/"):
            if self.api_rate_limit > 0:
                self.api_rate_limit -= 1
                headers = {"Retry-After": self.api_retry_after} if self.api_retry_after else {}
                return httpx.Response(429, headers=headers)
            return self._api(request)
        if "/resolve/" in path:
            return self._resolve(request)
        return httpx.Response(404)

    def _api(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/models":
            return httpx.Response(200, json=self._search(request))
        rest = path[len("/api/models/") :]
        if rest.endswith("/tree/main"):
            repo_id = rest[: -len("/tree/main")]
            repo = self.repos.get(repo_id)
            if repo is None:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=self._tree(repo))
        repo = self.repos.get(rest)
        if repo is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(
            200,
            json={
                "id": rest,
                "sha": repo.sha,
                "cardData": {"license": repo.license} if repo.license else {},
                "tags": ["openvino"],
            },
        )

    def _search(self, request: httpx.Request) -> list[dict[str, Any]]:
        if self.search_results is not None:
            return self.search_results
        q = request.url.params.get("search", "").lower()
        author = request.url.params.get("author")
        out = []
        for repo_id, repo in self.repos.items():
            if q and q not in repo_id.lower():
                continue
            if author and not repo_id.startswith(author + "/"):
                continue
            out.append(
                {
                    "id": repo_id,
                    "downloads": repo.downloads,
                    "likes": repo.likes,
                    "createdAt": "2026-01-02T03:04:05.000Z",
                    "tags": ["openvino"],
                    "pipeline_tag": "text-generation",
                }
            )
        out.sort(key=lambda r: -r["downloads"])
        return out[: int(request.url.params.get("limit", "30"))]

    def _tree(self, repo: FakeRepo) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        dirs = set()
        for path, f in repo.files.items():
            parts = path.split("/")
            for i in range(1, len(parts)):
                dirs.add("/".join(parts[:i]))
            size = f.listed_size if f.listed_size is not None else len(f.content)
            entry: dict[str, Any] = {"type": "file", "oid": "deadbeef", "size": size, "path": path}
            if f.lfs:
                entry["lfs"] = {"oid": f.sha256, "size": size, "pointerSize": 134}
            entries.append(entry)
        for d in sorted(dirs):
            entries.append({"type": "directory", "oid": "0", "size": 0, "path": d})
        return entries + list(repo.extra_tree)

    def _resolve(self, request: httpx.Request) -> httpx.Response:
        head, _, tail = request.url.path.lstrip("/").partition("/resolve/")
        _revision, _, file_path = tail.partition("/")
        repo = self.repos.get(head)
        f = repo.files.get(file_path) if repo else None
        if f is None:
            return httpx.Response(404)
        if f.rate_limit > 0:
            f.rate_limit -= 1
            headers = {"Retry-After": f.retry_after} if f.retry_after else {}
            return httpx.Response(429, headers=headers)
        if f.server_errors > 0:
            f.server_errors -= 1
            return httpx.Response(503)
        body = f.body
        total = len(body)
        start = 0
        match = _RANGE_RE.match(request.headers.get("Range", ""))
        if match and not f.ignore_range:
            start = int(match.group(1))
            if start >= total:
                return httpx.Response(416, headers={"Content-Range": f"bytes */{total}"})
            chunk = body[start:]
            headers = {
                "Content-Range": f"bytes {start}-{total - 1}/{total}",
                "Content-Length": str(len(chunk)),
            }
            return httpx.Response(206, headers=headers, content=chunk)
        if f.hang_after is not None:
            return httpx.Response(
                200,
                headers={"Content-Length": str(total)},
                stream=_HangingStream(body[: f.hang_after], f),
            )
        return httpx.Response(200, headers={"Content-Length": str(total)}, content=body)
