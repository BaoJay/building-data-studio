"""S2 cell tokens of a point, without the s2 library (pure functions, no I/O).

Google Open Buildings v3 is published one file per S2 cell (level 4 or level 6). To fetch the
buildings of an area, the app needs the tokens of the cells that cover it: the cell of each point
of a grid over the area. Only the lat/lng -> cell id path of S2 is needed, which is short:

    lat/lng -> unit vector -> cube face + (u, v) -> (s, t) (quadratic) -> (i, j) on a 2^30 grid
            -> position on the face's Hilbert curve -> 64-bit cell id -> parent at a level -> token

Reference: the S2 geometry library (s2cell_id.cc, s2coords.h), same constants.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

MAX_LEVEL = 30
_MAX_SIZE = 1 << MAX_LEVEL
_SWAP, _INVERT = 1, 2
# Hilbert curve tables: (orientation, ij of a sub-square) -> its position, and how the
# orientation changes going into the sub-square at a position.
_IJ_TO_POS = ((0, 1, 3, 2), (0, 3, 1, 2), (2, 3, 1, 0), (2, 1, 3, 0))
_POS_TO_ORIENTATION = (_SWAP, 0, 0, _INVERT | _SWAP)


def _face_uv(x: float, y: float, z: float) -> tuple[int, float, float]:
    ax, ay, az = abs(x), abs(y), abs(z)
    if ax >= ay and ax >= az:
        face = 0 if x > 0 else 3
    elif ay >= az:
        face = 1 if y > 0 else 4
    else:
        face = 2 if z > 0 else 5
    if face == 0:
        return face, y / x, z / x
    if face == 1:
        return face, -x / y, z / y
    if face == 2:
        return face, -x / z, -y / z
    if face == 3:
        return face, z / x, y / x
    if face == 4:
        return face, z / y, -x / y
    return face, -y / z, -x / z


def _st(u: float) -> float:
    """The quadratic projection S2 uses by default (kQuadraticProjection)."""
    return 0.5 * math.sqrt(1 + 3 * u) if u >= 0 else 1 - 0.5 * math.sqrt(1 - 3 * u)


def _ij(s: float) -> int:
    return max(0, min(_MAX_SIZE - 1, int(math.floor(_MAX_SIZE * s))))


def leaf_id(lat: float, lon: float) -> int:
    """The id of the level-30 cell holding a point."""
    phi, theta = math.radians(lat), math.radians(lon)
    x, y, z = math.cos(phi) * math.cos(theta), math.cos(phi) * math.sin(theta), math.sin(phi)
    face, u, v = _face_uv(x, y, z)
    i, j = _ij(_st(u)), _ij(_st(v))
    orientation = face & _SWAP
    pos = 0
    for k in range(MAX_LEVEL - 1, -1, -1):
        sub = (((i >> k) & 1) << 1) | ((j >> k) & 1)
        p = _IJ_TO_POS[orientation][sub]
        pos = (pos << 2) | p
        orientation ^= _POS_TO_ORIENTATION[p]
    return (face << 61) | (pos << 1) | 1


def parent(cell_id: int, level: int) -> int:
    if not 0 <= level <= MAX_LEVEL:
        raise ValueError(f"S2 level {level} ngoài khoảng 0–30.")
    lsb = 1 << (2 * (MAX_LEVEL - level))
    return (cell_id & -lsb) | lsb


def token(cell_id: int) -> str:
    """'313' for the level-4 cell over most of northern Vietnam: hex without trailing zeros."""
    return f"{cell_id:016x}".rstrip("0") or "X"


def cell_token(lat: float, lon: float, level: int) -> str:
    return token(parent(leaf_id(lat, lon), level))


def covering(bbox: tuple[float, float, float, float], level: int, step: float = 0.02) -> list[str]:
    """Tokens of the cells (at a level) holding a point of a grid over bbox (w, s, e, n).

    Level-6 cells are more than 50 km across, so a grid step of a few kilometres plus the box
    edges reaches every cell that meets the box.
    """
    w, s, e, n = bbox
    if not (w < e and s < n):
        raise ValueError(f"Khung không hợp lệ: {bbox}")
    nx, ny = max(1, math.ceil((e - w) / step)), max(1, math.ceil((n - s) / step))
    points: Iterable[tuple[float, float]] = (
        (s + (n - s) * iy / ny, w + (e - w) * ix / nx) for iy in range(ny + 1) for ix in range(nx + 1)
    )
    return sorted({cell_token(lat, lon, level) for lat, lon in points})
