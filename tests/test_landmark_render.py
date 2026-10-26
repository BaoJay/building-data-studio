"""Landmarks drawn by the 3D preview: the landmarks with a GLB of the PMTiles being viewed (the
version in use, else the newest),
matched by the resolved path of the file token (app/landmark_library.py, /api/landmarks/render),
and the library API the preview's drawer uses, end to end."""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

from app import landmark_library as lib
from app import server
from app.jobs import FileRegistry
from tests.test_landmark_library import footprint, make_glb, tower_gltf


def register(library, path: Path | str, building_id: str, name: str = "") -> str:
    source = {"name": Path(path).name, "path": str(path), "layer": "buildings", "id_field": "building_id",
              "zoom": 17, "parent_field": "parent_building_id"}
    return library.register(building_id=building_id, name=name, link="", footprint=footprint(), source=source)["id"]


def upload(library, lid: str) -> int:
    src = library.folder(lid) / ".upload-test.glb"
    make_glb(src, tower_gltf())
    return library.add_version(lid, src, original_name=f"{lid}.glb", note="")["versions"][-1]["version"]


@pytest.fixture
def files(tmp_path):
    """Two PMTiles of the same city and a library with landmarks on both."""
    a = tmp_path / "tiles" / "a.pmtiles"
    b = tmp_path / "tiles" / "b.pmtiles"
    a.parent.mkdir()
    a.write_bytes(b"PMTiles")
    b.write_bytes(b"PMTiles")
    library = lib.Library(tmp_path / "landmarks")
    on_a = register(library, a, "1", "Hồ Gươm")
    library.use(on_a, upload(library, on_a))
    register(library, a, "2", "Chưa có GLB")  # no version in use
    unused = register(library, a, "3", "Chưa dùng")
    upload(library, unused)
    on_b = register(library, b, "4", "Trên file B")
    library.use(on_b, upload(library, on_b))
    return {"a": a, "b": b, "library": library, "on_a": on_a, "on_b": on_b}


# ------------------------------------------------------------------ library


def test_the_landmarks_with_a_glb_of_this_file(files):
    library = files["library"]
    unused, item = library.render(files["a"])  # by name: "Chưa dùng", "Hồ Gươm"; "Chưa có GLB" is left out
    assert (item["id"], item["building_id"], item["version"], item["in_use"]) == (files["on_a"], "1", 1, 1)
    # No version in use: the newest is drawn, and says so.
    assert (unused["building_id"], unused["version"], unused["in_use"]) == ("3", 1, None)
    assert item["placement"] == {"heading": 0.0, "scale": 1.0, "anchor": "origin", "lon": 105.8375194, "lat": 21.0371115}
    assert item["bbox"] == [105.837, 21.037, 105.838, 21.0375]
    assert item["model"]["bounds"] == [[10, 0, -2], [14, 6, 0]]
    assert (item["layer"], item["id_field"], item["parent_field"]) == ("buildings", "building_id", "parent_building_id")
    assert [i["id"] for i in library.render(files["b"])] == [files["on_b"]]


def test_a_file_reached_another_way_is_the_same_file(files, tmp_path):
    link = tmp_path / "link.pmtiles"
    link.symlink_to(files["a"])
    same = [files["a"].parent / ".." / "tiles" / "a.pmtiles", link]
    for path in same:
        assert [i["building_id"] for i in files["library"].render(path)] == ["3", "1"], path


def test_a_file_without_landmarks_draws_none(files, tmp_path):
    other = tmp_path / "c.pmtiles"
    other.write_bytes(b"PMTiles")
    assert files["library"].render(other) == []


def test_records_without_a_source_path_are_never_drawn(files):
    library = files["library"]
    rec_path = library.folder(files["on_a"]) / "landmark.json"
    rec = json.loads(rec_path.read_text("utf-8"))
    rec["source"] = {"name": "a.pmtiles"}
    rec_path.write_text(json.dumps(rec), "utf-8")
    assert files["on_a"] not in [i["id"] for i in library.render(files["a"])]


@pytest.mark.parametrize("recorded", [None, "", "relative/a.pmtiles"])
def test_same_file_needs_the_recorded_path(files, recorded):
    assert not lib.same_file(recorded, files["a"])


def test_a_relative_recorded_path_matches_from_its_folder(files, monkeypatch):
    monkeypatch.chdir(files["a"].parent)
    assert lib.same_file("a.pmtiles", files["a"])


def test_render_is_a_reserved_landmark_id(files):
    lid = register(files["library"], files["a"], "render")
    assert lid != "render"


# ------------------------------------------------------------------ API


def call(method: str, path: str, query: str = "", body: bytes = b"", json_body=None) -> tuple[int, dict, bytes]:
    """One request through the ASGI app (no HTTP client needed)."""
    if json_body is not None:
        body = json.dumps(json_body).encode()

    async def run():
        out = {"status": 0, "headers": {}, "body": b""}
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(msg):
            if msg["type"] == "http.response.start":
                out["status"] = msg["status"]
                out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
            elif msg["type"] == "http.response.body":
                out["body"] += msg.get("body", b"")

        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
                 "headers": [(b"host", b"127.0.0.1:8765"), (b"content-type", b"application/json")],
                 "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765)}
        await server.app(scope, receive, send)
        return out

    out = anyio.run(run)
    return out["status"], out["headers"], out["body"]


def get_json(path: str, query: str = "") -> tuple[int, dict]:
    status, _, body = call("GET", path, query)
    return status, json.loads(body)


@pytest.fixture
def app_state(files, monkeypatch):
    registry = FileRegistry()
    monkeypatch.setattr(server, "registry", registry)
    monkeypatch.setattr(server, "library", files["library"])
    monkeypatch.setattr(server, "base_status", lambda *a: {"base": None})
    return {**files, "registry": registry, "ta": registry.register(files["a"]), "tb": registry.register(files["b"])}


def test_render_endpoint_answers_the_landmarks_of_the_token(app_state):
    status, data = get_json("/api/landmarks/render", f"f={app_state['ta']}")
    assert status == 200
    [unused, item] = data["landmarks"]
    assert unused["in_use"] is None
    lid = app_state["on_a"]
    assert (item["id"], item["version"], item["glb"]) == (lid, 1, f"/api/landmarks/{lid}/versions/1/glb")
    assert item["placement"]["anchor"] == "origin" and item["geometry"]["type"] == "Polygon"
    assert data["source"] == {"name": "a.pmtiles", "token": app_state["ta"]}
    # The GLB URL serves the version, and nothing of file B leaks into A.
    status, headers, body = call("GET", item["glb"])
    assert status == 200 and body[:4] == b"glTF" and headers["content-type"] == "model/gltf-binary"
    assert {i["building_id"] for i in data["landmarks"]} == {"1", "3"}


def test_render_endpoint_follows_the_resolved_path(app_state, tmp_path):
    link = tmp_path / "link.pmtiles"
    link.symlink_to(app_state["a"])
    token = app_state["registry"].register(link)
    status, data = get_json("/api/landmarks/render", f"f={token}")
    assert status == 200 and [i["building_id"] for i in data["landmarks"]] == ["3", "1"]


@pytest.mark.parametrize("query", ["", "f=", "f=not-a-token", "f=../../etc/passwd"])
def test_render_endpoint_refuses_unknown_tokens(app_state, query):
    status, data = get_json("/api/landmarks/render", query)
    assert status == 404 and "error" in data


def test_render_endpoint_refuses_a_file_gone(app_state):
    os.remove(app_state["b"])
    status, _ = get_json("/api/landmarks/render", f"f={app_state['tb']}")
    assert status == 404


def test_list_marks_the_landmarks_of_the_file_open(app_state):
    status, data = get_json("/api/landmarks")
    assert status == 200 and len(data["landmarks"]) == 4
    assert all("same_source" not in l for l in data["landmarks"])  # response unchanged without f
    _, data = get_json("/api/landmarks", f"f={app_state['ta']}")
    flags = {l["building_id"]: (l["same_source"], l["source_token"]) for l in data["landmarks"]}
    assert flags == {"1": (True, app_state["ta"]), "2": (True, app_state["ta"]), "3": (True, app_state["ta"]),
                     "4": (False, app_state["tb"])}
    _, data = get_json("/api/landmarks", "f=unknown")
    assert not any(l["same_source"] for l in data["landmarks"])


def test_library_crud_through_the_api(app_state):
    """Register, upload, placement, version in use, manifest and trash, as the drawer does them."""
    ta = app_state["ta"]
    lid = register(app_state["library"], app_state["a"], "9", "Nhà Hát Lớn")
    glb_path = app_state["a"].parent / "nha_hat.glb"
    make_glb(glb_path, tower_gltf())

    status, _, body = call("PUT", f"/api/landmarks/{lid}/versions", "name=nha_hat.glb&note=first", glb_path.read_bytes())
    assert status == 201
    rec = json.loads(body)
    assert [v["version"] for v in rec["versions"]] == [1] and rec["in_use"] is None
    status, _, _ = call("PUT", f"/api/landmarks/{lid}/versions", "name=x.obj", b"nope")
    assert status == 400

    # Not in use yet: drawn all the same (the newest version), marked as not in use.
    _, data = get_json("/api/landmarks/render", f"f={ta}")
    [item] = [i for i in data["landmarks"] if i["id"] == lid]
    assert (item["version"], item["in_use"]) == (1, None)

    placement = {"heading": 90, "scale": 2, "anchor": "bbox", "lon": 105.8376, "lat": 21.0372}
    status, _, body = call("PUT", f"/api/landmarks/{lid}/versions/1/placement", json_body=placement)
    assert status == 200 and json.loads(body)["versions"][0]["placement"]["heading"] == 90
    status, _, _ = call("PUT", f"/api/landmarks/{lid}/versions/1/placement", json_body={**placement, "scale": 0})
    assert status == 400

    status, _, body = call("POST", f"/api/landmarks/{lid}/use", json_body={"version": 1, "note": "ok"})
    assert status == 200 and json.loads(body)["in_use"] == 1
    _, data = get_json("/api/landmarks/render", f"f={ta}")
    [item] = [i for i in data["landmarks"] if i["id"] == lid]
    assert item["placement"] == {"heading": 90.0, "scale": 2.0, "anchor": "bbox", "lon": 105.8376, "lat": 21.0372}

    status, manifest = get_json("/api/landmarks/manifest.json")
    assert status == 200 and lid in [m["id"] for m in manifest["landmarks"]]

    status, _, body = call("PATCH", f"/api/landmarks/{lid}", json_body={"name": "Nhà Hát Lớn Hà Nội"})
    assert status == 200 and json.loads(body)["name"] == "Nhà Hát Lớn Hà Nội"

    status, _, body = call("POST", f"/api/landmarks/{lid}/use", json_body={"version": None})
    assert status == 200 and json.loads(body)["in_use"] is None
    status, manifest = get_json("/api/landmarks/manifest.json")
    assert lid not in [m["id"] for m in manifest["landmarks"]]  # the manifest keeps versions in use only
    _, data = get_json("/api/landmarks/render", f"f={ta}")
    [item] = [i for i in data["landmarks"] if i["id"] == lid]
    assert item["in_use"] is None

    status, _, body = call("DELETE", f"/api/landmarks/{lid}")
    assert status == 200 and "_trash" in json.loads(body)["moved_to"]
    status, _ = get_json(f"/api/landmarks/{lid}")
    assert status == 404


# ------------------------------------------------------------------ ground shadows of the GLB


def test_only_landmarks_in_use_of_this_file_are_released(files):
    assert [r["id"] for r in files["library"].released(files["a"])] == [files["on_a"]]


def test_shadow_route_casts_presets_with_sun_only(app_state, monkeypatch):
    from app import landmark_shadow, shadows

    monkeypatch.setattr(landmark_shadow, "preset_sun", lambda pid: shadows.Sun(270, 45) if pid == "day" else None)
    lid = app_state["on_a"]
    status, headers, body = call("GET", f"/api/landmarks/{lid}/versions/1/shadow", "preset=day")
    assert status == 200 and headers["content-type"].startswith("application/geo+json")
    assert json.loads(body)["type"] == "FeatureCollection"
    assert (app_state["library"].folder(lid) / "shadows").is_dir()
    for preset in ("night", "dark"):
        status, _, _ = call("GET", f"/api/landmarks/{lid}/versions/1/shadow", f"preset={preset}")
        assert status == 404, preset
    status, _ = get_json(f"/api/landmarks/{lid}/versions/9/shadow", "preset=day")
    assert status == 404


# ------------------------------------------------------------------ /landmarks is gone


def test_the_landmark_page_is_gone(app_state):
    for path in ("/landmarks", "/landmarks/"):
        status, _, _ = call("GET", path)
        assert status == 404, path
    status, _, _ = call("GET", "/static/landmarks.html")
    assert status == 404


def test_the_frontend_only_links_to_the_preview():
    static = Path(server.STATIC_DIR)
    for path in [*static.glob("*.js"), *static.glob("*.html")]:
        text = path.read_text("utf-8")
        for needle in ('"/landmarks', "`/landmarks", "'/landmarks", "landmarksUrl", "landmarks.js"):
            assert needle not in text, f"{path.name}: {needle}"
