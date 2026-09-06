"""podcleaner.eval.fixtures: manifest parsing and hash pinning (offline)."""

from __future__ import annotations

import hashlib
import json

import pytest

from podcleaner.eval.fixtures import FixtureError, FixtureStore, load_manifest, REPO_ROOT


def test_repo_manifest_parses_and_is_consistent():
    episodes = load_manifest()
    assert set(episodes) == {"ldn491", "ldn490", "solved-life-path", "hot-oh-canada"}
    for ep in episodes.values():
        assert ep.language in ("de", "en")
        assert "podcatcher" in ep.audio, "every episode has the listener-facing variant"
        for fx in ep.audio.values():
            assert fx.sha256 and len(fx.sha256) == 64 and fx.duration > 0
        for w in ep.windows:
            assert w["variant"] in ep.audio and w["duration"] > 0
        if ep.dai:
            assert (REPO_ROOT / "tests" / "integration" / ep.dai["file"]).exists()
        assert (REPO_ROOT / "tests" / "integration" / ep.label).exists()


def test_store_refuses_missing_and_mismatching(tmp_path):
    episodes = load_manifest()
    fx = episodes["ldn491"].audio["clean"]
    store = FixtureStore(tmp_path, allow_download=False)
    with pytest.raises(FixtureError) as exc:
        store.resolve(fx)
    assert exc.value.kind == "missing"
    bogus = store.path(fx)
    bogus.parent.mkdir(parents=True)
    bogus.write_bytes(b"not the episode")
    with pytest.raises(FixtureError) as exc:
        store.resolve(fx)
    assert exc.value.kind == "hash_mismatch"


def test_store_accepts_matching_bytes(tmp_path):
    data = b"hello audio"
    manifest = {"episodes": {"x": {"podcast": "p", "language": "en", "audio": {"podcatcher": {
        "file": "x.mp3", "sha256": hashlib.sha256(data).hexdigest(), "duration": 1.0}}}}}
    mpath = tmp_path / "m.json"
    mpath.write_text(json.dumps(manifest))
    episodes = load_manifest(mpath)
    store = FixtureStore(tmp_path / "store", allow_download=False)
    dest = store.path(episodes["x"].audio["podcatcher"])
    dest.parent.mkdir(parents=True)
    dest.write_bytes(data)
    assert store.audio(episodes["x"]) == dest
