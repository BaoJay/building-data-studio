"""OSM tags -> building attributes (pure functions, no I/O).

The convert pipeline turns `height` / `num_floors` into `height_m` (height, then levels × m_per_level,
then a default). What it needs from this module is clean numbers: metres in `height` and
`min_height`, a level count in `num_floors`, and a verdict on whether a feature is a building, a
`building:part`, or neither. Free-text OSM values ("12 m", "40'", "12'6\"", "10-12", "tall") are the
reason this is its own module.
"""

from __future__ import annotations

import re
from typing import Any

FEET = 0.3048
MAX_HEIGHT_M = 1000.0  # taller than any building: a typo (e.g. a value given in cm or mm)
MAX_LEVELS = 250

_NUM = r"(\d+(?:[.,]\d+)?)"
_METRES_RE = re.compile(rf"^{_NUM}\s*(?:m|meters?|metres?)?$", re.IGNORECASE)
_FEET_RE = re.compile(rf"^{_NUM}\s*(?:ft|feet|foot|')$", re.IGNORECASE)
_FEET_INCHES_RE = re.compile(rf"^(\d+)\s*'\s*{_NUM}\s*(?:\"|in|inch|inches)?$", re.IGNORECASE)
_NOT_BUILDING = {"no", "none", "false", "0"}
_NO_VALUE = {"yes", "true", "1"}


def _float(text: str) -> float:
    return float(text.replace(",", "."))


def parse_length(value: Any) -> float | None:
    """A length tag in metres, or None when it is empty, not a plain length, or not plausible.

    Accepts "12", "12.5", "12,5", "12 m", "12m", "40'", "40 ft", "12'6\"" (feet and inches). Rejects
    ranges ("10-12"), words, negatives and zero.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    metres: float | None = None
    if m := _METRES_RE.match(text):
        metres = _float(m.group(1))
    elif m := _FEET_RE.match(text):
        metres = _float(m.group(1)) * FEET
    elif m := _FEET_INCHES_RE.match(text):
        metres = (int(m.group(1)) * 12 + _float(m.group(2))) * FEET / 12
    if metres is None or not 0 < metres <= MAX_HEIGHT_M:
        return None
    return round(metres, 3)


def parse_levels(value: Any, *, minimum: float = 0.0) -> float | None:
    """A level count (building:levels, building:min_level). "3;4" keeps the larger count."""
    if value is None:
        return None
    counts = []
    for piece in str(value).split(";"):
        piece = piece.strip().replace(",", ".")
        try:
            counts.append(float(piece))
        except ValueError:
            return None
    if not counts:
        return None
    n = max(counts)
    if n != n or not minimum <= n <= MAX_LEVELS:  # NaN, negative, absurd
        return None
    return n


def is_part(tags: dict[str, str]) -> bool:
    """`building:part=*` (any value but "no") marks a 3D part, whether or not `building` is also set."""
    value = (tags.get("building:part") or "").strip().lower()
    return bool(value) and value not in _NOT_BUILDING


def is_building(tags: dict[str, str]) -> bool:
    value = (tags.get("building") or "").strip().lower()
    return bool(value) and value not in _NOT_BUILDING


def is_underground(tags: dict[str, str]) -> bool:
    """Nothing to extrude: `location=underground`, `tunnel=*`, or a negative `layer` with no height."""
    if (tags.get("location") or "").strip().lower() == "underground":
        return True
    if (tags.get("tunnel") or "").strip().lower() not in ("", "no"):
        return True
    return False


def kind(tags: dict[str, str]) -> str | None:
    """'part', 'building', or None for a feature that is neither (or is underground)."""
    if is_underground(tags):
        return None
    if is_part(tags):
        return "part"
    return "building" if is_building(tags) else None


def _text(tags: dict[str, str], key: str) -> str | None:
    value = (tags.get(key) or "").strip()
    return value or None


def building_attrs(tags: dict[str, str], m_per_level: float) -> dict[str, Any]:
    """The output columns of the base (osmbase.OUTPUT_COLUMNS) that come from the tags.

    `height` is the tagged height in metres (OSM's `height` already includes the roof); `num_floors`
    is `building:levels`. `min_height` falls back to `building:min_level × m_per_level`, the way the
    Overture base does. Class is the `building` value ("yes" carries no class); for a part it is the
    `building:part` value.
    """
    part = is_part(tags)
    value = _text(tags, "building:part" if part else "building")
    cls = None if value is None or value.lower() in _NO_VALUE else value
    min_level = parse_levels(tags.get("building:min_level"))
    min_height = parse_length(tags.get("min_height"))
    if min_height is None and min_level:
        min_height = round(min_level * m_per_level, 3)
    return {
        "name": _text(tags, "name") or _text(tags, "name:vi"),
        "class": cls,
        "subtype": None,
        "height": parse_length(tags.get("height")),
        "num_floors": parse_levels(tags.get("building:levels"), minimum=1),
        "min_height": min_height,
        "min_floor": min_level,
        "roof_shape": _text(tags, "roof:shape"),
        "roof_height": parse_length(tags.get("roof:height")),
        "roof_color": _text(tags, "roof:colour"),
        "roof_material": _text(tags, "roof:material"),
        "facade_color": _text(tags, "building:colour"),
        "facade_material": _text(tags, "building:material"),
    }


def osm_id(osm_type: str, number: int | str) -> str:
    """('way', 12) -> 'way/12': the id openstreetmap.org uses, same as the Overture base's building_id."""
    return f"{osm_type}/{number}"


def tag_issues(tags: dict[str, str]) -> list[tuple[str, str]]:
    """(key, raw value) of numeric tags present but unusable: the list a mapper can fix on OSM."""
    out = []
    for key in ("height", "min_height", "roof:height"):
        if tags.get(key, "").strip() and parse_length(tags[key]) is None:
            out.append((key, tags[key]))
    for key, floor in (("building:levels", 1.0), ("building:min_level", 0.0)):
        if tags.get(key, "").strip() and parse_levels(tags[key], minimum=floor) is None:
            out.append((key, tags[key]))
    return out
