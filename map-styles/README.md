# Style bản đồ của app Mobile

Bản lưu các `style.json` app Mobile (MapLibre Native) đang dùng, để đối chiếu và để trang xem 3D có bản dự phòng khi không tải được style live.

| File | Là gì |
|---|---|
| `streets-v1.style.json` | Style nền đang chạy trên app (màn home). Trùng từng byte với `https://assets.vmaps.vn/styles/streets-v1/style.json` (kiểm tra ngày 12/10/2026). |
| `streets-v1-3d.style.json` | Trùng từng byte với `streets-v1.style.json`. Không có khác biệt 2D / 3D. |
| `streets-v1-3d-qa.style.json` | Bản QA: có thêm layer `building-3d` (`fill-extrusion`, màu `#cfcec9`, độ mờ 0.48) nhưng không có khối `light`. |
| `hd-map-v2.style.json` | Các layer HD map (mặt đường, vạch kẻ, vỉa hè). URL tiles là chỗ trống `{{HDMAP_TILEJSON_URL}}`, app điền lúc chạy. |
| `weather-gray-v1.style.json` | Nền ranh giới quốc gia cho lớp thời tiết. |

## Độ phủ so với 4 chế độ ánh sáng

Các file trên **chỉ có một bộ màu (ban ngày)**. Không có biến thể bình minh / hoàng hôn / ban đêm, cũng không có biểu thức nào đổi màu theo chế độ.

- `streets-v1` có một khối `light` cố định (`#ffffff`, cường độ 0.25, vị trí `[1.15, 210, 35]`). Nhưng style này **không có layer 3D nào**: building chỉ là `fill` 2D từ zoom 12 đến 14. Building 3D là do app tự thêm, từ PMTiles của Building Data Studio.
- MapLibre không vẽ được bóng building đổ xuống mặt đất. `light` chỉ làm sáng / tối các mặt bên của khối 3D. Trang xem 3D có bản thử nghiệm **bóng vẽ sẵn** (xem mục "Đổ bóng vẽ sẵn" trong README chính).
- Sprite không có icon SDF, nên icon POI và biển số đường (CT, QL…) **không đổi màu được qua style**. Muốn icon hợp nền tối thì cần bộ sprite riêng cho ban đêm.

## 4 chế độ trong trang xem 3D

Định nghĩa ở `app/vmap.py`. **Ban ngày** là style Mobile nguyên bản; building lấy màu / độ mờ của layer `building-3d` trong bản QA. Ba chế độ còn lại là **đề xuất**, sinh tự động từ style ban ngày:

- Mỗi màu trong `paint` được xếp vào một nhóm theo tên layer: đất, cây xanh, nước, lòng đường, viền đường, đường ray, ranh giới, building, chữ, viền chữ.
- Mỗi nhóm được chỉnh trong không gian màu OKLab: độ sáng, độ đậm, ám màu. Ban đêm đảo độ sáng của đường (cao tốc thành đường sáng nhất) và của nhãn (chữ sáng trên viền tối).
- Riêng chữ trên biển số đường (`road_shield`) giữ nguyên, vì nền biển là ảnh sprite.
- Layout, filter, zoom và các giá trị không phải màu không đổi. Layer mới thêm vào style sau này cũng tự vào đúng nhóm nếu đặt tên theo quy ước hiện có.

| Thành phần | Layer | Bình minh * | Ban ngày | Hoàng hôn * | Ban đêm * |
|---|---|---|---|---|---|
| Nền đất | `background` | `#f3e6e2` | `#f6f3e9` | `#e5cfbd` | `#1b1f26` |
| Nước | `water` | `#90c3e6` | `#8fd6ea` | `#7ca6cf` | `#09233d` |
| Công viên | `park` | `#bad39c` | `#bde69a` | `#afbc84` | `#1e2e1d` |
| Cao tốc (lòng đường) | `road_motorway` | `#b3b1ba` | `#aeb6bd` | `#b0a7a1` | `#535962` |
| Cao tốc (viền) | `road_motorway_casing` | `#817f89` | `#7f8790` | `#7a7171` | `#101620` |
| Đường chính | `road_trunk_primary` | `#eae3e9` | `#e6e9ec` | `#e4d7ce` | `#41454c` |
| Đường nhỏ | `road_minor` | `#fff9fc` | `#ffffff` | `#fbebe0` | `#3b3d43` |
| Tên đường (chữ) | `road_label` | `#42424b` | `#404348` | `#403d40` | `#b9bcc1` |
| Tên đường (viền chữ) | `road_label` | `#fffbff` | `#ffffff` | `#fff7ec` | `#101624` |
| Địa danh (chữ) | `place_city` | `#585963` | `#565a60` | `#545257` | `#a4a8ae` |
| Building 3D (màu / độ mờ) | app tự thêm | `#d5cbca` / 0.48 | `#cfcec9` / 0.48 | `#cfbcaf` / 0.52 | `#5c616c` / 0.75 |
| `light` (màu, cường độ, vị trí) | gốc style | `#ffe0cc`, 0.3, [1.15, 90, 50] | `#ffffff`, 0.25, [1.15, 210, 35] | `#ffc796`, 0.4, [1.15, 270, 55] | `#c8d4f5`, 0.25, [1.15, 210, 30] |
| Bóng vẽ sẵn (màu / độ mờ, thử nghiệm) | `fill` dưới nhãn | `#4b3f6b` / 0.22 | `#3a4150` / 0.24 | `#4e2f45` / 0.26 | không có |
| Hướng nắng → độ dài bóng | theo `light` | từ đông, cao 40° → 1,19× | từ tây nam, cao 55° → 0,70× | từ tây, cao 35° → 1,43× | — |

`*` = đề xuất, chưa có trong style Mobile. Vị trí `light` là `[bán kính, góc phương vị, góc cực]`: phương vị 90 là mặt trời ở phía đông, 270 là phía tây; góc cực càng lớn thì mặt trời càng thấp.

## Gửi cho team Mobile

Trên trang xem 3D, chọn nền **VMAP streets-v1**, chọn chế độ rồi bấm **Tải style.json chế độ này**. Hoặc gọi thẳng khi app đang chạy:

```bash
curl -o streets-v1-night.style.json "http://127.0.0.1:8765/api/vmap/style.json?preset=night&download=1"
```

File tải về là style đầy đủ, giữ nguyên URL sprite / glyphs / tiles gốc, chỉ khác màu và khối `light`. Màu building 3D của chế độ nằm ở `metadata["vmap:building-3d"]`, vì layer 3D là do app tự thêm.

Lưu ý: ví dụ layer `buildings-3d` ở mục 8 của README chính dùng `#d9d3c9` / 0.9, khác với bản QA của Mobile (`#cfcec9` / 0.48). Chế độ Ban ngày đang theo bản QA, nên chốt lại với team Mobile giá trị nào là chuẩn.
