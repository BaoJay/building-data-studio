"""H3 cells as GeoJSON for the /preview map: the grid of one resolution over a bbox, and the
cells of a pasted list."""

from __future__ import annotations

import math
import re
from typing import Any

import h3

from . import canonical

GRID_RES = 8
MAX_GRID_CELLS = 6000  # ~5,000 r8 cells cover a 30 x 30 km view; beyond that the map is too far out
MAX_LIST_CELLS = 20000


class H3Error(ValueError):
    pass


def _feature(cell: str) -> dict[str, Any]:
    ring = [[round(lng, 7), round(lat, 7)] for lat, lng in h3.cell_to_boundary(cell)]
    ring.append(ring[0])
    return {"type": "Feature", "properties": {"h3": cell, "res": h3.get_resolution(cell)},
            "geometry": {"type": "Polygon", "coordinates": [ring]}}


def _collection(cells) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": [_feature(c) for c in cells]}


def grid(bbox: str, res: int = GRID_RES) -> dict[str, Any]:
    """Cells of `res` whose centre falls in bbox 'west,south,east,north'."""
    try:
        w, s, e, n = (float(v) for v in bbox.split(","))
    except ValueError:
        raise H3Error("bbox phải là 'west,south,east,north'.") from None
    if not (-180 <= w < e <= 180 and -90 <= s < n <= 90):
        raise H3Error("bbox không hợp lệ.")
    # Estimate the count first so a world-wide bbox is refused before polygon_to_cells fills memory.
    km2 = (e - w) * (n - s) * 111.32 ** 2 * math.cos(math.radians((s + n) / 2))
    if km2 / h3.average_hexagon_area(res, "km^2") > MAX_GRID_CELLS * 1.5:
        raise H3Error("Khung nhìn quá rộng để vẽ lưới H3, hãy phóng to thêm.")
    shape = h3.LatLngPoly([(s, w), (s, e), (n, e), (n, w)])
    cells = h3.polygon_to_cells(shape, res)
    if len(cells) > MAX_GRID_CELLS:
        raise H3Error("Khung nhìn quá rộng để vẽ lưới H3, hãy phóng to thêm.")
    return _collection(sorted(cells))


def parse(text: str) -> tuple[list[str], list[str]]:
    """Valid cells (deduplicated, in pasted order) and the tokens that are not H3 cells."""
    cells, bad, seen = [], [], set()
    for tok in re.split(r"[\s,;\"'\[\]()]+", text or ""):
        tok = tok.strip().lower()
        if not tok or tok in seen:
            continue
        seen.add(tok)
        if re.fullmatch(r"[0-9a-f]{15}", tok) and h3.is_valid_cell(tok):
            cells.append(tok)
        else:
            bad.append(tok)
    if len(cells) > MAX_LIST_CELLS:
        raise H3Error(f"Tối đa {MAX_LIST_CELLS} ô một lần.")
    return cells, bad


def cells(text: str) -> dict[str, Any]:
    good, bad = parse(text)
    out = _collection(good)
    out["invalid"] = bad
    return out


def presets() -> list[dict[str, Any]]:
    return [{"key": k, "label": v["label"], "cells": list(v["cells"])} for k, v in canonical.H3_PRESETS.items()]
