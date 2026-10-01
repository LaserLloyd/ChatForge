"""Registry scan, adoption and confined delete. All on tmp dirs, never the real library."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from aichat.models import registry as registry_mod
from aichat.models.catalog import Catalog
from aichat.models.hf_search import ModelError, RepoFile
from aichat.models.registry import (
    GRAPH_FILE,
    REQUIRED_FILES,
    SIDECAR_NAME,
    Registry,
    model_slug,
    read_sidecar,
)

REPO = "OpenVINO/Qwen3-4B-int4-ov"

MODEL_FILES = {
    "openvino_model.xml": b"<xml/>" * 10,
    "openvino_model.bin": b"\x01" * 4000,
    "openvino_tokenizer.xml": b"<t/>" * 5,
    "openvino_tokenizer.bin": b"\x02" * 300,
    "openvino_detokenizer.xml": b"<d/>" * 5,
    "openvino_detokenizer.bin": b"\x03" * 200,
    "config.json": b"{}",
    ".gitattributes": b"*.bin filter=lfs",
}


@pytest.fixture
def dirs(aichat_home: Path) -> tuple[Path, Path]:
    models = aichat_home / "models"
    cache = aichat_home / "cache"
    models.mkdir()
    cache.mkdir()
    return models, cache


def make_model(models: Path, repo_id: str, files: dict[str, bytes] | None = None) -> Path:
    target = models.joinpath(*repo_id.split("/"))
    target.mkdir(parents=True, exist_ok=True)
    for rel, data in (MODEL_FILES if files is None else files).items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return target


def add_hf_cli_junk(model_dir: Path) -> None:
    """What `hf download` leaves behind, plus an OVMS graph.pbtxt."""
    cache = model_dir / ".cache" / "huggingface" / "download"
    cache.mkdir(parents=True)
    (cache / "openvino_model.bin.metadata").write_bytes(b"m" * 50_000)
    (model_dir / ".cache" / "huggingface" / ".gitignore").write_text("*")
    (model_dir / GRAPH_FILE).write_text("node { calculator: 'HttpLLMCalculator' }" * 10)


def repo_listing(files: dict[str, bytes] | None = None) -> list[RepoFile]:
    src = MODEL_FILES if files is None else files
    return [
        RepoFile(path=p, size=len(d), sha256=("a" * 64 if p.endswith(".bin") else None))
        for p, d in src.items()
    ]


def counted_size(files: dict[str, bytes]) -> int:
    return sum(len(d) for d in files.values())


# -- scan ------------------------------------------------------------------


def test_scan_finds_complete_model_and_ignores_dot_dirs_and_graph(dirs) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    add_hf_cli_junk(model_dir)
    (models / ".hidden" / "repo").mkdir(parents=True)
    (models / ".hidden" / "repo" / "openvino_model.xml").write_text("x")
    (models / "OpenVINO" / ".staging").mkdir()
    (models / "OpenVINO" / ".staging" / "f").write_text("x")
    (models / "OpenVINO" / "empty-dir").mkdir()

    records = Registry(models, cache).scan()

    assert [r.id for r in records] == [REPO]
    rec = records[0]
    assert rec.complete and rec.missing == []
    assert rec.path == model_dir
    assert rec.size_bytes == counted_size(MODEL_FILES)  # no .cache, no graph.pbtxt
    assert rec.source is None


def test_scan_reports_missing_required_files(dirs) -> None:
    models, cache = dirs
    files = {k: v for k, v in MODEL_FILES.items() if k != "openvino_detokenizer.xml"}
    make_model(models, "OpenVINO/partial", files)
    (models / "OpenVINO" / "partial" / "openvino_model.bin.part").write_bytes(b"z" * 99)
    rec = Registry(models, cache).get("OpenVINO/partial")
    assert rec is not None
    assert not rec.complete
    assert rec.missing == ["openvino_detokenizer.xml"]
    assert rec.size_bytes == counted_size(files)  # .part not counted


def test_graph_pbtxt_alone_is_not_a_completeness_file(dirs) -> None:
    models, cache = dirs
    make_model(models, "OpenVINO/only-graph", {GRAPH_FILE: b"g"})
    rec = Registry(models, cache).get("OpenVINO/only-graph")
    assert rec is not None and not rec.complete
    assert rec.missing == list(REQUIRED_FILES)
    assert rec.size_bytes == 0


def test_scan_attaches_catalog_and_state(dirs, tmp_path: Path) -> None:
    models, cache = dirs
    make_model(models, REPO)
    state = tmp_path / "state.json"
    state.write_text(
        json.dumps(
            {
                "last_used": {REPO: 1234.5},
                "compiled": {f"{REPO}|NPU|4096|2026.4.0": {"at": 1, "load_s": 300.0}},
            }
        )
    )
    catalog = Catalog.from_dict({"model": [{"id": REPO, "label": "Q", "npu": "recommended"}]})
    rec = Registry(models, cache, catalog=catalog, state_file=state).scan()[0]
    assert rec.catalog is not None and rec.catalog.id == REPO
    assert rec.last_used_at == 1234.5
    assert rec.compiled == {"NPU|4096|2026.4.0": True}
    assert rec.to_dict()["catalog"]["npu"] == "recommended"


def test_scan_missing_models_dir_is_empty(tmp_path: Path) -> None:
    assert Registry(tmp_path / "nope", tmp_path / "cache").scan() == []


def test_sidecar_size_mismatch_marks_incomplete(dirs) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    reg = Registry(models, cache)
    reg.adopt(REPO, repo_listing())
    (model_dir / "openvino_tokenizer.bin").write_bytes(b"short")
    rec = reg.scan()[0]
    assert not rec.complete
    assert rec.missing == ["openvino_tokenizer.bin (size mismatch)"]


# -- adopt -----------------------------------------------------------------


def test_adopt_by_size_writes_sidecar_and_keeps_graph_and_dot_dirs(dirs) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    add_hf_cli_junk(model_dir)
    graph_before = (model_dir / GRAPH_FILE).read_text()
    reg = Registry(models, cache)
    assert [r.id for r in reg.unadopted()] == [REPO]

    # The repo listing may itself mention graph.pbtxt: ignored on both sides.
    listing = [*repo_listing(), RepoFile(path=GRAPH_FILE, size=1)]
    rec = reg.adopt(REPO, listing, revision="abc123", license="apache-2.0")

    assert rec.source == "adopted" and rec.revision == "abc123" and rec.complete
    sidecar = read_sidecar(model_dir)
    assert sidecar is not None
    assert sidecar["schema"] == 1
    assert sidecar["repo_id"] == REPO
    assert sidecar["source"] == "adopted"
    assert sidecar["license"] == "apache-2.0"
    paths = {f["path"] for f in sidecar["files"]}
    assert GRAPH_FILE not in paths and ".gitattributes" not in paths
    assert "openvino_model.bin" in paths
    bin_entry = next(f for f in sidecar["files"] if f["path"] == "openvino_model.bin")
    assert bin_entry == {"path": "openvino_model.bin", "size": 4000, "sha256": "a" * 64}
    assert sidecar["downloaded_at"].endswith("Z")
    # Nothing deleted, nothing moved.
    assert (model_dir / GRAPH_FILE).read_text() == graph_before
    assert (model_dir / ".cache" / "huggingface" / "download").is_dir()
    assert not list(model_dir.glob(".aichat-write-test-*"))
    assert reg.unadopted() == []


def test_adopt_refuses_size_mismatch(dirs) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    listing = repo_listing()
    listing[1] = RepoFile(path="openvino_model.bin", size=9999, sha256=None)
    with pytest.raises(ModelError) as info:
        Registry(models, cache).adopt(REPO, listing)
    assert info.value.code == "adopt_mismatch"
    assert "openvino_model.bin" in info.value.message
    assert not (model_dir / SIDECAR_NAME).exists()


def test_adopt_refuses_missing_repo_file(dirs) -> None:
    models, cache = dirs
    make_model(models, REPO)
    listing = [*repo_listing(), RepoFile(path="generation_config.json", size=10)]
    with pytest.raises(ModelError) as info:
        Registry(models, cache).adopt(REPO, listing)
    assert info.value.code == "adopt_mismatch"


def test_adopt_refuses_incomplete_model(dirs) -> None:
    models, cache = dirs
    make_model(models, REPO, {"openvino_model.xml": b"x"})
    with pytest.raises(ModelError) as info:
        Registry(models, cache).adopt(REPO, None)
    assert info.value.code == "incomplete_model"


def test_adopt_requires_writable_dir(dirs, monkeypatch: pytest.MonkeyPatch) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    monkeypatch.setattr(registry_mod, "_probe_writable", lambda _d: False)
    with pytest.raises(ModelError) as info:
        Registry(models, cache).adopt(REPO, repo_listing())
    assert info.value.code == "not_writable"
    assert not (model_dir / SIDECAR_NAME).exists()


def test_probe_writable_on_tmp_dir(tmp_path: Path) -> None:
    assert registry_mod._probe_writable(tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert not registry_mod._probe_writable(tmp_path / "missing")


def test_adopt_offline_lists_local_files_without_hashes(dirs) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    add_hf_cli_junk(model_dir)
    Registry(models, cache).adopt(REPO)
    sidecar = read_sidecar(model_dir)
    assert sidecar is not None
    assert all(f["sha256"] is None for f in sidecar["files"])
    paths = {f["path"] for f in sidecar["files"]}
    assert GRAPH_FILE not in paths
    assert not any(p.startswith(".cache") for p in paths)


def test_adopt_rejects_traversal_repo_id(dirs) -> None:
    models, cache = dirs
    with pytest.raises(ModelError) as info:
        Registry(models, cache).adopt("../outside", None)
    assert info.value.code == "invalid_repo_id"


# -- delete ----------------------------------------------------------------


def test_delete_removes_model_and_its_compile_cache(dirs) -> None:
    models, cache = dirs
    model_dir = make_model(models, REPO)
    add_hf_cli_junk(model_dir)
    other = make_model(models, "OpenVINO/other-int4-ov")
    model_cache = cache / "ov" / model_slug(REPO) / "NPU-4096"
    model_cache.mkdir(parents=True)
    (model_cache / "blob").write_bytes(b"c" * 10)
    other_cache = cache / "ov" / model_slug("OpenVINO/other-int4-ov")
    other_cache.mkdir(parents=True)
    # HF CLI caches can be read-only; delete must still work.
    ro = model_dir / "readonly.txt"
    ro.write_text("x")
    os.chmod(ro, 0o444)

    reg = Registry(models, cache)
    reg.scan()
    removed = reg.delete(REPO)

    assert not model_dir.exists()
    assert not (cache / "ov" / model_slug(REPO)).exists()
    assert len(removed) == 2
    assert other.is_dir() and other_cache.is_dir()
    assert (models / "OpenVINO").is_dir()  # still holds "other"
    assert reg.get(REPO) is None


def test_delete_removes_empty_publisher_dir(dirs) -> None:
    models, cache = dirs
    make_model(models, "Pub/only")
    Registry(models, cache).delete("Pub/only")
    assert not (models / "Pub").exists()
    assert models.is_dir()


@pytest.mark.parametrize(
    "bad", ["../models", "OpenVINO/..", "OpenVINO/../../etc", "..\\x/y", "/abs/x", "a/b/c"]
)
def test_delete_rejects_traversal_ids(dirs, bad: str) -> None:
    models, cache = dirs
    make_model(models, REPO)
    with pytest.raises(ModelError) as info:
        Registry(models, cache).delete(bad)
    assert info.value.code == "invalid_repo_id"
    assert (models / "OpenVINO" / "Qwen3-4B-int4-ov").is_dir()


def test_delete_unknown_model_is_not_found(dirs) -> None:
    models, cache = dirs
    with pytest.raises(ModelError) as info:
        Registry(models, cache).delete("OpenVINO/missing")
    assert info.value.code == "not_found"


def test_assert_inside_model_dirs_confines(dirs, tmp_path: Path) -> None:
    models, cache = dirs
    reg = Registry(models, cache)
    inside = models / "a" / "b"
    assert reg._assert_inside_model_dirs([inside]) == [inside.resolve()]
    for escape in (tmp_path / "elsewhere", models, models / ".." / "x"):
        with pytest.raises(ModelError) as info:
            reg._assert_inside_model_dirs([escape])
        assert info.value.code == "path_escape"


def _link_dir(link: Path, target: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_delete_refuses_link_to_outside(dirs, tmp_path: Path) -> None:
    models, cache = dirs
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "openvino_model.xml").write_text("keep me")
    (models / "OpenVINO").mkdir()
    try:
        _link_dir(models / "OpenVINO" / "linked", outside)
    except OSError as exc:  # pragma: no cover - platform without link support
        pytest.skip(f"cannot create a directory link: {exc}")
    with pytest.raises(ModelError) as info:
        Registry(models, cache).delete("OpenVINO/linked")
    assert info.value.code == "path_escape"
    assert (outside / "openvino_model.xml").read_text() == "keep me"


def test_delete_refuses_publisher_link_to_outside(dirs, tmp_path: Path) -> None:
    models, cache = dirs
    outside = tmp_path / "otherdrive"
    (outside / "repo").mkdir(parents=True)
    (outside / "repo" / "f").write_text("keep")
    try:
        _link_dir(models / "Pub", outside)
    except OSError as exc:  # pragma: no cover
        pytest.skip(f"cannot create a directory link: {exc}")
    with pytest.raises(ModelError) as info:
        Registry(models, cache).delete("Pub/repo")
    assert info.value.code == "path_escape"
    assert (outside / "repo" / "f").exists()


def test_model_dir_and_cache_dir_helpers(dirs) -> None:
    models, cache = dirs
    reg = Registry(models, cache)
    assert reg.model_dir(REPO) == models / "OpenVINO" / "Qwen3-4B-int4-ov"
    assert reg.cache_dir_for(REPO) == cache / "ov" / "OpenVINO--Qwen3-4B-int4-ov"
    assert model_slug(REPO) == "OpenVINO--Qwen3-4B-int4-ov"
    with pytest.raises(ModelError):
        reg.model_dir("../x")


@pytest.mark.parametrize(
    "repo_id",
    [
        "Pub/model-",  # the compile-cache slug strips a trailing "-" ...
        "Pub/v1.0.-",  # ... and ".-" runs
    ],
)
def test_delete_finds_the_compile_cache_the_runtime_wrote(dirs, repo_id) -> None:
    from aichat.runtime import compile_cache

    models, cache = dirs
    make_model(models, repo_id)
    compiled = compile_cache.cache_dir_for(cache, repo_id, "NPU", 4096)
    compiled.mkdir(parents=True)
    (compiled / "blob").write_bytes(b"c")
    reg = Registry(models, cache)
    assert reg.cache_dir_for(repo_id) == compile_cache.model_cache_root(cache, repo_id)
    assert reg.cache_dir_for(repo_id) != cache / "ov" / model_slug(repo_id)
    removed = reg.delete(repo_id)
    assert len(removed) == 2
    assert not compiled.parent.exists()
