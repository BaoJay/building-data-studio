"""Ground shadow of a landmark cast from its GLB mesh: triangles of a .glb, placement, the
projection along the sun, the cache next to the GLB, and the LoD1 pieces it replaces."""

from __future__ import annotations

import json
import struct

import pytest

from app import glb, landmark_shadow, shadows, tools
from app import landmark_library as lib
from tests.test_landmark_library import make_glb

needs_gdal = pytest.mark.skipif(tools.which("ogr2ogr") is None, reason="needs GDAL (ogr2ogr)")

M = landmark_shadow.METERS_PER_DEG  # near (0, 0), where the test landmarks stand
WEST_45 = shadows.Sun(270, 45)  # casts eastward exactly as far as a point is high
BOX_FACES = [0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6, 0, 4, 5, 0, 5, 1, 1, 5, 6, 1, 6, 2, 2, 6, 7, 2, 7, 3, 3, 7, 4, 3, 4, 0]


def box_glb(path, sx=10.0, sy=20.0, sz=4.0, **extra):
    """A .glb holding one box [0, sx] × [0, sy] × [0, sz] (glTF metres, +Y up), 12 triangles."""
    corners = [(x, y, z) for y in (0, sy) for x, z in ((0, 0), (sx, 0), (sx, sz), (0, sz))]
    positions = struct.pack("<24f", *[v for c in corners for v in c])
    indices = struct.pack("<36H", *BOX_FACES)
    gltf = {
        "asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1}]}],
        "buffers": [{"byteLength": len(positions) + len(indices)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
                        {"buffer": 0, "byteOffset": len(positions), "byteLength": len(indices)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3",
                       "min": [0, 0, 0], "max": [sx, sy, sz]},
                      {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"}],
        **extra,
    }
    make_glb(path, gltf, positions + indices)
    return path


def placement(**kw):
    return {"heading": 0.0, "scale": 1.0, "anchor": "bbox", "lon": 0.0, "lat": 0.0, **kw}


def area_m2(geometry):
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    total = 0.0
    for rings in polys:
        for i, ring in enumerate(rings):
            a = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2
            total += abs(a) * (1 if i == 0 else -1)
    return total * M * M


def bounds_m(geometry):
    pts = [p for poly in geometry["coordinates"] for ring in poly for p in ring]
    return (min(p[0] for p in pts) * M, min(p[1] for p in pts) * M, max(p[0] for p in pts) * M, max(p[1] for p in pts) * M)


def square(half):
    ring = [[-half / M, -half / M], [half / M, -half / M], [half / M, half / M], [-half / M, half / M]]
    return {"geometry": {"type": "Polygon", "coordinates": [ring + ring[:1]]}}


# ------------------------------------------------------------------ triangles and placement


def test_triangles_of_an_indexed_box(tmp_path):
    path = box_glb(tmp_path / "b.glb")
    tris = list(glb.triangles(path))
    assert len(tris) == 12 == glb.inspect(path)["triangles"]
    pts = {p for t in tris for p in t}
    assert pts == {(x, y, z) for x in (0, 10) for y in (0, 20) for z in (0, 4)}


def test_triangles_go_through_the_node_tree_and_strips(tmp_path):
    positions = struct.pack("<12f", 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0)
    gltf = {"asset": {"version": "2.0"}, "nodes": [{"translation": [5, 0, 0], "children": [1]}, {"mesh": 0, "scale": [2, 2, 2]}],
            "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "mode": 5}]}],
            "bufferViews": [{"buffer": 0, "byteLength": 48}],
            "accessors": [{"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"}]}
    make_glb(tmp_path / "s.glb", gltf, positions)
    tris = list(glb.triangles(tmp_path / "s.glb"))
    assert tris == [((5, 0, 0), (7, 0, 0), (5, 2, 0)), ((5, 2, 0), (7, 0, 0), (7, 2, 0))]


@pytest.mark.parametrize("ext", ["KHR_draco_mesh_compression", "EXT_meshopt_compression"])
def test_packed_geometry_cannot_cast(tmp_path, ext):
    path = box_glb(tmp_path / "d.glb", extensionsUsed=[ext])
    with pytest.raises(glb.GlbError):
        list(glb.triangles(path))


def test_place_mirrors_the_preview():
    # heading 0: +X east, front (+Z) south; +Y up.
    assert landmark_shadow.place((1, 2, 3), (0, 0, 0), placement()) == pytest.approx((1, -3, 2))
    # heading 90: clockwise seen from above, so east turns south.
    assert landmark_shadow.place((1, 0, 0), (0, 0, 0), placement(heading=90)) == pytest.approx((0, -1, 0))
    assert landmark_shadow.place((1, 0, 0), (0, 0, 0), placement(scale=3)) == pytest.approx((3, 0, 0))
    assert landmark_shadow.anchor_point([[0, 1, 0], [10, 20, 4]], "bbox") == (5, 1, 2)
    assert landmark_shadow.anchor_point([[0, 1, 0], [10, 20, 4]], "origin") == (0, 0, 0)


# ------------------------------------------------------------------ the shadow


@pytest.fixture
def box(tmp_path):
    path = box_glb(tmp_path / "box.glb")
    return path, glb.inspect(path)


@needs_gdal
def test_box_casts_its_height_away_from_the_sun(box):
    path, model = box
    feats = json.loads(landmark_shadow.compute(path, model, placement(), WEST_45))["features"]
    # 10 m east-west × 4 m north-south, 20 m high, sun from the west at 45°: 4 × (10 + 20).
    assert area_m2(feats[0]["geometry"]) == pytest.approx(120, rel=0.01)
    assert bounds_m(feats[0]["geometry"]) == pytest.approx((-5, -2, 25, 2), abs=0.01)


@needs_gdal
def test_heading_scale_and_anchor_move_the_shadow(box):
    path, model = box
    cast = lambda **kw: json.loads(landmark_shadow.compute(path, model, placement(**kw), WEST_45))["features"][0]["geometry"]
    assert area_m2(cast(heading=90)) == pytest.approx(10 * (4 + 20), rel=0.01)
    assert area_m2(cast(scale=2)) == pytest.approx(8 * (20 + 40), rel=0.01)
    assert bounds_m(cast(anchor="origin")) == pytest.approx((0, -4, 30, 0), abs=0.01)




# ------------------------------------------------------------------ cache, presets, manifest


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.setattr(landmark_shadow, "preset_sun", lambda pid: WEST_45 if pid in ("dawn", "day", "dusk") else None)
    library = lib.Library(tmp_path / "landmarks")
    fp = {"geometry": square(5)["geometry"], "centroid": [0.0, 0.0], "area_m2": 100.0, "bbox": [0, 0, 0, 0],
          "size_m": [10, 10], "height": 8.0, "base": 0.0, "matched": "id", "pieces": 1, "height_field": "h"}
    pmtiles = tmp_path / "a.pmtiles"
    pmtiles.write_bytes(b"")
    rec = library.register(building_id="L", name="Lăng", link="", footprint=fp,
                           source={"name": "a.pmtiles", "path": str(pmtiles), "layer": "b", "id_field": "id", "parent_field": "parent"})
    box_glb(library.folder(rec["id"]) / ".up.glb")
    library.add_version(rec["id"], library.folder(rec["id"]) / ".up.glb", original_name="lang.glb", note="")
    return library, rec["id"], pmtiles


@needs_gdal
def test_shadow_is_cached_per_placement(library):
    library, lid, _ = library
    rec = library.read(lid)
    first = landmark_shadow.for_version(library.folder(lid), rec, rec["versions"][0], "day")
    assert first.parent.name == "shadows" and first.name.startswith("v1-day-")
    assert landmark_shadow.for_version(library.folder(lid), rec, rec["versions"][0], "day") == first
    rec = library.set_placement(lid, 1, placement(heading=90, lon=0.0, lat=0.0))
    second = landmark_shadow.for_version(library.folder(lid), rec, rec["versions"][0], "day")
    assert second != first and second.is_file() and not first.exists()


def test_night_casts_nothing(library):
    library, lid, _ = library
    rec = library.read(lid)
    with pytest.raises(shadows.ShadowError):
        landmark_shadow.for_version(library.folder(lid), rec, rec["versions"][0], "night")


@needs_gdal
def test_only_released_landmarks_cast_from_their_glb(library):
    library, lid, pmtiles = library
    assert landmark_shadow.released(library, pmtiles, "day") == ([], [])
    library.use(lid, 1)
    [lm], problems = landmark_shadow.released(library, pmtiles, "day")
    assert (lm.building_id, lm.id_field, lm.parent_field, lm.layer, problems) == ("L", "id", "parent", "b", [])


@needs_gdal
def test_manifest_lists_the_shadow_files(library):
    library, lid, _ = library
    library.use(lid, 1)
    data = landmark_shadow.add_to_manifest(library, library.manifest())
    [item] = data["landmarks"]
    assert sorted(item["shadows"]) == ["dawn", "day", "dusk"]
    assert all((library.root / f).is_file() for f in item["shadows"].values())
    assert data["shadow_presets"]["day"]["sun"] == {"azimuth": 270, "altitude": 45}


# ------------------------------------------------------------------ replacing the LoD1 shadow


@needs_gdal
def test_glb_shadow_replaces_the_lod1_pieces_of_its_building_and_parts(tmp_path):
    def feat(half, cx, props):
        ring = [[(cx + x) / shadows.METERS_PER_DEG_LON_EQ, y / shadows.METERS_PER_DEG_LAT] for x, y in
                ((-half, -half), (half, -half), (half, half), (-half, half), (-half, -half))]
        return {"type": "Feature", "properties": props, "geometry": {"type": "Polygon", "coordinates": [ring]}}

    landmark = [feat(5, 0, {"id": "L", "h": 10}), feat(2, 0, {"id": "L-part", "parent": "L", "h": 30})]
    other = [feat(5, 100, {"id": "other", "h": 10})]
    glb_shadow = tmp_path / "lmk.geojson"  # 4 × 2 m, from inside the landmark's footprint to 1 m past it
    ring = [[2 / M, -1 / M], [6 / M, -1 / M], [6 / M, 1 / M], [2 / M, 1 / M], [2 / M, -1 / M]]
    glb_shadow.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}}]}))

    def cast(landmarks=(), features=landmark + other):
        src = tmp_path / "b.geojson"
        src.write_text(json.dumps({"type": "FeatureCollection", "features": features}))
        req = shadows.Request(path=src, layer="b", zoom=None, bbox=(-0.001, -0.001, 0.002, 0.001),
                              height="h", base=None, parts=None, sun=WEST_45, landmarks=landmarks)
        body, stats = shadows.compute(req)
        return sum(area_m2(f["geometry"]) for f in json.loads(body)["features"]), stats

    lod1, _ = cast()
    lm = landmark_shadow.LandmarkShadow("L", "id", "parent", "b", glb_shadow)
    with_glb, stats = cast((lm,))
    assert stats["landmarks"] == 1
    # other: 10 × 10; L: 10 × 10, and its 30 m part (4 m wide) reaches 17 m further: 4 × 17.
    assert lod1 == pytest.approx(100 + 100 + 4 * 17, rel=0.02)
    # other, plus the whole GLB strip: the landmark's footprint does not cut its own shadow.
    assert with_glb == pytest.approx(10 * 10 + 8, rel=0.02)
    # An area holding the landmark only (no footprint left to cut out) still casts.
    alone, _ = cast((lm,), landmark)
    assert alone == pytest.approx(8, rel=0.03)
