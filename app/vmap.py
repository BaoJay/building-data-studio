"""VMAP basemap (the styles the mobile app uses) for the 3D preview, one per light preset.

assets.vmaps.vn sends no CORS headers, so the browser cannot load the style, sprite or
glyphs from another origin. The server fetches them instead (style: live, falling back to
the copy in map-styles/), points sprite / glyphs back at itself, and serves them same-origin.
The vector tiles (tasco-maps.dnpwater.vn) do allow CORS and are loaded directly.

Presets: VMAP publishes one streets-v2 style per light preset (listed under `styles` in
CATALOG), each with its own colours, sprite and root `light`. A preset here is that style as
published. Only the 3D building look is ours: no streets-v2 style has a fill-extrusion layer,
because the app extrudes the PMTiles from this studio itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import net

UPSTREAM = "https://assets.vmaps.vn/"
CATALOG = "https://api-app.vmaps.vn//map-proxy/v2/map/layers"  # where VMAP lists its styles
MAP_STYLES = Path(__file__).resolve().parent.parent / "map-styles"
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


class UpstreamUnreachable(UpstreamError):
    """No answer at all (offline, DNS, timeout), as opposed to an HTTP error status."""


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


# ------------------------------------------------------------------ 3D building look

# The 3D building look of the mobile app: one base colour for every preset, shaded by the
# style's own `light`. INFERRED, NOT PUBLISHED: no VMAP style has a fill-extrusion layer, so
# the value was measured from Tasco Map admin screenshots (2026-10-16). It replaces #cfcec9 /
# 0.48 of the old streets-v1-3d-qa. When Mobile confirms the real value, change it here, or
# for one run set BDS_VMAP_BUILDING_COLOR / BDS_VMAP_BUILDING_OPACITY.
INFERRED_BUILDING = {"color": "#ddcca9", "opacity": 0.85}
BUILDING_COLOR_ENV = "BDS_VMAP_BUILDING_COLOR"
BUILDING_OPACITY_ENV = "BDS_VMAP_BUILDING_OPACITY"


def building_look(env: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """{color, opacity, origin}: the inferred look, with any environment override applied
    (origin "env"). A bad value raises ValueError rather than silently drawing another colour."""
    color_text = env.get(BUILDING_COLOR_ENV, "").strip()
    opacity_text = env.get(BUILDING_OPACITY_ENV, "").strip()
    color = parse_color(color_text or INFERRED_BUILDING["color"])
    if color is None:
        raise ValueError(f"{BUILDING_COLOR_ENV}={color_text!r} is not a colour (e.g. #ddcca9)")
    try:
        opacity = float(opacity_text) if opacity_text else INFERRED_BUILDING["opacity"]
    except ValueError:
        opacity = -1.0
    if not 0.0 <= opacity <= 1.0:
        raise ValueError(f"{BUILDING_OPACITY_ENV}={opacity_text!r} must be a number from 0 to 1")
    return {
        "color": format_color(color),
        "opacity": opacity,
        "origin": "env" if color_text or opacity_text else "inferred",
    }


MOBILE_BUILDING = building_look()  # read once, so a bad override stops the server at start


# ------------------------------------------------------------------ presets


@dataclass(frozen=True)
class Preset:
    id: str
    label: str
    style: str  # VMAP style id under styles/, as listed in CATALOG
    dark: bool  # dark ground: the preview uses its dark colour set on it
    note: str
    # Precomputed ground shadows (app/shadows.py), cast along the style's own `light`. The
    # look is ours: VMAP publishes no shadows. None: no sun, no shadows.
    shadow: dict[str, Any] | None = None
    origin: str = "mobile"  # "mobile": a style VMAP publishes, used as is

    @property
    def style_path(self) -> str:
        return f"styles/{self.style}/style.json"

    @property
    def local_copy(self) -> Path:
        return MAP_STYLES / f"{self.style}.style.json"

    def building(self) -> dict[str, Any]:
        return dict(MOBILE_BUILDING)


PRESETS: dict[str, Preset] = {
    p.id: p
    for p in (
        Preset(
            id="dawn",
            label="Bình minh",
            style="streets-v2-dawn",
            dark=False,
            note="Nền kem đào, nắng vàng cam.",
            shadow={"color": "#4b3f6b", "opacity": 0.22},
        ),
        Preset(
            id="day",
            label="Ban ngày",
            style="streets-v2-day",
            dark=False,
            note="Nền sáng trung tính, nắng trắng.",
            shadow={"color": "#3a4150", "opacity": 0.24},
        ),
        Preset(
            id="dusk",
            label="Hoàng hôn",
            style="streets-v2-dusk",
            dark=True,
            note="Nền tím xám, nắng cam rất thấp nên bóng dài.",
            shadow={"color": "#2b1d2b", "opacity": 0.3},
        ),
        Preset(
            id="night",
            label="Ban đêm",
            style="streets-v2-night",
            dark=True,
            note="Nền xanh than, ánh trăng xanh xám.",
        ),
        Preset(
            id="dark",
            label="Nền tối",
            style="streets-v2-dark",
            dark=True,
            note="Biến thể giao diện tối của style, không gắn với giờ nào trong ngày.",
        ),
    )
}
DEFAULT_PRESET = "day"


def apply_preset(base: dict[str, Any], preset_id: str, asset_base: str | None = None) -> dict[str, Any]:
    """The preset's published style, with our 3D building look in its metadata and, for the
    preview, sprite / glyphs pointed at the local proxy. `base` is not modified."""
    preset = PRESETS[preset_id]
    style = {**base}
    style["metadata"] = {
        **(base.get("metadata") or {}),
        "vmap:light-preset": preset.id,
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
        "catalog": CATALOG,
        "presets": [
            {
                "id": p.id,
                "label": p.label,
                "origin": p.origin,
                "style": p.style,
                "url": UPSTREAM + p.style_path,
                "dark": p.dark,
                "note": p.note,
                "building": p.building(),
                "shadow": p.shadow,
            }
            for p in PRESETS.values()
        ],
    }


# ------------------------------------------------------------------ upstream fetch + cache

_lock = threading.Lock()
_cache: OrderedDict[str, tuple[float, bytes, str]] = OrderedDict()
_built: dict[tuple[str, str, str | None], bytes] = {}
_parsed: OrderedDict[str, dict[str, Any]] = OrderedDict()
_offline_until = 0.0


_ssl = net.ssl_context()


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
        raise UpstreamUnreachable(f"{url}: {getattr(exc, 'reason', exc)}") from exc
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


def base_style(preset_id: str) -> tuple[str, dict[str, Any], str]:
    """(digest, style, source): the preset's live VMAP style, or its repo copy when offline."""
    global _offline_until
    preset = PRESETS[preset_id]
    raw = None
    if time.monotonic() >= _offline_until:
        try:
            raw, source = _fetch(preset.style_path, STYLE_TTL)[0], "live"
        except UpstreamUnreachable:
            # Do not wait for the timeout again on every preset switch.
            _offline_until = time.monotonic() + OFFLINE_RETRY
        except UpstreamError:
            pass  # only this style is missing upstream: its repo copy, the others stay live
    if raw is None:
        raw, source = preset.local_copy.read_bytes(), "repo"
    digest = hashlib.sha1(raw).hexdigest()
    with _lock:
        style = _parsed.get(digest)
    if style is None:
        style = json.loads(raw)
        with _lock:
            _parsed[digest] = style
            while len(_parsed) > 2 * len(PRESETS):  # one per preset, plus a superseded copy each
                _parsed.popitem(last=False)
    return digest, style, source


def effective_light(preset_id: str) -> dict[str, Any] | None:
    """The light a preset renders with: the root `light` of its own VMAP style."""
    return base_style(preset_id)[1].get("light")


def style_bytes(preset_id: str, asset_base: str | None) -> tuple[bytes, str]:
    digest, base, source = base_style(preset_id)
    key = (digest, preset_id, asset_base)
    with _lock:
        body = _built.get(key)
    if body is None:
        body = json.dumps(apply_preset(base, preset_id, asset_base), ensure_ascii=False, separators=(",", ":")).encode()
        with _lock:
            for k in [k for k in _built if k[1] == preset_id and k[0] != digest]:
                del _built[k]
            _built[key] = body
    return body, source
