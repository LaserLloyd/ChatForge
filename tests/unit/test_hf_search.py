"""HfSearch and the repo-id / filename safety boundary."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest

from aichat.models.hf_search import (
    HfSearch,
    ModelError,
    _next_page_url,
    _retry_after_seconds,
    file_url,
    parse_hf_timestamp,
    safe_filename,
    safe_relpath,
    validate_repo_id,
)
from tests.fakes.hf_api import FakeFile, FakeHfApi

REPO = "OpenVINO/Qwen3-4B-int4-ov"


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def fake() -> FakeHfApi:
    api = FakeHfApi()
    api.add_repo(
        REPO,
        {
            "openvino_model.bin": FakeFile(b"b" * 100),
            "config.json": FakeFile(b"{}", lfs=False),
            "sub/extra.txt": FakeFile(b"hello", lfs=False),
        },
    )
    api.add_repo("OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov", {"a.bin": b"x"}, downloads=50)
    api.add_repo("someone/qwen-thing", {"a.bin": b"x"}, downloads=10)
    return api


@pytest.fixture
def sleeps() -> _Sleeps:
    return _Sleeps()


@pytest.fixture
async def hf(fake: FakeHfApi, sleeps: _Sleeps):
    client = fake.client()
    search = HfSearch(client=client, sleep=sleeps)
    yield search
    await client.aclose()


async def test_search_sends_plan_params_and_maps_results(hf: HfSearch, fake: FakeHfApi) -> None:
    results = await hf.search("qwen", author="OpenVINO")
    params = fake.requests[-1].url.params
    assert fake.requests[-1].url.path == "/api/models"
    assert params["search"] == "qwen"
    assert params["author"] == "OpenVINO"
    assert params["sort"] == "downloads"
    assert params["limit"] == "30"
    assert [r["id"] for r in results] == [REPO, "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov"]
    first = results[0]
    assert first["downloads"] == 100 and first["likes"] == 5
    assert first["last_modified"] == "2026-01-02T03:04:05.000Z"


async def test_search_without_author_omits_param(hf: HfSearch, fake: FakeHfApi) -> None:
    results = await hf.search("qwen", limit=500)
    assert "author" not in fake.requests[-1].url.params
    assert fake.requests[-1].url.params["limit"] == "100"
    assert len(results) == 3


async def test_repo_files_returns_sizes_and_lfs_oids(hf: HfSearch, fake: FakeHfApi) -> None:
    files = await hf.repo_files(REPO)
    request = fake.requests[-1]
    assert request.url.path == f"/api/models/{REPO}/tree/main"
    assert request.url.params["recursive"] == "true"
    by_path = {f.path: f for f in files}
    assert set(by_path) == {"openvino_model.bin", "config.json", "sub/extra.txt"}
    bin_file = fake.repos[REPO].files["openvino_model.bin"]
    assert by_path["openvino_model.bin"].size == 100
    assert by_path["openvino_model.bin"].sha256 == bin_file.sha256
    assert by_path["config.json"].sha256 is None
    assert by_path["config.json"].size == 2


async def test_repo_meta_reads_sha_and_license(hf: HfSearch) -> None:
    meta = await hf.repo_meta(REPO)
    assert meta.sha == "0123456789abcdef0123456789abcdef01234567"
    assert meta.license == "apache-2.0"


async def test_429_backs_off_with_retry_after(
    hf: HfSearch, fake: FakeHfApi, sleeps: _Sleeps
) -> None:
    fake.api_rate_limit = 2
    fake.api_retry_after = "2"
    files = await hf.repo_files(REPO)
    assert len(files) == 3
    assert sleeps.calls == [2.0, 2.0]


async def test_429_without_retry_after_uses_exponential_backoff(
    hf: HfSearch, fake: FakeHfApi, sleeps: _Sleeps
) -> None:
    fake.api_rate_limit = 2
    fake.api_retry_after = None
    await hf.search("x")
    assert sleeps.calls == [1.0, 2.0]


async def test_429_exhausted_raises_rate_limit(hf: HfSearch, fake: FakeHfApi) -> None:
    fake.api_rate_limit = 10
    with pytest.raises(ModelError) as info:
        await hf.search("x")
    assert info.value.code == "rate_limit"


async def test_404_is_not_found(hf: HfSearch) -> None:
    with pytest.raises(ModelError) as info:
        await hf.repo_files("OpenVINO/does-not-exist")
    assert info.value.code == "not_found"


@pytest.mark.parametrize(
    "bad",
    ["../evil", "OpenVINO/..", "OpenVINO/../../x", "a/b/c", "noslash", "/abs/x", "a/b..c", "con/x"],
)
async def test_traversal_repo_id_rejected_before_any_request(
    hf: HfSearch, fake: FakeHfApi, bad: str
) -> None:
    with pytest.raises(ModelError) as info:
        await hf.repo_files(bad)
    assert info.value.code in {"invalid_repo_id", "unsafe_filename"}
    assert fake.requests == []


async def test_traversal_filename_in_tree_rejects_repo(hf: HfSearch, fake: FakeHfApi) -> None:
    fake.repos[REPO].extra_tree.append({"type": "file", "path": "../../evil.bin", "size": 1})
    with pytest.raises(ModelError) as info:
        await hf.repo_files(REPO)
    assert info.value.code == "unsafe_filename"


async def test_tree_pagination_follows_same_origin_only() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "cursor=2" in str(request.url):
            return httpx.Response(
                200,
                json=[{"type": "file", "path": "b.bin", "size": 2}],
                headers={"Link": '<https://evil.example/api?cursor=3>; rel="next"'},
            )
        return httpx.Response(
            200,
            json=[{"type": "file", "path": "a.bin", "size": 1}],
            headers={
                "Link": f'<https://huggingface.co/api/models/{REPO}/tree/main?cursor=2>; rel="next"'
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        files = await HfSearch(client=client).repo_files(REPO)
    assert [f.path for f in files] == ["a.bin", "b.bin"]
    assert len(calls) == 2
    assert not any("evil.example" in c for c in calls)


def test_next_page_url_rejects_lookalike_host() -> None:
    link = '<https://huggingface.co.evil.example/next>; rel="next"'
    assert _next_page_url(link, endpoint="https://huggingface.co") is None


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "..",
        ".",
        "a/b",
        "a\\b",
        "C:x.bin",
        "/etc/passwd",
        "con.json",
        "aux",
        "x.bin.",
        "x.bin ",
        "x:stream",
        "nul\x00.bin",
    ],
)
def test_safe_filename_rejects(name: str) -> None:
    with pytest.raises(ModelError):
        safe_filename(name)


@pytest.mark.parametrize("name", ["openvino_model.bin", ".gitattributes", "tokenizer.json"])
def test_safe_filename_accepts(name: str) -> None:
    assert safe_filename(name) == name


@pytest.mark.parametrize("path", ["../x", "a/../b", "a//b", "/a", "a\\b", "a/", "a/con"])
def test_safe_relpath_rejects(path: str) -> None:
    with pytest.raises(ModelError):
        safe_relpath(path)


def test_safe_relpath_accepts_nested() -> None:
    assert safe_relpath("sub/dir/file.json") == "sub/dir/file.json"


@pytest.mark.parametrize("good", [REPO, "OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov", "a/b", "A_1/b.c"])
def test_validate_repo_id_accepts(good: str) -> None:
    assert validate_repo_id(good) == good


def test_validate_repo_id_rejects_unicode_and_length() -> None:
    for bad in ["OpenVINO/Qwén", "a/" + "b" * 97, "-a/b", "a/.b"]:
        with pytest.raises(ModelError):
            validate_repo_id(bad)


def test_file_url_layout_and_quoting() -> None:
    url = file_url(REPO, "sub/a b.json", endpoint="https://huggingface.co/", revision="main")
    assert url == f"https://huggingface.co/{REPO}/resolve/main/sub/a%20b.json"
    with pytest.raises(ModelError):
        file_url(REPO, "../x", endpoint="https://huggingface.co")


def test_parse_hf_timestamp() -> None:
    parsed = parse_hf_timestamp("2026-08-18T12:59:16.000Z")
    assert parsed == dt.datetime(2026, 8, 18, 12, 59, 16, tzinfo=dt.UTC)
    assert parse_hf_timestamp("garbage") is None
    assert parse_hf_timestamp(None) is None
    naive = parse_hf_timestamp("2026-08-18T12:59:16")
    assert naive is not None and naive.tzinfo is not None


def test_retry_after_seconds() -> None:
    assert _retry_after_seconds("5", 1.0) == 5.0
    assert _retry_after_seconds(None, 1.5) == 1.5
    assert _retry_after_seconds("soon", 3.0) == 3.0
    assert _retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT", 3.0) == 0.0
