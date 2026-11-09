# Building Data Studio

App chạy local (trên trình duyệt) để chuyển dữ liệu building từ **Parquet / GeoPackage** (hoặc một sheet **Excel**) sang:

- **GeoPackage (.gpkg)**: mở bằng QGIS để kiểm tra chiều cao của khối building trong khu vực
- **PMTiles (.pmtiles)**: publish và serve cho app mobile (MapLibre, dựng 3D bằng `fill-extrusion`)

Có giao diện kéo thả, nút tải file và nút mở thư mục output. App tự sinh báo cáo chiều cao và có trang xem bản đồ 3D.

Tab **So sánh 2 file** so sánh bản production (A) với bản sắp release (B) và đưa ra kết luận **Pass / Warn / Fail** trước khi release (xem [mục 9](#9-so-sánh-2-file-trước-release)).

Trang **Indoor map** (`/indoor`, bản PoC) vẽ bản đồ trong nhà cho một toà được chọn (TTTM, siêu thị) theo chuẩn **OGC IndoorGML 2.0**: tầng, không gian, cửa, thang, tự sinh đồ thị điều hướng, tìm đường nhiều tầng và xuất IndoorGML / GeoJSON (xem [mục 10](#10-indoor-map-poc-theo-indoorgml-20)).

Trang xem 3D (`/preview`) hiện building LoD1 cùng mọi **landmark 3D** (file GLB) đang dùng trên file PMTiles đó. Drawer **Landmark** bên phải quản lý file GLB của từng building theo `building_id` và footprint, cùng các version đã dùng. Mỗi version có hướng và toạ độ neo riêng (xem [Landmark 3D trong trang xem 3D](#landmark-3d-trong-trang-xem-3d)).

**Base Hà Nội**: building OpenStreetMap của toàn thành phố, app tự tạo một lần. Trang xem 3D mở thẳng base này, không cần upload hay convert file (xem [Base Hà Nội](#base-hà-nội-building-osm-toàn-thành-phố)).

**Canonical Building Data**: ghép OSM với Google Open Buildings (và GBA nếu có) thành một bộ building chuẩn. Cột giống `canonical_building` của platform, `building_id` giữ nguyên qua các lần build, landmark 3D không bị mất khi build bản mới. Khi đã có, trang xem 3D mở canonical đang dùng thay cho base (xem [Canonical Building Data](#canonical-building-data-osm--google-open-buildings)).

```
.xlsx (một sheet) ── cột WKT / GeoJSON, hoặc way ID OSM ── Overpass + buffer ──► GeoPackage dựng sẵn ─┐
                                                                                                    ▼
input (.parquet / .gpkg / .fgb / .geojson)
  └─ ogr2ogr + SQL ──► GeoPackage chuẩn hoá (EPSG:4326, height_m, h_src, h_outlier)
       ├─ ogrinfo SQL ──► has_parts / is_part (toà nhà nhiều khối)
       ├─ ogr2ogr ──► GeoPackage ở CRS khác (tuỳ chọn, vd VN-2000 / UTM)
       ├─ báo cáo chiều cao ──► report.md / report.json
       └─ FlatGeobuf (tạm) ── tippecanoe ──► PMTiles (theo schema VMAP) ── pmtiles verify + kiểm tra schema
```

---

## 1. Cài đặt (làm một lần)

Cần **GDAL ≥ 3.8 có driver Parquet**, **tippecanoe của felt** (ghi thẳng ra `.pmtiles`) và Python ≥ 3.11. `pmtiles` CLI không bắt buộc (dùng để `pmtiles verify` và đọc header). Lần chạy đầu, `run.sh` tự tạo `.venv` và cài các dependency đã pin trong `requirements.txt`.

### macOS

```bash
brew install gdal tippecanoe pmtiles
```

### Linux / WSL (Ubuntu 24.04)

GDAL cài bằng `apt` **không có driver Parquet** (file `.parquet` sẽ báo `Unable to find driver 'Parquet'`), nên lấy GDAL và tippecanoe từ conda-forge qua micromamba.

1. Python và venv:

   ```bash
   sudo apt update
   sudo apt install -y python3 python3-venv curl
   ```

2. Cài micromamba, chạy riêng lệnh này, bấm Enter để nhận mặc định ở mọi câu hỏi:

   ```bash
   "${SHELL}" <(curl -L micro.mamba.pm/install.sh)
   ```

   Sau đó **đóng terminal và mở terminal mới** (hoặc `source ~/.bashrc`). Nếu dán các lệnh sau vào cùng terminal cũ, micromamba sẽ báo `Shell not initialized`.

3. Tạo env `gdal` có GDAL (kèm driver Parquet) và tippecanoe:

   ```bash
   micromamba create -y -n gdal -c conda-forge gdal libgdal-arrow-parquet tippecanoe
   ~/micromamba/envs/gdal/bin/ogrinfo --formats | grep -i parquet   # phải thấy dòng "Parquet"
   ```

   Không cần `micromamba activate gdal` trước khi chạy app: `run.sh` tự dùng env `~/micromamba/envs/gdal` nếu có. Env ở chỗ khác thì đặt `BDS_GDAL_ENV=/đường/dẫn/env`.

4. `pmtiles` CLI (không bắt buộc). Tên file trên GitHub có kèm số phiên bản, xem bản mới ở [go-pmtiles releases](https://github.com/protomaps/go-pmtiles/releases):

   ```bash
   cd /tmp
   curl -fLO https://github.com/protomaps/go-pmtiles/releases/download/v1.31.2/go-pmtiles_1.31.2_Linux_x86_64.tar.gz
   tar xzf go-pmtiles_1.31.2_Linux_x86_64.tar.gz pmtiles
   sudo mv pmtiles /usr/local/bin/
   pmtiles version
   ```

Trên WSL không cần cài trình duyệt hay trình quản lý file trong Ubuntu: app nhận ra WSL và dùng `explorer.exe` để mở trình duyệt Windows và nút **Mở thư mục**.

## 2. Chạy app

```bash
cd ~/dev/building-data-studio
./run.sh
```

Trên macOS có thể double-click file **`Building Data Studio.command`** trong Finder. Trình duyệt sẽ tự mở http://127.0.0.1:8765 (trên WSL là trình duyệt mặc định của Windows; nếu không tự mở thì mở link này trên Windows, hoặc chạy `./run.sh --no-browser`). Có thể đổi cổng bằng `./run.sh --port 9000`. Bấm `Ctrl+C` trong terminal để dừng.

Server chỉ lắng nghe trên `127.0.0.1`, máy khác trong mạng không truy cập được.

### Demo nhanh: 3D building Hà Nội, không cần file

1. Chạy `./run.sh`. Lần đầu, terminal báo `Chưa có base Hà Nội: đang tạo…`. Thẻ **Base Hà Nội** ở đầu trang chính hiện từng bước. Đợi khoảng 3–5 phút, cần internet.
2. Khi thẻ hiện số building (vd *212.373 building + 1.517 khối con*), bấm **Xem bản đồ 3D**, hoặc mở thẳng http://127.0.0.1:8765/preview. Bản đồ mở ở Hồ Gươm, zoom 15,5.
   - Click một building: drawer **Landmark** bên phải hiện thuộc tính của nó: `building_id` (id OSM, vd `way/1327061631`), `name`, `height_m`, `h_src`.
   - Ô **Bản đồ nền** chọn VMAP để xem như app Mobile, rồi đổi 5 chế độ ánh sáng.
3. Để thêm một landmark, ngay trên trang này:
   1. Click building trên bản đồ.
   2. Ở drawer, nhập tên và **ID liên kết** (nên dùng chính id OSM, vd `way/1327061631`), rồi bấm **Đăng ký landmark**.
   3. Chọn file `.glb`: mô hình hiện ngay trên building để xem thử, chưa upload. Bấm **Upload version mới** để lưu nó thành một version.
   4. Chỉnh hướng, tỉ lệ và toạ độ neo, rồi bấm **Lưu placement** và **Dùng**.
4. Từ đó, mỗi lần mở trang xem 3D, landmark này đứng thay cho extrusion của building, cùng mọi landmark đang dùng khác của base.
5. Cần xem dữ liệu trên QGIS thì bấm **Tải GeoPackage** ở thẻ Base, hoặc **Mở thư mục** để mở `base/hanoi/<lần tạo>/`.

Base chỉ tạo một lần. Từ lần chạy sau, app mở base có sẵn ngay, không cần mạng (trừ bản đồ nền). Muốn lấy OSM mới hơn thì bấm **Cập nhật** ở thẻ Base. Chi tiết về dữ liệu và các tuỳ chọn: [Base Hà Nội](#base-hà-nội-building-osm-toàn-thành-phố).

## 3. Cách dùng

### Base Hà Nội (building OSM toàn thành phố)

Lần đầu chạy `./run.sh`, app tự tạo **base Hà Nội**: mọi building OpenStreetMap trong ranh giới Thành phố Hà Nội, khoảng 3–5 phút, cần internet. Thẻ **Base Hà Nội** ở đầu trang chính hiện tiến trình. Khi xong:

- **Xem bản đồ 3D** (`/preview`) mở file PMTiles của base, tại Hồ Gươm. Landmark đăng ký ngay trên base, nên landmark và building LoD1 luôn dùng cùng `building_id` và cùng footprint. Vẫn mở được file `.pmtiles` khác như trước.
- **Tải GeoPackage** tải `hanoi_osm_buildings.gpkg` để xem trên QGIS. File có layer `buildings` (đã qua pipeline convert: `height_m`, `h_src`, `h_outlier`, `has_parts`, `is_part`) và layer `boundary` (ranh giới thành phố).
- **Cập nhật** tạo lại từ release Overture mới nhất. Base cũ vẫn được dùng cho tới khi bản mới chạy xong.

Nguồn dữ liệu là [Overture Maps](https://docs.overturemaps.org/) (theme `buildings` và `divisions`), đọc trực tiếp từ bucket S3 công khai bằng DuckDB. Overture phát hành lại OSM mỗi tháng với schema cố định, và ghi nguồn geometry của từng feature. App **chỉ giữ feature có geometry từ OpenStreetMap**: footprint Google Open Buildings / Microsoft ML Buildings mà Overture thêm vào đều bị bỏ.

```
Overture (S3, GeoParquet) ─ DuckDB ─┬─ divisions: ranh giới VN-HN ─────────────────────► layer boundary
                                    └─ buildings + building_part, nguồn OSM, trong ranh giới
                                         └─ pipeline convert ──► hanoi_osm_buildings.gpkg + .pmtiles (z12–16)
```

| Cột | Ý nghĩa |
|---|---|
| `building_id` | id OSM: `way/123` hoặc `relation/456` (mở được tại `openstreetmap.org/way/123`) |
| `parent_building_id` | với khối con (`building:part`): id OSM của building chứa nó |
| `name`, `class`, `subtype` | tên, loại building (`apartments`, `school`…). Khối con lấy `class` của building cha |
| `height`, `num_floors` | chiều cao (m) và số tầng từ tag OSM. `height_m` lấy `height`, rồi `num_floors × 3,5 m`, rồi 3,5 m (một tầng) |
| `min_height`, `min_floor` | đáy khối (m) / tầng bắt đầu. `min_height` trống thì tính `min_floor × 3,5 m` |
| `roof_*`, `facade_*` | mái và mặt ngoài (hình dạng, màu, vật liệu) từ tag OSM |
| `osm_version`, `osm_timestamp`, `overture_id` | phiên bản và thời điểm sửa cuối của way / relation trên OSM, GERS id của Overture |

Building ngầm (`is_underground`) bị bỏ. Một building được giữ khi chạm ranh giới; một khối con được giữ khi building cha của nó được giữ.

Số liệu lần tạo đầu (Overture `2026-10-21.0`, ảnh chụp OSM ngày 2026-09-12): **212.373 building + 1.517 khối con** trên 3.354 km². Chỉ **5,4%** có chiều cao thật (`height` hoặc số tầng), còn lại dùng chiều cao mặc định (lúc đó là 4 m, nay là 3,5 m). GeoPackage nặng 81 MB, PMTiles 20 MB. Để so sánh, Overture có tổng cộng khoảng 4,2 triệu building trong khung bao Hà Nội; phần lớn là footprint Google / Microsoft nhận diện bằng AI.

Dữ liệu nằm trong `base/hanoi/` cạnh `output/` và không commit vào git. Mỗi lần tạo là một thư mục mới; `base/hanoi/base.json` trỏ tới bản đang dùng. Các tuỳ chọn:

| Cách | Tác dụng |
|---|---|
| `./run.sh --no-base` hoặc `BDS_BASE_AUTOBUILD=0` | Không tự tạo base khi khởi động (vẫn bấm **Tạo base** được) |
| `BDS_BASE_DIR=/đường/dẫn` | Đổi chỗ lưu base, ví dụ thư mục dùng chung với team |
| `BDS_OVERTURE_RELEASE=2026-10-21.0` | Dùng một release Overture cố định thay vì bản mới nhất |
| `.venv/bin/python -m app.osmbase hanoi [--release …]` | Tạo base từ terminal, không cần mở app |

Lần đầu, DuckDB tự tải extension `spatial` và `httpfs` từ extensions.duckdb.org. Nếu máy không vào được địa chỉ đó, cài bằng pip: `.venv/bin/pip install duckdb-extension-spatial duckdb-extension-httpfs`. Hai gói này phải cùng version với `duckdb`.

#### Lấy thẳng từ OpenStreetMap (không qua Overture)

Overture chỉ cập nhật OSM mỗi tháng. Muốn dữ liệu mới theo ngày, tạo base trực tiếp từ OSM (`app/osmdirect.py`), có hai nguồn:

| Nguồn | Độ mới | Dùng khi |
|---|---|---|
| **Overpass API** | khoảng 1 phút sau OSM | Cập nhật nhanh. App chia ranh giới thành ô 0,05°, mỗi ô một truy vấn; máy chủ báo quá tải thì tự chia ô nhỏ hơn. Hà Nội cần vài trăm truy vấn, nên nên trỏ tới Overpass riêng (`BDS_OVERPASS_URL`) nếu chạy thường xuyên |
| **File `.osm.pbf` / `.osm`** | theo ngày tải file (Geofabrik cập nhật hằng ngày) | Máy chủ Overpass công cộng không đủ sức, hoặc chạy offline. Cần GDAL (đã có sẵn cho app) |

```bash
.venv/bin/python -m app.osmdirect hanoi                                  # Overpass
.venv/bin/python -m app.osmdirect hanoi --file vietnam-latest.osm.pbf     # file OSM
.venv/bin/python -m app.osmdirect hanoi --overpass https://my-host/api/interpreter --tile 0.1
```

Hoặc `POST /api/base/build` với `{"source": "overpass"}` hay `{"source": "file", "osm_file": "/đường/dẫn/vietnam.osm.pbf"}`. Kết quả ghi vào cùng `base/hanoi/base.json` (có thêm `source_kind`) nên trang 3D, landmark và GeoPackage dùng như bình thường. `osm_snapshot` là thời điểm dữ liệu OSM của Overpass; với file, là lần sửa mới nhất của một building trong file.

App tự làm những việc Overture đã làm sẵn:

- **Chiều cao** (`app/osmtags.py`): đọc `height` ở dạng `12`, `12,5`, `12 m`, `40'`, `12'6"`. Từ chối khoảng (`10-12`), chữ, số âm, và giá trị trên 1.000 m. `building:levels` chấp nhận `3;4` (lấy số lớn hơn). `min_height` trống thì dùng `building:min_level × 3,5 m`. `height_m` vẫn do pipeline convert quyết định như cũ.
- **`building:part`** (chuẩn Simple 3D Buildings): `building:part=*` (khác `no`) là khối con. Khối con nhận làm cha **building outline nhỏ nhất chứa một điểm của nó** (OSM không có liên kết tường minh). Khối con không có outline vẫn là building thường nên không bị ẩn. Outline có khối con nhận `has_parts` và bị ẩn khi dựng 3D.
- **Multipolygon**: ghép các way `outer`/`inner` thành vòng; relation thiếu member (way không khép kín) bị bỏ và đếm trong log.
- **Nguồn chiều cao** (`height_lineage`, chỉ trong GeoPackage, không đưa vào PMTiles): `osm:height` (tag `height`), `osm:levels*3.5m` (`building:levels` × 3,5 m), `default:3.5m`, suy ra từ `h_src` nên luôn khớp `height_m`. Thứ tự ưu tiên giữ nguyên: tag OSM, rồi số tầng, rồi 3,5 m; không lấy chiều cao từ Overture.
- **Tag lỗi**: tag số không dùng được (`height=tall`, `building:levels=0`…) được ghi vào `*_tag_issues.csv` (kèm link OSM) để sửa trực tiếp trên OSM.
- Bỏ building `location=underground` / `tunnel=*`. Ranh giới lấy từ relation OSM có `ISO3166-2=VN-HN`.

So với Overture: không có footprint Google / Microsoft (vốn đã bị loại ở base này), nhưng cũng không có bước chuẩn hoá `class` và `roof_*` đã được Overture xử lý; các cột này lấy nguyên từ tag OSM.

Dữ liệu OSM theo giấy phép ODbL. PMTiles của base ghi sẵn attribution `© OpenStreetMap contributors (ODbL) · Overture Maps <release>`; giữ dòng này khi đưa base lên app.

### Canonical Building Data (OSM + Google Open Buildings)

Thẻ **Canonical Building Data** ở trang chính ghép nhiều nguồn footprint thành **một** bộ building chuẩn: mỗi công trình ngoài thực tế là một dòng. Thuật toán theo *Tài liệu chi tiết thuật toán Building Conflation* (tu-liem-v1.3), với các quy tắc đã chốt cho Building Data Studio (cấu hình `osm-first-v1`). File Parquet có đúng các cột của `canonical_building` trên platform (21 field của schema VMAP), thêm `height_lineage` và vài cột để kiểm tra.

```
Ingest            OSM (Overpass, hoặc file .osm.pbf) · Google Open Buildings v3 (app tự tải) · GBA (file, tuỳ chọn)
  │               mỗi nguồn là một snapshot không sửa, trong canonical/sources/
Normalize         polygon hợp lệ trong vùng · GOB confidence ≥ 0,75 · bỏ footprint máy < 10 m² · tách GBA theo nguồn gốc
  │
Entity matching   đồ thị chồng lấn giữa các nguồn · merge edge khi IoU ≥ 0,25 hoặc coverage ≥ 0,40 · connected component
  │
Conflation        OSM > GOB > GBA-ms: footprint lấy nguyên của nguồn thắng, nguồn thấp hơn là bằng chứng
  │
Build             building_id ổn định · khoá landmark · chiều cao · building_tier · kiểm tra invariants
                  → .parquet + .gpkg + .pmtiles (schema VMAP) + review_osm.gpkg / review_gob.gpkg + báo cáo
```

#### Build

Bấm **Build canonical…**, chọn rồi **Bắt đầu build**:

| Mục | Lựa chọn |
|---|---|
| Vùng build | **Toàn Hà Nội** (mặc định), **48 ô H3 r8** (release 25/11, 38,6 km²), hoặc dán danh sách ô H3. Building được tính vào vùng theo điểm bên trong footprint; nguồn được đọc rộng thêm 200 m để mép vùng ghép giống giữa vùng |
| Nguồn OSM | **Overpass** (tải mới, vài phút với cả Hà Nội), **file .osm.pbf** (vd Geofabrik), hoặc **snapshot đã tải** ở lần trước |
| Google Open Buildings | App tự tải các ô S2 cấp 6 phủ vùng. Hà Nội nằm trọn trong ô `3135`, khoảng 1,4 GB, chỉ tải lần đầu; file gốc giữ ở `canonical/sources/gob/raw/`. Tick **Tải lại** để lấy bản mới |
| File GBA | Tuỳ chọn. Global Building Atlas (`.parquet`, `.gpkg`, `.geojson`, `.fgb`), cần cột chiều cao và cột nguồn gốc (`source` / `origin`). Bản ghi gốc Microsoft là footprint `gba_ms`; mọi bản ghi có chiều cao là ứng viên chiều cao. Không có cột nguồn gốc thì chỉ dùng chiều cao |
| File POI | Tuỳ chọn. POI dump Parquet (`latitude`, `longitude`, `place_category_code`) cho luật tier 4 |
| File canonical production | Nên chọn ở lần build đầu (vd `canonical_building_v7_20261028.parquet`): toà đã có trên production giữ nguyên `building_id`. App nhớ trong ID registry, lần sau không cần |
| Đặt làm đang dùng | Bỏ tick để build thử mà không đổi bản trang 3D đang mở |

Từ terminal:

```bash
.venv/bin/python -m app.canonical hanoi                                            # toàn Hà Nội, OSM qua Overpass
.venv/bin/python -m app.canonical hanoi --h3 hanoi-48 --osm vietnam-latest.osm.pbf # 48 ô H3, OSM từ file
.venv/bin/python -m app.canonical hanoi --osm snapshot --seed canonical_building_v7_20261028.parquet --no-activate
```

Hoặc `POST /api/canonical/build` với `{"aoi": "h3", "h3_preset": "hanoi-48", "osm_source": "file", "osm_file": "…"}`.

#### Quy tắc ghép (`osm-first-v1`)

| Trường hợp | Kết quả |
|---|---|
| OSM và GOB chồng nhau đủ để thành merge edge | Giữ footprint OSM. GOB là bằng chứng (`source_ids`, `group_id`) |
| Chỉ có GOB | Lên tile luôn: `winning_source = google_open_building`, `confidence` = điểm của Google, `source_id` = plus code |
| GOB chồng một phần lên OSM nhưng không đủ merge (IoU ≥ 0,10 hoặc coverage ≥ 0,20) | Bỏ GOB, không đưa vào review |
| GOB chỉ chạm mép OSM (dưới ngưỡng trên) | Giữ cả hai |
| GBA gốc Microsoft (`gba_ms`) | Footprint độc lập, xếp sau GOB; chỉ thắng ở chỗ OSM và GOB đều không có |
| 1 OSM – nhiều GOB / nhiều OSM – 1 GOB / nhiều – nhiều, hoặc 1:1 mà IoU nhóm < 0,50 | `split` / `aggregate` / `ambiguous`: giữ OSM, đưa vào danh sách review |

Footprint luôn copy nguyên từ nguồn thắng (không union, average hay snap). Ngưỡng nằm trong `Params` của `app/conflation.py`. Không sửa một cấu hình đã dùng: thêm cấu hình mới (vd `osm-first-v2`) vào `CONFIGS`. Mỗi build ghi `config_version` và `params_hash` vào `build.json`.

#### Chiều cao

| Ưu tiên | Nguồn | `height_provenance` | `height_lineage` |
|---|---|---|---|
| 1 | Tag `height` của OSM (2–500 m) | `osm_height` | `osm:height` |
| 2 | `building:levels` × 3,5 m (1–100 tầng) | `osm_levels` | `osm:levels*3.5m` |
| 3 | GBA: polygon GBA chồng nhiều nhất lên footprint này, IoU ≥ 0,50 | `gba` | `gba:<nguồn gốc>:<id GBA>` |
| 4 | Mặc định 3,5 m (một tầng) | `default` | `default:3.5m` |

Khối con (`building:part`) lấy tag riêng, số tầng riêng, rồi chiều cao của building cha (`parent:way/…`). `min_height` lấy tag `min_height`, rồi `building:min_level × 3,5 m`, chỉ khi thấp hơn `height_m`. `height` và `h_m` trong PMTiles là bản sao của `height_m`.

#### Các cột

| Cột | Giá trị |
|---|---|
| `building_id` | UUID ổn định qua các build (xem dưới) |
| `source_id` | id trong nguồn thắng: `way/123`, plus code của GOB, id GBA |
| `winning_source` | `osm`, `google_open_building`, `gba_ms`, hoặc `landmark` (footprint cũ của landmark bị mất toà) |
| `winning_observation_id` | id của bản ghi nguồn thắng (cố định theo nguồn + phiên bản) |
| `group_id` | id của component khi nhiều footprint được ghép; trống khi đứng riêng |
| `confidence` | điểm confidence của GOB; trống với OSM |
| `status` | `active` |
| `resolution_mode`, `decided_by`, `updated_by` | `auto`; toà có landmark: `manual` / `landmark` |
| `build_id`, `config_version`, `updated_at` | `build-bldg-YYYYMMDD-HHMMSS`, `osm-first-v1`, thời điểm build |
| `description` | `Rule osm-first-v1, build build-bldg-…: footprint from osm, height from osm_levels` |
| `parent_building_id`, `min_height` | khối con: `building_id` của toà cha; đáy khối (chuỗi, như schema) |
| `building_tier` | theo luật `building_tier_v1` (dưới) |
| Chỉ trong Parquet / GPKG | `height_lineage`, `name`, `building_levels`, `footprint_area_m2`, `conflation_relation_type`, `group_iou`, `source_ids`, `conflation_reasons`, `tier_flags` |

#### building_id ổn định

`canonical/hanoi/id_registry.parquet` nhớ mỗi bản ghi nguồn (`osm:way/123`, `gob:<plus code>`…) đã thuộc `building_id` nào. Build mới tìm theo footprint thắng trước, rồi theo các bằng chứng: một nhà OSM mới vẽ đúng chỗ một footprint Google cũ sẽ **kế thừa** id của footprint Google đó. Không tìm được thì sinh UUIDv7 mới. Một id không bao giờ được giao cho toà khác. Registry chỉ được ghi khi build thành công. Báo cáo ghi số id giữ / kế thừa / mới.

#### building_tier

Luật `building_tier_v1` theo *[B-05] Quy chuẩn Building Tier* (khớp luật đầu tiên): `T1_iconic` (pin trong `config/building_tier/iconic_pins.geojson`, hoặc toà có landmark), `T1_landmark`, `T2_commercial`, `T2_highrise_residential`, `T2_large`, `T3_residential`. Cao tầng = ≥ 9 tầng hoặc chiều cao đo được ≥ 30 m. Khối con lấy tier của toà cha. Cờ `minor_structure` và `tiny_footprint_qc` (< 15 m²) nằm trong `tier_flags`. Luật mới (`building_tier_v2`…) là một hàm mới trong `RULES` của `app/buildingtier.py`, chạy sau conflation nên không đổi ingest, footprint, chiều cao hay danh sách review.

#### Danh sách review cho MapOps

Mỗi build có `review_osm.gpkg` và `review_gob.gpkg` (thêm `review_gba.gpkg` khi có GBA), layer `review`: footprint nguồn của mọi trường hợp cần xem, cùng `issue_type` (`ambiguous`, `split`, `aggregate`, `coverage_gap`, `large_footprint` > 5.000 m², `landmark_lost`, `landmark_footprint_changed`), `severity`, `group_id`, `role` (winner / evidence), `canonical_building_id`, `group_iou`. Mở cả hai file trên QGIS để so footprint OSM và GOB của cùng một `group_id`. `conflation_bridge.parquet` lưu mọi cặp chồng lấn giữa các nguồn (IoU, coverage, khoảng cách tâm, match score), kể cả cặp dưới ngưỡng.

#### Kiểm tra trước khi lưu

Build chỉ được lưu khi qua hết: geometry hợp lệ, `building_id` không trùng, mỗi bản ghi nguồn thuộc tối đa một building, `one_to_one` có IoU nhóm ≥ 0,50, coverage gap có reason, chiều cao trong 2–500 m, khối con có toà cha, `min_height` < `height_m`. Không qua thì build báo lỗi; canonical đang dùng, ID registry và landmark giữ nguyên.

#### Thư mục, báo cáo và rollback

```
canonical/                                  (BDS_CANONICAL_DIR)
  sources/osm/<snapshot>/osm.parquet         + source.json
  sources/gob/<snapshot>/gob.parquet         + source.json; raw/<ô S2>_buildings.csv.gz
  hanoi/canonical.json                       build đang dùng
  hanoi/id_registry.parquet
  hanoi/canonical_building_hanoi__<giờ>/     .parquet .gpkg .pmtiles review_*.gpkg conflation_bridge.parquet
                                             canonical_report.md build.json report.md log.txt
```

`canonical_report.md` ghi nguồn và snapshot, số liệu normalize, đồ thị, quan hệ, nguồn chiều cao, tier, id, invariants, review và landmark. Bảng **Các build** trên thẻ có **Xem 3D**, **Báo cáo** và **Đặt làm đang dùng** (rollback về một build cũ). Thư mục `canonical/` không commit vào git.

So sánh hai build (hoặc với file production) bằng tab **So sánh 2 file**, khớp theo `building_id`.

### Convert một file

1. **Chọn file**: kéo thả file vào ô, hoặc dán đường dẫn rồi bấm **Mở**. Với file lớn nên dán đường dẫn, vì kéo thả sẽ copy file vào thư mục `uploads/`.
2. App đọc file và hiển thị: số feature, cột geometry, CRS, extent, danh sách cột kèm cảnh báo (nếu có).
3. **Tuỳ chọn**: app tự đoán cột chiều cao, cột ID, cột parent, cột nguồn chiều cao. Kiểm tra lại rồi bấm **Chạy convert**.
4. Theo dõi tiến trình từng bước, có thể **Huỷ** giữa chừng.
5. Khi xong, mỗi file có nút **Tải về** và **Hiện trong Finder**. Ngoài ra có nút **Mở thư mục output** và **Xem bản đồ 3D**.
6. **Báo cáo chiều cao** hiển thị ngay bên dưới. Bấm **Xem 3D** ở bảng outlier hoặc bảng toà cao nhất để bay thẳng tới building đó trên bản đồ.

Kéo thả file `.pmtiles` có sẵn vào app để xem 3D mà không cần convert.

### Convert một sheet Excel (.xlsx)

File Excel không có geometry sẵn. Sau khi mở file, app hiện một form để dựng geometry trước:

1. **Sheet**: app chọn sẵn sheet đầu tiên có cột hình học, đổi được.
2. **Dòng header**: app tự dò, bỏ qua dòng tiêu đề và ghi chú phía trên bảng. Nếu bảng có một dòng nhãn tiếng Việt và ngay dưới là một dòng tên máy (`way_id`, `buffer_moi_ben_m`…), app lấy dòng tên máy làm tên cột. Bảng xem trước tô đậm dòng header; sửa số dòng nếu dò sai (0 = sheet không có header).
3. **Nguồn hình học**, một trong hai:
   - **Cột WKT / GeoJSON**: mỗi ô là một hình (`POLYGON ((…))` hoặc `{"type": "Polygon", …}`). Nhập CRS của toạ độ (mặc định `EPSG:4326`).
   - **Cột way ID OpenStreetMap**: mỗi ô là một way ID (`123`, `w123`, `way/123` hoặc link openstreetmap.org/way/123). App tải hình đường từ **Overpass API** (cần internet, có thể mất 1–2 phút), rồi buffer **mỗi bên** theo cột buffer đã chọn thành polygon. Dòng không có số buffer dùng **Buffer mặc định**. Lưu ý chọn cột bề rộng *mỗi bên*, không phải cột mặt cắt cả đường.
4. Bấm **Tạo dữ liệu bản đồ**. App ghi một GeoPackage vào `uploads/` (layer mang tên sheet, thêm cột `xlsx_row` = số dòng trong Excel, và với way ID thêm `osm_way_id`, `buffer_m`). Dòng không dựng được hình (ô trống, WKT lỗi, way đã bị xoá khỏi OSM) bị bỏ qua và được liệt kê trong cảnh báo.
5. Phần còn lại giống file thường: chọn tuỳ chọn rồi **Chạy convert**. Sheet có cột chiều cao / số tầng thì dựng 3D như building. Không có thì ô **Chiều cao** tắt sẵn, output là polygon phẳng giữ mọi cột của sheet, và trang xem bản đồ mở ở chế độ 2D.

Đổi máy chủ Overpass bằng biến môi trường `BDS_OVERPASS_URL` (mặc định `https://overpass-api.de/api/interpreter`).

### Về file Parquet không có metadata

Một số file Parquet lưu geometry ở cột WKB/WKT (ví dụ `geometry_wkb`) mà không có metadata GeoParquet và không ghi CRS. App sẽ:

- tự nhận cột geometry (có thể đổi trong phần *Nguồn dữ liệu*)
- **không tự âm thầm gán CRS**: nếu extent là kinh/vĩ độ, app *đề xuất* `EPSG:4326` và hiện cảnh báo để bạn xác nhận. Nếu dữ liệu ở hệ khác (VN-2000…), hãy sửa ô **CRS nguồn**.

## 4. Chiều cao `height_m`

Mỗi building lấy **giá trị hợp lệ đầu tiên** theo thứ tự:

| Ưu tiên | Nguồn | `h_src` |
|---|---|---|
| 1 | Cột chiều cao, nếu nằm trong khoảng hợp lệ (mặc định 2–500 m) | `height` |
| 2 | Số tầng × mét/tầng (mặc định 3,5 m; số tầng 1–200) | `levels` |
| 3 | Chiều cao mặc định (mặc định 3,5 m, tức một tầng theo quy ước B-03) | `default` |

- **Cột nguồn chiều cao** (vd `height_provenance`): các dòng có giá trị trong *“Giá trị = không có số liệu thật”* (mặc định `default`) được xem là **không có số đo**. Như vậy báo cáo phân biệt được chiều cao thật với chiều cao mặc định.
- **`h_outlier = 1`** khi giá trị gốc nằm ngoài khoảng hợp lệ, ví dụ 0 m hoặc 49.380 m. Phần *Giá trị ngoài khoảng hợp lệ* có 3 cách xử lý:
  - *Bỏ qua* (khuyên dùng): dùng số tầng hoặc giá trị mặc định
  - *Kẹp về max*
  - *Giữ nguyên*: chỉ đánh dấu
- File canonical đo chiều cao ngay ở cột `height_m`. App đọc giá trị gốc của cột này rồi ghi giá trị đã xử lý vào chính `height_m`, nên giá trị hợp lệ giữ nguyên, chỉ dòng ngoài khoảng hợp lệ bị thay theo cách xử lý outlier. Giá trị gốc được giữ ở cột `h_raw` (chỉ trong GeoPackage), để bảng outlier trong báo cáo hiện đúng số gốc (vd 49.380 m, −8 m).
- Muốn chỉ convert mà không tính chiều cao thì bỏ tick **Chiều cao height_m**. Khi đó `height_m` của file (nếu có) được chuyển nguyên.
- File tạo trước khi app đổi sang `height_m` (base Hà Nội cũ, output cũ, bản production đang serve) có cột `h_m`. Trang xem 3D và tab **So sánh 2 file** vẫn đọc được cột này. Bấm **Cập nhật** ở thẻ Base để tạo lại base với `height_m`.

## 5. Các cột app thêm vào

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `height_m` | real | Chiều cao dùng để dựng 3D (m). Nếu file có sẵn `height_m`, giá trị đã xử lý thay giá trị gốc |
| `h_raw` | real | Giá trị gốc của `height_m`. Chỉ có khi cột chiều cao chính là `height_m` |
| `h_src` | text | Nguồn của `height_m`: `height` / `levels` / `clamped` / `default` |
| `h_outlier` | int 0/1 | 1 = giá trị gốc bất thường, cần kiểm tra |
| `has_parts` | boolean | Outline có khối con trỏ về (nên **ẩn** khi dựng 3D) |
| `is_part` | boolean | Là khối con (có `parent_building_id` khác chính nó) |

Mọi cột gốc được giữ nguyên trong GeoPackage, trừ khi bạn bỏ tick. PMTiles chỉ chứa các thuộc tính được chọn, mặc định là: `building_id, height_m, min_height, has_parts, is_part, parent_building_id, building_tier`. Khi bật **Theo schema** (tự bật với parquet canonical), PMTiles chứa đúng các field của schema VMAP, xem [Schema PMTiles của VMAP](#schema-pmtiles-của-vmap).

## 6. Output

Mỗi lần chạy tạo một thư mục mới `output/<tên>__<YYYYMMDD-HHMMSS>/`, không ghi đè lần chạy trước:

| File | Nội dung |
|---|---|
| `<tên>.gpkg` | Layer `buildings`, kèm spatial index |
| `<tên>.pmtiles` | Layer `buildings`, mặc định z13–15 (app MapLibre tự overzoom khi phóng to hơn) |
| `report.md` / `report.json` | Báo cáo chiều cao, đối soát số lượng, lệnh đã chạy, phiên bản công cụ, cấu hình |
| `log.txt` | Log đầy đủ của lần chạy |

Mỗi bước đều có **đối soát số feature** (input → GPKG → PMTiles). Nếu số lượng bị lệch, app ghi cảnh báo vào log và báo cáo.

## 7. Kiểm tra trên QGIS

Kéo file `.gpkg` vào QGIS, chuột phải layer → **Filter…**:

| Muốn xem | Biểu thức |
|---|---|
| Building có chiều cao bất thường | `"h_outlier" = 1` |
| Building đang dùng chiều cao mặc định | `"h_src" = 'default'` |
| Building cao trên 100 m | `"height_m" > 100` |
| Toàn bộ khối của một toà nhà | `"parent_building_id" = '<id>' OR "building_id" = '<id>'` |

Để xem 3D: **Layer Properties → 3D View → Single Symbol**, ở ô **Extrusion** bấm nút data-defined và đặt biểu thức `"height_m"`. Sau đó mở **View → 3D Map Views → New 3D Map View**.

## 8. Dùng PMTiles trên app mobile (MapLibre)

### Schema PMTiles của VMAP

PMTiles publish cho app Mobile / web theo schema đã thống nhất với VMAP. Schema lưu ở [`schemas/vmap-buildings.json`](schemas/vmap-buildings.json), lấy từ `vector_layers` của TileJSON `building_v7_20261028.pmtiles`: layer `buildings`, zoom 13–16, 21 field.

Trong tab **Convert**, nhóm PMTiles có ô **Theo schema `vmap-buildings`**. Ô này tự bật khi file có từ 40 % field của schema trở lên; parquet canonical có đủ. Khi bật:

- Tên layer và zoom lấy theo schema (ô nhập bị khoá). Danh sách **Thuộc tính đưa vào PMTiles** chỉ để xem: field có dữ liệu được tô, field dữ liệu không có bị gạch ngang, field app tính hoặc chép có viền nét đứt.
- Mỗi field được ép đúng kiểu của schema: `min_height`, `updated_at`… thành chuỗi, `height_m` thành số. Chữ không phải số (vd `abc`) thành rỗng, không thành 0.
- `height_m` là chiều cao app tính ([mục 4](#4-chiều-cao-height_m)). `height` và `h_m` là bản sao của `height_m`. `h_m` chỉ giữ tạm cho app Mobile bản cũ và sẽ bị bỏ ở bản sau (mục `deprecated` trong file schema).
- Cột của dữ liệu không có trong schema (vd `superseded_by`) không vào tile. Log và báo cáo liệt kê các cột này, cùng các field schema mà dữ liệu không có.
- Sau khi cắt tile, app đọc `vector_layers` của file PMTiles và so với schema: thiếu hoặc thừa field, sai kiểu, sai zoom. Nếu lệch, bước **Kiểm tra PMTiles** ghi *Lệch schema* và báo cáo có cảnh báo, nhưng job vẫn hoàn tất. tippecanoe bỏ field rỗng trên mọi building, nên cột toàn NULL cũng hiện là thiếu.

PMTiles theo schema không có `has_parts` / `is_part` (GeoPackage vẫn có), nên app Mobile không ẩn được outline đã chia khối con: outline và các khối con vẽ chồng lên nhau.

Thêm hoặc đổi field: sửa `schemas/vmap-buildings.json`, mỗi field một dòng `"tên": "String"` (hoặc `"Number"`, `"Boolean"`). `copies` khai báo field chép giá trị từ field khác. Không cần sửa code: lần convert sau dùng schema mới. Bỏ hẳn `h_m` thì xoá nó khỏi `fields`, `copies` và `deprecated`.

### Layer 3D

```json
{
  "id": "buildings-3d",
  "type": "fill-extrusion",
  "source": "buildings",
  "source-layer": "buildings",
  "minzoom": 13,
  "filter": ["!=", ["get", "has_parts"], true],
  "paint": {
    "fill-extrusion-height": ["get", "height_m"],
    "fill-extrusion-base": ["to-number", ["get", "min_height"], 0],
    "fill-extrusion-color": "#d9d3c9",
    "fill-extrusion-opacity": 0.9
  }
}
```

`min_height` là chuỗi theo schema, nên đổi sang số bằng `to-number` trước khi đưa vào `fill-extrusion-base` (giá trị rỗng hoặc không phải số thành 0). Filter `has_parts != true` ẩn các outline đã được chia thành khối con, để không bị vẽ chồng; filter chỉ có tác dụng với PMTiles có `has_parts` (không theo schema VMAP). Cách làm này giống pipeline `vmap-buildings-to-pmtiles`.

### Xem trên nền bản đồ của app Mobile (VMAP) với 5 chế độ ánh sáng

Trong trang xem 3D, ô **Bản đồ nền** có lựa chọn **VMAP streets-v2 (nền app Mobile)**. Khi chọn nền này:

- Ô **Ánh sáng bản đồ** hiện 4 nút **Bình minh / Ban ngày / Hoàng hôn / Ban đêm**. Mỗi nút là một style VMAP đang phát hành (`https://assets.vmaps.vn/styles/streets-v2-{dawn,day,dusk,night}/style.json`), dùng nguyên bản: màu, sprite và khối `light` của chính style đó. Dòng ghi chú dưới các nút ghi tên style và `light` của nó.
- Building tự chuyển sang cách tô **Như app Mobile**: một màu building cho mọi chế độ, `light` của style tô sáng / tối các mặt. Màu này (`#ddcca9`, độ mờ 0.85) là **ước lượng** từ ảnh chụp Tasco Map admin, vì không style VMAP nào có layer 3D. Đổi được trong `app/vmap.py` hoặc bằng biến môi trường `BDS_VMAP_BUILDING_COLOR` / `BDS_VMAP_BUILDING_OPACITY`. Vẫn chọn lại được các cách tô khác (chiều cao, outlier…).
- Nút **Tải style.json chế độ này** tải style của chế độ đang chọn (giữ nguyên URL sprite / glyphs / tiles gốc, màu building 3D ở `metadata["vmap:building-3d"]`) để gửi team Mobile.
- `assets.vmaps.vn` không bật CORS nên server local tải hộ style, sprite và glyphs. Nếu không tải được style live, app dùng bản lưu `map-styles/streets-v2-{chế độ}.style.json` và báo dưới ô chọn nền.

Chi tiết các file style của Mobile, bảng màu và `light` của từng chế độ: [`map-styles/README.md`](map-styles/README.md).

### Đổ bóng vẽ sẵn (thử nghiệm)

MapLibre không vẽ được bóng building đổ xuống mặt đất. Ô **Đổ bóng (vẽ sẵn, thử nghiệm)** trong trang xem 3D tính sẵn bóng của từng building rồi vẽ thành một layer `fill` mờ, nằm dưới nhãn và dưới building:

- Hướng và độ dài bóng lấy theo `light` của style VMAP đang chọn: Bình minh nắng từ phía đông nam (cao 40°), Ban ngày từ phía nam (cao 70°, bóng ngắn), Hoàng hôn từ phía tây nam (cao 10°, bóng dài gần 6 lần chiều cao); Ban đêm không có bóng. Nền khác VMAP dùng nắng Ban ngày. Độ dài bóng = chiều cao / tan(góc cao mặt trời).
- Bóng được tính cho vùng quanh khung nhìn (từ zoom 15, tối đa khoảng 5 km mỗi chiều), khoảng 4 giây cho 12 nghìn building. Dịch bản đồ ra ngoài vùng đã tính thì tự tính lại. Bật / tắt được, lựa chọn được nhớ trong trình duyệt.
- Mỗi building được quét theo tia nắng từ đáy (`min_height`) tới đỉnh: footprint dời tới hai độ cao cộng một hình bình hành cho mỗi cạnh, nên footprint lõm (chữ L, chữ U, có sân trong) vẫn đúng. Toàn bộ bóng được gộp thành một hình (không bị đậm lên ở chỗ chồng nhau) và trừ đi footprint.
- Nút **Tải layer bóng vùng này (GeoJSON)** để team Mobile thử trên app.
- Landmark đã có version đang dùng thì đổ bóng theo chính mesh GLB thay cho hộp LoD1 của building (xem [Landmark 3D trong trang xem 3D](#landmark-3d-trong-trang-xem-3d) bên dưới).
- Giới hạn: bóng chỉ đổ xuống mặt đất, không đổ lên mái hay tường building bên cạnh, và chỉ đúng với hướng nắng cố định của từng chế độ.

### Layer bóng trong PMTiles (cho app Mobile)

Trong tab **Convert**, nhóm PMTiles có ô **Thêm layer bóng `shadows`**. Khi bật, file PMTiles output có 2 layer:

| Layer | Nội dung | Zoom |
|---|---|---|
| `buildings` | như trước (tile building cắt y như khi không bật bóng) | theo Min / Max zoom |
| `shadows` | bóng đã gộp, mỗi feature có thuộc tính `preset` = `dawn` / `day` / `dusk` | từ zoom 15 (hoặc Min zoom nếu lớn hơn) tới Max zoom |

Cách tạo: sau khi cắt tile building, app tính bóng cho cả lớp GeoPackage chuẩn hoá (chia lưới ô 0,01°, mỗi building đọc một lần, mảnh bóng cắt theo ô rồi gộp theo ô + chế độ bằng nhiều tiến trình song song), cắt tile layer `shadows` riêng rồi `tile-join` vào file building. Hướng nắng lấy từ `light` của 3 style VMAP có nắng (Bình minh, Ban ngày, Hoàng hôn) lúc chạy và được ghi vào `report.json` (`shadows.presets`). Ô **Chỉ đổ bóng building cao hơn (m)** bỏ bóng của building thấp (footprint của chúng vẫn được trừ khỏi bóng).

Trên app, thêm một layer dùng chung source với building; khi người dùng đổi chế độ thì đổi `filter` và màu, Ban đêm thì ẩn layer:

```json
{
  "id": "building-shadow",
  "type": "fill",
  "source": "buildings",
  "source-layer": "shadows",
  "minzoom": 15,
  "filter": ["==", ["get", "preset"], "dusk"],
  "paint": { "fill-color": "#2b1d2b", "fill-opacity": 0.3, "fill-antialias": false }
}
```

Đặt layer này dưới các layer nhãn chữ và dưới layer building 3D. `fill-antialias: false` tránh đường viền mảnh ở mép các ô lưới. Màu / độ mờ của từng chế độ nằm trong bảng ở [`map-styles/README.md`](map-styles/README.md). Trang xem 3D tự nhận layer `shadows` có sẵn trong file và hiển thị đúng như vậy (không tính lại).

Số đo trên `canonical_building_v2` (1.197.099 building, máy dev 15 nhân, 7 luồng gộp), ngoài 231 MB của tile building z13–17. Các số này đo với hướng nắng cũ (Hoàng hôn cao 35°). Style streets-v2-dusk đặt mặt trời ở 10° nên bóng Hoàng hôn dài gấp khoảng 4 lần, và layer `shadows` sẽ lớn hơn:

| Đổ bóng | Building có bóng | Tính bóng | Cắt tile bóng + gộp | Layer `shadows` (z15–17) | PMTiles gộp |
|---|---|---|---|---|---|
| Mọi building (0 m) | 1.195.916 | ~2 phút | ~1,2 phút | 194 MB | 421 MB |
| Cao hơn 4 m | 28.948 | ~17 giây | vài giây | 6,2 MB | ~232 MB (ước tính) |

Cả lượt convert có bóng (mọi building) mất khoảng 4,5 phút, RAM đỉnh khoảng 3 GB (lúc 7 tiến trình gộp chạy song song).

96,9% building của bản này đang dùng chiều cao mặc định (`h_src = default`, lúc đo là 4 m, nay là 3,5 m), nên ngưỡng 4 m bỏ gần hết bóng của nhà "đoán chiều cao" và làm layer nhẹ đi khoảng 30 lần.

### Bay quanh toà cao nhất

Nút **Bay quanh toà cao nhất** trong panel trái của trang xem 3D lấy 6 building cao nhất trong khung nhìn (theo cột chiều cao, bỏ `h_outlier`, gộp khối con vào toà mẹ, hai toà cách nhau dưới 120 m tính là một điểm dừng), rồi lần lượt bay tới từng toà và lượn quanh nó một vòng. Toà đang xem được tô như khi click; thẻ dưới bản đồ hiện tên, `building_id`, chiều cao và một skyline nhỏ của các điểm dừng. Chạy hết thì camera bay về chỗ cũ. Bấm **Dừng**, Esc hoặc chạm vào bản đồ để dừng ở chỗ đang xem.

Chỉ xét building đã tải trong khung nhìn: thu nhỏ bản đồ (vd zoom 13 với base Hà Nội) để tìm trong vùng rộng hơn. Máy bật "giảm chuyển động" (prefers-reduced-motion) thì camera nhảy thẳng tới từng toà, dừng 4 giây, không bay lượn.

### Landmark 3D trong trang xem 3D

Trang xem 3D (`/preview`) là nơi duy nhất xem và quản lý landmark: panel bên trái điều khiển bản đồ (layer, tô màu, 2D/3D, bóng, bản đồ nền, chế độ ánh sáng), bản đồ ở giữa, drawer **Landmark** bên phải. Nút **Đóng** thu drawer lại để bản đồ rộng ra, nút **Landmark** trên bản đồ mở lại. Trên màn hình hẹp, drawer là một tấm trượt từ dưới lên và panel trái tự thu gọn.

**Trên bản đồ**:

- Mọi landmark **đang dùng** của file PMTiles đang mở được vẽ cùng lúc, mỗi mô hình với hướng, tỉ lệ, điểm neo và toạ độ neo của version đang dùng. Landmark của Canonical Building Data được vẽ trên **mọi build** của canonical (theo `building_id`) và trên base OSM (theo `source_id`), xem [Landmark qua các version build](#landmark-qua-các-version-build). Landmark khác chỉ được vẽ trên file nó được đăng ký (server so đường dẫn thật, `source.path`); ở file khác nó vẫn có trong danh sách nhưng không được vẽ.
- File GLB chỉ được tải khi landmark ở trong vùng đang xem (cộng thêm 25% mỗi phía), tối đa 3 file cùng lúc, và giữ trong bộ nhớ tối đa 24 mô hình. Đổi bản đồ nền hay chế độ ánh sáng không tải lại file.
- **Đổi file theo zoom (LOD)**: version có nhiều file theo `asset.extras.zoom_range` (ví dụ Tasco z17 `[17, null]`, z16 `[16, 17]`, z15 `[15, 16]`; khoảng `min ≤ zoom < max`) thì bản đồ vẽ file của zoom đang xem, giống app Mobile. Trong lúc file mới đang tải, file cũ vẫn ở trên bản đồ nên extrusion không nháy. Ngoài mọi khoảng zoom (ví dụ zoom 14,5) landmark không được vẽ và building giữ extrusion LoD1, trừ landmark đang mở trong drawer: nó hiện file gần nhất để còn chỉnh, và drawer ghi rõ app sẽ không vẽ ở zoom đó. Version chỉ có một file không ghi `zoom_range` thì vẽ ở mọi zoom như trước.
- Extrusion của building, gồm các khối con (`parent_building_id`), chỉ bị ẩn khi file GLB đã tải xong. File lỗi (ví dụ nén Draco) thì extrusion vẫn giữ, lỗi hiện trong drawer. Bóng: landmark có version đang dùng đổ bóng theo mesh GLB của version đó (Bình minh, Ban ngày, Hoàng hôn; Ban đêm không đổ bóng), gộp chung với bóng building xung quanh nên không đậm lên ở chỗ chồng nhau. Landmark chưa có version đang dùng, hoặc file GLB không đọc được hình học (nén Draco / meshopt), giữ bóng hộp LoD1. Bóng đi theo placement đã lưu: đang kéo thử placement hay xem version khác thì bóng chưa đổi. Mỗi tam giác của GLB được đặt như mô hình trên bản đồ (heading, tỉ lệ, điểm neo), chiếu xuống mặt đất theo tia nắng rồi gộp lại. Khác bóng LoD1, bóng landmark không bị footprint của chính building đó cắt đi: mô hình thường không phủ kín footprint LoD1, nên bóng phủ cả phần đất trống quanh mô hình trong footprint (dưới thân mô hình thì bị mô hình che). Kết quả tính một lần cho mỗi (file GLB, placement, chế độ) và lưu ở `landmarks/<id>/shadows/`. Với PMTiles có sẵn layer `shadows`, bóng GLB được vẽ đè lên, bóng hộp LoD1 trong file vẫn nằm dưới.
- Click mô hình hoặc footprint của landmark để mở nó trong drawer. Click building thường để xem thuộc tính và đăng ký. Nếu click vào một khối con, drawer hỏi chọn cả building cha hay chỉ khối đó.
- **Landmark hiển thị** (panel trái, dưới *Dựng 3D*) so sánh model GLB với khối LoD1 của building trên toàn bản đồ: **GLB** (mặc định, model thay extrusion), **LOD1** (ẩn mọi model, hiện lại extrusion; bóng đổ theo hộp LoD1, server bỏ bóng GLB qua `/api/shadows?landmarks=0`, file có sẵn layer `shadows` thì ẩn lớp bóng GLB đè lên) và **Chồng** (model đặc, khối LoD1 cùng chiều cao/đáy tô màu ochre trong suốt đè lên: phần LoD1 cao/rộng hơn model nổi màu, phần model nhô khỏi hộp giữ nguyên; bóng vẫn theo GLB). Lựa chọn nằm trong URL (`lm3d=lod1` / `lm3d=both`) để gửi link so sánh.
- URL giữ landmark đang mở (`lm=<id>`) hoặc building vừa click (`id=`), cùng `f`, `name`, `idcol`, `lon`, `lat`, `z` (và `lm3d`). Tải lại trang, hoặc Back / Forward, trở về đúng file, vị trí và landmark. Ô **Building từ PMTiles** ở panel trái chuyển sang base hoặc các file đã có landmark.

**Trong drawer**:

- **Danh sách**: tìm theo tên, `building_id` hoặc ID liên kết. Landmark của file đang mở đứng đầu. Cuối danh sách có **Mở thư mục** và **Tải manifest**.
- **Đăng ký**: nhập tên và **ID liên kết**, là một id tuỳ ý để tìm building ở hệ thống khác, ví dụ canonical id của MapOps hoặc `way/695966191`. Mỗi `building_id` chỉ đăng ký được một lần. Khi đăng ký, server đọc footprint (polygon đã ghép các mảnh tile, diện tích, tâm, chiều cao) trong PMTiles ở zoom lớn nhất và lưu cùng landmark.
- **File GLB**: chọn một file `.glb`, hoặc cả bộ theo zoom (z15 / z16 / z17), để xem thử ngay trên building. File chỉ được đọc trong trình duyệt, chỉ được upload khi bấm **Upload version mới**. Có thể thử file cả trên building chưa đăng ký. Chọn nhiều file thì mỗi file phải ghi `asset.extras.zoom_range` và các khoảng không được chồng nhau. Khi upload, file chi tiết nhất thành version, các file còn lại được thêm vào version đó (`landmarks/<id>/v2_z16.glb`…) và có dòng riêng trong **Lịch sử**. Server từ chối file có khoảng zoom trùng, hoặc có anchor lệch quá 1 m so với file của version.
- **Version**: mỗi lần upload là một version. File cũ giữ nguyên, không bị ghi đè. Server đọc GLB và ghi lại số mesh, số tam giác, hộp bao, sha256, các kiểu nén mà bản đồ không đọc được (Draco, meshopt, KTX2), `asset.extras` (anchor, zoom) và số vật liệu theo `emissive_group` / kính. Bảng version hiện các file theo zoom (bấm `z16`, `z15` để tải từng file) và dòng tóm tắt như `led_warm 1 · led_wash 10 · led_white 1 · kính 4 · có anchor`. Nếu file trùng byte với một version cũ, app báo **Trùng file với vN**. Nút **Xem** đặt version đó lên bản đồ thay cho version đang dùng. Khi đóng landmark, version đang dùng trở lại.
- **Placement của từng version**:
  - **Hướng**: thanh trượt 0–360°, ô nhập số và nút **Xoay 180°**. Hướng tính bằng độ, theo chiều kim đồng hồ nhìn từ trên xuống: ở hướng 0, trục +X của GLB chỉ hướng đông và mặt trước (+Z) quay về hướng nam.
  - **Tỉ lệ** và **Điểm neo trên mô hình** (gốc GLB hoặc tâm đáy hộp bao).
  - **Toạ độ neo (lon, lat)**: chỗ điểm neo đứng trên mặt đất. Có thể nhập số, bấm **Chọn trên bản đồ**, kéo điểm ◆ trên bản đồ, bấm **Về tâm footprint**, hoặc **Đặt theo anchor của GLB** (hiện khi GLB có `asset.extras.anchor`).

  Mô hình trên bản đồ đổi ngay khi bạn chỉnh. Drawer ghi khoảng cách từ toạ độ neo tới tâm footprint, file đang vẽ ở zoom hiện tại, và so chiều cao mô hình (đã nhân tỉ lệ) với chiều cao building, cảnh báo khi lệch quá cả 2 m lẫn 5%. Bấm **Lưu placement** để lưu, **Hoàn tác** để bỏ. Version đầu tiên đặt ở tâm footprint, hướng 0. Version sau bắt đầu với placement của version đang dùng.

  GLB có `asset.extras.anchor` (pipeline landmark mới, ví dụ `Tasco_Tower_v2_shell_lit_*.glb`) đã dựng theo Bắc thật: glTF +X = Đông, −Z = Bắc, +Y = lên, gốc toạ độ = anchor. Version của nó luôn bắt đầu ở **hướng 0, tỉ lệ 1, gốc GLB tại anchor**, kể cả khi version đang dùng đã được xoay tay (ví dụ Tasco v3 dựng theo trục nhà). Drawer cảnh báo khi placement đang lệch khỏi anchor.
- **Đang dùng**: bấm **Dùng** ở một version (kèm ghi chú nếu cần) để chọn version đó, hoặc **Bỏ dùng** để không dùng version nào. Mỗi lần đổi được ghi vào **Lịch sử**, cùng các lần đăng ký, upload, lưu placement và sửa tên. Bảng version ghi trạng thái **đang dùng**, **đã dùng đến ngày…** hoặc **chưa dùng**.
- **Tải manifest** tải `landmarks-manifest.json`. File này liệt kê version đang dùng của mọi landmark, kèm đường dẫn file, sha256 và placement, để gửi team Mobile hoặc đối chiếu với MapOps. Version có file theo zoom có thêm `lods`: `[{lod_zoom, zoom_range, file, sha256, size}]`, file chi tiết nhất đứng đầu (chính là `file`). Mỗi landmark có thêm `shadows` (file GeoJSON bóng theo từng chế độ có nắng, đường dẫn tương đối trong thư mục landmark như `file`), và `shadow_presets` ở đầu file ghi màu, độ mờ và hướng nắng của từng chế độ. App vẽ các file này như layer `shadows` (thay bóng LoD1 của building đó).

Dữ liệu nằm trong thư mục `landmarks/` cạnh `output/`. Thư mục này không commit vào git. Đổi chỗ lưu bằng biến môi trường `BDS_LANDMARK_DIR`, ví dụ trỏ tới một thư mục Google Drive dùng chung với team. Mỗi landmark là một thư mục `landmarks/<id>/`, gồm `landmark.json` và `v1.glb`, `v2.glb`… (cùng `v2_z16.glb`, `v2_z15.glb` cho các file theo zoom). **Xoá landmark** chỉ chuyển thư mục đó vào `landmarks/_trash/`, không xoá file nào.

#### Landmark qua các version build

Landmark gắn với **dataset và `building_id`** của Canonical Building Data, không gắn với một file PMTiles. `building_id` là UUID của canonical, giữ nguyên qua các build, cũng là id app Mobile dùng để nhận ra landmark. Record lưu thêm `dataset` (vd `hanoi`) và `source_id` (way OSM của toà).

- **Mỗi build tìm lại toà của từng landmark**: theo `building_id`, rồi theo `source_id` OSM, cuối cùng theo footprint (IoU ≥ 0,50). Kết quả nằm trong `build.json` (`landmarks.ids`) và báo cáo.
- **Toà có landmark bị khoá**: giữ `building_id`, `resolution_mode = manual`, `decided_by = landmark`, `building_tier = T1_iconic`. Footprint nguồn đổi nhiều (IoU < 0,90 so với footprint lúc đăng ký) thì giữ footprint cũ và cảnh báo `landmark_footprint_changed`.
- **Mất toà** (way OSM bị xoá, toà bị tách…): build vẫn chạy, giữ footprint cũ của landmark với cùng `building_id` (`winning_source = landmark`), ghi cảnh báo `landmark_lost` vào báo cáo và `review_osm.gpkg`.
- **Landmark đăng ký trước đây trên base OSM** (`building_id` = `way/…`) được chuyển sang UUID ở build canonical đầu tiên có toà đó. Lịch sử ghi *Gắn với canonical*, id cũ giữ ở `source.building_id`, nên base OSM vẫn vẽ được landmark.
- **Tải manifest** ghi `building_id` (UUID), `source_id` và `dataset` của từng landmark.

**Upload landmark cho đúng**:

1. Build canonical (thẻ **Canonical Building Data**) và đặt làm đang dùng.
2. Mở **Xem bản đồ 3D**: trang mở canonical đang dùng (ô **Building từ PMTiles** ghi *canonical đang dùng*). Không đăng ký trên một file `.pmtiles` kéo thả hay trên base OSM.
3. Click toà. Nếu click trúng khối con, chọn **cả building cha** để mọi khối con cùng được ẩn dưới mô hình.
4. Nhập tên rồi bấm **Đăng ký landmark**. Ô ID liên kết để trống thì app tự điền way OSM của toà.
5. Chọn **cả bộ GLB z15 / z16 / z17** và upload trong một lần. GLB nên có `asset.extras.anchor` (đặt mô hình theo Bắc thật) và không nén Draco / meshopt / KTX2.
6. Kiểm tra hướng, tỉ lệ, toạ độ neo, bấm **Lưu placement** rồi **Dùng**.
7. Build canonical lần sau giữ landmark này. Nếu báo cáo có cảnh báo landmark, mở `review_osm.gpkg` để xem toà đó.

### Landmark dưới `light` của style

Mục **Kiểm tra ánh sáng (QA)** trong drawer giúp kiểm tra mô hình dưới cả 5 chế độ ánh sáng trước khi upload lên MapOps:

- Mô hình được tô sáng bằng đúng công thức fill-extrusion của MapLibre, với `light` của style đang hiển thị. App đọc `light` qua `map.getLight()` và đọc lại mỗi khi style đổi, nên khi chuyển chế độ ánh sáng, landmark đổi sáng cùng lúc với các building xung quanh.
- **Phát sáng theo nhóm** (handoff 03/11/2026, bản lưu ở [`docs/handoff/`](docs/handoff/HANDOFF-building-data-studio-night-lighting.md)). GLB mang màu và nhóm, app quyết định cường độ. Mỗi vật liệu đọc `extras.emissive_group`, không suy từ tên vật liệu. Vật liệu không có nhóm, hoặc `emissiveFactor` bằng 0, thì không phát sáng. Có hai cách trộn:
  - **REPLACE** (`led_warm`, `led_white`, `led_line`, `glow`, `sign*`, `logo*`, `windows*`, `lobby`, nhóm lạ): `màu = mix(màu chiếu sáng, E, gain × m)`. Bề mặt chính là đèn, nên ban đêm màu của nó là màu đèn. Line LED Tasco giờ vàng ấm, không còn bị cộng thành trắng.
  - **ADD** (`led_wash`): `màu = màu chiếu sáng + E × gain`. Đây là ánh LED hắt lên tường, bake sẵn bằng Cycles thành lightmap JPEG ở `TEXCOORD_1` của các vật liệu `*_BAKED`. Ban ngày gain = 0 nên vật liệu `*_BAKED` trông y như vật liệu gốc.

  `E` = `emissiveFactor` (đổi từ linear sang sRGB, `[1, 0,8, 0,5]` → `[1, 0,906, 0,735]`) × `emissiveTexture`. `m` = độ phủ của mask. Gain theo preset × nhóm nằm trong bảng `EMISSIVE_GAIN`: LED dawn 0,4 · day 0 · dusk 0,9 · night 1; `led_wash` 0,3 · 0 · 0,8 · 1; `lobby` 0,3 · 0 · 0,6 · 0,8; `windows*` 0; nhóm khác 0,3 · 0 · 0,6 · 1. Gain nhân thêm `extras.emissive_strength` nếu có. Preset đọc từ `metadata["tasco:light-preset"]`.
- **Kính** (`extras.surface_type: "glass"`): phản chiếu sơ bộ trong shader (Fresnel Schlick F0 0,04 về một dải màu trời theo preset). Không cần cubemap, không thêm lượt vẽ. Phản chiếu night 0,9 · dusk 0,7 · dawn 0,6 · day 0, tức ban ngày tắt để toà nhà giống SketchUp. Vị trí camera tính từ nghịch đảo ma trận model-view-projection của từng model.
- Để QA, ô **So sánh** hiện extrusion gốc của building ngay bên cạnh landmark (đục, màu so sánh). Ô **Tô landmark một màu** thay texture bằng chính màu đó và tắt phát sáng lẫn kính: khi ấy mái, và các mặt tường quay cùng hướng, của landmark và extrusion phải sáng tối như nhau. Mục này còn ghi gain của preset hiện tại, màu mong đợi của mái và tường 4 hướng, cùng các uniform ánh sáng để đối chiếu khi port.

#### Thuộc tính GLB cho Mobile

Mục **Thuộc tính GLB cho Mobile** trong drawer liệt kê mọi giá trị trong GLB mà renderer của studio đang đọc. Mỗi dòng có quy tắc phải port, kèm giá trị của GLB đang xem. Mục này còn có bảng gain theo preset, màu trời của kính, danh sách vật liệu (nhóm, cách trộn, gain, `surface_type`, `texCoord`) và những thứ cố ý không đọc. Nút **Sao chép (Markdown)** chép cả danh sách để gửi team Mobile. Danh sách gốc là `GLB_CONTRACT` trong `landmark-light.js`:

| Thuộc tính trong GLB | Dùng để |
|---|---|
| `asset.extras.anchor {lat, lon}` | Đặt model: góc 0, gốc GLB tại anchor |
| `asset.extras.lod_zoom`, `zoom_range [min, max\|null]` | Đổi file theo zoom (min ≤ zoom < max) |
| `materials[].extras.emissive_group` | Cách trộn (REPLACE / ADD) và gain theo preset |
| `materials[].extras.emissive_strength` | Nhân vào gain (thiếu thì dùng `KHR_materials_emissive_strength`, rồi 1) |
| `materials[].emissiveFactor` | Màu E (linear, đổi sang sRGB) |
| `materials[].emissiveTexture {index, texCoord}` | Lightmap / mask, giữ `texCoord` |
| `TEXCOORD_1` | UV của lightmap `led_wash` |
| `materials[].extras.surface_type` | `"glass"`: kính phản chiếu |
| `baseColorFactor × COLOR_0 × baseColorTexture` | Màu nền, chiếu sáng như fill-extrusion |
| `NORMAL`, `alphaMode MASK`, `doubleSided`, `KHR_texture_transform` | Ánh sáng, Fresnel, cắt alpha, hai mặt, UV |

Go baker của Mobile phải giữ `TEXCOORD_1`, `emissiveTexture` + `texCoord`, ảnh JPEG, `materials[].extras` và `asset.extras`. Nếu baker bỏ `extras`, cần một chỗ khác mang `emissive_group` và `surface_type`.

Công thức, shader và ghi chú cho Mobile (Android / iOS) nằm chung trong [`app/static/landmark-light.js`](app/static/landmark-light.js). MapOps web và renderer LMK3 của app Mobile port nguyên công thức này. `tests/test_landmark.py` kiểm tra công thức với chính code của MapLibre, với màu pixel MapLibre vẽ ra ở cả 5 chế độ, và chạy file JS bằng Node để so với bản port Python. Ba điều dễ sai khi port:

- Mọi phép tính làm trên giá trị sRGB thô (0..1), như màu viết trong style: không đổi sang linear, không tone mapping. Texture phải upload dạng RGBA8 thường, không dùng sRGB texture.
- Vector light dài bằng `radial` (1,15), không chuẩn hoá. Tính theo hướng đông-bắc-lên với pháp tuyến hướng ra ngoài, nó là `r·(sin a·sin p, cos a·sin p, cos p)`: mặt tường quay về phía phương vị `a` sáng nhất. Trong code MapLibre có hai chỗ ngược dấu triệt tiêu nhau: phương vị cộng 90° trong hệ toạ độ tile, và pháp tuyến tường hướng vào trong.
- Ở chế độ Ban đêm (`light` màu `#3f4455`), MapLibre kẹp gần như mọi mặt về mức sàn `0,3 × (1 − màu light)`. Building nào cũng thành một màu tối, texture của landmark cũng vậy; chỉ emissive và kính làm landmark khác đi.
- Emissive theo nhóm: `emissiveFactor` là linear, phải đổi sang sRGB trước khi trộn. Lightmap `led_wash` phải lấy bằng `TEXCOORD_1`, nếu không quầng sáng sẽ lặp thành sọc theo tile của ảnh màu nền.

## 9. So sánh 2 file trước release

Mở tab **So sánh 2 file**, chọn **A** (bản đang chạy production) và **B** (bản sắp release), rồi bấm **So sánh**. A và B nhận mọi định dạng app hỗ trợ (`.parquet`, `.gpkg`, `.fgb`, `.geojson`, `.pmtiles`), và hai file có thể khác định dạng nhau, ví dụ A là PMTiles đang serve, B là Parquet mới.

```
A, B ─ ogr2ogr ─► FlatGeobuf ─ ogr2ogr + SpatiaLite ─► Parquet (+ diện tích, tâm, bbox, hash WKB)
                                                        └─ DuckDB: khớp, so sánh, chất lượng, lưới ─► release gate
```

### Khớp building

| Cách khớp | Khi nào dùng |
|---|---|
| **Theo ID** (mặc định khi A và B có chung cột ID, vd `building_id`) | Canonical ↔ canonical. Nếu ID không ổn định, báo cáo đếm số cặp “xoá + thêm” nằm cùng vị trí hoặc cùng `source_id` |
| **Theo vị trí** (mặc định khi không có ID chung) | Hai pipeline khác nhau, vd vmap → canonical. Building được ghép khi tâm gần nhau (3–50 m tuỳ kích thước) và diện tích lệch không quá 2 lần. Số liệu là ước lượng |

**PMTiles**: app đọc tile ở zoom lớn nhất (bằng GDAL, có bật `JSON_FIELD` để nhanh hơn khoảng 15 lần với schema nhiều tag OSM). Building bị cắt qua biên tile được **ghép lại** theo ID. Nếu không có ID, app ghép theo thuộc tính giống nhau cộng với đoạn nằm trên biên tile trùng nhau, nên nhà liền kề chỉ chạm nhau ở biên sẽ không bị gộp. Geometry đọc từ tile đã bị lượng tử hoá, vì vậy chênh lệch ≤ 5 % diện tích và ≤ 1 m được xem là “khác nhỏ” (nhiễu), không tính là thay đổi.

### 7 nhóm kiểm tra

1. **Tổng quan**: số building, dung lượng, CRS, kiểu geometry, extent, layer và zoom (PMTiles), `build_id`, `config_version`.
2. **Schema**: field B thiếu (kèm tỉ lệ A có dữ liệu), field mới, field đổi kiểu, tỉ lệ null thay đổi.
3. **Khớp**: khớp / thêm mới / bị xoá, ID trùng, ID rỗng, building đổi ID.
4. **Building khớp có thay đổi**: geometry (diện tích lệch %, tâm dịch m), chiều cao (Δh, mặc định ↔ có số đo), số building đổi theo từng cột.
5. **Chất lượng A và B**: % có chiều cao thật, outlier, phân bố provenance và tier, khối con mồ côi, `superseded_by` trỏ tới ID không tồn tại.
6. **Theo khu vực**: lưới (mặc định 0,05° ≈ 5,5 km) đếm A / B / thêm / xoá / đổi cho từng ô, và bản đồ diff: lam = thêm (`cat-1`), đỏ = xoá (`map-outlier`), hoàng thổ = đổi (`cat-2`), màu lấy từ token của design system.
7. **Release gate**: so với các ngưỡng bên dưới.

### Release gate (mặc định, chỉnh được trên UI)

| Mức | Điều kiện |
|---|---|
| **Fail** | B thiếu field bắt buộc, hoặc field đổi kiểu không tương thích · ID trùng / rỗng (khi khớp theo ID) · geometry NULL / rỗng · CRS hoặc kiểu geometry khác A · đổi tên layer PMTiles · số building lệch > ±10 % · bị xoá > 5 % của A |
| **Warn** | Số building lệch > ±2 % · bị xoá > 1 % · > 2 % building khớp đổi geometry đáng kể (diện tích > 20 % hoặc tâm dịch > 10 m) · % có chiều cao thật giảm > 1 điểm · outlier tăng · tỉ lệ null của 1 cột tăng > 5 điểm · khối con mồ côi / `superseded_by` treo tăng · thiếu field không bắt buộc · zoom PMTiles đổi |
| **Pass** | Không vi phạm ngưỡng nào |

- **Field bắt buộc**: mặc định là mọi field của A. Khi đổi schema (vd vmap có 690 tag OSM → canonical 20 field), hãy bỏ chọn các field mà app mobile không dùng. Khi đó thiếu field không bắt buộc chỉ là Warn.
- **Bỏ qua khi xét “có thay đổi”**: các cột đổi theo mỗi lần build (`build_id`, `updated_at`, `description`…) được bỏ qua sẵn. Khi khớp theo vị trí, mọi cột chung đều bị bỏ qua (A và B khác pipeline nên cùng tên cột chưa chắc cùng nghĩa). Cột bị bỏ qua vẫn được đếm trong báo cáo.

### Output

Mỗi lần so sánh tạo thư mục `output/diff__<B>__vs__<A>__<ngày-giờ>/`:

| File | Nội dung |
|---|---|
| `<tên>.gpkg` | Layer `added` (geometry B), `removed` (geometry A), `changed` (geometry B) và `changed_before` (geometry A, để đối chiếu), `grid`. Cột `diff_*` ghi lý do: `diff_geom`, `diff_area_pct`, `diff_shift_m`, `diff_h_a`, `diff_h_b`, `diff_dh`, `diff_cols` |
| `<tên>.pmtiles` + `diff_grid.geojson` | Dữ liệu cho bản đồ diff trong app (nút **Xem bản đồ diff**, hoặc nút **Xem** ở từng dòng của báo cáo) |
| `diff_report.md` / `diff_report.json` | Báo cáo đầy đủ: kết luận gate, số liệu 7 nhóm, ngưỡng, lệnh đã chạy |
| `log.txt` | Log đầy đủ |

Trên QGIS: mở `changed` và `changed_before` cùng lúc để thấy hình dạng cũ và mới; lọc `"diff_geom" = 'major'` hoặc `abs("diff_dh") > 3`.

Thời gian chạy trên máy dev: canonical v1 (369 nghìn) ↔ v2 (1,2 triệu) khoảng 50 giây; PMTiles vmap (1,27 triệu, z17) ↔ canonical v2 khoảng 50 giây.

## 10. Indoor map (PoC) theo IndoorGML 2.0

Trang `/indoor` (nút **Indoor map (PoC)** trên trang chính) là tool vẽ bản đồ trong nhà cho các toà VMAP chọn làm indoor (TTTM, siêu thị). Dữ liệu theo mô hình [OGC IndoorGML 2.0](https://docs.ogc.org/is/22-045r5/22-045r5.html):

| Trên editor | IndoorGML 2.0 |
|---|---|
| Không gian (phòng, gian hàng, hành lang, thang…) | **CellSpace** trong **PrimalSpaceLayer**; loại đi được ghi lớp Navigation: `GeneralSpace` (gian hàng, WC, bãi xe), `TransitionSpace` (hành lang, thang máy, thang bộ, thang cuốn), `AnchorSpace` (lối vào, nối ra ngoài trời); giếng trời / kết cấu là `CellSpace` thường (không đi được) |
| Cửa / lối mở | **CellBoundary** (`ConnectionBoundary`) nằm trên bức tường chung của hai không gian; lối mở không cánh ghi `isVirtual = true` |
| Tự sinh, không vẽ tay | **DualSpaceLayer** (Node-Relation Graph): mỗi không gian đi được ↔ một **Node**, mỗi cửa nối hai không gian ↔ một **Edge**, các thang cùng *nhóm thang* (`vgroup`) ở hai tầng liền nhau ↔ một **Edge** đứng. Quan hệ `duality` hai chiều giữa Cell ↔ Node và Boundary ↔ Edge |
| Tầng | thuộc tính `level` của CellSpace; tầng có thứ tự (`ordinal`, 0 = tầng trệt, âm = hầm), cao độ sàn và chiều cao |

**Quy trình demo** (3 phút):

1. Zoom tới một TTTM trên base Hà Nội, bấm **Chọn toà từ base** rồi click toà: outline, tên và `building_id` lấy từ base (toà có `num_floors` thì tự tạo đủ số tầng). Chưa có base thì **Vẽ outline** bằng tay.
2. **Tạo layout mẫu**: sinh một TTTM 4 tầng (hầm xe B1, tầng 1 có siêu thị và 2 lối vào, tầng 2, tầng 3 ăn uống) quanh một hành lang, có 2 lõi thang máy + thang bộ + WC và thang cuốn giữa sảnh. Layout tự co lại cho vừa trong outline; toà lớn thì đặt một khối ≤ 160 × 64 m, phần còn lại vẽ tiếp.
3. Tab **Tìm đường**: chọn *Từ* bãi xe B1, *Đến* một quán ở tầng 3 → đường đi qua các cửa và thang, các bước chỉ đường, độ dài. Tick **Tránh thang bộ và thang cuốn** để thấy tuyến đổi sang thang máy (xe lăn, xe đẩy). Thanh chọn tầng bên phải đánh dấu các tầng tuyến đi qua.
4. **3D xếp tầng**: các tầng chồng theo cao độ (kéo *Giãn tầng* để tách ra), tuyến đường là dải tối trên từng tầng và cột ở chỗ đi thang.
5. Tab **Kiểm tra** và nút xuất **IndoorGML (.gml)** / **IndoorGML JSON** / **GeoJSON (tiles)**.

**Vẽ** (thanh công cụ trên bản đồ, phím tắt trong ngoặc): **Chọn** (V) để chọn, kéo đỉnh, kéo điểm giữa cạnh để thêm đỉnh, Alt+click đỉnh để xoá đỉnh, kéo cả hình, Del để xoá; **Đa giác** (P) và **Chữ nhật** (R, theo trục dài của toà nhà) vẽ không gian loại đang chọn ở ô bên cạnh; **Cửa** (D) click sát tường, cửa nối mọi không gian chung bức tường đó; **POI** (I). Con trỏ bắt vào đỉnh rồi cạnh có sẵn để các phòng khít tường nhau; Shift khoá hướng theo trục toà mỗi 45°, Alt tắt bắt điểm. Ctrl+Z / Ctrl+Shift+Z hoàn tác / làm lại, PageUp / PageDown đổi tầng.

**Tầng**: **+ Trên** / **+ Dưới** thêm tầng và copy sẵn thang máy, thang bộ, thang cuốn (cùng hình, cùng cửa, cùng nhóm thang) của tầng đang chọn để các tầng thẳng hàng; **Nhân bản** copy toàn bộ tầng. Với một thang, drawer bên phải có các ô tick tầng để thêm / bỏ thang đó ở tầng khác. Tầng dưới hiện mờ (nét đứt) phía sau tầng đang vẽ để căn thẳng.

**Ảnh mặt bằng**: chọn ảnh bản vẽ của tầng (PNG / JPG), **Vừa outline** đặt ảnh theo trục toà, chỉnh độ mờ, góc xoay, độ rộng (m), **Kéo ảnh** để dịch; rồi vẽ đè lên ảnh (số hoá). Ảnh chỉ lưu trong trình duyệt.

**Kiểm tra** trước khi xuất: không gian nằm ngoài outline, hai không gian chồng nhau, polygon tự cắt hoặc quá nhỏ, cửa không nằm trên tường nào, cửa chỉ chạm một không gian (cửa ra ngoài), thang chưa nối sang tầng khác, gian hàng chưa có tên, không gian không đi tới được từ lối vào. Click một dòng để tới đối tượng.

**Tìm đường**: Dijkstra trên đồ thị dual space. Trọng số là mét đi bộ (tâm không gian → cửa → tâm không gian kế tiếp); đi thang quy đổi theo chiều cao: thang máy 25 m chờ + 1 m mỗi mét cao, thang cuốn 2,5 m/m, thang bộ 4 m/m.

**Lưu và xuất**: project tự lưu trong trình duyệt (localStorage); **Lưu project** / **Mở project…** để chuyển máy hoặc gửi người khác (`*.indoor-project.json`, schema `bds-indoor/1`). Ba định dạng xuất:

- `*.indoorgml.gml`: IndoorGML 2.0 GML (namespace `core` / `navi`, toạ độ EPSG:4326 thứ tự lat lon). **Bản nháp**: viết tay theo mô hình khái niệm, chưa validate với XSD chính thức.
- `*.indoorgml.json`: cùng cấu trúc `IndoorFeatures > ThematicLayer > PrimalSpaceLayer / DualSpaceLayer` dạng JSON, geometry GeoJSON.
- `*.indoor.geojson`: GeoJSON phẳng cho tile, mỗi feature có `feature_type` (`venue`, `level`, `unit`, `opening`, `poi`, `node`, `edge`) cùng `level_id`, `ordinal`. Đưa vào tippecanoe ra PMTiles, hoặc nạp PostGIS rồi serve bằng Martin; app Mobile lọc theo `ordinal` để hiện từng tầng:

  ```bash
  tippecanoe -o indoor.pmtiles -l indoor -Z15 -z22 --no-feature-limit --no-tile-size-limit toa-nha.indoor.geojson
  ```

Giới hạn của PoC: không gian là polygon không lỗ (giếng trời vẽ thành không gian riêng), một ThematicLayer (Physical), chưa có InterLayerConnection, chưa có cửa một chiều, chưa có backend lưu chung (mỗi người một project trong trình duyệt).

## 11. Lưu ý

- **Bản đồ nền** trong trang xem 3D lấy từ VMAP / OpenFreeMap / OpenStreetMap, nên cần internet. Khi offline thì chọn *Không nền*, building vẫn hiển thị bình thường. Lựa chọn nền và chế độ ánh sáng được nhớ trong trình duyệt.
- File kéo thả được copy vào `uploads/` và **không tự xoá**. Hãy dọn thư mục này khi cần.
- Lịch sử chạy trong app chỉ giữ trong phiên hiện tại. File output vẫn còn trên đĩa sau khi tắt app.
- Tuỳ chọn chung (zoom, khoảng hợp lệ, thư mục output…) được nhớ trong trình duyệt. Nút **Khôi phục mặc định** đưa chúng về như ban đầu.

## 12. Lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| Chip `Thiếu ogr2ogr/tippecanoe` đỏ | macOS: `brew install gdal tippecanoe`. Linux / WSL: tạo env `gdal` như [mục 1](#linux--wsl-ubuntu-2404). Rồi chạy lại `./run.sh` |
| `Unable to find driver 'Parquet'` | GDAL đang dùng không có driver Parquet (thường là GDAL của `apt`). Linux / WSL: tạo env `gdal` như [mục 1](#linux--wsl-ubuntu-2404) |
| “File không khai báo CRS — hãy nhập CRS nguồn” | Nhập CRS ở ô **CRS nguồn**, ví dụ `EPSG:4326` |
| “Không tìm thấy cột geometry” | File Parquet thường: chọn cột WKB/WKT ở *Nguồn dữ liệu* |
| Job lỗi giữa chừng | Mở **Log chi tiết**. File trung gian được giữ trong `_work/` của thư mục output |
| Cổng 8765 đang bận | `./run.sh --port 9000` |
| Excel: “Overpass trả lỗi HTTP 429/504” hoặc “Không tải được hình học từ Overpass” | Máy chủ Overpass đang bận hoặc không có mạng: đợi vài phút rồi bấm lại **Tạo dữ liệu bản đồ**, hoặc đặt `BDS_OVERPASS_URL` sang máy chủ khác |
| Thẻ Base báo lỗi `Không tải được extension DuckDB` | Máy không vào được extensions.duckdb.org: `.venv/bin/pip install duckdb-extension-spatial duckdb-extension-httpfs` (cùng version với `duckdb`), rồi bấm **Tạo base** |
| Thẻ Base báo lỗi `Không đọc được danh sách release Overture` / lỗi HTTP khi tải building | Kiểm tra internet / VPN (app đọc `overturemaps-us-west-2.s3.amazonaws.com`), rồi bấm **Tạo base**. Có thể đặt cố định `BDS_OVERTURE_RELEASE=2026-10-21.0` |
| Trang 3D báo “Chưa có base Hà Nội” | Base chưa tạo, hoặc đã bị xoá khỏi `base/hanoi/`: bấm **Tạo base** ở trang chính |
| Không muốn app tự tạo base khi khởi động | `./run.sh --no-base` hoặc `BDS_BASE_AUTOBUILD=0` |
| Landmark có trong danh sách nhưng không hiện trên bản đồ | Landmark đăng ký trên file PMTiles khác và chưa gắn với canonical (drawer ghi *PMTiles khác*: build canonical một lần để gắn), chưa có version **đang dùng**, hoặc file GLB lỗi (drawer ghi lỗi, ví dụ nén Draco: xuất lại GLB không nén) |
| Build canonical báo lỗi *Invariant không đạt* | Xem dòng lỗi trong log: build không được lưu, bản đang dùng giữ nguyên. Thường do dữ liệu nguồn lỗi; mở `_work/` của build đó để xem |
| Build canonical tải Google Open Buildings bị ngắt | Bấm build lại: file đang tải dở (`.part`) được tải tiếp, không tải lại từ đầu |

## 13. Phát triển

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
node --test "tests/js/*.test.mjs"   # riêng phần JS (pytest cũng chạy, nếu máy có Node)
BC_SAMPLE_PARQUET=/đường/dẫn/file.parquet .venv/bin/python -m pytest -q -k real_sample   # chạy thêm trên file thật
BC_DIFF_A=/đường/dẫn/A BC_DIFF_B=/đường/dẫn/B .venv/bin/python -m pytest -q -k real_files  # so sánh 2 file thật
```

| Đường dẫn | Vai trò |
|---|---|
| `app/server.py` | HTTP API + phục vụ giao diện (Starlette/uvicorn) |
| `app/probe.py` | Đọc file đầu vào, nhận diện cột geometry / CRS / cột chiều cao |
| `app/sqlbuild.py` | Sinh SQL tính `height_m` (và `h_raw`), `h_src`, `h_outlier`, `has_parts` |
| `app/tileschema.py` | Schema PMTiles (`schemas/`): đọc file schema, sinh SQL ép kiểu từng field, kiểm tra `vector_layers` của PMTiles sau khi cắt tile |
| `app/pipeline.py` | Các bước convert, đối soát, ghi báo cáo |
| `app/report.py` | Thống kê chiều cao từ GeoPackage |
| `app/diff.py` | Job so sánh 2 file: đọc A/B bằng GDAL, gọi engine, xuất GPKG / PMTiles / báo cáo |
| `app/diffcore.py` | Engine so sánh bằng DuckDB: ghép mảnh tile, khớp theo ID / vị trí, thay đổi, chất lượng, lưới |
| `app/diffgate.py` | Release gate (ngưỡng Pass / Warn / Fail), so sánh schema, `diff_report.md` |
| `app/jobs.py` | Hàng đợi job (chạy lần lượt từng job), huỷ job |
| `app/vmap.py` | Nền VMAP cho trang xem 3D: proxy style / sprite / glyphs từ assets.vmaps.vn, 5 chế độ ánh sáng (mỗi chế độ một style streets-v2), màu building 3D ước lượng |
| `app/shadows.py` | Đổ bóng vẽ sẵn: tính bóng building theo hướng nắng của từng chế độ, cho một vùng (trang xem 3D) hoặc cả GeoPackage (layer `shadows` của PMTiles) |
| `app/landmark_shadow.py` | Bóng mặt đất của landmark tính từ mesh GLB của version đang dùng, theo từng chế độ có nắng; lưu ở `landmarks/<id>/shadows/` |
| `app/landmark.py` | Neo landmark trong trang xem 3D: footprint của một `building_id` trong PMTiles (ghép mảnh tile, tâm, diện tích, chiều cao) |
| `app/landmark_library.py` | Thư viện landmark: version GLB, placement của từng version, version đang dùng và lịch sử (thư mục `landmarks/`), landmark đang dùng của một file PMTiles |
| `app/glb.py` | Đọc GLB không cần thư viện 3D: số mesh / tam giác, hộp bao, kiểu nén |
| `app/osmdirect.py`, `app/osmtags.py`, `app/osmgeom.py` | Base lấy thẳng từ OSM (Overpass hoặc file `.osm.pbf`): chuẩn hoá height / `building:part`, ghép multipolygon, cùng `base.json` với `osmbase` |
| `app/osmbase.py` | Base Hà Nội: ranh giới và building OSM từ Overture (DuckDB), chạy qua pipeline convert, ghi `base/hanoi/base.json` |
| `app/canonical.py` | Canonical Building Data: các bước ingest → normalize → matching → conflation → build, snapshot nguồn, `canonical.json`, rollback, landmark, CLI |
| `app/conflation.py` | Thuật toán conflation (DuckDB + union-find): normalize, overlap graph, component, footprint thắng, chiều cao, danh sách review; tham số trong `CONFIGS` |
| `app/idregistry.py` | `building_id` ổn định qua các build (ID registry, UUIDv7, giữ id của file production) |
| `app/buildingtier.py` | `building_tier` theo [B-05], mỗi phiên bản luật một hàm (`RULES`) |
| `app/gob.py`, `app/s2cell.py` | Tải Google Open Buildings v3 theo ô S2 (tính token S2 không cần thư viện), snapshot GeoParquet |
| `config/building_tier/` | Pin của danh sách "3D Building Landmark" cho luật tier 1 |
| `map-styles/` | Các style.json app Mobile đang dùng (bản lưu để đối chiếu, xem `map-styles/README.md`) |
| `schemas/` | Schema PMTiles đã thống nhất với VMAP (`vmap-buildings.json`): layer, zoom, field và kiểu |
| `app/static/` | Giao diện (HTML/CSS/JS thuần): `app.js` (convert), `diff.js` (so sánh), `util.js`; trang bản đồ 3D / diff (`preview.js`); `basemap.js` (bản đồ nền và chế độ ánh sáng); landmark trong trang xem 3D: `landmark-workspace.js` (state, thao tác, URL), `landmark-drawer.js` (giao diện drawer), `landmark-store.js` (tải GLB theo vùng nhìn, cache, filter extrusion; không phụ thuộc trình duyệt, test bằng Node), `landmark-model.js` (vẽ nhiều GLB trong một custom layer) và `landmark-light.js` (công thức ánh sáng landmark, để các team khác port); `building-edges.js` (ô **Edge**: viền mái, cạnh đứng, chân tường của từng building như Edge trong SketchUp, một custom layer WebGL trên extrusion, từ z15); `tour.js` (bay quanh toà cao nhất: chọn điểm dừng, camera; test bằng Node); indoor map PoC: `indoor.js` (editor, bản đồ, drawer) và `indoor-model.js` (mô hình IndoorGML, đồ thị, tìm đường, kiểm tra, xuất file; không phụ thuộc trình duyệt, test bằng Node); MapLibre + pmtiles.js + three.js 0.186.1 (vendored, chế độ diff không tải three.js) |
| `tests/` | Unit test + end-to-end (cần GDAL & tippecanoe); `tests/js/` chạy bằng Node |
