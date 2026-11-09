"""/api/pick-file: only plain '.ext' suffixes reach the PowerShell dialog filter."""

import asyncio
import json

from app import server


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


def test_pick_file_drops_unsafe_suffixes(monkeypatch):
    seen = {}
    monkeypatch.setattr(server, "pick_file_dialog", lambda exts: seen.setdefault("exts", exts) and "/mnt/c/x.pbf")
    res = asyncio.run(server.pick_file(_Req({"accept": [".pbf", ".osm", "'; rm -rf /'", ".a|b", 3, ".parquet"]})))
    assert seen["exts"] == [".pbf", ".osm", ".parquet"]
    assert json.loads(res.body) == {"path": "/mnt/c/x.pbf"}
