# Building Data Studio

App chạy local (trên trình duyệt) để chuyển dữ liệu building từ **Parquet / GeoPackage** (hoặc một sheet **Excel**) sang:

- **GeoPackage (.gpkg)**: mở bằng QGIS để kiểm tra chiều cao của khối building trong khu vực
- **PMTiles (.pmtiles)**: publish và serve cho app mobile (MapLibre, dựng 3D bằng `fill-extrusion`)

Có giao diện kéo thả, nút tải file và nút mở thư mục output. App tự sinh báo cáo chiều cao và có trang xem bản đồ 3D.

Tab **So sánh 2 file** so sánh bản production (A) với bản sắp release (B) và đưa ra kết luận **Pass / Warn / Fail** trước khi release (xem [mục 9](#9-so-sánh-2-file-trước-release)).

Trang xem 3D (`/preview`) hiện building LoD1 cùng mọi **landmark 3D** (file GLB) đang dùng trên file PMTiles đó. Drawer **Landmark** bên phải quản lý file GLB của từng building theo `building_id` và footprint, cùng các version đã dùng. Mỗi version có hướng và toạ độ neo riêng (xem [Landmark 3D trong trang xem 3D](#landmark-3d-trong-trang-xem-3d)).

**Base Hà Nội**: building OpenStreetMap của toàn thành phố, app tự tạo một lần. Trang xem 3D mở thẳng base này, không cần upload hay convert file (xem [Base Hà Nội](#base-hà-nội-building-osm-toàn-thành-phố)).

```
.xlsx (một sheet) ── cột WKT / GeoJSON, hoặc way ID OSM ── Overpass + buffer ──► GeoPackage dựng sẵn ─┐
                                                                                                    ▼
input (.parquet / .gpkg / .fgb / .geojson)
  └─ ogr2ogr + SQL ──► GeoPackage chuẩn hoá (EPSG:4326, h_m, h_src, h_outlier)
       ├─ ogrinfo SQL ──► has_parts / is_part (toà nhà nhiều khối)
       ├─ ogr2ogr ──► GeoPackage ở CRS khác (tuỳ chọn, vd VN-2000 / UTM)
       ├─ báo cáo chiều cao ──► report.md / report.json
       └─ FlatGeobuf (tạm) ── tippecanoe ──► PMTiles ── pmtiles verify
```

---

## 1. Cài đặt (làm một lần)

```bash
brew install gdal tippecanoe pmtiles
```

Cần **GDAL ≥ 3.8** (để có driver Parquet), **tippecanoe của felt** (ghi thẳng ra `.pmtiles`) và Python ≥ 3.11. Lần chạy đầu, `run.sh` tự tạo `.venv` và cài các dependency đã pin trong `requirements.txt`.

## 2. Chạy app

```bash
cd ~/dev/building-data-studio
./run.sh
```

Hoặc double-click file **`Building Data Studio.command`** trong Finder. Trình duyệt sẽ tự mở http://127.0.0.1:8765. Có thể đổi cổng bằng `./run.sh --port 9000`. Bấm `Ctrl+C` trong terminal để dừng.

Server chỉ lắng nghe trên `127.0.0.1`, máy khác trong mạng không truy cập được.

### Demo nhanh: 3D building Hà Nội, không cần file

1. Chạy `./run.sh`. Lần đầu, terminal báo `Chưa có base Hà Nội: đang tạo…`. Thẻ **Base Hà Nội** ở đầu trang chính hiện từng bước. Đợi khoảng 3–5 phút, cần internet.
2. Khi thẻ hiện số building (vd *212.373 building + 1.517 khối con*), bấm **Xem bản đồ 3D**, hoặc mở thẳng http://127.0.0.1:8765/preview. Bản đồ mở ở Hồ Gươm, zoom 15,5.
   - Click một building: drawer **Landmark** bên phải hiện thuộc tính của nó: `building_id` (id OSM, vd `way/1327061631`), `name`, `h_m`, `h_src`.
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
- **Tải GeoPackage** tải `hanoi_osm_buildings.gpkg` để xem trên QGIS. File có layer `buildings` (đã qua pipeline convert: `h_m`, `h_src`, `h_outlier`, `has_parts`, `is_part`) và layer `boundary` (ranh giới thành phố).
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
| `height`, `num_floors` | chiều cao (m) và số tầng từ tag OSM. `h_m` lấy `height`, rồi `num_floors × 3,5 m`, rồi 4 m |
| `min_height`, `min_floor` | đáy khối (m) / tầng bắt đầu. `min_height` trống thì tính `min_floor × 3,5 m` |
| `roof_*`, `facade_*` | mái và mặt ngoài (hình dạng, màu, vật liệu) từ tag OSM |
| `osm_version`, `osm_timestamp`, `overture_id` | phiên bản và thời điểm sửa cuối của way / relation trên OSM, GERS id của Overture |

Building ngầm (`is_underground`) bị bỏ. Một building được giữ khi chạm ranh giới; một khối con được giữ khi building cha của nó được giữ.

Số liệu lần tạo đầu (Overture `2026-10-21.0`, ảnh chụp OSM ngày 2026-09-12): **212.373 building + 1.517 khối con** trên 3.354 km². Chỉ **5,4%** có chiều cao thật (`height` hoặc số tầng), còn lại dùng 4 m mặc định. GeoPackage nặng 81 MB, PMTiles 20 MB. Để so sánh, Overture có tổng cộng khoảng 4,2 triệu building trong khung bao Hà Nội; phần lớn là footprint Google / Microsoft nhận diện bằng AI.

Dữ liệu nằm trong `base/hanoi/` cạnh `output/` và không commit vào git. Mỗi lần tạo là một thư mục mới; `base/hanoi/base.json` trỏ tới bản đang dùng. Các tuỳ chọn:

| Cách | Tác dụng |
|---|---|
| `./run.sh --no-base` hoặc `BDS_BASE_AUTOBUILD=0` | Không tự tạo base khi khởi động (vẫn bấm **Tạo base** được) |
| `BDS_BASE_DIR=/đường/dẫn` | Đổi chỗ lưu base, ví dụ thư mục dùng chung với team |
| `BDS_OVERTURE_RELEASE=2026-10-21.0` | Dùng một release Overture cố định thay vì bản mới nhất |
| `.venv/bin/python -m app.osmbase hanoi [--release …]` | Tạo base từ terminal, không cần mở app |

Lần đầu, DuckDB tự tải extension `spatial` và `httpfs` từ extensions.duckdb.org. Nếu máy không vào được địa chỉ đó, cài bằng pip: `.venv/bin/pip install duckdb-extension-spatial duckdb-extension-httpfs`. Hai gói này phải cùng version với `duckdb`.

Dữ liệu OSM theo giấy phép ODbL. PMTiles của base ghi sẵn attribution `© OpenStreetMap contributors (ODbL) · Overture Maps <release>`; giữ dòng này khi đưa base lên app.

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

## 4. Chiều cao `h_m`

Mỗi building lấy **giá trị hợp lệ đầu tiên** theo thứ tự:

| Ưu tiên | Nguồn | `h_src` |
|---|---|---|
| 1 | Cột chiều cao, nếu nằm trong khoảng hợp lệ (mặc định 2–500 m) | `height` |
| 2 | Số tầng × mét/tầng (mặc định 3,5 m; số tầng 1–200) | `levels` |
| 3 | Chiều cao mặc định (mặc định 4 m) | `default` |

- **Cột nguồn chiều cao** (vd `height_provenance`): các dòng có giá trị trong *“Giá trị = không có số liệu thật”* (mặc định `default`) được xem là **không có số đo**. Như vậy báo cáo phân biệt được chiều cao thật với chiều cao mặc định.
- **`h_outlier = 1`** khi giá trị gốc nằm ngoài khoảng hợp lệ, ví dụ 0 m hoặc 49.380 m. Phần *Giá trị ngoài khoảng hợp lệ* có 3 cách xử lý:
  - *Bỏ qua* (khuyên dùng): dùng số tầng hoặc giá trị mặc định
  - *Kẹp về max*
  - *Giữ nguyên*: chỉ đánh dấu
- Muốn chỉ convert mà không tính `h_m` thì bỏ tick **Chiều cao h_m**.

## 5. Các cột app thêm vào

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| `h_m` | real | Chiều cao dùng để dựng 3D (m) |
| `h_src` | text | Nguồn của `h_m`: `height` / `levels` / `clamped` / `default` |
| `h_outlier` | int 0/1 | 1 = giá trị gốc bất thường, cần kiểm tra |
| `has_parts` | boolean | Outline có khối con trỏ về (nên **ẩn** khi dựng 3D) |
| `is_part` | boolean | Là khối con (có `parent_building_id` khác chính nó) |

Mọi cột gốc được giữ nguyên trong GeoPackage, trừ khi bạn bỏ tick. PMTiles chỉ chứa các thuộc tính được chọn, mặc định là: `building_id, h_m, min_height, has_parts, is_part, parent_building_id, building_tier`.

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
| Building cao trên 100 m | `"h_m" > 100` |
| Toàn bộ khối của một toà nhà | `"parent_building_id" = '<id>' OR "building_id" = '<id>'` |

Để xem 3D: **Layer Properties → 3D View → Single Symbol**, ở ô **Extrusion** bấm nút data-defined và đặt biểu thức `"h_m"`. Sau đó mở **View → 3D Map Views → New 3D Map View**.

## 8. Dùng PMTiles trên app mobile (MapLibre)

```json
{
  "id": "buildings-3d",
  "type": "fill-extrusion",
  "source": "buildings",
  "source-layer": "buildings",
  "minzoom": 13,
  "filter": ["!=", ["get", "has_parts"], true],
  "paint": {
    "fill-extrusion-height": ["get", "h_m"],
    "fill-extrusion-base": ["coalesce", ["get", "min_height"], 0],
    "fill-extrusion-color": "#d9d3c9",
    "fill-extrusion-opacity": 0.9
  }
}
```

Filter `has_parts != true` ẩn các outline đã được chia thành khối con, để không bị vẽ chồng. Cách làm này giống pipeline `vmap-buildings-to-pmtiles`.

### Xem trên nền bản đồ của app Mobile (VMAP) với 5 chế độ ánh sáng

Trong trang xem 3D, ô **Bản đồ nền** có lựa chọn **VMAP streets-v2 (nền app Mobile)**. Khi chọn nền này:

- Ô **Ánh sáng bản đồ** hiện 5 nút **Bình minh / Ban ngày / Hoàng hôn / Ban đêm / Nền tối**. Mỗi nút là một style VMAP đang phát hành (`https://assets.vmaps.vn/styles/streets-v2-{dawn,day,dusk,night,dark}/style.json`), dùng nguyên bản: màu, sprite và khối `light` của chính style đó. Dòng ghi chú dưới các nút ghi tên style và `light` của nó.
- Building tự chuyển sang cách tô **Như app Mobile**: một màu building cho mọi chế độ, `light` của style tô sáng / tối các mặt. Màu này (`#ddcca9`, độ mờ 0.85) là **ước lượng** từ ảnh chụp Tasco Map admin, vì không style VMAP nào có layer 3D. Đổi được trong `app/vmap.py` hoặc bằng biến môi trường `BDS_VMAP_BUILDING_COLOR` / `BDS_VMAP_BUILDING_OPACITY`. Vẫn chọn lại được các cách tô khác (chiều cao, outlier…).
- Nút **Tải style.json chế độ này** tải style của chế độ đang chọn (giữ nguyên URL sprite / glyphs / tiles gốc, màu building 3D ở `metadata["vmap:building-3d"]`) để gửi team Mobile.
- `assets.vmaps.vn` không bật CORS nên server local tải hộ style, sprite và glyphs. Nếu không tải được style live, app dùng bản lưu `map-styles/streets-v2-{chế độ}.style.json` và báo dưới ô chọn nền.

Chi tiết các file style của Mobile, bảng màu và `light` của từng chế độ: [`map-styles/README.md`](map-styles/README.md).

### Đổ bóng vẽ sẵn (thử nghiệm)

MapLibre không vẽ được bóng building đổ xuống mặt đất. Ô **Đổ bóng (vẽ sẵn, thử nghiệm)** trong trang xem 3D tính sẵn bóng của từng building rồi vẽ thành một layer `fill` mờ, nằm dưới nhãn và dưới building:

- Hướng và độ dài bóng lấy theo `light` của style VMAP đang chọn: Bình minh nắng từ phía đông nam (cao 40°), Ban ngày từ phía nam (cao 70°, bóng ngắn), Hoàng hôn từ phía tây nam (cao 10°, bóng dài gần 6 lần chiều cao); Ban đêm và Nền tối không có bóng. Nền khác VMAP dùng nắng Ban ngày. Độ dài bóng = chiều cao / tan(góc cao mặt trời).
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

Trên app, thêm một layer dùng chung source với building; khi người dùng đổi chế độ thì đổi `filter` và màu, Ban đêm và Nền tối thì ẩn layer:

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

96,9% building của bản này đang dùng chiều cao mặc định 4 m (`h_src = default`), nên ngưỡng 4 m bỏ gần hết bóng của nhà "đoán chiều cao" và làm layer nhẹ đi khoảng 30 lần.

### Bay quanh toà cao nhất

Nút **Bay quanh toà cao nhất** trong panel trái của trang xem 3D lấy 6 building cao nhất trong khung nhìn (theo cột chiều cao, bỏ `h_outlier`, gộp khối con vào toà mẹ, hai toà cách nhau dưới 120 m tính là một điểm dừng), rồi lần lượt bay tới từng toà và lượn quanh nó một vòng. Toà đang xem được tô như khi click; thẻ dưới bản đồ hiện tên, `building_id`, chiều cao và một skyline nhỏ của các điểm dừng. Chạy hết thì camera bay về chỗ cũ. Bấm **Dừng**, Esc hoặc chạm vào bản đồ để dừng ở chỗ đang xem.

Chỉ xét building đã tải trong khung nhìn: thu nhỏ bản đồ (vd zoom 13 với base Hà Nội) để tìm trong vùng rộng hơn. Máy bật "giảm chuyển động" (prefers-reduced-motion) thì camera nhảy thẳng tới từng toà, dừng 4 giây, không bay lượn.

### Landmark 3D trong trang xem 3D

Trang xem 3D (`/preview`) là nơi duy nhất xem và quản lý landmark: panel bên trái điều khiển bản đồ (layer, tô màu, 2D/3D, bóng, bản đồ nền, chế độ ánh sáng), bản đồ ở giữa, drawer **Landmark** bên phải. Nút **Đóng** thu drawer lại để bản đồ rộng ra, nút **Landmark** trên bản đồ mở lại. Trên màn hình hẹp, drawer là một tấm trượt từ dưới lên và panel trái tự thu gọn.

**Trên bản đồ**:

- Mọi landmark **đang dùng** của chính file PMTiles đang mở được vẽ cùng lúc, mỗi mô hình với hướng, tỉ lệ, điểm neo và toạ độ neo của version đang dùng. Server so file bằng đường dẫn thật (`source.path` lúc đăng ký), kể cả base mặc định. Landmark đăng ký trên file khác vẫn có trong danh sách, nhưng không vẽ trên file này.
- File GLB chỉ được tải khi landmark ở trong vùng đang xem (cộng thêm 25% mỗi phía), tối đa 3 file cùng lúc, và giữ trong bộ nhớ tối đa 24 mô hình. Đổi bản đồ nền hay chế độ ánh sáng không tải lại file.
- Extrusion của building, gồm các khối con (`parent_building_id`), chỉ bị ẩn khi file GLB đã tải xong. File lỗi (ví dụ nén Draco) thì extrusion vẫn giữ, lỗi hiện trong drawer. Bóng: landmark có version đang dùng đổ bóng theo mesh GLB của version đó (Bình minh, Ban ngày, Hoàng hôn; Ban đêm và Nền tối không đổ bóng), gộp chung với bóng building xung quanh nên không đậm lên ở chỗ chồng nhau. Landmark chưa có version đang dùng, hoặc file GLB không đọc được hình học (nén Draco / meshopt), giữ bóng hộp LoD1. Bóng đi theo placement đã lưu: đang kéo thử placement hay xem version khác thì bóng chưa đổi. Mỗi tam giác của GLB được đặt như mô hình trên bản đồ (heading, tỉ lệ, điểm neo), chiếu xuống mặt đất theo tia nắng rồi gộp lại. Khác bóng LoD1, bóng landmark không bị footprint của chính building đó cắt đi: mô hình thường không phủ kín footprint LoD1, nên bóng phủ cả phần đất trống quanh mô hình trong footprint (dưới thân mô hình thì bị mô hình che). Kết quả tính một lần cho mỗi (file GLB, placement, chế độ) và lưu ở `landmarks/<id>/shadows/`. Với PMTiles có sẵn layer `shadows`, bóng GLB được vẽ đè lên, bóng hộp LoD1 trong file vẫn nằm dưới.
- Click mô hình hoặc footprint của landmark để mở nó trong drawer. Click building thường để xem thuộc tính và đăng ký. Nếu click vào một khối con, drawer hỏi chọn cả building cha hay chỉ khối đó.
- URL giữ landmark đang mở (`lm=<id>`) hoặc building vừa click (`id=`), cùng `f`, `name`, `idcol`, `lon`, `lat`, `z`. Tải lại trang, hoặc Back / Forward, trở về đúng file, vị trí và landmark. Ô **Building từ PMTiles** ở panel trái chuyển sang base hoặc các file đã có landmark.

**Trong drawer**:

- **Danh sách**: tìm theo tên, `building_id` hoặc ID liên kết. Landmark của file đang mở đứng đầu. Cuối danh sách có **Mở thư mục** và **Tải manifest**.
- **Đăng ký**: nhập tên và **ID liên kết**, là một id tuỳ ý để tìm building ở hệ thống khác, ví dụ canonical id của MapOps hoặc `way/695966191`. Mỗi `building_id` chỉ đăng ký được một lần. Khi đăng ký, server đọc footprint (polygon đã ghép các mảnh tile, diện tích, tâm, chiều cao) trong PMTiles ở zoom lớn nhất và lưu cùng landmark.
- **File GLB**: chọn một file `.glb` để xem thử ngay trên building. File chỉ được đọc trong trình duyệt, chỉ được upload khi bấm **Upload version mới**. Có thể thử file cả trên building chưa đăng ký.
- **Version**: mỗi lần upload là một version. File cũ giữ nguyên, không bị ghi đè. Server đọc GLB và ghi lại số mesh, số tam giác, hộp bao, sha256 và các kiểu nén mà bản đồ không đọc được (Draco, meshopt, KTX2). Nếu file trùng byte với một version cũ, app báo **Trùng file với vN**. Nút **Xem** đặt version đó lên bản đồ thay cho version đang dùng. Khi đóng landmark, version đang dùng trở lại.
- **Placement của từng version**:
  - **Hướng**: thanh trượt 0–360°, ô nhập số và nút **Xoay 180°**. Hướng tính bằng độ, theo chiều kim đồng hồ nhìn từ trên xuống: ở hướng 0, trục +X của GLB chỉ hướng đông và mặt trước (+Z) quay về hướng nam.
  - **Tỉ lệ** và **Điểm neo trên mô hình** (gốc GLB hoặc tâm đáy hộp bao).
  - **Toạ độ neo (lon, lat)**: chỗ điểm neo đứng trên mặt đất. Có thể nhập số, bấm **Chọn trên bản đồ**, kéo điểm ◆ trên bản đồ, hoặc bấm **Về tâm footprint**.

  Mô hình trên bản đồ đổi ngay khi bạn chỉnh. Drawer ghi khoảng cách từ toạ độ neo tới tâm footprint, và so chiều cao mô hình (đã nhân tỉ lệ) với chiều cao building, cảnh báo khi lệch quá cả 2 m lẫn 5%. Bấm **Lưu placement** để lưu, **Hoàn tác** để bỏ. Version đầu tiên đặt ở tâm footprint, hướng 0. Version sau bắt đầu với placement của version đang dùng.
- **Đang dùng**: bấm **Dùng** ở một version (kèm ghi chú nếu cần) để chọn version đó, hoặc **Bỏ dùng** để không dùng version nào. Mỗi lần đổi được ghi vào **Lịch sử**, cùng các lần đăng ký, upload, lưu placement và sửa tên. Bảng version ghi trạng thái **đang dùng**, **đã dùng đến ngày…** hoặc **chưa dùng**.
- **Tải manifest** tải `landmarks-manifest.json`. File này liệt kê version đang dùng của mọi landmark, kèm đường dẫn file, sha256 và placement, để gửi team Mobile hoặc đối chiếu với MapOps. Mỗi landmark có thêm `shadows` (file GeoJSON bóng theo từng chế độ có nắng, đường dẫn tương đối trong thư mục landmark như `file`), và `shadow_presets` ở đầu file ghi màu, độ mờ và hướng nắng của từng chế độ. App vẽ các file này như layer `shadows` (thay bóng LoD1 của building đó).

Dữ liệu nằm trong thư mục `landmarks/` cạnh `output/`. Thư mục này không commit vào git. Đổi chỗ lưu bằng biến môi trường `BDS_LANDMARK_DIR`, ví dụ trỏ tới một thư mục Google Drive dùng chung với team. Mỗi landmark là một thư mục `landmarks/<id>/`, gồm `landmark.json` và `v1.glb`, `v2.glb`… **Xoá landmark** chỉ chuyển thư mục đó vào `landmarks/_trash/`, không xoá file nào.

### Landmark dưới `light` của style

Mục **Kiểm tra ánh sáng (QA)** trong drawer giúp kiểm tra mô hình dưới cả 5 chế độ ánh sáng trước khi upload lên MapOps:

- Mô hình được tô sáng bằng đúng công thức fill-extrusion của MapLibre, với `light` của style đang hiển thị. App đọc `light` qua `map.getLight()` và đọc lại mỗi khi style đổi, nên khi chuyển chế độ ánh sáng, landmark đổi sáng cùng lúc với các building xung quanh. Texture emissive (ví dụ cửa sổ sáng đèn) chỉ bật với style ban đêm, tức khi `metadata["tasco:light-preset"]` là `"night"`.
- Để QA, ô **So sánh** hiện extrusion gốc của building ngay bên cạnh landmark (đục, màu so sánh). Ô **Tô landmark một màu** thay texture bằng chính màu đó: khi ấy mái, và các mặt tường quay cùng hướng, của landmark và extrusion phải sáng tối như nhau. Mục này còn ghi màu mong đợi của mái và tường 4 hướng, cùng các uniform ánh sáng để đối chiếu khi port.

Công thức, shader và ghi chú cho Mobile (Android / iOS) nằm chung trong [`app/static/landmark-light.js`](app/static/landmark-light.js). MapOps web và renderer LMK3 của app Mobile port nguyên công thức này. `tests/test_landmark.py` kiểm tra công thức với chính code của MapLibre, với màu pixel MapLibre vẽ ra ở cả 5 chế độ, và chạy file JS bằng Node để so với bản port Python. Ba điều dễ sai khi port:

- Mọi phép tính làm trên giá trị sRGB thô (0..1), như màu viết trong style: không đổi sang linear, không tone mapping. Texture phải upload dạng RGBA8 thường, không dùng sRGB texture.
- Vector light dài bằng `radial` (1,15), không chuẩn hoá. Tính theo hướng đông-bắc-lên với pháp tuyến hướng ra ngoài, nó là `r·(sin a·sin p, cos a·sin p, cos p)`: mặt tường quay về phía phương vị `a` sáng nhất. Trong code MapLibre có hai chỗ ngược dấu triệt tiêu nhau: phương vị cộng 90° trong hệ toạ độ tile, và pháp tuyến tường hướng vào trong.
- Ở chế độ Ban đêm (`light` màu `#3f4455`), MapLibre kẹp gần như mọi mặt về mức sàn `0,3 × (1 − màu light)`. Building nào cũng thành một màu tối, texture của landmark cũng vậy; chỉ emissive làm landmark khác đi.

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

## 10. Lưu ý

- **Bản đồ nền** trong trang xem 3D lấy từ VMAP / OpenFreeMap / OpenStreetMap, nên cần internet. Khi offline thì chọn *Không nền*, building vẫn hiển thị bình thường. Lựa chọn nền và chế độ ánh sáng được nhớ trong trình duyệt.
- File kéo thả được copy vào `uploads/` và **không tự xoá**. Hãy dọn thư mục này khi cần.
- Lịch sử chạy trong app chỉ giữ trong phiên hiện tại. File output vẫn còn trên đĩa sau khi tắt app.
- Tuỳ chọn chung (zoom, khoảng hợp lệ, thư mục output…) được nhớ trong trình duyệt. Nút **Khôi phục mặc định** đưa chúng về như ban đầu.

## 11. Lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| Chip `Thiếu ogr2ogr/tippecanoe` đỏ | `brew install gdal tippecanoe` rồi chạy lại `./run.sh` |
| “File không khai báo CRS — hãy nhập CRS nguồn” | Nhập CRS ở ô **CRS nguồn**, ví dụ `EPSG:4326` |
| “Không tìm thấy cột geometry” | File Parquet thường: chọn cột WKB/WKT ở *Nguồn dữ liệu* |
| Job lỗi giữa chừng | Mở **Log chi tiết**. File trung gian được giữ trong `_work/` của thư mục output |
| Cổng 8765 đang bận | `./run.sh --port 9000` |
| Excel: “Overpass trả lỗi HTTP 429/504” hoặc “Không tải được hình học từ Overpass” | Máy chủ Overpass đang bận hoặc không có mạng: đợi vài phút rồi bấm lại **Tạo dữ liệu bản đồ**, hoặc đặt `BDS_OVERPASS_URL` sang máy chủ khác |
| Thẻ Base báo lỗi `Không tải được extension DuckDB` | Máy không vào được extensions.duckdb.org: `.venv/bin/pip install duckdb-extension-spatial duckdb-extension-httpfs` (cùng version với `duckdb`), rồi bấm **Tạo base** |
| Thẻ Base báo lỗi `Không đọc được danh sách release Overture` / lỗi HTTP khi tải building | Kiểm tra internet / VPN (app đọc `overturemaps-us-west-2.s3.amazonaws.com`), rồi bấm **Tạo base**. Có thể đặt cố định `BDS_OVERTURE_RELEASE=2026-10-21.0` |
| Trang 3D báo “Chưa có base Hà Nội” | Base chưa tạo, hoặc đã bị xoá khỏi `base/hanoi/`: bấm **Tạo base** ở trang chính |
| Không muốn app tự tạo base khi khởi động | `./run.sh --no-base` hoặc `BDS_BASE_AUTOBUILD=0` |
| Landmark có trong danh sách nhưng không hiện trên bản đồ | Landmark đăng ký trên file PMTiles khác (drawer ghi *PMTiles khác*, có link mở đúng file), chưa có version **đang dùng**, hoặc file GLB lỗi (drawer ghi lỗi, ví dụ nén Draco: xuất lại GLB không nén) |

## 12. Phát triển

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
| `app/sqlbuild.py` | Sinh SQL tính `h_m`, `h_src`, `h_outlier`, `has_parts` |
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
| `app/osmbase.py` | Base Hà Nội: ranh giới và building OSM từ Overture (DuckDB), chạy qua pipeline convert, ghi `base/hanoi/base.json` |
| `map-styles/` | Các style.json app Mobile đang dùng (bản lưu để đối chiếu, xem `map-styles/README.md`) |
| `app/static/` | Giao diện (HTML/CSS/JS thuần): `app.js` (convert), `diff.js` (so sánh), `util.js`; trang bản đồ 3D / diff (`preview.js`); `basemap.js` (bản đồ nền và chế độ ánh sáng); landmark trong trang xem 3D: `landmark-workspace.js` (state, thao tác, URL), `landmark-drawer.js` (giao diện drawer), `landmark-store.js` (tải GLB theo vùng nhìn, cache, filter extrusion; không phụ thuộc trình duyệt, test bằng Node), `landmark-model.js` (vẽ nhiều GLB trong một custom layer) và `landmark-light.js` (công thức ánh sáng landmark, để các team khác port); `tour.js` (bay quanh toà cao nhất: chọn điểm dừng, camera; test bằng Node); MapLibre + pmtiles.js + three.js 0.186.1 (vendored, chế độ diff không tải three.js) |
| `tests/` | Unit test + end-to-end (cần GDAL & tippecanoe); `tests/js/` chạy bằng Node |
