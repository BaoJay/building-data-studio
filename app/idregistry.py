"""Stable building_id of the canonical buildings, kept across builds.

A canonical building is an entity, its footprint only an observation of it, so its id must not
follow one polygon. The registry maps every source record that has been part of a building
(`osm:way/123`, `gob:7PH72Q6M+H2CX`, `gba_ms:…`) to the building_id it was given:

    canonical/<area>/id_registry.parquet      key, building_id, build_id

A new build looks up, for each canonical building, the key of its winning footprint first, then
the keys of its evidence (a house mapped on OSM where a Google footprint used to stand keeps the
id the Google footprint had). The first id not already taken in this build wins; with none, a new
UUIDv7 (time-ordered, like the platform's ids). Ids are never handed to another building: once the
build succeeds, every key of every building points to that building's id.

The registry can be seeded once from a canonical file already in production (e.g.
canonical_building_v7_<date>.parquet): `source_id` way/… or a plus code -> its building_id, so the
buildings on the app keep their ids.
"""

from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .sqlbuild import sql_str

FILE_NAME = "id_registry.parquet"
OSM_ID_RE = re.compile(r"^(way|relation|node)/\d+$")
PLUS_CODE_RE = re.compile(r"^[23456789CFGHJMPQRVWX]{8}\+[23456789CFGHJMPQRVWX]{2,}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def uuid7() -> str:
    """A UUIDv7: 48-bit Unix time in ms, then random bits (RFC 9562)."""
    ms = time.time_ns() // 1_000_000
    rand = int.from_bytes(secrets.token_bytes(10), "big")
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= ((rand >> 62) & 0xFFF) << 64  # rand_a, 12 bits
    value |= 0b10 << 62  # variant
    value |= rand & ((1 << 62) - 1)  # rand_b
    return str(uuid.UUID(int=value))


def source_key(source_id: str | None) -> str | None:
    """The registry key of a source_id of a canonical file: way/… -> osm:way/…, a plus code -> gob:…"""
    sid = (source_id or "").strip()
    if OSM_ID_RE.match(sid):
        return f"osm:{sid}"
    if PLUS_CODE_RE.match(sid.upper()):
        return f"gob:{sid.upper()}"
    return None


class Registry:
    """key -> building_id, loaded in memory for one build."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.ids: dict[str, str] = {}

    @classmethod
    def load(cls, con: Any, path: Path) -> Registry:
        reg = cls(path)
        if path.is_file():
            rows = con.execute(f"SELECT key, building_id FROM read_parquet({sql_str(str(path))})").fetchall()
            reg.ids = dict(rows)
        return reg

    def __len__(self) -> int:
        return len(self.ids)

    def seed(self, con: Any, canonical_file: Path) -> dict[str, int]:
        """Add the keys of a canonical file (source_id -> building_id) that the registry lacks."""
        src = f"read_parquet({sql_str(str(canonical_file))})"
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        if not {"building_id", "source_id"} <= cols:
            raise ValueError(f"{canonical_file.name} không có cột building_id và source_id.")
        where = "WHERE coalesce(status, 'active') = 'active'" if "status" in cols else ""
        rows = con.execute(f"SELECT source_id, building_id FROM {src} {where}").fetchall()
        added = skipped = 0
        for sid, bid in rows:
            key = source_key(sid)
            if key is None or not bid:
                skipped += 1
                continue
            if key not in self.ids:
                self.ids[key] = str(bid)
                added += 1
        return {"rows": len(rows), "added": added, "skipped": skipped}

    def assign(self, items: Iterable[tuple[Any, str, list[str], str | None]]) -> tuple[dict[Any, str], dict[str, int]]:
        """building_id of each canonical building.

        items: (row key, winner key, evidence keys by decreasing overlap, locked id or None). Locked
        ids (landmarks) are given first, then the ids of winners already in the registry, then
        ids inherited from evidence, then new ones; within a pass, in the order given, which the
        caller sorts so a rebuild of the same input gives the same answer.
        """
        items = list(items)
        out: dict[Any, str] = {}
        used: set[str] = set()
        stats = {"kept": 0, "inherited": 0, "new": 0, "locked": 0}
        for row, _, _, locked in items:
            if locked and locked not in used:
                out[row] = locked
                used.add(locked)
                stats["locked"] += 1
        # A footprint that still exists keeps its id before any other building may inherit it.
        for row, winner, _, _ in items:
            bid = self.ids.get(winner)
            if row not in out and bid is not None and bid not in used:
                out[row] = bid
                used.add(bid)
                stats["kept"] += 1
        for row, _, evidence, _ in items:
            if row in out:
                continue
            bid = next((b for b in (self.ids.get(k) for k in evidence) if b is not None and b not in used), None)
            if bid is not None:
                stats["inherited"] += 1
            else:
                bid = uuid7()
                stats["new"] += 1
            out[row] = bid
            used.add(bid)
        return out, stats

    def update(self, members: Iterable[tuple[str, str]]) -> None:
        """Point every key (winner and evidence) to the id of the building it is now part of."""
        for key, bid in members:
            self.ids[key] = bid

    def save(self, con: Any, build_id: str) -> None:
        """Write the registry atomically (through a CSV next to it)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        csv_path = self.path.with_name(f".{self.path.stem}.csv.tmp")
        with csv_path.open("w", encoding="utf-8") as fh:
            fh.write("key,building_id\n")
            for key, bid in sorted(self.ids.items()):
                fh.write(f'"{key.replace(chr(34), chr(34) * 2)}",{bid}\n')
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        con.execute(f"COPY (SELECT key, building_id, {sql_str(build_id)} AS build_id "
                    f"FROM read_csv({sql_str(str(csv_path))}, header = true, all_varchar = true)) "
                    f"TO {sql_str(str(tmp))} (FORMAT PARQUET, COMPRESSION ZSTD)")
        os.replace(tmp, self.path)
        csv_path.unlink(missing_ok=True)
