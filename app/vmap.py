"""VMAP basemap (the style the mobile app uses) for the 3D preview, in four map-light presets.

assets.vmaps.vn sends no CORS headers, so the browser cannot load the style, sprite or
glyphs from another origin. The server fetches them instead (style: live, falling back to
the copy in map-styles/), points sprite / glyphs back at itself, and serves them same-origin.
The vector tiles (tasco-maps.dnpwater.vn) do allow CORS and are loaded directly.

Presets: "day" is the mobile style as published. Dawn / dusk / night do not exist in the
mobile style yet; they are suggestions derived from it by regrading every paint colour by
its role (ground, water, road, label…) in OKLab, plus a matching `light` block.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

UPSTREAM = "https://assets.vmaps.vn/"
STYLE_PATH = "styles/streets-v1/style.json"
LOCAL_COPY = Path(__file__).resolve().parent.parent / "map-styles" / "streets-v1.style.json"
STYLE_TTL = 300.0
OFFLINE_RETRY = 60.0
ASSET_TTL = 24 * 3600.0
TIMEOUT = 15.0
CACHE_ENTRIES = 600

# Only what a style needs: sprite sheets and glyph ranges. No "..", no other hosts.
_ASSET_RE = re.compile(
    r"^(styles/[A-Za-z0-9_-]+/sprite(@[23]x)?\.(json|png)"
    r"|glyphs/[A-Za-z0-9 ,_-]+/\d{1,5}-\d{1,5}\.pbf)$"
)


class UpstreamError(RuntimeError):
    pass


class AssetNotAllowed(ValueError):
    pass


# ------------------------------------------------------------------ colour


Rgba = tuple[float, float, float, float]  # sRGB 0..1, alpha 0..1

_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FUNC_RE = re.compile(r"^(rgba?|hsla?)\(([^)]*)\)$")


def parse_color(value: str) -> Rgba | None:
    """#rgb / #rgba / #rrggbb / #rrggbbaa / rgb[a]() / hsl[a](). Anything else -> None."""
    s = value.strip()
    m = _HEX_RE.match(s)
    if m:
        h = m.group(1)
        if len(h) in (3, 4):
            h = "".join(ch * 2 for ch in h)
        chans = [int(h[i:i + 2], 16) / 255 for i in range(0, len(h), 2)]
        return (chans[0], chans[1], chans[2], chans[3] if len(chans) == 4 else 1.0)
    m = _FUNC_RE.match(s.lower())
    if not m:
        return None
    parts = [p.strip() for p in re.split(r"[,\s/]+", m.group(2).strip()) if p.strip()]
    if len(parts) not in (3, 4):
        return None
    try:
        alpha = _channel(parts[3], 1.0) if len(parts) == 4 else 1.0
        if m.group(1).startswith("rgb"):
            r, g, b = (_channel(p, 255.0) for p in parts[:3])
        else:
            hue = float(parts[0].removesuffix("deg")) % 360
            r, g, b = _hsl_to_rgb(hue, _channel(parts[1], 100.0), _channel(parts[2], 100.0))
    except ValueError:
        return None
    return (_clamp(r), _clamp(g), _clamp(b), _clamp(alpha))


def _channel(text: str, scale: float) -> float:
    return float(text[:-1]) / 100 if text.endswith("%") else float(text) / scale


def _hsl_to_rgb(h: float, s: float, light: float) -> tuple[float, float, float]:
    c = (1 - abs(2 * light - 1)) * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = light - c / 2
    r, g, b = [(c, x, 0), (x, c, 0), (0, c, x), (0, x, c), (x, 0, c), (c, 0, x)][int(h // 60) % 6]
    return (r + m, g + m, b + m)


def format_color(c: Rgba) -> str:
    r, g, b = (round(_clamp(v) * 255) for v in c[:3])
    if c[3] >= 0.9995:
        return f"#{r:02x}{g:02x}{b:02x}"
    return f"rgba({r}, {g}, {b}, {round(c[3], 3):g})"


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _to_srgb(c: float) -> float:
    c = _clamp(c)
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def to_oklab(c: Rgba) -> tuple[float, float, float]:
    r, g, b = (_to_linear(v) for v in c[:3])
    lc = math.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    mc = math.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    sc = math.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (
        0.2104542553 * lc + 0.7936177850 * mc - 0.0040720468 * sc,
        1.9779984951 * lc - 2.4285922050 * mc + 0.4505937099 * sc,
        0.0259040371 * lc + 0.7827717662 * mc - 0.8086757660 * sc,
    )


def from_oklab(lab: tuple[float, float, float], alpha: float = 1.0) -> Rgba:
    L, a, b = lab
    lc = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    mc = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    sc = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (
        _to_srgb(4.0767416621 * lc - 3.3077115913 * mc + 0.2309699292 * sc),
        _to_srgb(-1.2684380046 * lc + 2.6097574011 * mc - 0.3413193965 * sc),
        _to_srgb(-0.0040960863 * lc - 0.7034186147 * mc + 1.7076147010 * sc),
        alpha,
    )


@dataclass(frozen=True)
class Grade:
    """Regrade one colour in OKLab: L' = l_add + l_mul * L (a negative l_mul inverts, for dark
    mode), chroma scaled, then hue pulled toward `tint` by `tint_amount`. Alpha is kept."""

    l_mul: float = 1.0
    l_add: float = 0.0
    chroma: float = 1.0
    tint: str | None = None
    tint_amount: float = 0.0

    def apply(self, c: Rgba) -> Rgba:
        L, a, b = to_oklab(c)
        L = _clamp(self.l_add + self.l_mul * L)
        a, b = a * self.chroma, b * self.chroma
        if self.tint and self.tint_amount:
            tint = parse_color(self.tint)
            if tint is None:
                raise ValueError(f"bad tint {self.tint!r}")
            _, ta, tb = to_oklab(tint)
            a += (ta - a) * self.tint_amount
            b += (tb - b) * self.tint_amount
        return from_oklab((L, a, b), c[3])


# ------------------------------------------------------------------ presets

ROLES = ("ground", "green", "water", "road", "casing", "rail", "boundary", "building", "text", "halo", "keep")

# Building colour of the mobile 3D style (streets-v1-3d-qa, layer building-3d). The published
# streets-v1 has no extrusion layer: the app extrudes the PMTiles from this studio itself.
MOBILE_BUILDING = {"color": "#cfcec9", "opacity": 0.48}

_ROSE = "#e8b0c4"
_AMBER = "#f0a074"
_NAVY = "#1f2a44"


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    origin: str  # "mobile": as published; "suggested": not in the mobile style yet
    dark: bool
    note: str
    grades: dict[str, Grade] = field(default_factory=dict)
    light: dict[str, Any] | None = None  # None = keep the style's own light
    building_opacity: float = MOBILE_BUILDING["opacity"]

    def grade(self, role: str) -> Grade | None:
        return None if role == "keep" else self.grades.get(role)

    def building(self) -> dict[str, Any]:
        color = MOBILE_BUILDING["color"]
        grade = self.grade("building")
        if grade:
            color = format_color(grade.apply(parse_color(color)))  # type: ignore[arg-type]
        return {"color": color, "opacity": self.building_opacity}


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            id="dawn",
            label="Bình minh",
            origin="suggested",
            dark=False,
            note="Nắng sớm hồng nhạt, mặt trời thấp ở phía đông.",
            grades={
                "ground": Grade(l_mul=0.97, tint=_ROSE, tint_amount=0.18),
                "green": Grade(l_mul=0.95, chroma=0.85, tint=_ROSE, tint_amount=0.1),
                "water": Grade(l_mul=0.95, tint="#98a9e6", tint_amount=0.35),
                "road": Grade(l_mul=0.99, tint=_ROSE, tint_amount=0.15),
                "casing": Grade(l_mul=0.97, tint=_ROSE, tint_amount=0.15),
                "rail": Grade(l_mul=0.97, tint=_ROSE, tint_amount=0.1),
                "boundary": Grade(tint=_ROSE, tint_amount=0.1),
                "building": Grade(tint=_ROSE, tint_amount=0.15),
                "text": Grade(tint="#5d4370", tint_amount=0.12),
                "halo": Grade(tint=_ROSE, tint_amount=0.2),
            },
            light={"anchor": "map", "color": "#ffe0cc", "intensity": 0.3, "position": [1.15, 90, 50]},
        ),
        Preset(
            id="day",
            label="Ban ngày",
            origin="mobile",
            dark=False,
            note="Nguyên bản style streets-v1 app Mobile đang dùng.",
        ),
        Preset(
            id="dusk",
            label="Hoàng hôn",
            origin="suggested",
            dark=False,
            note="Nắng chiều cam ấm, mặt trời thấp ở phía tây, nước ngả tím.",
            grades={
                "ground": Grade(l_mul=0.9, tint=_AMBER, tint_amount=0.24),
                "green": Grade(l_mul=0.88, chroma=0.8, tint=_AMBER, tint_amount=0.15),
                "water": Grade(l_mul=0.85, tint="#7d7fc8", tint_amount=0.4),
                "road": Grade(l_mul=0.95, tint="#f5c39e", tint_amount=0.3),
                "casing": Grade(l_mul=0.9, tint=_AMBER, tint_amount=0.2),
                "rail": Grade(l_mul=0.9, tint=_AMBER, tint_amount=0.15),
                "boundary": Grade(l_mul=0.95, tint=_AMBER, tint_amount=0.1),
                "building": Grade(l_mul=0.95, tint=_AMBER, tint_amount=0.22),
                "text": Grade(l_mul=0.95, tint="#5a2f2a", tint_amount=0.12),
                "halo": Grade(l_mul=0.99, tint=_AMBER, tint_amount=0.25),
            },
            light={"anchor": "map", "color": "#ffc796", "intensity": 0.4, "position": [1.15, 270, 55]},
            building_opacity=0.52,
        ),
        Preset(
            id="night",
            label="Ban đêm",
            origin="suggested",
            dark=True,
            note="Nền xanh than; đường lớn sáng hơn đường nhỏ; chữ sáng trên viền tối.",
            grades={
                # Ground collapses to one dark navy; roads invert so the motorway (darkest
                # grey by day) becomes the brightest line; labels invert, halos go dark.
                "ground": Grade(l_mul=0.1, l_add=0.142, chroma=0.4, tint=_NAVY, tint_amount=0.35),
                "green": Grade(l_mul=0.32, chroma=0.5, tint=_NAVY, tint_amount=0.2),
                "water": Grade(l_mul=0.3, chroma=0.35, tint="#223f6b", tint_amount=0.6),
                "road": Grade(l_mul=-0.45, l_add=0.81, chroma=0.6, tint=_NAVY, tint_amount=0.2),
                "casing": Grade(l_mul=0.2, l_add=0.075, chroma=0.6, tint=_NAVY, tint_amount=0.3),
                "rail": Grade(l_mul=-0.45, l_add=0.81, chroma=0.5),
                "boundary": Grade(l_mul=-0.75, l_add=0.92, chroma=0.6),
                "building": Grade(l_mul=0.58, chroma=0.6, tint=_NAVY, tint_amount=0.4),
                "text": Grade(l_mul=-0.75, l_add=1.08, chroma=0.9),
                "halo": Grade(l_mul=0.0, l_add=0.2, chroma=0.0, tint=_NAVY, tint_amount=0.6),
            },
            light={"anchor": "map", "color": "#c8d4f5", "intensity": 0.25, "position": [1.15, 210, 30]},
            building_opacity=0.75,
        ),
    )
}
DEFAULT_PRESET = "day"


def layer_role(layer_id: str, prop: str) -> str:
    """Which grade a paint colour gets. Name-based, so new layers of the same kind follow."""
    lid = layer_id.lower()
    if lid.startswith("road_shield") or prop.startswith("icon-"):
        return "keep"  # text drawn on sprite shields, whose colours a style cannot change
    if prop == "text-color":
        return "text"
    if prop == "text-halo-color":
        return "halo"
    if any(k in lid for k in ("water", "sea", "reef", "river", "ocean")):
        return "water"
    if "rail" in lid:
        return "rail"
    if lid.endswith("_casing"):
        return "casing"
    if lid.startswith(("road_", "tunnel_", "bridge_", "aeroway_runway", "aeroway_taxiway")):
        return "road"
    if "building" in lid:
        return "building"
    if any(k in lid for k in ("park", "wood", "grass", "pitch", "track", "cemetery", "wetland", "forest", "scrub")):
        return "green"
    if any(k in lid for k in ("boundary", "admin", "outline")):
        return "boundary"
    return "ground"


def _recolor(value: Any, grade: Grade) -> Any:
    if isinstance(value, str):
        c = parse_color(value)
        return format_color(grade.apply(c)) if c else value
    if isinstance(value, list):
        return [_recolor(v, grade) for v in value]
    if isinstance(value, dict):
        return {k: _recolor(v, grade) for k, v in value.items()}
    return value


def apply_preset(base: dict[str, Any], preset_id: str, asset_base: str | None = None) -> dict[str, Any]:
    """New style dict for a preset. `base` is not modified (sources are shared, not copied)."""
    preset = PRESETS[preset_id]
    layers = []
    for layer in base.get("layers", []):
        paint = layer.get("paint")
        if paint:
            new_paint = {}
            for prop, value in paint.items():
                grade = preset.grade(layer_role(layer["id"], prop)) if prop.endswith("-color") else None
                new_paint[prop] = _recolor(value, grade) if grade else value
            layer = {**layer, "paint": new_paint}
        layers.append(layer)
    style = {**base, "layers": layers}
    style["name"] = f"{base.get('name', 'VMAP')} — {preset.label}"
    if preset.light:
        style["light"] = dict(preset.light)
    style["metadata"] = {
        **(base.get("metadata") or {}),
        "vmap:light-preset": preset.id,
        "vmap:light-preset-origin": preset.origin,
        # The published style has no 3D buildings; the app adds its own extrusion layer.
        "vmap:building-3d": preset.building(),
    }
    if asset_base:
        style.update(_rewrite_assets(style, asset_base))
    return style


def _rewrite_assets(style: dict[str, Any], asset_base: str) -> dict[str, Any]:
    def local(url: Any) -> Any:
        return asset_base + url[len(UPSTREAM):] if isinstance(url, str) and url.startswith(UPSTREAM) else url

    out: dict[str, Any] = {}
    if "glyphs" in style:
        out["glyphs"] = local(style["glyphs"])
    sprite = style.get("sprite")
    if isinstance(sprite, str):
        out["sprite"] = local(sprite)
    elif isinstance(sprite, list):
        out["sprite"] = [{**s, "url": local(s.get("url"))} for s in sprite]
    return out


def presets_info() -> dict[str, Any]:
    return {
        "default": DEFAULT_PRESET,
        "upstream": UPSTREAM + STYLE_PATH,
        "presets": [
            {
                "id": p.id,
                "label": p.label,
                "origin": p.origin,
                "dark": p.dark,
                "note": p.note,
                "building": p.building(),
                "light": p.light,
            }
            for p in PRESETS.values()
        ],
    }


# ------------------------------------------------------------------ upstream fetch + cache

_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_built: dict[tuple[str, str, str | None], bytes] = {}
_parsed: dict[str, dict[str, Any]] = {}
_offline_until = 0.0


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    # python.org builds on macOS have no CA bundle until "Install Certificates" is run;
    # the system bundle covers that (harmless where the default store already works).
    if os.path.exists("/etc/ssl/cert.pem"):
        try:
            ctx.load_verify_locations("/etc/ssl/cert.pem")
        except ssl.SSLError:
            pass
    return ctx


_ssl = _ssl_context()


def _fetch(path: str, ttl: float) -> tuple[bytes, str]:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(path)
        if hit and now - hit[0] < ttl:
            _cache.move_to_end(path)
            return hit[1], hit[2]
    url = UPSTREAM + urllib.parse.quote(path, safe="/@,")
    req = urllib.request.Request(url, headers={"User-Agent": "building-data-studio"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_ssl) as resp:  # noqa: S310 — fixed https host
            body = resp.read()
            ctype = resp.headers.get_content_type() or "application/octet-stream"
    except urllib.error.HTTPError as exc:
        raise UpstreamError(f"{url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpstreamError(f"{url}: {getattr(exc, 'reason', exc)}") from exc
    with _lock:
        _cache[path] = (now, body, ctype)
        _cache.move_to_end(path)
        while len(_cache) > CACHE_ENTRIES:
            _cache.popitem(last=False)
    return body, ctype


def fetch_asset(path: str) -> tuple[bytes, str]:
    if not _ASSET_RE.match(path):
        raise AssetNotAllowed(path)
    return _fetch(path, ASSET_TTL)


def base_style() -> tuple[str, dict[str, Any], str]:
    """(digest, style, source): the live mobile style, or the repo copy when offline."""
    global _offline_until
    raw = None
    if time.monotonic() >= _offline_until:
        try:
            raw, source = _fetch(STYLE_PATH, STYLE_TTL)[0], "live"
        except UpstreamError:
            # Do not wait for the timeout again on every preset switch.
            _offline_until = time.monotonic() + OFFLINE_RETRY
    if raw is None:
        raw, source = LOCAL_COPY.read_bytes(), "repo"
    digest = hashlib.sha1(raw).hexdigest()
    with _lock:
        style = _parsed.get(digest)
    if style is None:
        style = json.loads(raw)
        with _lock:
            _parsed.clear()
            _parsed[digest] = style
    return digest, style, source


def style_bytes(preset_id: str, asset_base: str | None) -> tuple[bytes, str]:
    digest, base, source = base_style()
    key = (digest, preset_id, asset_base)
    with _lock:
        body = _built.get(key)
    if body is None:
        body = json.dumps(apply_preset(base, preset_id, asset_base), ensure_ascii=False, separators=(",", ":")).encode()
        with _lock:
            for k in [k for k in _built if k[0] != digest]:
                del _built[k]
            _built[key] = body
    return body, source
