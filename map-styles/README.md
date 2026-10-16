# Style bản đồ của app Mobile

Bản lưu các `style.json` app Mobile (MapLibre Native) đang dùng, để đối chiếu và để trang xem 3D có bản dự phòng khi không tải được style live.

VMAP liệt kê các style đang phát hành ở mục `styles` của `https://api-app.vmaps.vn//map-proxy/v2/map/layers`.

| File | Là gì |
|---|---|
| `streets-v2-day.style.json` | Chế độ Ban ngày. Trùng từng byte với `https://assets.vmaps.vn/styles/streets-v2-day/style.json` (kiểm tra ngày 16/10/2026). |
| `streets-v2-dawn.style.json` | Chế độ Bình minh, bản của `styles/streets-v2-dawn` (cùng ngày). |
| `streets-v2-dusk.style.json` | Chế độ Hoàng hôn, bản của `styles/streets-v2-dusk` (cùng ngày). |
| `streets-v2-night.style.json` | Chế độ Ban đêm, bản của `styles/streets-v2-night` (cùng ngày). |
| `streets-v2-dark.style.json` | Giao diện tối, bản của `styles/streets-v2-dark` (cùng ngày). |
| `streets-v1.style.json` | Style nền cũ (chỉ có ban ngày), trùng từng byte với `styles/streets-v1/style.json` (12/10/2026). Giữ lại để đối chiếu, trang xem 3D không dùng nữa. |
| `streets-v1-3d.style.json` | Trùng từng byte với `streets-v1.style.json`. |
| `streets-v1-3d-qa.style.json` | Bản QA cũ: có thêm layer `building-3d` (`fill-extrusion`, màu `#cfcec9`, độ mờ 0.48) nhưng không có khối `light`. |
| `hd-map-v2.style.json` | Các layer HD map (mặt đường, vạch kẻ, vỉa hè). URL tiles là chỗ trống `{{HDMAP_TILEJSON_URL}}`, app điền lúc chạy. |
| `weather-gray-v1.style.json` | Nền ranh giới quốc gia cho lớp thời tiết. |

Danh sách còn có `streets-v2` / `streets-v2-3d` (bảng màu và `light` của streets-v1 cũ, phát hành lại) và `streets-v2-3d-dark` (giống hệt `streets-v2-dark`, chỉ khác đường dẫn sprite). Trang xem 3D không dùng ba style này.

## Các chế độ của streets-v2

Mỗi chế độ là một style riêng: cùng layer, filter, zoom; khác màu `paint`, khác sprite (`styles/streets-v1-{chế độ}/sprite`) và có khối `light` riêng ở gốc style. Metadata ghi `tasco:style-variant` (và `tasco:light-preset`, trừ `dark`).

- **Không style nào có layer 3D.** Building chỉ là `fill` 2D từ zoom 12 đến 14. Building 3D là do app tự thêm, từ PMTiles của Building Data Studio.
- MapLibre không vẽ được bóng building đổ xuống mặt đất. `light` chỉ làm sáng / tối các mặt bên của khối 3D. Trang xem 3D có bản thử nghiệm **bóng vẽ sẵn**, và tab Convert tạo được layer `shadows` trong PMTiles để app dùng (xem mục "Đổ bóng vẽ sẵn" và "Layer bóng trong PMTiles" trong README chính).
- Sprite không có icon SDF, nên icon POI và biển số đường (CT, QL…) không đổi màu được qua style. Mỗi chế độ có bộ sprite riêng. Bộ nào cũng thiếu một số icon mà style gọi tới (vd `office`, `atm`), nên MapLibre báo "Image … could not be loaded". Lỗi này có từ streets-v1.

| Thành phần | Layer | Bình minh | Ban ngày | Hoàng hôn | Ban đêm | Nền tối |
|---|---|---|---|---|---|---|
| Style | | `streets-v2-dawn` | `streets-v2-day` | `streets-v2-dusk` | `streets-v2-night` | `streets-v2-dark` |
| Nền đất | `background` | `#f6dfca` | `#f5f1f0` | `#695e65` | `#1a273d` | `#162640` |
| Nước | `water` | `#cfd7ce` | `#ade4ff` | `#4a5464` | `#0a1932` | `#000d2b` |
| Công viên | `park` | `#cddba7` | `#abe9a5` | `#63775b` | `#2f4a45` | `#113c4b` |
| Cao tốc (lòng đường) | `road_motorway` | `#bfb2b6` | `#919dbe` | `#6a6a7c` | `#424e69` | `#5f7896` |
| Đường nhỏ | `road_minor` | `#e2d1ca` | `#d2d7e4` | `#8a8690` | `#596478` | `#3d5b77` |
| Tên đường (chữ / viền) | `road_label` | `#424242` / `#f2f2f2` | `#424242` / `#f2f2f2` | `#e4e4e4` / `#4d4d4d` | `#e4e4e4` / `#333333` | `#b4c0d6` / `#162640` |
| Building 2D (z12–14) | `building` | `#f7e0c7` | `#f7f4ec` | `#716669` | `#253144` | `#5a5863` |
| `light` (màu, cường độ, vị trí) | gốc style | `#feca8b`, 0.365, [1.15, 120, 50] | `#ffffff`, 0.2, [1.15, 180, 20] | `#fec286`, 0.2, [1.15, 240, 80] | `#3f4455`, 0.275, [1.15, 270, 20] | `#b0b9c9`, 0.22, [1.15, 210, 35] |
| Building 3D (màu / độ mờ) | app tự thêm | `#ddcca9` / 0.85 ⁽¹⁾ | như cột bên | như cột bên | như cột bên | như cột bên |
| Bóng vẽ sẵn (màu / độ mờ) ⁽²⁾ | `fill` dưới nhãn | `#4b3f6b` / 0.22 | `#3a4150` / 0.24 | `#2b1d2b` / 0.3 | không có | không có |
| Hướng nắng → độ dài bóng | theo `light` | từ đông nam, cao 40° → 1,19× | từ nam, cao 70° → 0,36× | từ tây nam, cao 10° → 5,67× | — | — |

Vị trí `light` là `[bán kính, góc phương vị, góc cực]`: phương vị 90 là phía đông, 180 phía nam, 270 phía tây; góc cực càng lớn thì nguồn sáng càng thấp (cao = 90° − góc cực).

⁽¹⁾ **Ước lượng, không phải giá trị VMAP công bố.** Đo từ ảnh chụp Tasco Map admin (16/10/2026): building khối trông như một màu nền khoảng `#ddcca9`, độ mờ khoảng 0.85, được `light` của từng style tô sáng / tối. Giá trị nằm ở `INFERRED_BUILDING` trong `app/vmap.py`. Muốn thử giá trị khác mà không sửa code thì đặt biến môi trường trước khi chạy app:

```bash
BDS_VMAP_BUILDING_COLOR="#cfcec9" BDS_VMAP_BUILDING_OPACITY=0.48 ./run.sh
```

⁽²⁾ Màu bóng là của Building Data Studio (VMAP không có bóng). Hướng nắng lấy từ `light` của từng style. Ban đêm và Nền tối không đổ bóng: ban đêm không có nắng, còn Nền tối là giao diện tối chứ không phải một giờ trong ngày.

## Gửi cho team Mobile

Trên trang xem 3D, chọn nền **VMAP streets-v2**, chọn chế độ rồi bấm **Tải style.json chế độ này**. Hoặc gọi thẳng khi app đang chạy:

```bash
curl -o streets-v2-night.style.json "http://127.0.0.1:8765/api/vmap/style.json?preset=night&download=1"
```

File tải về là style VMAP của chế độ đó, giữ nguyên URL sprite / glyphs / tiles gốc. Building 3D trang xem đang dùng nằm ở `metadata["vmap:building-3d"]` (`origin: "inferred"` = giá trị ước lượng ở trên, `"env"` = đặt qua biến môi trường).

Lưu ý: ví dụ layer `buildings-3d` ở mục 8 của README chính dùng `#d9d3c9` / 0.9, bản QA cũ dùng `#cfcec9` / 0.48, còn trang xem 3D dùng `#ddcca9` / 0.85 (ước lượng). Cần chốt lại với team Mobile giá trị nào là chuẩn.
