"""building_tier_v2: the rule engine against [B-05] Quy chuẩn Building Tier v2 (§6 Bộ Test Chuẩn)."""

import pytest

from app.buildingtier import classify_v2, height_class, normalize_name

M = 3.5  # m per level, as the Convert height rule


def run(tags, *, area, levels=None, height=None):
    lv = levels if levels is not None else None
    h = height if height is not None else (lv * M if lv else M)
    return classify_v2(tags, levels=lv, height_m=h, area=area), height_class(h)


# §6: (case, tags, area, levels, height tag) -> (tier, rule, basis, flags, height_class)
CASES = [
    ("1 Tasco Tower", {"building": "yes", "building:levels:underground": "2", "name": "Tòa nhà Tasco"},
     2753, 27, 20, ("T1", "t1_scale_high_rise", "levels_ge_25", (), "HC0")),
    ("2 Landmark 72", {"building": "commercial", "wikidata": "Q609140", "name": "Landmark72"},
     17911, None, None, ("T2", "t2_scale_medium_area", "gfa_10000_to_30000", ("flag_needs_levels",), "HC0")),
    ("3 Lăng Bác", {"building": "yes", "historic": "monument", "tourism": "attraction", "wikidata": "Q874234",
                    "name": "Lăng Chủ tịch Hồ Chí Minh"},
     1267, None, 7.8, ("T1", "t1_national_provincial_landmark", "osm_historic_monument", (), "HC0")),
    ("4 Phủ Chủ tịch", {"building": "yes", "government": "presidency", "historic": "castle",
                        "tourism": "attraction", "name": "Phủ Chủ Tịch"},
     1189, 4, None, ("T1", "t1_national_provincial_landmark", "pl1_function_name_match", (), "HC0")),
    ("5 Nhà Quốc hội", {"building": "yes", "office": "government", "government": "legislative",
                        "name": "Nhà Quốc hội"},
     10643, 3, 19.7, ("T1", "t1_national_provincial_landmark", "pl1_function_name_match", (), "HC0")),
    ("6 Văn phòng Chính phủ", {"building": "yes", "office": "government", "name": "Văn phòng Chính phủ"},
     4471, 9, None, ("T1", "t1_national_provincial_landmark", "pl1_function_name_match", (), "HC2")),
    ("6' Tập thể VPCP (đối chứng)", {"building": "yes", "name": "Tập thể Văn phòng Chính phủ - Lô B"},
     500, None, None, ("T3", "t3_default", "fallback_default", (), "HC0")),
    ("8 Viettel", {"building": "yes", "office": "company", "building:levels": "0", "roof:levels": "9",
                   "name": "Tập đoàn Viettel"},
     7006, 0, None, ("T3", "t3_scale_small_area", "gfa_1000_to_10000", (), "HC0")),
    ("9 TASCO Mall", {"building": "yes", "shop": "mall", "building:levels:underground": "1",
                      "name": "TASCO Mall Long Biên"},
     14177, 3, None, ("T2", "t2_scale_medium_area", "gfa_10000_to_30000", (), "HC0")),
    ("10 Nhà ga T2", {"building": "yes", "aeroway": "terminal", "name": "Nhà ga Quốc tế T2"},
     86586, 4, None, ("T1", "t1_national_provincial_landmark", "pl1_tag_only", (), "HC0")),
    ("11 Nhà Tang lễ Quốc gia", {"building": "yes", "amenity": "funeral_hall", "name": "Nhà Tang lễ Quốc gia"},
     2887, None, None, ("T1", "t1_national_provincial_landmark", "pl1_function_name_match", (), "HC0")),
    ("12 Điểm ký hợp đồng tang lễ", {"building": "yes", "amenity": "funeral_hall",
                                     "name": "Điểm ký hợp đồng tang lễ số 11"},
     94, None, None, ("T3", "t3_default", "fallback_default", (), "HC0")),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_b05_standard_cases(case):
    _, tags, area, levels, height, (tier, rule, basis, flags, hc) = case
    r, got_hc = run(tags, area=area, levels=levels, height=height)
    assert (r.building_tier, r.tier_rule, r.tier_basis, r.qc_flags, got_hc) == (tier, rule, basis, flags, hc)


def test_landmark72_parts_height_class():
    # §4.1: the parts keep the parent's tier, their height_class is their own.
    assert [height_class(h) for h in (336, 212, 27)] == ["HC4", "HC4", "HC0"]


@pytest.mark.parametrize("h,hc", [(None, None), (3.5, "HC0"), (27.9, "HC0"), (28, "HC1"), (28.1, "HC2"),
                                  (75, "HC2"), (75.5, "HC3"), (200, "HC3"), (200.1, "HC4")])
def test_height_class_bounds(h, hc):
    assert height_class(h) == hc


@pytest.mark.parametrize("tags,area,expect", [
    ({"building": "garage"}, 40000, ("t3_minor_structure", "minor_tag", ("flag_minor_structure",))),
    ({"building": "yes"}, 40000, ("t1_scale_large_area", "gfa_gt_30000", ("flag_review_footprint",))),
    ({"building": "yes"}, 10, ("t3_default", "tiny_footprint", ("flag_tiny_footprint_qc",))),
    ({"building": "school"}, 500, ("t2_function_non_residential", "nonres_tag", ("flag_needs_levels",))),
    ({"building": "yes", "tourism": "hotel"}, 500, ("t2_function_non_residential", "lodging_tag", ("flag_needs_levels",))),
    ({"building": "apartments"}, 500, ("t2_function_apartments", "apartments_tag", ("flag_needs_levels",))),
    ({"building": "house"}, 500, ("t3_residential_standard", "res_tag", ())),
    ({"building": "yes", "heritage": "2"}, 50, ("t1_national_provincial_landmark", "osm_heritage_tag", ())),
    ({"building": "yes", "aeroway": "hangar"}, 50, ("t1_national_provincial_landmark", "pl1_tag_only", ())),
    ({"leisure": "stadium", "building": "yes", "capacity": "40,192"}, 900,
     ("t1_national_provincial_landmark", "pl1_capacity_tag", ())),
    ({"leisure": "stadium", "building": "yes", "capacity": "15000"}, 900, ("t3_default", "fallback_default", ())),
])
def test_rules(tags, area, expect):
    r = classify_v2(tags, levels=None, height_m=3.5, area=area)
    assert (r.tier_rule, r.tier_basis, r.qc_flags) == expect


def test_scale_rules_use_levels_and_height():
    assert classify_v2({}, levels=12, height_m=42, area=500).tier_basis == "levels_8_to_24"
    assert classify_v2({}, levels=None, height_m=80, area=500).tier_basis == "height_gt_75"
    assert classify_v2({}, levels=None, height_m=30, area=500).tier_basis == "height_gt_28"
    r = classify_v2({}, levels=None, height_m=30, area=500)
    assert r.building_tier == "T2" and r.qc_flags == ("flag_needs_levels",)


@pytest.mark.parametrize("tags,landmark", [
    ({"office": "government", "name": "Bộ Y tế"}, True),
    ({"office": "government", "name": "Bộ Văn hoá, Thể thao và Du lịch"}, True),
    ({"office": "government", "name": "Bộ Tài chính - Tổng cục Thuế"}, False),  # excluded word
    ({"office": "government", "name": "Bộ Chỉ huy Quân sự Thủ đô"}, False),
    ({"office": "government", "name": "Cục Thuế - Bộ Tài chính"}, False),  # does not start with "bộ"
    ({"office": "government", "name": "UBND TP. Hà Nội"}, True),
    ({"office": "government", "name": "Tỉnh uỷ Thanh Hoá"}, True),
    ({"office": "government", "name": "Thành ủy Hà Nội"}, True),
    ({"office": "government", "name": "UBND phường Ba Đình, thành phố Hà Nội"}, False),
    ({"office": "government", "name": "TAND Tối cao"}, True),
    ({"amenity": "hospital", "building": "yes", "name": "Bệnh viện Bạch Mai"}, False),
    ({"amenity": "hospital", "building": "yes", "official_name": "Bệnh viện TW Quân đội 108"}, True),
    ({"tourism": "museum", "building": "yes", "name:vi": "Bảo tàng Lịch sử Quốc gia"}, True),
    ({"building": "church", "amenity": "conference_centre", "name": "Trung tâm Hội nghị Quốc gia"}, False),  # religious
    ({"name": "Văn phòng Chính phủ"}, False),  # name without a function tag
])
def test_table_a(tags, landmark):
    r = classify_v2({"building": "yes", **tags}, levels=None, height_m=3.5, area=500)
    assert (r.tier_basis == "pl1_function_name_match") is landmark


def test_normalize_name():
    assert normalize_name("UBND TP.HCM") == "ủy ban nhân dân thành phố hồ chí minh"
    assert normalize_name("Tỉnh uỷ  Khánh Hoà") == normalize_name("tỉnh ủy khánh hòa")
