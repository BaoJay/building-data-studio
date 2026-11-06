"""Landmark library: GLB inspection, versions, placements and the history of versions in use."""

from __future__ import annotations

import json
import math
import struct

import pytest

from app import glb, landmark, tools
from app import landmark_library as lib

needs_gdal = pytest.mark.skipif(tools.which("ogr2ogr") is None, reason="needs GDAL")

CENTROID = [105.8375194, 21.0371115]


def make_glb(path, gltf: dict, bin_chunk: bytes = b"") -> None:
    """A .glb with this glTF JSON (and an optional BIN chunk)."""
    data = json.dumps(gltf).encode()
    data += b" " * (-len(data) % 4)
    chunks = struct.pack("<II", len(data), glb.JSON_CHUNK) + data
    if bin_chunk:
        bin_chunk += b"\0" * (-len(bin_chunk) % 4)
        chunks += struct.pack("<II", len(bin_chunk), 0x004E4942) + bin_chunk
    path.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(chunks)) + chunks)


def tower_gltf(**extra) -> dict:
    """A parent node (moved 10 m east, turned 90° about +Y, scaled ×2) over a box mesh 1 × 3 × 2 m
    drawn as 12 triangles, plus a triangle strip of 8 vertices (6 triangles)."""
    s = math.sin(math.pi / 4)
    return {
        "asset": {"version": "2.0", "generator": "test"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [
            {"translation": [10, 0, 0], "rotation": [0, s, 0, s], "scale": [2, 2, 2], "children": [1]},
            {"mesh": 0},
        ],
        "meshes": [{"primitives": [
            {"attributes": {"POSITION": 0}, "indices": 1},
            {"attributes": {"POSITION": 0}, "mode": 5},
        ]}],
        "accessors": [
            {"componentType": 5126, "count": 8, "type": "VEC3", "min": [0, 0, 0], "max": [1, 3, 2]},
            {"componentType": 5123, "count": 36, "type": "SCALAR"},
        ],
        "materials": [{}],
        **extra,
    }


def test_inspect_counts_and_box_through_the_node_tree(tmp_path):
    path = tmp_path / "t.glb"
    make_glb(path, tower_gltf(), b"\0" * 16)
    info = glb.inspect(path)
    assert (info["meshes"], info["triangles"], info["materials"], info["unreadable"]) == (2, 18, 1, [])
    # +90° about +Y sends model +Z to +X and +X to -Z: x' = 2z + 10, z' = -2x.
    assert info["bounds"] == [[10, 0, -2], [14, 6, 0]]


def test_inspect_flags_what_the_preview_cannot_decode(tmp_path):
    path = tmp_path / "d.glb"
    make_glb(path, tower_gltf(extensionsUsed=["KHR_draco_mesh_compression", "KHR_materials_unlit"]))
    assert glb.inspect(path)["unreadable"] == ["Draco"]


def test_inspect_dequantises_normalised_positions(tmp_path):
    gltf = tower_gltf()
    gltf["accessors"][0] = {"componentType": 5123, "normalized": True, "count": 8, "type": "VEC3",
                            "min": [0, 0, 0], "max": [65535, 65535, 0]}
    gltf["nodes"][0] = {"children": [1]}
    path = tmp_path / "q.glb"
    make_glb(path, gltf)
    assert glb.inspect(path)["bounds"] == [[0, 0, 0], [1, 1, 0]]


@pytest.mark.parametrize("raw", [b"", b"PK\x03\x04 not a glb", b"glTF" + struct.pack("<II", 1, 12)])
def test_inspect_rejects_other_files(tmp_path, raw):
    path = tmp_path / "x.glb"
    path.write_bytes(raw)
    with pytest.raises(glb.GlbError):
        glb.inspect(path)


# ------------------------------------------------------------------ library


@pytest.fixture
def library(tmp_path):
    return lib.Library(tmp_path / "landmarks")


def footprint(**kw) -> dict:
    ring = [[105.837, 21.037], [105.838, 21.037], [105.838, 21.0375], [105.837, 21.0375], [105.837, 21.037]]
    return {"geometry": {"type": "Polygon", "coordinates": [ring]}, "centroid": CENTROID, "area_m2": 10613.6,
            "bbox": [105.837, 21.037, 105.838, 21.0375], "size_m": [104.0, 55.6], "height": 19.7, "base": 0.0,
            "matched": "id", "pieces": 1, "height_field": "h_m", **kw}


def register(library, building_id="way/695966191", **kw):
    return library.register(building_id=building_id, name=kw.pop("name", "Nhà Quốc Hội"), link=kw.pop("link", ""),
                            footprint=footprint(), source={"name": "hn.pmtiles", "path": "/nowhere/hn.pmtiles"})


def upload(library, lid, tmp_path, note="", payload=b""):
    src = library.folder(lid) / ".upload-test.glb"
    make_glb(src, tower_gltf(), payload)
    return library.add_version(lid, src, original_name="nha_quoc_hoi.glb", note=note)


def test_register_reads_back_and_lists(library):
    rec = register(library, link="01a12e3a-b634-7657")
    assert rec["id"] == "way_695966191"
    assert (library.root / "way_695966191" / "landmark.json").is_file()
    assert library.get(rec["id"])["link"] == "01a12e3a-b634-7657"
    [item] = library.list()
    assert (item["building_id"], item["versions"], item["in_use"], item["centroid"]) == ("way/695966191", 0, None, CENTROID)
    assert item["geometry"]["type"] == "Polygon"


def test_one_landmark_per_building_and_unique_folders(library):
    register(library)
    with pytest.raises(lib.LibraryError, match="đã có"):
        register(library)
    # A different building whose id cleans up to the same folder name gets its own folder.
    assert register(library, building_id="way_695966191")["id"] == "way_695966191-2"
    assert register(library, building_id="manifest.json")["id"] == "bmanifest.json"


def test_versions_keep_every_file_and_start_from_the_placement_in_use(library, tmp_path):
    lid = register(library)["id"]
    rec = upload(library, lid, tmp_path, note="Normals")
    v1 = rec["versions"][0]
    assert (v1["version"], v1["file"], v1["original_name"], v1["status"]) == (1, "v1.glb", "nha_quoc_hoi.glb", "unused")
    assert v1["placement"] == {"heading": 0.0, "scale": 1.0, "anchor": "origin", "lon": CENTROID[0], "lat": CENTROID[1]}
    assert v1["model"]["triangles"] == 18 and len(v1["sha256"]) == 64

    placed = {"heading": 11, "scale": 1.5, "anchor": "bbox", "lon": 105.8376, "lat": 21.0372}
    library.set_placement(lid, 1, placed)
    library.use(lid, 1, "Bản đầu")
    rec = upload(library, lid, tmp_path, payload=b"other bytes")
    v2 = rec["versions"][1]
    assert v2["placement"] == {**placed, "heading": 11.0}  # copied from v1, the version in use
    assert v2["same_as"] is None
    assert (library.root / lid / "v1.glb").is_file() and (library.root / lid / "v2.glb").is_file()
    assert not list((library.root / lid).glob(".upload-*"))
    rec = upload(library, lid, tmp_path)
    assert rec["versions"][2]["same_as"] == 1  # same bytes as v1


def test_history_tells_which_versions_were_used_and_when(library, tmp_path):
    lid = register(library)["id"]
    for _ in range(3):
        upload(library, lid, tmp_path)
    library.use(lid, 1)
    library.use(lid, 2, "Sửa vị trí")
    rec = library.get(lid)
    assert rec["in_use"] == 2
    assert [v["status"] for v in rec["versions"]] == ["used", "in_use", "unused"]
    (start1, end1), = rec["versions"][0]["used"]
    (start2, end2), = rec["versions"][1]["used"]
    assert end1 == start2 and end2 is None
    with pytest.raises(lib.LibraryError, match="đang được dùng"):
        library.use(lid, 2)
    with pytest.raises(lib.NotFound):
        library.use(lid, 9)
    rec = library.use(lid, None, "Gỡ khỏi bản đồ")
    assert rec["in_use"] is None and [v["status"] for v in rec["versions"]] == ["used", "used", "unused"]
    actions = [(e["action"], e.get("from"), e.get("to")) for e in rec["history"]]
    assert actions == [("register", None, None), ("upload", None, None), ("upload", None, None), ("upload", None, None),
                       ("use", None, 1), ("use", 1, 2), ("use", 2, None)]


@pytest.mark.parametrize(("raw", "error"), [
    ({"heading": 0, "scale": 0, "anchor": "origin", "lon": 105, "lat": 21}, "Tỉ lệ"),
    ({"heading": 0, "scale": 1, "anchor": "top", "lon": 105, "lat": 21}, "Điểm neo"),
    ({"heading": 0, "scale": 1, "anchor": "origin", "lon": 105, "lat": 89}, "Toạ độ"),
    ({"heading": "nan", "scale": 1, "anchor": "origin", "lon": 105, "lat": 21}, "hướng"),
    ({"scale": 1, "anchor": "origin", "lon": 105, "lat": 21}, "hướng"),
])
def test_placement_is_checked(raw, error):
    with pytest.raises(lib.LibraryError, match=error):
        lib.check_placement(raw)


def test_heading_wraps_to_one_turn():
    p = lib.check_placement({"heading": -10, "scale": 1, "anchor": "origin", "lon": 105, "lat": 21})
    assert p["heading"] == 350
    assert lib.check_placement({**p, "heading": 720.0004})["heading"] == 0


def test_manifest_lists_the_versions_in_use(library, tmp_path):
    a = register(library)["id"]
    b = register(library, building_id="77", name="Lăng Bác")["id"]
    upload(library, a, tmp_path)
    upload(library, b, tmp_path)
    library.use(b, 1)
    [item] = library.manifest()["landmarks"]
    assert (item["id"], item["version"], item["file"]) == (b, 1, f"{b}/v1.glb")


def test_edit_and_trash(library):
    lid = register(library)["id"]
    rec = library.update(lid, name="Nhà Quốc Hội Việt Nam", link="way/695966191")
    assert (rec["name"], rec["history"][-1]["fields"]) == ("Nhà Quốc Hội Việt Nam", ["name", "link"])
    dest = library.trash(lid)
    assert dest.parent == library.root / "_trash" and (dest / "landmark.json").is_file()
    with pytest.raises(lib.NotFound):
        library.get(lid)
    assert library.list() == []


@pytest.mark.parametrize("lid", ["", "../etc", "_trash", ".hidden", "a/b", "missing"])
def test_folder_only_opens_landmarks(library, lid):
    register(library)
    with pytest.raises(lib.NotFound):
        library.folder(lid)


# ------------------------------------------------------------------ asset.extras, material extras, levels of detail

TASCO = {"lat": 21.0156627, "lon": 105.7828616}


def lit_gltf(lod_zoom=17, zoom_range=(17, None), anchor=TASCO, **extra) -> dict:
    """tower_gltf() as the landmark pipeline writes it: asset.extras (anchor, zoom range) and
    material extras (an LED line, the baked wash, glass)."""
    extras = {"pipeline": "test", "lod_zoom": lod_zoom, "zoom_range": list(zoom_range)}
    if anchor:
        extras["anchor"] = anchor
    g = tower_gltf(**extra)
    g["asset"] = {"version": "2.0", "generator": "test", "extras": extras}
    g["materials"] = [
        {"name": "D01_Frost_White_LED_WARM", "emissiveFactor": [1, 0.8, 0.5], "extras": {"emissive_group": "led_warm", "surface_type": "sign"}},
        {"name": "Wall_BAKED", "emissiveFactor": [1, 1, 1], "extras": {"emissive_group": "led_wash", "emissive_suffix": "BAKED"}},
        {"name": "Glass", "extras": {"surface_type": "glass"}},
        {"name": "Group_without_light", "extras": {"emissive_group": "lobby"}},  # no emissiveFactor: never glows
    ]
    return g


def upload_lit(library, lid, *, lod=None, version=None, **kw):
    src = library.folder(lid) / ".upload-test.glb"
    make_glb(src, lit_gltf(**kw))
    if version is None:
        return library.add_version(lid, src, original_name=f"tasco_z{kw.get('lod_zoom', 17)}.glb", note="")
    return library.add_lod(lid, version, src, original_name=f"tasco_z{kw.get('lod_zoom', 17)}.glb")


def test_inspect_reads_asset_and_material_extras(tmp_path):
    path = tmp_path / "lit.glb"
    make_glb(path, lit_gltf())
    m = glb.inspect(path)
    assert m["anchor"] == TASCO
    assert m["lod"] == {"lod_zoom": 17, "zoom_range": [17, None]}
    assert m["emissive_groups"] == {"led_warm": 1, "led_wash": 1}
    assert m["glass"] == 1
    path2 = tmp_path / "plain.glb"
    make_glb(path2, tower_gltf())
    m = glb.inspect(path2)
    assert (m["anchor"], m["lod"], m["emissive_groups"], m["glass"]) == (None, None, {}, 0)


@pytest.mark.parametrize(("extras", "lod"), [
    ({"zoom_range": [16, 17], "lod_zoom": 16}, {"lod_zoom": 16, "zoom_range": [16, 17]}),
    ({"zoom_range": [17, None]}, {"lod_zoom": 17, "zoom_range": [17, None]}),
    ({"zoom_range": [15.5, 16]}, {"lod_zoom": 15.5, "zoom_range": [15.5, 16]}),
    ({"zoom_range": [17, 16]}, None),
    ({"zoom_range": [17]}, None),
    ({"zoom_range": ["17", None]}, None),
    ({"zoom_range": [True, None]}, None),
    ({}, None),
    (None, None),
])
def test_zoom_range_of_asset_extras(extras, lod):
    assert glb.lod_of(extras) == lod


@pytest.mark.parametrize(("extras", "anchor"), [
    ({"anchor": {"lat": 21.0156627, "lon": 105.7828616}}, {"lat": 21.0156627, "lon": 105.7828616}),
    ({"anchor": {"lat": 105.78, "lon": 21.01}}, None),  # swapped: lat beyond 85
    ({"anchor": [21.0156627, 105.7828616]}, None),
    ({"anchor": {"lat": "21", "lon": 105}}, None),
    ({}, None),
])
def test_anchor_of_asset_extras(extras, anchor):
    assert glb.anchor_of(extras) == anchor


def test_a_georeferenced_glb_is_placed_at_its_anchor_heading_0(library, tmp_path):
    lid = register(library)["id"]
    upload(library, lid, tmp_path)  # an older model, turned by hand
    library.set_placement(lid, 1, {"heading": 37, "scale": 1.2, "anchor": "bbox", "lon": 105.8376, "lat": 21.0372})
    library.use(lid, 1)
    v2 = upload_lit(library, lid)["versions"][1]
    # Not the hand-made placement of v1: the GLB says where it stands.
    assert v2["placement"] == {"heading": 0.0, "scale": 1.0, "anchor": "origin", "lon": TASCO["lon"], "lat": TASCO["lat"]}


def test_files_per_zoom_range_join_their_version(library, tmp_path):
    lid = register(library)["id"]
    upload_lit(library, lid)
    upload_lit(library, lid, version=1, lod_zoom=15, zoom_range=(15, 16))
    rec = upload_lit(library, lid, version=1, lod_zoom=16, zoom_range=(16, 17))
    v = rec["versions"][0]
    assert [(x["lod_zoom"], x["zoom_range"], x["file"]) for x in v["lods"]] == [(16, [16, 17], "v1_z16.glb"), (15, [15, 16], "v1_z15.glb")]
    assert rec["history"][-1] == {**rec["history"][-1], "action": "lod", "version": 1, "lod_zoom": 16}
    assert len(rec["versions"]) == 1
    path, item = library.glb_path(lid, 1, 15)
    assert path.name == "v1_z15.glb" and item["zoom_range"] == [15, 16]
    assert library.glb_path(lid, 1, 17)[0].name == "v1.glb"  # the version's own range
    assert library.glb_path(lid, 1)[0].name == "v1.glb"
    with pytest.raises(lib.NotFound):
        library.glb_path(lid, 1, 14)
    # What the preview and the app read: most detailed first, own file flagged.
    lods = lib.version_lods(library.read(lid)["versions"][0])
    assert [(x["lod_zoom"], x["own"], x["file"]) for x in lods] == [(17, True, "v1.glb"), (16, False, "v1_z16.glb"), (15, False, "v1_z15.glb")]
    library.use(lid, 1)
    [item] = library.manifest()["landmarks"]
    assert [(x["lod_zoom"], x["zoom_range"], x["file"]) for x in item["lods"]] == [
        (17, [17, None], f"{lid}/v1.glb"), (16, [16, 17], f"{lid}/v1_z16.glb"), (15, [15, 16], f"{lid}/v1_z15.glb")]


@pytest.mark.parametrize(("kw", "error"), [
    ({"lod_zoom": 16, "zoom_range": (16, 18)}, "trùng"),          # overlaps z17 [17, ∞)
    ({"lod_zoom": 17, "zoom_range": (12, 13)}, "trùng"),          # same lod_zoom
    ({"lod_zoom": 16, "zoom_range": (16, 17), "anchor": {"lat": 21.0166627, "lon": 105.7828616}}, "lệch"),  # 111 m away
])
def test_a_file_per_zoom_is_checked(library, tmp_path, kw, error):
    lid = register(library)["id"]
    upload_lit(library, lid)
    with pytest.raises(lib.LibraryError, match=error):
        upload_lit(library, lid, version=1, **kw)
    assert "lods" not in library.read(lid)["versions"][0]
    assert sorted(f.name for f in (library.root / lid).glob("v*.glb")) == ["v1.glb"]


def test_files_per_zoom_need_zoom_ranges(library, tmp_path):
    lid = register(library)["id"]
    upload(library, lid, tmp_path)  # no zoom_range: drawn at every zoom
    assert lib.version_lods(library.read(lid)["versions"][0]) == []
    with pytest.raises(lib.LibraryError, match="không có zoom_range"):
        upload_lit(library, lid, version=1, lod_zoom=16, zoom_range=(16, 17))
    upload_lit(library, lid)  # v2, with a range
    src = library.folder(lid) / ".upload-test.glb"
    make_glb(src, tower_gltf())
    with pytest.raises(lib.LibraryError, match="asset.extras.zoom_range"):
        library.add_lod(lid, 2, src, original_name="plain.glb")


def test_zoom_slots_set_the_range_of_files_without_one(library, tmp_path):
    """A z17 / z16 / z15 set chosen slot by slot: GLBs without asset.extras.zoom_range (or with
    another one) are drawn at the slot's range."""
    lid = register(library)["id"]
    src = library.folder(lid) / ".upload-test.glb"
    make_glb(src, tower_gltf())
    library.add_version(lid, src, original_name="tasco_z17.glb", note="", slot="17")
    make_glb(src, tower_gltf())
    library.add_lod(lid, 1, src, original_name="tasco_z16.glb", slot="16")
    src = library.folder(lid) / ".upload-test.glb"
    make_glb(src, lit_gltf(lod_zoom=15, zoom_range=(15, 16)))  # the slot wins over the GLB
    rec = library.add_lod(lid, 1, src, original_name="tasco_z15.glb", slot=15)
    v = rec["versions"][0]
    assert v["model"]["lod"] == {"lod_zoom": 17, "zoom_range": [17, None]}
    lods = lib.version_lods(v)
    assert [(x["lod_zoom"], x["zoom_range"], x["file"]) for x in lods] == [
        (17, [17, None], "v1.glb"), (16, [15, 17], "v1_z16.glb"), (15, [0, 15], "v1_z15.glb")]
    assert library.glb_path(lid, 1, 15)[0].name == "v1_z15.glb"
    make_glb(src, tower_gltf())
    with pytest.raises(lib.LibraryError, match="trùng"):  # the slot is taken
        library.add_lod(lid, 1, src, original_name="again.glb", slot="16")
    with pytest.raises(lib.LibraryError, match="ô zoom"):
        library.add_lod(lid, 1, src, original_name="z14.glb", slot="14")


def test_a_bad_upload_is_refused_and_leaves_no_version(library):
    lid = register(library)["id"]
    src = library.folder(lid) / ".upload-bad.glb"
    src.write_bytes(b"not a glb")
    with pytest.raises(glb.GlbError):
        library.add_version(lid, src, original_name="bad.glb", note="")
    assert library.get(lid)["versions"] == []


@needs_gdal
def test_outline_dissolves_tile_pieces():
    left = [[[0, 0], [0.001, 0], [0.001, 0.001], [0, 0.001], [0, 0]]]
    right = [[[0.001, 0], [0.002, 0], [0.002, 0.001], [0.001, 0.001], [0.001, 0]]]
    geom = landmark.outline([left, right])
    assert geom["type"] == "Polygon" and len(geom["coordinates"]) == 1
    xs = [p[0] for p in geom["coordinates"][0]]
    assert (min(xs), max(xs)) == (0, 0.002)
    assert landmark.outline([left]) == {"type": "Polygon", "coordinates": left}
