"""What a .glb holds, read without a 3D library: enough to list a landmark version.

Mesh and triangle counts, the bounding box, and the compression extensions the 3D preview
cannot decode (three.js there runs without Draco / meshopt / KTX2 decoders). The box is the one
three.js computes for the preview (Box3.setFromObject): each primitive's POSITION min / max
(dequantised when normalised), its 8 corners through the node's world matrix, and the
axis-aligned box of the lot. glTF units are metres, +Y up.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any

MAGIC = b"glTF"
JSON_CHUNK = 0x4E4F534A
TRIANGLES, TRIANGLE_STRIP, TRIANGLE_FAN = 4, 5, 6
# Extensions whose data the preview cannot decode: the model would not show.
UNREADABLE = {
    "KHR_draco_mesh_compression": "Draco",
    "EXT_meshopt_compression": "meshopt",
    "KHR_meshopt_compression": "meshopt",
    "KHR_texture_basisu": "KTX2 / Basis",
}
# Largest value of a normalised integer component type (glTF accessor.componentType).
NORMALIZED_MAX = {5120: 127, 5121: 255, 5122: 32767, 5123: 65535}
IDENTITY = [1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 1.0]


class GlbError(ValueError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    """The glTF JSON of a .glb (its first chunk)."""
    with path.open("rb") as fh:
        header = fh.read(12)
        if len(header) < 12 or header[:4] != MAGIC:
            raise GlbError("Không phải file GLB (thiếu chữ ký glTF ở đầu file).")
        version = struct.unpack("<I", header[4:8])[0]
        if version != 2:
            raise GlbError(f"GLB phiên bản {version}: chỉ đọc được glTF 2.0.")
        chunk = fh.read(8)
        if len(chunk) < 8:
            raise GlbError("File GLB bị cắt cụt.")
        size, kind = struct.unpack("<II", chunk)
        if kind != JSON_CHUNK:
            raise GlbError("Chunk đầu tiên của GLB không phải JSON.")
        data = fh.read(size)
        if len(data) < size:
            raise GlbError("File GLB bị cắt cụt.")
    try:
        gltf = json.loads(data)
    except ValueError as exc:
        raise GlbError(f"Không đọc được JSON trong GLB: {exc}") from None
    if not isinstance(gltf, dict):
        raise GlbError("JSON trong GLB không phải một object glTF.")
    return gltf


def node_matrix(node: dict[str, Any]) -> list[float]:
    """Local matrix of a node, column-major like glTF (and three.js Matrix4.compose)."""
    if "matrix" in node:
        return [float(v) for v in node["matrix"]]
    tx, ty, tz = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    sx, sy, sz = node.get("scale", [1, 1, 1])
    xx, yy, zz = x * x * 2, y * y * 2, z * z * 2
    xy, xz, yz = x * y * 2, x * z * 2, y * z * 2
    wx, wy, wz = w * x * 2, w * y * 2, w * z * 2
    return [
        (1 - (yy + zz)) * sx, (xy + wz) * sx, (xz - wy) * sx, 0,
        (xy - wz) * sy, (1 - (xx + zz)) * sy, (yz + wx) * sy, 0,
        (xz + wy) * sz, (yz - wx) * sz, (1 - (xx + yy)) * sz, 0,
        tx, ty, tz, 1,
    ]


def multiply(a: list[float], b: list[float]) -> list[float]:
    """a · b, both column-major."""
    return [sum(a[k * 4 + r] * b[c * 4 + k] for k in range(4)) for c in range(4) for r in range(4)]


def transform(m: list[float], p: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = p
    return (m[0] * x + m[4] * y + m[8] * z + m[12],
            m[1] * x + m[5] * y + m[9] * z + m[13],
            m[2] * x + m[6] * y + m[10] * z + m[14])


def _position_box(accessor: dict[str, Any]) -> tuple[list[float], list[float]] | None:
    lo, hi = accessor.get("min"), accessor.get("max")
    if not (isinstance(lo, list) and isinstance(hi, list) and len(lo) >= 3 and len(hi) >= 3):
        return None
    lo, hi = [float(v) for v in lo[:3]], [float(v) for v in hi[:3]]
    if accessor.get("normalized"):
        top = NORMALIZED_MAX.get(accessor.get("componentType"))
        if top:
            lo, hi = [max(v / top, -1.0) for v in lo], [max(v / top, -1.0) for v in hi]
    return lo, hi


def _triangles(primitive: dict[str, Any], accessors: list[dict[str, Any]]) -> int:
    ref = primitive.get("indices", (primitive.get("attributes") or {}).get("POSITION"))
    if not isinstance(ref, int) or not 0 <= ref < len(accessors):
        return 0
    count = int(accessors[ref].get("count", 0))
    mode = primitive.get("mode", TRIANGLES)
    if mode == TRIANGLES:
        return count // 3
    if mode in (TRIANGLE_STRIP, TRIANGLE_FAN):
        return max(count - 2, 0)
    return 0  # points / lines


def inspect(path: Path) -> dict[str, Any]:
    """Counts, box and unreadable extensions of the default scene of a .glb."""
    gltf = read_json(path)
    nodes = gltf.get("nodes") or []
    meshes = gltf.get("meshes") or []
    accessors = gltf.get("accessors") or []
    scenes = gltf.get("scenes") or []
    if scenes:
        index = gltf.get("scene", 0)
        roots = (scenes[index] if isinstance(index, int) and 0 <= index < len(scenes) else scenes[0]).get("nodes") or []
    else:
        children = {c for n in nodes for c in n.get("children") or []}
        roots = [i for i in range(len(nodes)) if i not in children]

    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    stats = {"meshes": 0, "triangles": 0}
    seen: set[int] = set()

    def walk(i: int, parent: list[float]) -> None:
        if not isinstance(i, int) or not 0 <= i < len(nodes) or i in seen:
            return  # a broken or cyclic node graph: skip, do not loop
        seen.add(i)
        node = nodes[i]
        world = multiply(parent, node_matrix(node))
        mesh = node.get("mesh")
        if isinstance(mesh, int) and 0 <= mesh < len(meshes):
            for prim in meshes[mesh].get("primitives") or []:
                stats["meshes"] += 1
                stats["triangles"] += _triangles(prim, accessors)
                pos = (prim.get("attributes") or {}).get("POSITION")
                box = _position_box(accessors[pos]) if isinstance(pos, int) and 0 <= pos < len(accessors) else None
                if not box:
                    continue
                for corner in ((x, y, z) for x in (box[0][0], box[1][0]) for y in (box[0][1], box[1][1]) for z in (box[0][2], box[1][2])):
                    for axis, v in enumerate(transform(world, corner)):
                        lo[axis] = min(lo[axis], v)
                        hi[axis] = max(hi[axis], v)
        for child in node.get("children") or []:
            walk(child, world)
        seen.discard(i)

    for root in roots:
        walk(root, IDENTITY)
    used = set(gltf.get("extensionsUsed") or []) | set(gltf.get("extensionsRequired") or [])
    bounds = [[round(v, 4) + 0.0 for v in lo], [round(v, 4) + 0.0 for v in hi]] if lo[0] <= hi[0] else None  # + 0.0: no -0.0
    return {
        "meshes": stats["meshes"],
        "triangles": stats["triangles"],
        "materials": len(gltf.get("materials") or []),
        "textures": len(gltf.get("images") or []),
        "bounds": bounds,  # [[min x, y, z], [max x, y, z]] in model metres, +Y up
        "unreadable": sorted({label for ext, label in UNREADABLE.items() if ext in used}),
        "generator": str((gltf.get("asset") or {}).get("generator") or ""),
    }
