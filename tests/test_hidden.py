"""Soft delete of buildings in the 3D preview: the ids kept per PMTiles in <library>/_hidden.json."""

from app import landmark_library, server


def test_hide_show_and_show_all(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "library", landmark_library.Library(tmp_path / "lib"))
    monkeypatch.setattr(server, "CANONICAL_DIR", tmp_path / "canonical")
    a, b = tmp_path / "a.pmtiles", tmp_path / "b.pmtiles"
    a.write_bytes(b""); b.write_bytes(b"")

    assert server.set_hidden(a, "1", True) == ["1"]
    assert server.set_hidden(a, "2", True) == ["1", "2"]
    assert server.set_hidden(a, "2", True) == ["1", "2"]  # hiding twice keeps one entry
    assert server.set_hidden(b, "9", True) == ["9"]       # each file has its own list
    assert server.set_hidden(a, "1", False) == ["2"]
    assert server.set_hidden(a, None, False) == []
    assert server._read_hidden() == {f"file:{b.resolve()}": ["9"]}
