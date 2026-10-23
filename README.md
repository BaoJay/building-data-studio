# Building Data Studio

App chạy local (trên trình duyệt) để chuyển dữ liệu building từ **Parquet / GeoPackage** sang:

- **GeoPackage (.gpkg)**: mở bằng QGIS để kiểm tra chiều cao của khối building trong khu vực
- **PMTiles (.pmtiles)**: publish và serve cho app mobile (MapLibre, dựng 3D bằng `fill-extrusion`)

Có giao diện kéo thả, nút tải file và nút mở thư mục output. App tự sinh báo cáo chiều cao và có trang xem bản đồ 3D.

Tab **So sánh 2 file** so sánh bản production (A) với bản sắp release (B) và đưa ra kết luận **Pass / Warn / Fail** trước khi release (xem [mục 9](#9-so-sánh-2-file-trước-release)).

Trang **Landmark 3D** lưu file GLB của từng building theo `building_id` và footprint, cùng các version đã dùng. Mỗi version có hướng và toạ độ neo riêng (xem [Thư viện landmark](#thư-viện-landmark-landmarks)).

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

### Landmark 3D (GLB) dưới `light` của style

Mục **Landmark 3D (GLB)** ở cuối panel trang xem 3D đặt một file `.glb` lên một building. Dùng nó để kiểm tra mô hình dưới cả 5 chế độ ánh sáng trước khi upload lên MapOps:

- Chọn file `.glb`. File chỉ được đọc trong trình duyệt, không upload. Click building trên bản đồ, bấm **Lấy building đang chọn**, rồi **Đặt lên building**. Server đọc footprint của `building_id` đó trong PMTiles ở zoom lớn nhất, ghép các mảnh tile lại, và neo mô hình ở tâm footprint, trên mặt đất. Với building chia khối con, server tìm theo id của chính building trước, không thấy mới tìm theo `parent_building_id`.
- Chỉnh được **Hướng**, **Tỉ lệ** và **Điểm neo**. Hướng tính bằng độ, theo chiều kim đồng hồ nhìn từ trên xuống: ở hướng 0, trục +X của GLB chỉ hướng đông và mặt trước (+Z) quay về hướng nam. Điểm neo là gốc GLB hoặc tâm đáy hộp bao. Dòng **Chiều cao** so chiều cao mô hình (đã nhân tỉ lệ) với chiều cao của building, và cảnh báo khi lệch quá cả 2 m lẫn 5%.
- Extrusion của building đó và các khối con của nó bị ẩn bằng filter theo id. Bóng vẽ sẵn của building vẫn giữ.
- Nếu building đang chọn đã có trong [thư viện landmark](#thư-viện-landmark-landmarks), panel hiện version đang dùng và nút **Xem vN từ thư viện**. Nút này tải file GLB của version đó, cùng hướng, tỉ lệ, điểm neo và toạ độ neo đã lưu.
- Mô hình được tô sáng bằng đúng công thức fill-extrusion của MapLibre, với `light` của style đang hiển thị. App đọc `light` qua `map.getLight()` và đọc lại mỗi khi style đổi, nên khi chuyển chế độ ánh sáng, landmark đổi sáng cùng lúc với các building xung quanh. Texture emissive (ví dụ cửa sổ sáng đèn) chỉ bật với style ban đêm, tức khi `metadata["tasco:light-preset"]` là `"night"`.
- Để QA, ô **So sánh** hiện extrusion gốc của building ngay bên cạnh landmark (đục, màu so sánh). Ô **Tô landmark một màu** thay texture bằng chính màu đó: khi ấy mái, và các mặt tường quay cùng hướng, của landmark và extrusion phải sáng tối như nhau. Panel còn ghi màu mong đợi của mái và tường 4 hướng, cùng các uniform ánh sáng để đối chiếu khi port.

Công thức, shader và ghi chú cho Mobile (Android / iOS) nằm chung trong [`app/static/landmark-light.js`](app/static/landmark-light.js). MapOps web và renderer LMK3 của app Mobile port nguyên công thức này. `tests/test_landmark.py` kiểm tra công thức với chính code của MapLibre, với màu pixel MapLibre vẽ ra ở cả 5 chế độ, và chạy file JS bằng Node để so với bản port Python. Ba điều dễ sai khi port:

- Mọi phép tính làm trên giá trị sRGB thô (0..1), như màu viết trong style: không đổi sang linear, không tone mapping. Texture phải upload dạng RGBA8 thường, không dùng sRGB texture.
- Vector light dài bằng `radial` (1,15), không chuẩn hoá. Tính theo hướng đông-bắc-lên với pháp tuyến hướng ra ngoài, nó là `r·(sin a·sin p, cos a·sin p, cos p)`: mặt tường quay về phía phương vị `a` sáng nhất. Trong code MapLibre có hai chỗ ngược dấu triệt tiêu nhau: phương vị cộng 90° trong hệ toạ độ tile, và pháp tuyến tường hướng vào trong.
- Ở chế độ Ban đêm (`light` màu `#3f4455`), MapLibre kẹp gần như mọi mặt về mức sàn `0,3 × (1 − màu light)`. Building nào cũng thành một màu tối, texture của landmark cũng vậy; chỉ emissive làm landmark khác đi.

### Thư viện landmark (`/landmarks`)

Trang **Landmark 3D** lưu các file GLB của từng building và các version đã dùng. Mở trang bằng tab **Landmark 3D ↗** trên trang chính, nút **Landmark 3D** cạnh **Xem bản đồ 3D** của một file PMTiles, hoặc link **Mở thư viện landmark** trong trang xem 3D. Trang chia 3 cột: danh sách landmark, bản đồ và chi tiết landmark đang chọn.

- **Đăng ký**: chọn PMTiles ở ô **Bản đồ**, rồi click một building. Building đã đăng ký có màu vàng. Nhập tên và **ID liên kết**, là một id tuỳ ý để tìm building ở hệ thống khác, ví dụ canonical id của MapOps hoặc `way/695966191`. Mỗi `building_id` chỉ đăng ký được một lần. Nếu click vào một khối con, app hỏi đăng ký cho cả building cha hay chỉ khối đó. Khi đăng ký, server đọc footprint (polygon đã ghép các mảnh tile, diện tích, tâm, chiều cao) và lưu cùng landmark. Vì vậy sau này mở thư viện không cần file PMTiles đó nữa.
- **Version**: mỗi lần **Upload version mới** là một version. File cũ giữ nguyên, không bị ghi đè. Server đọc GLB và ghi lại số mesh, số tam giác, hộp bao, sha256 và các kiểu nén mà bản đồ không đọc được (Draco, meshopt, KTX2). Nếu file trùng byte với một version cũ, app báo **Trùng file với vN**.
- **Placement của từng version**:
  - **Hướng**: thanh trượt 0–360°, ô nhập số và nút **Xoay 180°**. Hướng dùng cùng quy ước với trang xem 3D.
  - **Tỉ lệ** và **Điểm neo trên mô hình** (gốc GLB hoặc tâm đáy hộp bao).
  - **Toạ độ neo (lon, lat)**: chỗ điểm neo đứng trên mặt đất. Có thể nhập số, bấm **Chọn trên bản đồ**, kéo điểm ◆ trên bản đồ, hoặc bấm **Về tâm footprint**.

  Mô hình trên bản đồ đổi ngay khi bạn chỉnh, và được tô sáng như trang xem 3D. Panel ghi khoảng cách từ toạ độ neo tới tâm footprint, và so chiều cao mô hình với chiều cao building. Bấm **Lưu placement** để lưu. Version đầu tiên đặt ở tâm footprint, hướng 0. Version sau bắt đầu với placement của version đang dùng.
- **Đang dùng**: bấm **Dùng** ở một version (kèm ghi chú nếu cần) để chọn version đó, hoặc **Bỏ dùng** để không dùng version nào. Mỗi lần đổi được ghi vào **Lịch sử**, cùng các lần đăng ký, upload, lưu placement và sửa tên. Bảng version ghi trạng thái **đang dùng**, **đã dùng đến ngày…** hoặc **chưa dùng**.
- **Tải manifest** tải `landmarks-manifest.json`. File này liệt kê version đang dùng của mọi landmark, kèm đường dẫn file, sha256 và placement, để gửi team Mobile hoặc đối chiếu với MapOps.

Dữ liệu nằm trong thư mục `landmarks/` cạnh `output/`. Thư mục này không commit vào git. Đổi chỗ lưu bằng biến môi trường `BDS_LANDMARK_DIR`, ví dụ trỏ tới một thư mục Google Drive dùng chung với team. Mỗi landmark là một thư mục `landmarks/<id>/`, gồm `landmark.json` và `v1.glb`, `v2.glb`… **Xoá landmark** chỉ chuyển thư mục đó vào `landmarks/_trash/`, không xoá file nào.

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
| `app/vmap.py` | Nền VMAP cho trang xem 3D: proxy style / sprite / glyphs từ assets.vmaps.vn, 5 chế độ ánh sáng (mỗi chế độ một style streets-v2), màu building 3D ước lượng |
| `app/shadows.py` | Đổ bóng vẽ sẵn: tính bóng building theo hướng nắng của từng chế độ, cho một vùng (trang xem 3D) hoặc cả GeoPackage (layer `shadows` của PMTiles) |
| `app/landmark.py` | Neo landmark trong trang xem 3D: footprint của một `building_id` trong PMTiles (ghép mảnh tile, tâm, diện tích, chiều cao) |
| `app/landmark_library.py` | Thư viện landmark: version GLB, placement của từng version, version đang dùng và lịch sử (thư mục `landmarks/`) |
| `app/glb.py` | Đọc GLB không cần thư viện 3D: số mesh / tam giác, hộp bao, kiểu nén |
| `map-styles/` | Các style.json app Mobile đang dùng (bản lưu để đối chiếu, xem `map-styles/README.md`) |
| `app/static/` | Giao diện (HTML/CSS/JS thuần): `app.js` (convert), `diff.js` (so sánh), `util.js`; trang bản đồ 3D / diff; `basemap.js` (bản đồ nền và chế độ ánh sáng, dùng chung); `landmark-model.js` (vẽ GLB trên bản đồ), `landmark.js` (mục Landmark của trang xem 3D), `landmarks.js` (trang thư viện landmark) và `landmark-light.js` (công thức ánh sáng landmark, để các team khác port); MapLibre + pmtiles.js + three.js 0.186.1 (vendored, trang xem 3D chỉ tải three.js khi mở mục Landmark) |
| `tests/` | Unit test + end-to-end (cần GDAL & tippecanoe) |
