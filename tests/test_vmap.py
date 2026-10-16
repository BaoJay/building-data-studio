"""VMAP basemap presets: each one is the style VMAP publishes for it, with its own light."""

import json
import time

import pytest

from app import shadows, vmap

PRESET_IDS = ["dawn", "day", "dusk", "night", "dark"]

# Root `light` of each published streets-v2 style, as seen on 2026-10-16 (repo copies).
PUBLISHED_LIGHTS = {
    "day": ("#ffffff", 0.2, [1.15, 180, 20]),
    "dawn": ("#feca8b", 0.365, [1.15, 120, 50]),
    "dusk": ("#fec286", 0.2, [1.15, 240, 80]),
    "night": ("#3f4455", 0.275, [1.15, 270, 20]),
    "dark": ("#b0b9c9", 0.22, [1.15, 210, 35]),
}


def _copy(preset):
    return json.loads(vmap.PRESETS[preset].local_copy.read_text())


@pytest.fixture
def offline(monkeypatch):
    def unreachable(path, ttl):
        raise vmap.UpstreamUnreachable("offline")

    monkeypatch.setattr(vmap, "_fetch", unreachable)
    monkeypatch.setattr(vmap, "_offline_until", 0.0)


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


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_repo_copy_is_the_published_style_of_its_preset(preset):
    p = vmap.PRESETS[preset]
    assert p.style == f"streets-v2-{preset}"
    assert p.origin == "mobile"
    style = _copy(preset)
    assert style["metadata"]["tasco:style-variant"] == preset
    color, intensity, position = PUBLISHED_LIGHTS[preset]
    assert style["light"] == {"anchor": "map", "color": color, "intensity": intensity, "position": position}
    assert not any(layer["type"] == "fill-extrusion" for layer in style["layers"])


@pytest.mark.parametrize("preset", PRESET_IDS)
def test_preset_is_the_style_as_published(preset):
    base = _copy(preset)
    before = json.dumps(base)
    style = vmap.apply_preset(base, preset)
    assert style["layers"] is base["layers"] and style["sources"] is base["sources"]
    assert style["light"] == base["light"]
    assert style["sprite"] == base["sprite"] and style["glyphs"] == base["glyphs"]
    assert style["metadata"]["vmap:light-preset"] == preset
    assert style["metadata"]["vmap:building-3d"] == vmap.MOBILE_BUILDING
    assert {k: v for k, v in style["metadata"].items() if not k.startswith("vmap:")} == base["metadata"]
    assert json.dumps(base) == before  # input not mutated


def test_building_look_is_inferred_and_configurable():
    assert vmap.building_look({}) == {"color": "#ddcca9", "opacity": 0.85, "origin": "inferred"}
    assert vmap.building_look({"BDS_VMAP_BUILDING_COLOR": "#CFCEC9"}) == {"color": "#cfcec9", "opacity": 0.85, "origin": "env"}
    assert vmap.building_look({"BDS_VMAP_BUILDING_OPACITY": "0.5"})["opacity"] == 0.5
    for env in ({"BDS_VMAP_BUILDING_COLOR": "beige"}, {"BDS_VMAP_BUILDING_OPACITY": "1.5"},
                {"BDS_VMAP_BUILDING_OPACITY": "abc"}):
        with pytest.raises(ValueError):
            vmap.building_look(env)
    # One look for every preset: the style's own light shades it.
    assert {json.dumps(p.building()) for p in vmap.PRESETS.values()} == {json.dumps(vmap.MOBILE_BUILDING)}


def test_shadows_follow_each_style_light(offline):
    for preset, (_, _, (_, azimuth, polar)) in PUBLISHED_LIGHTS.items():
        sun = shadows.Sun.from_light(vmap.effective_light(preset))
        assert (sun.azimuth, sun.altitude) == (azimuth, 90 - polar)


def test_each_preset_fetches_its_own_style(monkeypatch):
    fetched = []

    def fake(path, ttl):
        fetched.append(path)
        preset = path.split("/")[1].removeprefix("streets-v2-")
        return json.dumps({"version": 8, "sources": {}, "layers": [], "light": {"position": [1, 1, 1], "color": preset}}).encode(), "application/json"

    monkeypatch.setattr(vmap, "_fetch", fake)
    monkeypatch.setattr(vmap, "_offline_until", 0.0)
    for preset in PRESET_IDS:
        body, source = vmap.style_bytes(preset, None)
        assert source == "live"
        assert json.loads(body)["light"]["color"] == preset
        assert vmap.effective_light(preset)["color"] == preset
    assert set(fetched) == {f"styles/streets-v2-{p}/style.json" for p in PRESET_IDS}


def test_a_style_missing_upstream_falls_back_alone(monkeypatch):
    live = json.dumps({"version": 8, "sources": {}, "layers": [], "light": {"color": "live"}}).encode()

    def fake(path, ttl):
        if "dawn" in path:
            raise vmap.UpstreamError("HTTP 404")
        return live, "application/json"

    monkeypatch.setattr(vmap, "_fetch", fake)
    monkeypatch.setattr(vmap, "_offline_until", 0.0)
    assert vmap.style_bytes("dawn", None)[1] == "repo"
    assert vmap.effective_light("dawn") == _copy("dawn")["light"]
    assert vmap.style_bytes("day", None)[1] == "live"


def test_unreachable_upstream_uses_the_repo_copies_for_a_while(monkeypatch, offline):
    body, source = vmap.style_bytes("night", None)
    assert source == "repo"
    style = json.loads(body)
    assert style["metadata"]["vmap:light-preset"] == "night"
    assert style["layers"] == _copy("night")["layers"]
    assert vmap._offline_until > time.monotonic()

    def must_not_fetch(path, ttl):
        raise AssertionError("fetched while backing off")

    monkeypatch.setattr(vmap, "_fetch", must_not_fetch)
    assert vmap.style_bytes("dusk", None)[1] == "repo"


def test_assets_point_at_the_local_proxy_only_for_the_preview():
    base = _copy("dusk")
    local = vmap.apply_preset(base, "dusk", "http://127.0.0.1:8765/api/vmap/assets/")
    assert local["sprite"] == "http://127.0.0.1:8765/api/vmap/assets/styles/streets-v1-dusk/sprite"
    assert local["glyphs"] == "http://127.0.0.1:8765/api/vmap/assets/glyphs/{fontstack}/{range}.pbf"
    assert local["sources"]["openmaptiles"]["url"] == base["sources"]["openmaptiles"]["url"]  # tiles allow CORS
    exported = vmap.apply_preset(base, "dusk")
    assert exported["sprite"] == base["sprite"] and exported["glyphs"] == base["glyphs"]


@pytest.mark.parametrize("path, ok", [
    ("styles/streets-v1-3d/sprite.json", True),
    ("styles/streets-v1-dusk/sprite@2x.png", True),
    ("glyphs/Roboto Medium/7680-7935.pbf", True),
    ("glyphs/Roboto Regular,Noto Sans Regular/0-255.pbf", True),
    ("styles/streets-v2-day/style.json", False),
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


def test_presets_info_lists_the_published_presets_in_order():
    info = vmap.presets_info()
    assert [p["id"] for p in info["presets"]] == PRESET_IDS
    assert info["default"] == "day"
    assert {p["origin"] for p in info["presets"]} == {"mobile"}
    assert info["presets"][0]["url"] == "https://assets.vmaps.vn/styles/streets-v2-dawn/style.json"
    assert [p["id"] for p in info["presets"] if p["dark"]] == ["dusk", "night", "dark"]
