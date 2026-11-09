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

from collections.abc import Callable
from typing import Any

DEFAULT_RULE = "building_tier_v1"
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
