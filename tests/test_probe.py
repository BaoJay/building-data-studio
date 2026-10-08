from app.probe import Field, choose_nlt, describe_crs, guess_geometry_columns, suggest_mapping

COMPANY_FIELDS = [
    Field("building_id", "String"), Field("source_id", "String"), Field("height_m", "Real"),
    Field("height_provenance", "String"), Field("winning_source", "String"), Field("status", "String"),
    Field("min_height", "Integer64"), Field("parent_building_id", "String"), Field("building_tier", "String"),
]


def test_suggest_company_schema() -> None:
    s = suggest_mapping(COMPANY_FIELDS)
    assert s["height_col"] == "height_m"
    assert s["levels_col"] is None
    assert s["id_col"] == "building_id"
    assert s["parent_col"] == "parent_building_id"
    assert s["prov_col"] == "height_provenance"
    assert s["prov_missing"] == ["default"]
    assert s["category_cols"] == ["height_provenance", "building_tier"]
    assert s["pmtiles_attrs"] == [
        "building_id", "h_m", "min_height", "has_parts", "is_part", "parent_building_id", "building_tier",
    ]


def test_suggest_osm_schema_with_text_values() -> None:
    fields = [Field("osm_id", "String"), Field("height", "String"), Field("building:levels", "String"),
              Field("min_height", "String"), Field("building", "String")]
    s = suggest_mapping(fields)
    assert (s["height_col"], s["levels_col"], s["id_col"]) == ("height", "building:levels", "osm_id")
    assert s["prov_col"] is None and s["prov_missing"] == []


def test_generated_fields_are_never_suggested() -> None:
    s = suggest_mapping([Field("h_m", "Real"), Field("has_parts", "Integer")])
    assert s["height_col"] is None


def test_guess_geometry_columns_prefers_named_binary() -> None:
    fields = [Field("blob", "Binary"), Field("geometry_wkb", "Binary"), Field("wkt", "String"), Field("x", "Real")]
    assert guess_geometry_columns(fields) == ["geometry_wkb", "blob", "wkt"]


def test_missing_crs_with_lonlat_extent_suggests_wgs84() -> None:
    crs = describe_crs(None, [105.2, 10.3, 107.6, 21.4])
    assert crs["missing"] and crs["suggested"] == "EPSG:4326"


def test_missing_crs_with_projected_extent_requires_input() -> None:
    crs = describe_crs(None, [500000, 1100000, 620000, 1300000])
    assert crs["missing"] and crs["suggested"] is None


def test_declared_crs_label() -> None:
    crs = describe_crs({"wkt": "...", "projjson": {"name": "WGS 84", "id": {"authority": "EPSG", "code": 4326}}}, None)
    assert not crs["missing"] and crs["definition"] == "EPSG:4326"


def test_choose_nlt() -> None:
    assert choose_nlt("Geometry", ["MULTIPOLYGON", "POLYGON"]) == "MULTIPOLYGON"
    assert choose_nlt("Multi Polygon", None) == "MULTIPOLYGON"
    assert choose_nlt("Point", None) == "POINT"
    assert choose_nlt("Geometry", ["POINT", "POLYGON"]) == "PROMOTE_TO_MULTI"
    assert choose_nlt("Unknown", None) == "PROMOTE_TO_MULTI"
