# building_tier: dữ liệu cho luật tier

`iconic_pins.geojson`: các pin của danh sách "3D Building Landmark" (luật 1 của `building_tier_v1`, [B-05] Quy chuẩn Building Tier). Toà chứa pin (lấy footprint lớn nhất), hoặc footprint gần pin nhất trong 30 m (cờ `iconic_pin_nearest`), nhận tier `T1_iconic`.

- Mỗi pin là một `Point` (EPSG:4326) với `name`. `osm_id` và `source` chỉ để tra cứu.
- Bản đầu lấy 7 toà `T1_iconic` của `canonical_building_v7_20261028`. Danh sách chốt 30/9 có 9 toà: còn thiếu **Sân vận động Mỹ Đình** và **Toà nhà Viettel**. Thêm 2 pin này (toạ độ trong My Maps) vào file khi có.
- Mọi building trong thư viện landmark (có GLB) cũng nhận `T1_iconic`, không cần pin.

Sửa file này không đổi footprint, chiều cao hay building_id: chỉ cột `building_tier` của lần build sau.
