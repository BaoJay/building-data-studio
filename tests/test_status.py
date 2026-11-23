"""MapOps `status` in the 3D preview: the changes kept per PMTiles in <library>/_status.json."""

import json

import pytest

from app import landmark_library, server


@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "library", landmark_library.Library(tmp_path / "lib"))
    monkeypatch.setattr(server, "CANONICAL_DIR", tmp_path / "canonical")
    a, b = tmp_path / "a.pmtiles", tmp_path / "b.pmtiles"
    a.write_bytes(b""); b.write_bytes(b"")
    return a, b


def statuses(changes):
    return {k: v["status"] for k, v in changes.items()}


def test_set_drop_and_drop_all(files):
    a, b = files
    assert statuses(server.set_status(a, "1", "retired")) == {"1": "retired"}
    assert statuses(server.set_status(a, "2", "active")) == {"1": "retired", "2": "active"}
    assert statuses(server.set_status(a, "2", "retired")) == {"1": "retired", "2": "retired"}  # one entry per id
    assert statuses(server.set_status(b, "9", "retired")) == {"9": "retired"}  # each file has its own changes
    assert statuses(server.set_status(a, "1", None)) == {"2": "retired"}       # back to the tiles' status
    assert server.set_status(a, None, None) == {}
    assert statuses(server.get_status(b)) == {"9": "retired"}
    assert server.get_status(a) == {}
    assert server.get_status(b)["9"]["updated_at"]


def test_rejects_unknown_status(files):
    with pytest.raises(ValueError):
        server.set_status(files[0], "1", "deleted")


def test_hidden_ids_become_retired(files):
    a, _ = files
    key = f"file:{a.resolve()}"
    server.library.root.mkdir(parents=True)
    (server.library.root / "_hidden.json").write_text(json.dumps({key: ["1", "2"]}))
    assert statuses(server.get_status(a)) == {"1": "retired", "2": "retired"}
    assert not (server.library.root / "_hidden.json").exists()
    assert (server.library.root / "_hidden.json.migrated").exists()
    assert statuses(server.set_status(a, "1", "active")) == {"1": "active", "2": "retired"}


def test_csv_for_mapops(files):
    a, _ = files
    server.set_status(a, "b", "active")
    server.set_status(a, "a", "retired")
    lines = server.status_csv(server.get_status(a)).splitlines()
    assert lines[0] == "building_id,status,updated_at"
    assert [l.split(",")[:2] for l in lines[1:]] == [["a", "retired"], ["b", "active"]]
