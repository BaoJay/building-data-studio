"""OSM way geometries -> polygons (pure functions, no I/O).

Overpass (`out geom`) returns each way as a list of points and each multipolygon relation as ways
tagged with a role. Rings must be stitched from those ways, and inner rings assigned to the outer
ring that contains them, before a polygon exists.
"""

from __future__ import annotations

from typing import Iterable

Point = tuple[float, float]  # lon, lat
Ring = list[Point]  # closed: first == last
Polygon = list[Ring]  # outer ring, then holes

MIN_RING_POINTS = 4  # three corners + closing point


def ring_area(ring: Ring) -> float:
    """Signed shoelace area in square degrees (positive = counter-clockwise); only compared, never reported."""
    return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:])) / 2


def point_in_ring(pt: Point, ring: Ring) -> bool:
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def stitch(ways: Iterable[list[Point]]) -> tuple[list[Ring], int]:
    """Join open ways end to end into closed rings. Returns (rings, number of ways left unclosed)."""
    pending = [list(w) for w in ways if len(w) >= 2]
    rings: list[Ring] = []
    while pending:
        chain = pending.pop(0)
        while chain[0] != chain[-1]:
            for i, w in enumerate(pending):
                if w[0] == chain[-1]:
                    chain += w[1:]
                elif w[-1] == chain[-1]:
                    chain += w[-2::-1]
                elif w[-1] == chain[0]:
                    chain = w + chain[1:]
                elif w[0] == chain[0]:
                    chain = w[:0:-1] + chain
                else:
                    continue
                del pending[i]
                break
            else:
                return rings, 1 + len(pending)  # a dangling chain: the relation is incomplete
        rings.append(chain)
    return rings, 0


def polygons(outers: Iterable[list[Point]], inners: Iterable[list[Point]] = ()) -> tuple[list[Polygon], int]:
    """Polygons from outer and inner member ways. Returns (polygons, number of ways that did not close).

    Rings shorter than a triangle and zero-area rings are dropped. A hole goes to the smallest outer
    ring that contains one of its points; a hole inside no outer ring is dropped.
    """
    outer_rings, bad_o = stitch(outers)
    inner_rings, bad_i = stitch(inners)
    result: list[Polygon] = []
    for ring in outer_rings:
        if len(ring) >= MIN_RING_POINTS and ring_area(ring) != 0:
            result.append([ring])
    for hole in inner_rings:
        if len(hole) < MIN_RING_POINTS or ring_area(hole) == 0:
            continue
        host = None
        for poly in result:
            if point_in_ring(hole[0], poly[0]) and (host is None or abs(ring_area(poly[0])) < abs(ring_area(host[0]))):
                host = poly
        if host is not None:
            host.append(hole)
    return result, bad_o + bad_i


def closed_way(points: list[Point]) -> list[Polygon]:
    """A single closed way as polygons ([] when it is open, too short, or has no area)."""
    if len(points) < MIN_RING_POINTS or points[0] != points[-1] or ring_area(points) == 0:
        return []
    return [[list(points)]]


def to_wkt(polys: list[Polygon]) -> str:
    """MULTIPOLYGON (or POLYGON for one) in WKT; outer rings counter-clockwise, holes clockwise."""
    def fmt(ring: Ring, ccw: bool) -> str:
        if (ring_area(ring) > 0) != ccw:
            ring = ring[::-1]
        return "(" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in ring) + ")"

    parts = ["(" + ", ".join([fmt(poly[0], True), *(fmt(h, False) for h in poly[1:])]) + ")" for poly in polys]
    return f"POLYGON {parts[0]}" if len(parts) == 1 else f"MULTIPOLYGON ({', '.join(parts)})"


def bbox(polys: list[Polygon]) -> tuple[float, float, float, float]:
    xs = [x for poly in polys for x, _ in poly[0]]
    ys = [y for poly in polys for _, y in poly[0]]
    return min(xs), min(ys), max(xs), max(ys)


def grid(box: tuple[float, float, float, float], step: float) -> list[tuple[float, float, float, float]]:
    """Cells of `step` degrees covering box (w, s, e, n), west to east, south to north."""
    w, s, e, n = box
    cells = []
    y = s
    while y < n:
        x = w
        while x < e:
            cells.append((round(x, 7), round(y, 7), round(min(x + step, e), 7), round(min(y + step, n), 7)))
            x += step
        y += step
    return cells


def box_touches(polys: list[Polygon], box: tuple[float, float, float, float]) -> bool:
    """Whether box may overlap the polygons: a box corner or centre lies inside one, or one of its
    outline vertices lies in the box. (A boundary edge crossing a box with no vertex inside needs a
    box smaller than the edge; administrative boundaries have a vertex every few hundred metres.)"""
    w, s, e, n = box
    probes = [(w, s), (e, s), (e, n), (w, n), ((w + e) / 2, (s + n) / 2)]
    for poly in polys:
        if any(point_in_ring(p, poly[0]) for p in probes):
            return True
        if any(w <= x <= e and s <= y <= n for x, y in poly[0]):
            return True
    return False


def split(box: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """A box in four quarters."""
    w, s, e, n = box
    mx, my = (w + e) / 2, (s + n) / 2
    return [(w, s, mx, my), (mx, s, e, my), (w, my, mx, n), (mx, my, e, n)]
