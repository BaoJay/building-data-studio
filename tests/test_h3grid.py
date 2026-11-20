"""H3 cells for the /preview map (app/h3grid.py): the r8 grid of a view, a pasted list, the preset."""

import pytest

from app import h3grid


def test_grid_covers_view_with_r8_cells():
    g = h3grid.grid("105.80,21.00,105.85,21.04")
    assert 20 < len(g["features"]) < 50
    f = g["features"][0]
    assert f["properties"]["res"] == 8
    ring = f["geometry"]["coordinates"][0]
    assert ring[0] == ring[-1] and len(ring) == 7


@pytest.mark.parametrize("bbox", ["100,10,110,25", "a,b,c,d", "105.9,21,105.8,21.1"])
def test_grid_refuses_wide_or_bad_bbox(bbox):
    with pytest.raises(h3grid.H3Error):
        h3grid.grid(bbox)


def test_parse_dedups_keeps_order_and_reports_junk():
    cells, bad = h3grid.parse('88415CB4A5FFFFF, 88415cb4a1fffff\n"88415cb4a5fffff" foo 8841436945ffff0')
    assert cells == ["88415cb4a5fffff", "88415cb4a1fffff"]
    assert bad == ["foo", "8841436945ffff0"]


def test_preset_of_the_release():
    assert len(h3grid.presets()[0]["cells"]) == 48
