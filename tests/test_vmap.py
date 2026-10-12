"""VMAP basemap presets: day is the mobile style untouched; the suggested presets only regrade colours."""

import json

import pytest

from app import vmap


@pytest.fixture(scope="module")
def base():
    return json.loads(vmap.LOCAL_COPY.read_text())


def _layer(style, layer_id):
    return next(layer for layer in style["layers"] if layer["id"] == layer_id)


def _lightness(value):
    return vmap.to_oklab(vmap.parse_color(value))[0]


def _colours(value):
    if isinstance(value, str):
        return [value] if vmap.parse_color(value) else []
    if isinstance(value, list):
        return [c for v in value for c in _colours(v)]
    if isinstance(value, dict):
        return [c for v in value.values() for c in _colours(v)]
    return []


@pytest.mark.parametrize("text, expected", [
    ("#abc", "#aabbcc"),
    ("#F6F3E9", "#f6f3e9"),
    ("rgba(113, 180, 94, 0.42)", "rgba(113, 180, 94, 0.42)"),
    ("rgba(255,255,255,0)", "rgba(255, 255, 255, 0)"),
    ("rgb(0 128 255)", "#0080ff"),
    ("hsl(120, 100%, 25%)", "#008000"),
    ("#11223380", "rgba(17, 34, 51, 0.502)"),
])
def test_parse_format(text, expected):
    assert vmap.format_color(vmap.parse_color(text)) == expected


@pytest.mark.parametrize("text", ["identity", "CT", "#12", "rgb(1,2)", "Roboto Medium"])
def test_non_colours_are_left_alone(text):
    assert vmap.parse_color(text) is None


def test_oklab_roundtrip():
    for text in ("#f6f3e9", "#8fd6ea", "#7f8790", "#a5444b", "#000000", "#ffffff"):
        c = vmap.parse_color(text)
        assert vmap.format_color(vmap.from_oklab(vmap.to_oklab(c))) == text


def test_day_is_the_mobile_style(base):
    day = vmap.apply_preset(base, "day")
    assert day["layers"] == base["layers"]
    assert day["sources"] is base["sources"]
    assert day["light"] == base["light"]
    assert day["sprite"] == base["sprite"] and day["glyphs"] == base["glyphs"]
    assert day["metadata"]["vmap:light-preset-origin"] == "mobile"
    assert day["metadata"]["vmap:building-3d"] == vmap.MOBILE_BUILDING


@pytest.mark.parametrize("preset", ["dawn", "dusk", "night"])
def test_suggested_presets_only_change_colours(base, preset):
    style = vmap.apply_preset(base, preset)
    assert style["light"] == vmap.PRESETS[preset].light
    assert style["metadata"]["vmap:light-preset-origin"] == "suggested"
    assert len(style["layers"]) == len(base["layers"])
    changed = 0
    for new, old in zip(style["layers"], base["layers"], strict=True):
        assert {k: v for k, v in new.items() if k != "paint"} == {k: v for k, v in old.items() if k != "paint"}
        for prop, value in (old.get("paint") or {}).items():
            if not prop.endswith("-color"):
                assert new["paint"][prop] == value
                continue
            after = _colours(new["paint"][prop])
            assert len(after) == len(_colours(value))  # same expression shape, every colour still valid
            changed += new["paint"][prop] != value
    assert changed > 100
    assert base["layers"][0]["paint"]["background-color"] == "#f6f3e9"  # input not mutated


def test_night_is_dark_and_readable(base):
    night = vmap.apply_preset(base, "night")
    ground = _lightness(_layer(night, "background")["paint"]["background-color"])
    assert ground < 0.3
    # Road hierarchy flips: the motorway is the brightest line, every road is lighter than the ground.
    motorway = _lightness(_layer(night, "road_motorway")["paint"]["line-color"])
    minor = _lightness(_layer(night, "road_minor")["paint"]["line-color"])
    assert motorway > minor > ground + 0.05
    # Every label: light text on a dark halo.
    for layer in night["layers"]:
        paint = layer.get("paint") or {}
        if "text-color" not in paint or layer["id"] == "road_shield":
            continue
        halo = max(_lightness(c) for c in _colours(paint["text-halo-color"]))
        assert min(_lightness(c) for c in _colours(paint["text-color"])) > halo + 0.3, layer["id"]


def test_shield_text_and_alpha_are_kept(base):
    night = vmap.apply_preset(base, "night")
    # Shield backgrounds are sprite images the style cannot recolour, so their text stays put.
    assert _layer(night, "road_shield")["paint"] == _layer(base, "road_shield")["paint"]
    assert vmap.parse_color(_layer(night, "park_outline")["paint"]["line-color"])[3] == pytest.approx(0.42, abs=0.002)


def test_layer_roles():
    assert vmap.layer_role("waterway_tunnel", "line-color") == "water"
    assert vmap.layer_role("tunnel_motorway_casing", "line-color") == "casing"
    assert vmap.layer_role("tunnel_major_rail", "line-color") == "rail"
    assert vmap.layer_role("road_service_track", "line-color") == "road"
    assert vmap.layer_role("landuse_track", "fill-color") == "green"
    assert vmap.layer_role("park_outline", "line-color") == "green"
    assert vmap.layer_role("countries_outline", "line-color") == "boundary"
    assert vmap.layer_role("water_name_line", "text-color") == "text"
    assert vmap.layer_role("place_city", "text-halo-color") == "halo"
    assert vmap.layer_role("road_shield", "text-color") == "keep"
    assert vmap.layer_role("background", "background-color") == "ground"


def test_assets_point_at_the_local_proxy_only_for_the_preview(base):
    local = vmap.apply_preset(base, "dusk", "http://127.0.0.1:8765/api/vmap/assets/")
    assert local["sprite"] == "http://127.0.0.1:8765/api/vmap/assets/styles/streets-v1-3d/sprite"
    assert local["glyphs"] == "http://127.0.0.1:8765/api/vmap/assets/glyphs/{fontstack}/{range}.pbf"
    assert local["sources"]["openmaptiles"]["url"] == base["sources"]["openmaptiles"]["url"]  # tiles allow CORS
    exported = vmap.apply_preset(base, "dusk")
    assert exported["sprite"] == base["sprite"] and exported["glyphs"] == base["glyphs"]


@pytest.mark.parametrize("path, ok", [
    ("styles/streets-v1-3d/sprite.json", True),
    ("styles/streets-v1-3d/sprite@2x.png", True),
    ("glyphs/Roboto Medium/7680-7935.pbf", True),
    ("glyphs/Roboto Regular,Noto Sans Regular/0-255.pbf", True),
    ("styles/streets-v1/style.json", False),
    ("styles/../secret/sprite.json", False),
    ("glyphs/../../etc/0-255.pbf", False),
    ("tiles/16/1/2.pbf", False),
    ("https://evil.example/sprite.json", False),
])
def test_asset_whitelist(path, ok, monkeypatch):
    monkeypatch.setattr(vmap, "_fetch", lambda p, ttl: (b"x", "application/octet-stream"))
    if ok:
        assert vmap.fetch_asset(path) == (b"x", "application/octet-stream")
    else:
        with pytest.raises(vmap.AssetNotAllowed):
            vmap.fetch_asset(path)


def test_falls_back_to_the_repo_copy_when_offline(monkeypatch, base):
    def offline(path, ttl):
        raise vmap.UpstreamError("offline")

    monkeypatch.setattr(vmap, "_fetch", offline)
    monkeypatch.setattr(vmap, "_offline_until", 0.0)
    body, source = vmap.style_bytes("night", None)
    assert source == "repo"
    style = json.loads(body)
    assert style["metadata"]["vmap:light-preset"] == "night"
    assert len(style["layers"]) == len(base["layers"])


def test_presets_info_lists_the_four_in_order():
    info = vmap.presets_info()
    assert [p["id"] for p in info["presets"]] == ["dawn", "day", "dusk", "night"]
    assert info["default"] == "day"
    assert [p["origin"] for p in info["presets"]] == ["suggested", "mobile", "suggested", "suggested"]
