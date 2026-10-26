"""The 3D preview's landmark loading and map filters (app/static/landmark-store.js), tested under
Node by tests/js/*.test.mjs (node --test): many models, one request per GLB, three requests at a
time, a cache of 24 models, a source switch while loading, and the extrusion kept when a GLB fails."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js")
def test_node_suite():
    tests = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "tests" / "js").glob("*.test.mjs"))
    assert tests
    proc = subprocess.run(["node", "--test", *tests], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
