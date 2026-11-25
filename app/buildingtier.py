"""building_tier of the canonical buildings ([B-05] Quy chuẩn Building Tier), one function per rule version.

The tier answers "which building needs full attributes". It runs after conflation, on the
canonical footprints, and changes nothing else: a new rule version (building_tier_v2…) is a new
entry in RULES, and ingest, conflation and the review lists stay as they are.

building_tier_v1 (first match wins):

    1  T1_iconic                 a pin of the 3D landmark list inside the footprint (the largest one), or
                                 the nearest footprint within 30 m (flag iconic_pin_nearest); every
                                 building of the landmark library (a GLB stands on it)
    2  T1_landmark               a name and a landmark signal: wikidata with historic / tourism=
                                 attraction·museum·gallery / a temple, church, government, stadium…
                                 building; or historic=city_gate·monument·memorial…; or heritage=1·2
    3  T2_commercial             a non-residential building (commercial, retail, office, hotel, public,
                                 government, hospital, school, university, temple, industrial…) or
                                 tourism=hotel·guest_house
    4  T2_commercial             an anchor POI inside the footprint (PROP_COMM*, hotel, hospital,
                                 university, ESS_GOV*, supermarket, museum), when a POI file is given
    5  T2_highrise_residential   building=apartments·dormitory, or a residential tag and high-rise
    6  T2_large                  high-rise of unknown use
    7  T2_large                  footprint >= 2,000 m²
    8  T3_residential            a residential tag
    -  T3_residential            the rest; flags minor_structure (roof, carport, guardhouse…) and
                                 tiny_footprint_qc (< 15 m²)

High-rise = building:levels >= 9 or a measured height (not the 3.5 m default) >= 30 m. A
building:part takes the tier of its building.
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RULE = "building_tier_v2"
TIERS = ("T1_iconic", "T1_landmark", "T2_commercial", "T2_highrise_residential", "T2_large", "T3_residential")

NON_RESIDENTIAL = (
    "commercial", "retail", "office", "hotel", "public", "government", "civic", "hospital", "clinic", "school",
    "university", "college", "kindergarten", "temple", "shrine", "pagoda", "monastery", "church", "cathedral",
    "chapel", "basilica", "mosque", "synagogue", "religious", "industrial", "warehouse", "factory", "manufacture",
    "train_station", "transportation", "stadium", "sports_hall", "sports_centre", "museum", "supermarket", "bank",
    "fire_station", "police", "military", "barracks", "conference_centre", "theatre", "cinema", "library",
    "embassy", "parking", "hangar", "bus_station", "terminal", "data_center", "market", "mall", "palace",
)
LANDMARK_BUILDINGS = (
    "temple", "shrine", "pagoda", "monastery", "church", "cathedral", "chapel", "basilica", "mosque", "synagogue",
    "religious", "government", "civic", "public", "stadium", "palace", "castle", "museum", "mausoleum",
    "train_station",
)
LANDMARK_HISTORIC = ("city_gate", "monument", "memorial", "castle", "fort", "palace", "citywalls", "tomb", "mausoleum")
LANDMARK_TOURISM = ("attraction", "museum", "gallery")
HOTEL_TOURISM = ("hotel", "guest_house")
RESIDENTIAL = ("residential", "house", "detached", "semidetached_house", "terrace", "apartments", "dormitory",
               "bungalow", "villa", "hut", "cabin", "farm", "houseboat", "static_caravan")
APARTMENTS = ("apartments", "dormitory")
MINOR = ("roof", "carport", "garage", "garages", "shed", "guardhouse", "kiosk", "toilets", "service", "booth",
         "container", "shelter", "bunker", "cowshed", "sty", "greenhouse", "gatehouse")
POI_ANCHORS = ("PROP_COMM_%", "ESS_GOV_%", "%HOTEL%", "ESS_HEALTH_HOSPITAL", "ESS_EDU_UNIVERSITY",
               "SHOP_GROC_SUPERMARKET", "TOUR_CULTURE_MUSEUM")
HIGHRISE_LEVELS = 9
HIGHRISE_M = 30.0
LARGE_M2 = 2000.0
TINY_M2 = 15.0
PIN_NEAREST_M = 30.0


def _in(values: tuple[str, ...]) -> str:
    return "(" + ", ".join("'" + v.replace("'", "''") + "'" for v in values) + ")"


def _tag(key: str) -> str:
    return f"lower(nullif(trim(json_extract_string(tags, '$.\"{key}\"')), ''))"


def building_tier_v1(con: Any, *, has_pins: bool, has_poi: bool) -> None:
    """Fill `tier` (node, building_tier, tier_rule, tier_flags) from `canon` (+ `tier_pins`,
    `tier_poi` with points in metres, `landmark_lock`)."""
    con.execute(f"""CREATE OR REPLACE TABLE tier_in AS
        SELECT node, kind, key, parent_source_id, area, gm, name,
               CASE WHEN lower(building) IN ('yes', 'true', '1') THEN NULL ELSE lower(building) END AS b,
               {_tag('wikidata')} AS wikidata, {_tag('historic')} AS historic, {_tag('tourism')} AS tourism,
               {_tag('heritage')} AS heritage, levels,
               coalesce(levels, 0) >= {HIGHRISE_LEVELS}
                 OR (height_provenance NOT IN ('default', 'landmark') AND height_m >= {HIGHRISE_M}) AS highrise
        FROM canon""")
    con.execute("CREATE OR REPLACE TABLE tier_hits (node BIGINT, rule INTEGER, tier VARCHAR, flag VARCHAR)")
    # Rule 1: the landmark library, then pins of the 3D landmark list.
    con.execute("INSERT INTO tier_hits SELECT node, 1, 'T1_iconic', NULL FROM landmark_lock")
    if has_pins:
        con.execute("""INSERT INTO tier_hits
            SELECT arg_max(t.node, t.area), 1, 'T1_iconic', NULL
            FROM tier_pins AS p JOIN tier_in AS t ON t.kind = 'building' AND ST_Contains(t.gm, p.gm)
            GROUP BY p.pin_id""")
        con.execute(f"""INSERT INTO tier_hits
            SELECT arg_min(t.node, ST_Distance(t.gm, p.gm)), 1, 'T1_iconic', 'iconic_pin_nearest'
            FROM tier_pins AS p JOIN tier_in AS t ON t.kind = 'building' AND ST_DWithin(t.gm, p.gm, {PIN_NEAREST_M})
            WHERE p.pin_id NOT IN (SELECT p2.pin_id FROM tier_pins AS p2 JOIN tier_in AS t2
                                   ON t2.kind = 'building' AND ST_Contains(t2.gm, p2.gm))
            GROUP BY p.pin_id""")
    con.execute(f"""INSERT INTO tier_hits
        SELECT node, 2, 'T1_landmark', NULL FROM tier_in
        WHERE name IS NOT NULL AND (
          (wikidata IS NOT NULL AND (historic IS NOT NULL OR tourism IN {_in(LANDMARK_TOURISM)}
                                     OR b IN {_in(LANDMARK_BUILDINGS)}))
          OR historic IN {_in(LANDMARK_HISTORIC)} OR heritage IN ('1', '2'))""")
    con.execute(f"""INSERT INTO tier_hits
        SELECT node, 3, 'T2_commercial', NULL FROM tier_in
        WHERE b IN {_in(NON_RESIDENTIAL)} OR tourism IN {_in(HOTEL_TOURISM)}""")
    if has_poi:
        con.execute("""INSERT INTO tier_hits
            SELECT DISTINCT t.node, 4, 'T2_commercial', NULL
            FROM tier_poi AS p JOIN tier_in AS t ON t.kind = 'building' AND ST_Contains(t.gm, p.gm)""")
    con.execute(f"""INSERT INTO tier_hits
        SELECT node, 5, 'T2_highrise_residential', NULL FROM tier_in
        WHERE b IN {_in(APARTMENTS)} OR (b IN {_in(RESIDENTIAL)} AND highrise)""")
    con.execute("INSERT INTO tier_hits SELECT node, 6, 'T2_large', NULL FROM tier_in WHERE highrise")
    con.execute(f"INSERT INTO tier_hits SELECT node, 7, 'T2_large', NULL FROM tier_in WHERE area >= {LARGE_M2}")
    con.execute(f"INSERT INTO tier_hits SELECT node, 8, 'T3_residential', NULL FROM tier_in WHERE b IN {_in(RESIDENTIAL)}")
    con.execute(f"""CREATE OR REPLACE TABLE tier AS
        SELECT t.node,
               coalesce(h.tier, 'T3_residential') AS building_tier,
               coalesce(h.rule, 0) AS tier_rule,
               list_filter([h.flag,
                            CASE WHEN h.rule IS NULL AND t.b IN {_in(MINOR)} THEN 'minor_structure' END,
                            CASE WHEN h.rule IS NULL AND t.area < {TINY_M2} THEN 'tiny_footprint_qc' END],
                           x -> x IS NOT NULL) AS tier_flags
        FROM tier_in AS t
        LEFT JOIN (SELECT * FROM tier_hits QUALIFY row_number() OVER (PARTITION BY node ORDER BY rule, flag NULLS FIRST) = 1) AS h
          USING (node)""")
    # A building:part takes the tier of its building.
    con.execute("""CREATE OR REPLACE TEMP TABLE part_tier AS
        SELECT part.node, pt.building_tier, pt.tier_rule
        FROM canon AS part JOIN canon AS b ON b.key = 'osm:' || part.parent_source_id JOIN tier AS pt ON pt.node = b.node
        WHERE part.kind = 'part'""")
    con.execute("""UPDATE tier SET building_tier = pt.building_tier, tier_rule = pt.tier_rule, tier_flags = []::VARCHAR[]
        FROM part_tier AS pt WHERE tier.node = pt.node""")
    con.execute("ALTER TABLE tier ADD COLUMN tier_basis VARCHAR")
    con.execute("ALTER TABLE tier ADD COLUMN height_class VARCHAR")


RULES: dict[str, Callable[..., None]] = {"building_tier_v1": building_tier_v1}


def apply(con: Any, rule: str = DEFAULT_RULE, *, has_pins: bool = False, has_poi: bool = False) -> dict[str, Any]:
    if rule not in RULES:
        raise ValueError(f"Không có luật building_tier {rule!r} (có: {', '.join(RULES)}).")
    RULES[rule](con, has_pins=has_pins, has_poi=has_poi)
    counts = dict(con.execute("SELECT building_tier, count(*) FROM tier GROUP BY 1 ORDER BY 1").fetchall())
    flags = dict(con.execute("SELECT f, count(*) FROM (SELECT unnest(tier_flags) AS f FROM tier) GROUP BY 1").fetchall())
    return {"rule": rule, "counts": counts, "flags": flags}


def poi_where() -> str:
    return " OR ".join(f"place_category_code LIKE '{p}'" for p in POI_ANCHORS)


# =============================================================================================
# building_tier_v2 ([B-05] Quy chuẩn Building Tier v2): T1 / T2 / T3 + height_class, for any file
# run through Convert. The rule engine is plain Python (first match wins, B-05 §2); DuckDB
# only measures the footprints and finds the footprint of each building:part.
# =============================================================================================

V2_RULE = "building_tier_v2"
V2_COLUMNS = ("building_tier", "height_class", "tier_rule", "tier_basis", "qc_flags")

V2_NONRES = frozenset((
    "commercial", "retail", "office", "hotel", "public", "government", "civic", "mall", "hospital", "university",
    "college", "school", "train_station", "transportation", "museum", "stadium", "sports_hall", "supermarket",
    "temple", "church", "industrial", "warehouse", "kindergarten", "hangar"))
V2_RES = frozenset(("house", "apartments", "residential", "terrace", "detached", "dormitory", "semidetached_house"))
V2_MINOR = frozenset(("roof", "carport", "guardhouse", "construction", "shed", "garage", "garages", "hut", "toilets",
                      "kiosk"))
V2_LODGING = frozenset(("hotel", "guest_house", "hostel", "motel", "apartment"))
V2_APARTMENTS = frozenset(("apartments", "dormitory"))
V2_RELIGIOUS = frozenset(("church", "cathedral", "temple", "mosque", "religious", "chapel", "shrine"))
V2_MINISTRIES = (
    "quốc phòng", "công an", "ngoại giao", "nội vụ", "tư pháp", "tài chính", "công thương",
    "nông nghiệp và môi trường", "xây dựng", "văn hóa, thể thao và du lịch", "khoa học và công nghệ",
    "giáo dục và đào tạo", "y tế", "dân tộc và tôn giáo", "kế hoạch và đầu tư", "giao thông vận tải",
    "tài nguyên và môi trường", "thông tin và truyền thông", "lao động - thương binh và xã hội",
    "nông nghiệp và phát triển nông thôn")
V2_PROVINCES = (
    "hà nội", "huế", "hải phòng", "đà nẵng", "hồ chí minh", "cần thơ", "tuyên quang", "cao bằng", "lai châu",
    "lào cai", "thái nguyên", "điện biên", "lạng sơn", "sơn la", "phú thọ", "bắc ninh", "quảng ninh", "hưng yên",
    "ninh bình", "thanh hóa", "nghệ an", "hà tĩnh", "quảng trị", "quảng ngãi", "gia lai", "khánh hòa", "lâm đồng",
    "đắk lắk", "đồng nai", "tây ninh", "vĩnh long", "đồng tháp", "cà mau", "an giang")
NAME_KEYS = ("name", "official_name", "alt_name", "name:vi")
# Tags read by the rule engine; a file column with one of these names is a tag.
V2_TAG_KEYS = ("building", "building:part", "building:levels", "amenity", "historic", "heritage", "tourism",
               "leisure", "healthcare", "office", "government", "aeroway", "capacity", *NAME_KEYS)

TINY_V2_M2 = 15.0

FLAG_REVIEW_FOOTPRINT = "flag_review_footprint"
FLAG_MINOR = "flag_minor_structure"
FLAG_TINY = "flag_tiny_footprint_qc"
FLAG_NEEDS_LEVELS = "flag_needs_levels"


# ------------------------------------------------------------------ names (B-05 Bảng A)
_TONE = {"oà": "òa", "oá": "óa", "oả": "ỏa", "oã": "õa", "oạ": "ọa", "oè": "òe", "oé": "óe", "oẻ": "ỏe",
         "oẽ": "õe", "oẹ": "ọe", "uỳ": "ùy", "uý": "úy", "uỷ": "ủy", "uỹ": "ũy", "uỵ": "ụy"}
_TONE_RE = re.compile("|".join(_TONE))
# Abbreviations B-05 accepts (UBND, HĐND, TAND, VKSND, TW) and TP / HCM for the province names.
_ABBR = {"ubnd": "ủy ban nhân dân", "hđnd": "hội đồng nhân dân", "hdnd": "hội đồng nhân dân",
         "tand": "tòa án nhân dân", "vksnd": "viện kiểm sát nhân dân", "tw": "trung ương", "tp": "thành phố",
         "hcm": "hồ chí minh"}
_ABBR_RE = re.compile(r"\b(" + "|".join(_ABBR) + r")\b")
_PUNCT_RE = re.compile(r"[^\w]+")


def normalize_name(text: str | None) -> str:
    """Lower case, NFC, one tone-mark spelling (hoá = hóa, uỷ = ủy), abbreviations expanded, punctuation
    turned into single spaces. Both the names and the B-05 word lists go through it."""
    if not text:
        return ""
    s = unicodedata.normalize("NFC", str(text)).lower()
    s = _PUNCT_RE.sub(" ", s).strip()
    s = _ABBR_RE.sub(lambda m: _ABBR[m.group(1)], s)
    return _TONE_RE.sub(lambda m: _TONE[m.group(0)], s)


def _words(*items: str) -> tuple[str, ...]:
    return tuple(normalize_name(i) for i in items)


def _has(text: str, words: tuple[str, ...]) -> bool:
    """A whole-word match of any of `words` in the normalised `text`."""
    padded = f" {text} "
    return any(f" {w} " in padded for w in words)


_MINISTRY_PREFIXES = tuple(f"bộ {m}" for m in _words(*V2_MINISTRIES))
_PROVINCE_SEATS = tuple(f"{p} {prov}" for p in _words("tỉnh ủy", "thành ủy", "ủy ban nhân dân tỉnh",
                                                      "ủy ban nhân dân thành phố", "hội đồng nhân dân tỉnh",
                                                      "hội đồng nhân dân thành phố")
                        for prov in _words(*V2_PROVINCES))


def _l02(name: str) -> bool:
    return any(f"{name} ".startswith(f"{m} ") for m in _MINISTRY_PREFIXES)


def _l03(name: str) -> bool:
    return _has(name, _PROVINCE_SEATS)


# ------------------------------------------------------------------ tags
class Tags:
    """The tags of one building, values lower case and split on ';'."""

    def __init__(self, tags: dict[str, Any]) -> None:
        self.raw = {str(k): str(v).strip() for k, v in tags.items() if v is not None and str(v).strip() != ""}
        self._vals = {k: {p.strip().lower() for p in v.split(";") if p.strip()} for k, v in self.raw.items()}

    def vals(self, key: str) -> set[str]:
        return self._vals.get(key, set())

    def is_(self, key: str, *values: str) -> bool:
        return bool(self.vals(key) & set(values))

    def has(self, key: str) -> bool:
        return bool(self.vals(key) - {"no"})

    def names(self) -> list[str]:
        return [n for n in (normalize_name(self.raw.get(k)) for k in NAME_KEYS) if n]


def _gov(t: Tags) -> bool:
    return (t.is_("office", "government") or t.has("government") or t.is_("amenity", "townhall", "courthouse")
            or t.is_("building", "government", "civic", "public", "office"))


def _funeral(t: Tags) -> bool:
    return t.is_("amenity", "funeral_hall") or t.is_("building", "funeral_hall")


def _stadium(t: Tags) -> bool:
    return t.is_("leisure", "stadium") or t.is_("building", "stadium")


def _sports_hall(t: Tags) -> bool:
    return t.is_("leisure", "sports_centre", "sports_hall") or t.is_("building", "sports_hall")


def _conference(t: Tags) -> bool:
    return t.is_("amenity", "conference_centre") or t.is_("building", "civic", "public")


_QG, _TW = _words("quốc gia"), _words("trung ương")
# Bảng A: (id, tag test, name test, excluded words)
TABLE_A: tuple[tuple[str, Callable[[Tags], bool], Callable[[str], bool], tuple[str, ...]], ...] = (
    ("L01", _gov, lambda n: _has(n, _words("nhà quốc hội", "văn phòng quốc hội", "phủ chủ tịch", "trụ sở chính phủ",
                                           "văn phòng chính phủ", "trung ương đảng", "văn phòng trung ương")), ()),
    ("L02", _gov, _l02, _words("bộ chỉ huy", "bộ đội", "cục", "tổng cục", "viện", "sở")),
    ("L03", _gov, _l03, _words("phường", "xã", "đặc khu", "quận", "huyện", "thị xã")),
    ("L04", _gov, lambda n: _has(n, _words("tòa án nhân dân tối cao", "viện kiểm sát nhân dân tối cao")), ()),
    ("L05", lambda t: t.is_("tourism", "museum", "gallery") or t.is_("amenity", "library") or t.is_("building", "museum"),
     lambda n: _has(n, _QG), ()),
    ("L06", lambda t: t.is_("amenity", "hospital") or t.is_("healthcare", "hospital") or t.is_("building", "hospital"),
     lambda n: _has(n, _TW), ()),
    ("L07", _stadium, lambda n: _has(n, _QG), ()),
    ("L08", _sports_hall, lambda n: _has(n, _QG), ()),
    ("L09", _conference, lambda n: _has(n, _QG), ()),
    ("L10", _funeral, lambda n: _has(n, _QG), ()),
)
_CAPACITY_RE = re.compile(r"^\s*(\d{1,3}(?:[.,\s]\d{3})+|\d+)")


def parse_capacity(value: Any) -> int | None:
    m = _CAPACITY_RE.match(str(value or ""))
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def match_table_a(t: Tags) -> str | None:
    names = t.names()
    for row_id, tag_ok, name_ok, excluded in TABLE_A:
        if names and tag_ok(t) and any(name_ok(n) and not _has(n, excluded) for n in names):
            return row_id
    return None


def match_table_b(t: Tags) -> str | None:
    cap = parse_capacity(t.raw.get("capacity"))
    if cap is None:
        return None
    if _stadium(t) and cap > 20000:
        return "C1"
    if _sports_hall(t) and cap >= 5000:
        return "C2"
    if _conference(t) and cap > 1200:
        return "C3"
    return None


def match_table_c(t: Tags) -> str | None:
    if t.is_("aeroway", "terminal") or t.is_("building", "terminal"):
        return "H01"
    if t.is_("aeroway", "hangar"):
        return "H02"
    return None


def is_religious(t: Tags) -> bool:
    return t.is_("amenity", "place_of_worship") or bool(t.vals("building") & V2_RELIGIOUS)


# ------------------------------------------------------------------ rule engine
@dataclass(frozen=True)
class TierV2:
    building_tier: str | None
    tier_rule: str | None
    tier_basis: str | None
    qc_flags: tuple[str, ...] = ()


def _gt(x: float | None, v: float) -> bool:
    return x is not None and x > v


def classify_v2(tags: dict[str, Any] | Tags, *, levels: float | None, height_m: float | None,
                area: float | None) -> TierV2:
    """B-05 §2 for one footprint (not a building:part): the first rule that holds.

    levels: building_levels (0 counts as empty); height_m: the height used for 3D; area: footprint
    area in m², which is also the GFA of a footprint (B-05 §4.1). A comparison with an empty value
    is false."""
    t = tags if isinstance(tags, Tags) else Tags(tags)
    lv = levels if levels else None
    r = _engine(t, lv, height_m, area)
    if r.building_tier == "T2" and lv is None:
        return TierV2(r.building_tier, r.tier_rule, r.tier_basis, (*r.qc_flags, FLAG_NEEDS_LEVELS))
    return r


def _engine(t: Tags, lv: float | None, h: float | None, gfa: float | None) -> TierV2:
    lm = "t1_national_provincial_landmark"
    if t.is_("heritage", "1", "2"):
        return TierV2("T1", lm, "osm_heritage_tag")
    if t.is_("historic", "monument"):
        return TierV2("T1", lm, "osm_historic_monument")
    religious = is_religious(t)
    if not religious and match_table_a(t):
        return TierV2("T1", lm, "pl1_function_name_match")
    if not religious and match_table_b(t):
        return TierV2("T1", lm, "pl1_capacity_tag")
    if match_table_c(t):
        return TierV2("T1", lm, "pl1_tag_only")
    if t.vals("building") & V2_MINOR:
        return TierV2("T3", "t3_minor_structure", "minor_tag", (FLAG_MINOR,))
    if lv is not None and lv >= 25:
        return TierV2("T1", "t1_scale_high_rise", "levels_ge_25")
    if _gt(h, 75):
        return TierV2("T1", "t1_scale_high_rise", "height_gt_75")
    if _gt(gfa, 30000):
        return TierV2("T1", "t1_scale_large_area", "gfa_gt_30000", (FLAG_REVIEW_FOOTPRINT,))
    if lv is not None and 8 <= lv <= 24:
        return TierV2("T2", "t2_scale_mid_rise", "levels_8_to_24")
    if _gt(h, 28):
        return TierV2("T2", "t2_scale_mid_rise", "height_gt_28")
    if gfa is not None and 10000 < gfa <= 30000:
        return TierV2("T2", "t2_scale_medium_area", "gfa_10000_to_30000")
    if t.vals("building") & V2_NONRES:
        return TierV2("T2", "t2_function_non_residential", "nonres_tag")
    if t.vals("tourism") & V2_LODGING:
        return TierV2("T2", "t2_function_non_residential", "lodging_tag")
    if _funeral(t) and not any(_has(n, _words("hợp đồng")) for n in t.names()):
        return TierV2("T2", "t2_function_non_residential", "funeral_tag")
    if t.vals("building") & V2_APARTMENTS:
        return TierV2("T2", "t2_function_apartments", "apartments_tag")
    if t.vals("building") & V2_RES:
        return TierV2("T3", "t3_residential_standard", "res_tag")
    if gfa is not None and 1000 <= gfa <= 10000:
        return TierV2("T3", "t3_scale_small_area", "gfa_1000_to_10000")
    if gfa is not None and gfa < TINY_V2_M2:
        return TierV2("T3", "t3_default", "tiny_footprint", (FLAG_TINY,))
    return TierV2("T3", "t3_default", "fallback_default")


def height_class(height_m: float | None) -> str | None:
    """B-05 §3, from height_m alone: < 28 HC0 · = 28 HC1 · ≤ 75 HC2 · ≤ 200 HC3 · above HC4."""
    if height_m is None:
        return None
    if height_m < 28:
        return "HC0"
    if height_m == 28:
        return "HC1"
    if height_m <= 75:
        return "HC2"
    if height_m <= 200:
        return "HC3"
    return "HC4"


# ------------------------------------------------------------------ a whole file (Convert)
OSM_ID_COLUMNS = ("source_id", "osm_id", "building_id", "id", "@id", "winning_observation_id")
_OSM_ID_RE = r"(way|relation)/([0-9]+)"
SAMPLE_LIMIT = 20


def parse_tags(text: Any) -> dict[str, str]:
    """A JSON tags column; anything else is no tags."""
    try:
        obj = json.loads(text) if text else {}
    except ValueError:
        return {}
    return {str(k): str(v) for k, v in obj.items() if v is not None} if isinstance(obj, dict) else {}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _s(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def classify_file(gpkg: Path, layer: str, *, osm_tags: Path | None = None,
                  find_osm_tags: Callable[[tuple[float, float, float, float]], Path | None] | None = None,
                  levels_col: str | None = None, out_csv: Path,
                  log: Callable[[str], None] = lambda _: None, on_con: Callable[[Any], None] | None = None,
                  ) -> dict[str, Any]:
    """building_tier_v2 for every feature of one GeoPackage layer (EPSG:4326) -> out_csv
    (fid + V2_COLUMNS, qc_flags comma separated). Returns the report section.

    Tags come from the file (a tags column, columns named like OSM keys) and, for features whose id is
    an OSM way / relation (`way/123`), from an OSM tags Parquet (osm_id, tags[, kind]): `osm_tags`, or
    the one `find_osm_tags(bbox)` picks. A building:part is not classified: it takes the tier of the
    smallest footprint holding its ST_PointOnSurface (B-05 §4.1)."""
    import duckdb

    from .osmbase import load_extension
    from .osmtags import is_part as tags_is_part
    from .osmtags import parse_levels

    con = duckdb.connect()
    if on_con:
        on_con(con)
    try:
        load_extension(con, "spatial")
        src = f"ST_Read({_s(str(gpkg))}, layer = {_s(layer)})"
        cols = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
        geom = next(c for c, t in cols.items() if t.startswith("GEOMETRY"))
        text_cols = [c for c, t in cols.items() if t == "VARCHAR"]
        # The column holding OSM ids: the first candidate with at least one `way/…` / `relation/…`.
        id_col = None
        for c in [c for c in OSM_ID_COLUMNS if c in text_cols]:
            if con.execute(f"SELECT count(*) FROM (SELECT 1 FROM {src} WHERE regexp_matches({_q(c)}, {_s(_OSM_ID_RE)}) "
                           "LIMIT 1)").fetchone()[0]:
                id_col = c
                break
        osm_key = f"regexp_extract({_q(id_col)}, {_s(_OSM_ID_RE)}, 0)" if id_col else "NULL"
        tag_cols = [c for c in V2_TAG_KEYS if c in text_cols or (c in cols and c in ("capacity", "building:levels"))]
        tags_col = "tags" if "tags" in text_cols else None
        lv_col = levels_col if levels_col in cols else None
        num = lambda c: f"TRY_CAST({_q(c)} AS DOUBLE)" if c in cols else "NULL"
        con.execute(f"""CREATE TABLE f AS SELECT fid, {_q(geom)} AS g,
                ST_Area_Spheroid(ST_FlipCoordinates({_q(geom)})) AS area, {num('height_m')} AS height_m,
                {num(lv_col) if lv_col else 'NULL'} AS levels_file,
                {'coalesce(CAST(is_part AS BOOLEAN), false)' if 'is_part' in cols else 'false'} AS is_part_file,
                {osm_key} AS osm_key, {_q(tags_col) if tags_col else 'NULL'} AS tags_file,
                {', '.join(f'CAST({_q(c)} AS VARCHAR) AS {_q("t:" + c)}' for c in tag_cols) or 'NULL AS "t:"'}
            FROM {src}""")
        total, with_key = con.execute("SELECT count(*), count(osm_key) FROM f").fetchone()
        w, s, e, n = con.execute("SELECT min(ST_XMin(g)), min(ST_YMin(g)), max(ST_XMax(g)), max(ST_YMax(g)) FROM f").fetchone()
        source = osm_tags
        if source is None and with_key and find_osm_tags and w is not None:
            source = find_osm_tags((w, s, e, n))
        snap_cols: set[str] = set()
        if source is not None:
            snap_cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({_s(str(source))})").fetchall()}
            if not {"osm_id", "tags"} <= snap_cols:
                raise ValueError(f"File tag OSM cần cột osm_id và tags: {source}")
            kind = "kind" if "kind" in snap_cols else "NULL"
            con.execute(f"""CREATE TABLE osm AS SELECT osm_id, any_value(tags) AS tags, any_value({kind}) AS kind
                            FROM read_parquet({_s(str(source))}) WHERE osm_id IN (SELECT osm_key FROM f) GROUP BY osm_id""")
        else:
            con.execute("CREATE TABLE osm (osm_id VARCHAR, tags VARCHAR, kind VARCHAR)")
        found = con.execute("SELECT count(*) FROM f JOIN osm ON osm.osm_id = f.osm_key").fetchone()[0]
        log(f"Tier v2: {total:,} feature · id OSM ở cột {id_col or '(không có)'}: {with_key:,} · "
            f"tag OSM từ {source or '(không có file tag OSM)'}: {found:,} khớp · tag trong file: "
            f"{', '.join(filter(None, [tags_col, *tag_cols])) or '(không có)'}"
            f" · số tầng: {lv_col or '(chỉ tag building:levels)'}")

        # Footprints through the rule engine; parts set aside.
        results: dict[int, tuple[Any, ...]] = {}
        part_fids: list[int] = []
        heights: dict[int, float | None] = {}
        cur = con.execute(f"""SELECT f.fid, f.area, f.height_m, f.levels_file, f.is_part_file, f.tags_file,
                                     osm.tags, osm.kind {''.join(f', f.{_q("t:" + c)}' for c in tag_cols)}
                              FROM f LEFT JOIN osm ON osm.osm_id = f.osm_key""")
        while rows := cur.fetchmany(50_000):
            for fid, area, h, lv_file, part_file, tags_file, osm_json, kind, *direct in rows:
                tags: dict[str, Any] = {c: v for c, v in zip(tag_cols, direct) if v not in (None, "")}
                tags.update(parse_tags(tags_file))
                tags.update(parse_tags(osm_json))
                heights[fid] = h
                if part_file or kind == "part" or (kind is None and tags_is_part(tags)):
                    part_fids.append(fid)
                    continue
                lv = parse_levels(tags.get("building:levels"))
                if lv is None and lv_file is not None and lv_file > 0:
                    lv = lv_file
                r = classify_v2(tags, levels=lv, height_m=h, area=area)
                results[fid] = (r.building_tier, height_class(h), r.tier_rule, r.tier_basis, ",".join(r.qc_flags) or None)

        # building:part -> the smallest footprint holding its point on surface.
        orphans: list[int] = []
        if part_fids:
            con.execute("CREATE TABLE parts (fid BIGINT)")
            con.executemany("INSERT INTO parts VALUES (?)", [(p,) for p in part_fids])
            parents = dict(con.execute("""
                WITH p AS (SELECT f.fid, ST_PointOnSurface(f.g) AS pt FROM f JOIN parts USING (fid)),
                     b AS (SELECT fid, g, area FROM f WHERE fid NOT IN (SELECT fid FROM parts))
                SELECT p.fid, arg_min(b.fid, b.area) FROM p JOIN b ON ST_Contains(b.g, p.pt) GROUP BY p.fid""").fetchall())
            for fid in part_fids:
                parent = results.get(parents.get(fid, -1))
                if parent is None:
                    orphans.append(fid)
                    results[fid] = (None, height_class(heights[fid]), None, None, None)
                else:
                    results[fid] = (parent[0], height_class(heights[fid]), parent[2], parent[3], None)

        with out_csv.open("w", newline="", encoding="utf-8") as fh:
            wr = csv.writer(fh)
            wr.writerow(("fid", *V2_COLUMNS))
            wr.writerows((fid, *r) for fid, r in results.items())
    finally:
        con.close()

    def count(i: int) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in results.values():
            out[r[i] or ""] = out.get(r[i] or "", 0) + 1
        return dict(sorted(out.items()))

    flags: dict[str, int] = {}
    for r in results.values():
        for fl in (r[4] or "").split(","):
            if fl:
                flags[fl] = flags.get(fl, 0) + 1
    return {
        "rule": V2_RULE, "total": total, "counts": count(0), "height_class": count(1), "tier_rule": count(2),
        "tier_basis": count(3), "flags": dict(sorted(flags.items())),
        "parts": len(part_fids), "parts_without_parent": len(orphans), "orphan_fids": orphans[:SAMPLE_LIMIT],
        "osm_id_col": id_col, "with_osm_id": with_key, "osm_tags_file": str(source) if source else None,
        "osm_tags_found": found, "tag_columns": [c for c in [tags_col, *tag_cols] if c],
        "levels_col": lv_col,
    }


# ------------------------------------------------------------------ canonical build
def building_tier_v2(con: Any, **_: Any) -> None:
    """building_tier_v2 on the canonical build: fill `tier` (node, building_tier, tier_rule, tier_basis,
    height_class, tier_flags) from `canon`. A building:part takes the tier of its building (parent_source_id)."""
    import tempfile

    from .osmtags import parse_levels

    # Equi-join only: a non-key condition in the ON turns it into a nested loop (minutes at city scale).
    rows = con.execute("""SELECT c.node, c.part, b.node, c.area, c.tags, c.levels, c.height_m
                          FROM (SELECT *, kind = 'part' AS part,
                                       CASE WHEN kind = 'part' THEN 'osm:' || parent_source_id END AS parent_key
                                FROM canon) AS c
                          LEFT JOIN canon AS b ON b.key = c.parent_key""").fetchall()
    out: dict[int, tuple[Any, ...]] = {}
    for node, part, _, area, tags, levels, h in rows:
        if not part:
            t = parse_tags(tags)
            lv = parse_levels(t.get("building:levels")) or levels
            r = classify_v2(t, levels=lv, height_m=h, area=area)
            out[node] = (r.building_tier, r.tier_rule, r.tier_basis, height_class(h), ",".join(r.qc_flags))
    for node, part, parent, _, _, _, h in rows:
        if part:
            p = out.get(parent) or (None, None, None)
            out[node] = (p[0], p[1], p[2], height_class(h), "")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", encoding="utf-8") as fh:
        csv.writer(fh).writerows((n, *r) for n, r in out.items())
        fh.flush()
        con.execute(f"""CREATE OR REPLACE TABLE tier AS
            SELECT node, building_tier, tier_rule, tier_basis, height_class,
                   list_filter(string_split(coalesce(flags, ''), ','), x -> x <> '') AS tier_flags
            FROM read_csv({_s(fh.name)}, header = false, names = ['node', 'building_tier', 'tier_rule', 'tier_basis',
                          'height_class', 'flags'], types = {{'node': 'BIGINT'}}, all_varchar = true)""")


RULES = {"building_tier_v2": building_tier_v2, **RULES}  # first = the default shown on the canonical card
