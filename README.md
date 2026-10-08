# Building Data Studio

App chạy local (trên trình duyệt) để chuyển dữ liệu building từ **Parquet / GeoPackage** sang:

- **GeoPackage (.gpkg)**: mở bằng QGIS để kiểm tra chiều cao của khối building trong khu vực
- **PMTiles (.pmtiles)**: publish và serve cho app mobile (MapLibre, dựng 3D bằng `fill-extrusion`)

Có giao diện kéo thả, nút tải file và nút mở thư mục output. App tự sinh báo cáo chiều cao và có trang xem bản đồ 3D.

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

## 9. Lưu ý

- **Bản đồ nền** trong trang xem 3D lấy từ OpenFreeMap / OpenStreetMap, nên cần internet. Khi offline thì chọn *Không nền*, building vẫn hiển thị bình thường.
- File kéo thả được copy vào `uploads/` và **không tự xoá**. Hãy dọn thư mục này khi cần.
- Lịch sử chạy trong app chỉ giữ trong phiên hiện tại. File output vẫn còn trên đĩa sau khi tắt app.
- Tuỳ chọn chung (zoom, khoảng hợp lệ, thư mục output…) được nhớ trong trình duyệt. Nút **Khôi phục mặc định** đưa chúng về như ban đầu.

## 10. Lỗi thường gặp

| Hiện tượng | Cách xử lý |
|---|---|
| Chip `Thiếu ogr2ogr/tippecanoe` đỏ | `brew install gdal tippecanoe` rồi chạy lại `./run.sh` |
| “File không khai báo CRS — hãy nhập CRS nguồn” | Nhập CRS ở ô **CRS nguồn**, ví dụ `EPSG:4326` |
| “Không tìm thấy cột geometry” | File Parquet thường: chọn cột WKB/WKT ở *Nguồn dữ liệu* |
| Job lỗi giữa chừng | Mở **Log chi tiết**. File trung gian được giữ trong `_work/` của thư mục output |
| Cổng 8765 đang bận | `./run.sh --port 9000` |

## 11. Phát triển

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
BC_SAMPLE_PARQUET=/đường/dẫn/file.parquet .venv/bin/python -m pytest -q -k real_sample   # chạy thêm trên file thật
```

| Đường dẫn | Vai trò |
|---|---|
| `app/server.py` | HTTP API + phục vụ giao diện (Starlette/uvicorn) |
| `app/probe.py` | Đọc file đầu vào, nhận diện cột geometry / CRS / cột chiều cao |
| `app/sqlbuild.py` | Sinh SQL tính `h_m`, `h_src`, `h_outlier`, `has_parts` |
| `app/pipeline.py` | Các bước convert, đối soát, ghi báo cáo |
| `app/report.py` | Thống kê chiều cao từ GeoPackage |
| `app/jobs.py` | Hàng đợi job (chạy lần lượt từng job), huỷ job |
| `app/static/` | Giao diện (HTML/CSS/JS thuần), trang xem 3D, MapLibre + pmtiles.js (vendored) |
| `tests/` | Unit test + end-to-end (cần GDAL & tippecanoe) |
