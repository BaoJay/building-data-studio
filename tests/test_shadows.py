"""Precomputed ground shadows: sun maths, exact sweep of concave footprints, GDAL round trip."""

import json
import math

import pytest

from app import shadows, tools, vmap

needs_gdal = pytest.mark.skipif(tools.which("ogr2ogr") is None, reason="needs GDAL (ogr2ogr)")

M_LON = shadows.METERS_PER_DEG_LON_EQ  # at the equator, where the test buildings sit
M_LAT = shadows.METERS_PER_DEG_LAT


def _ring(*xy_m):
    """Ring in degrees from metre coordinates near (0, 0)."""
    pts = [[x / M_LON, y / M_LAT] for x, y in xy_m]
    return pts + [pts[0]]


def _area_m2(geometry):
    """Planar area of a (Multi)Polygon in degrees near the equator, in m²."""
    polys = geometry["coordinates"] if geometry["type"] == "MultiPolygon" else [geometry["coordinates"]]
    total = 0.0
    for rings in polys:
        for i, ring in enumerate(rings):
            a = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2
            total += abs(a) * (1 if i == 0 else -1)
    return total * M_LON * M_LAT


def test_sun_from_maplibre_light():
    sun = shadows.Sun.from_light({"position": [1.15, 270, 55]})
    assert (sun.azimuth, sun.altitude) == (270, 35)
    assert sun.length_factor == pytest.approx(1 / math.tan(math.radians(35)))


@pytest.mark.parametrize("azimuth, east, north", [(270, 1, 0), (90, -1, 0), (180, 0, 1), (0, 0, -1)])
def test_shadow_falls_away_from_the_sun(azimuth, east, north):
    dlon, dlat = shadows.Sun(azimuth, 45).offset_deg(0.0)
    assert dlon * M_LON == pytest.approx(east, abs=1e-9)
    assert dlat * M_LAT == pytest.approx(north, abs=1e-9)


def test_offset_is_scaled_by_latitude():
    dlon0, _ = shadows.Sun(270, 45).offset_deg(0.0)
    dlon60, _ = shadows.Sun(270, 45).offset_deg(60.0)
    assert dlon60 == pytest.approx(2 * dlon0)


def test_pieces_skip_edges_parallel_to_the_sun():
    square = [_ring((0, 0), (10, 0), (10, 10), (0, 10))]
    pieces = list(shadows.shadow_pieces(square, 0, 10, 1 / M_LON, 0))
    # two shifted copies + the two edges across the ray; the two along it sweep nothing
    assert len(pieces) == 4
    assert list(shadows.shadow_pieces(square, 5, 5, 1 / M_LON, 0)) == []


@pytest.mark.parametrize("bbox", ["1,2,3", "a,b,c,d", "10,10,9,11", "105,21,106,22", "0,-89,0.01,-88.99"])
def test_bad_bbox(bbox):
    with pytest.raises(shadows.ShadowError):
        shadows.parse_bbox(bbox)


def test_field_names_are_checked():
    assert shadows.check_field("building:levels", "x") == "building:levels"
    assert shadows.check_field("", "x") is None
    for bad in ("h;drop", "a,b", "x" * 65, "h\n"):
        with pytest.raises(shadows.ShadowError):
            shadows.check_field(bad, "x")


def test_every_sunny_preset_has_a_shadow_look():
    assert {p for p, v in vmap.PRESETS.items() if v.shadow} == {"dawn", "day", "dusk"}
    assert vmap.PRESETS["night"].shadow is None


@pytest.fixture
def run(tmp_path):
    """compute() over a GeoJSON of buildings in metres near (0, 0); sun from the west at 45°,
    so a building casts eastward exactly as far as it is tall."""

    def _run(features):
        src = tmp_path / "b.geojson"
        src.write_text(json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"b": 0, "has_parts": False, **props},
             "geometry": {"type": "Polygon", "coordinates": rings}}
            for rings, props in features
        ]}))
        req = shadows.Request(path=src, layer="b", zoom=None, bbox=(-0.001, -0.001, 0.001, 0.001),
                              height="h", base="b", parts="has_parts", sun=shadows.Sun(270, 45))
        body, stats = shadows.compute(req)
        feats = json.loads(body)["features"]
        return (_area_m2(feats[0]["geometry"]) if feats else 0.0), stats

    return _run


@needs_gdal
def test_square_casts_its_height_east(run):
    area, stats = run([([_ring((0, 0), (10, 0), (10, 10), (0, 10))], {"h": 10})])
    assert stats["buildings"] == 1
    assert area == pytest.approx(100, rel=0.01)  # [10, 20] × [0, 10]; the footprint is cut out


@needs_gdal
def test_concave_footprint_is_swept_exactly(run):
    # L: [0,20]×[0,10] ∪ [0,10]×[10,20], 5 m tall -> shadow [0,25]×[0,10] ∪ [0,15]×[10,20]
    # (400 m²) minus the footprint (300 m²). A convex hull would also shade the notch.
    ell = [_ring((0, 0), (20, 0), (20, 10), (10, 10), (10, 20), (0, 20))]
    area, _ = run([(ell, {"h": 5})])
    assert area == pytest.approx(100, rel=0.01)


@needs_gdal
def test_raised_base_and_parts(run):
    square = [_ring((0, 0), (10, 0), (10, 10), (0, 10))]
    # Lifted 5–10 m: shadow spans 5–20 m east, minus the footprint.
    area, _ = run([(square, {"h": 10, "b": 5})])
    assert area == pytest.approx(100, rel=0.01)
    # An outline drawn as parts casts nothing itself; a part on it still does.
    area, stats = run([(square, {"h": 50, "has_parts": True}), (square, {"h": 10})])
    assert stats["buildings"] == 1
    assert area == pytest.approx(100, rel=0.01)


@needs_gdal
def test_overlapping_shadows_are_dissolved(run):
    a = [_ring((0, 0), (10, 0), (10, 10), (0, 10))]
    b = [_ring((0, 2), (10, 2), (10, 8), (0, 8))]  # inside a, taller
    area, _ = run([(a, {"h": 10}), (b, {"h": 20})])
    assert area == pytest.approx(100 + 60, rel=0.01)  # one polygon, nothing counted twice


@needs_gdal
def test_no_heights_no_shadow(run):
    area, stats = run([([_ring((0, 0), (10, 0), (10, 10), (0, 10))], {"h": None})])
    assert area == 0 and stats["pieces"] == 0
