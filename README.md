# Building Data Studio

App chạy local (trên trình duyệt) để chuyển dữ liệu building từ **Parquet / GeoPackage** sang:

- **GeoPackage (.gpkg)**: mở bằng QGIS để kiểm tra chiều cao của khối building trong khu vực
- **PMTiles (.pmtiles)**: publish và serve cho app mobile (MapLibre, dựng 3D bằng `fill-extrusion`)

Có giao diện kéo thả, nút tải file và nút mở thư mục output. App tự sinh báo cáo chiều cao và có trang xem bản đồ 3D.

Tab **So sánh 2 file** so sánh bản production (A) với bản sắp release (B) và đưa ra kết luận **Pass / Warn / Fail** trước khi release (xem [mục 9](#9-so-sánh-2-file-trước-release)).

```
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

## 3. Cách dùng

1. **Chọn file**: kéo thả file vào ô, hoặc dán đường dẫn rồi bấm **Mở**. Với file lớn nên dán đường dẫn, vì kéo thả sẽ copy file vào thư mục `uploads/`.
2. App đọc file và hiển thị: số feature, cột geometry, CRS, extent, danh sách cột kèm cảnh báo (nếu có).
3. **Tuỳ chọn**: app tự đoán cột chiều cao, cột ID, cột parent, cột nguồn chiều cao. Kiểm tra lại rồi bấm **Chạy convert**.
4. Theo dõi tiến trình từng bước, có thể **Huỷ** giữa chừng.
5. Khi xong, mỗi file có nút **Tải về** và **Hiện trong Finder**. Ngoài ra có nút **Mở thư mục output** và **Xem bản đồ 3D**.
6. **Báo cáo chiều cao** hiển thị ngay bên dưới. Bấm **Xem 3D** ở bảng outlier hoặc bảng toà cao nhất để bay thẳng tới building đó trên bản đồ.

Kéo thả file `.pmtiles` có sẵn vào app để xem 3D mà không cần convert.

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

- **Bản đồ nền** trong trang xem 3D lấy từ OpenFreeMap / OpenStreetMap, nên cần internet. Khi offline thì chọn *Không nền*, building vẫn hiển thị bình thường.
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

## 12. Phát triển

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
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
| `app/static/` | Giao diện (HTML/CSS/JS thuần): `app.js` (convert), `diff.js` (so sánh), `util.js`; trang bản đồ 3D / diff; MapLibre + pmtiles.js (vendored) |
| `tests/` | Unit test + end-to-end (cần GDAL & tippecanoe) |
