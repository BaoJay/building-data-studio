import struct

from app.report import gpkg_envelope, histogram, quantile, render_markdown


def test_quantile_nearest_rank() -> None:
    pairs = [(4.0, 95), (20.0, 4), (300.0, 1)]
    assert quantile(pairs, 0.5) == 4.0
    assert quantile(pairs, 0.99) == 20.0
    assert quantile(pairs, 1.0) == 300.0
    assert quantile([], 0.5) is None


def test_histogram_bins_and_open_last_bin() -> None:
    bins = histogram([(-1.0, 1), (4.0, 2), (250.0, 3)], (0, 3, 6, 200), total=6)
    assert [b["count"] for b in bins] == [1, 2, 0, 3]
    assert bins[-1]["label"] == "≥ 200 m"
    assert bins[1]["label"] == "3–6 m"


def test_gpkg_envelope_from_header() -> None:
    header = b"GP" + bytes([0, 0b0000_0011]) + struct.pack("<i", 4326)
    blob = header + struct.pack("<4d", 105.0, 106.0, 10.0, 11.0) + b"\x01"
    assert gpkg_envelope(blob) == [105.0, 10.0, 106.0, 11.0]


def test_gpkg_envelope_point_without_envelope() -> None:
    header = b"GP" + bytes([0, 0b0000_0001]) + struct.pack("<i", 4326)
    wkb = b"\x01" + struct.pack("<I", 1) + struct.pack("<2d", 106.7, 10.8)
    assert gpkg_envelope(header + wkb) == [106.7, 10.8, 106.7, 10.8]


def test_gpkg_envelope_rejects_garbage() -> None:
    assert gpkg_envelope(None) is None
    assert gpkg_envelope(b"XX\x00\x00\x00\x00\x00\x00") is None


def test_render_markdown_smoke() -> None:
    report = {"total": 2, "accounting": {"input_count": 2, "normalized_count": 2}, "height": None,
              "categories": [{"column": "tier", "rows": [{"value": "A", "count": 2, "pct": 100.0}],
                              "other_count": 0}], "outliers": []}
    md = render_markdown(report, "demo")
    assert "Báo cáo chiều cao — demo" in md and "| A | 2 | 100.0 |" in md
