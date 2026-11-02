# PRD: Release building data 48 ô H3 Hà Nội (mốc 25/11/2026)

> **Owner / sign-off:** BaoNHG · **Squad:** Building (B-01, B-03, GPMB) · **Trạng thái:** 📝 Draft v0.1 (02/11/2026)
> **Data freeze:** 17/11/2026 · **Release:** 25/11/2026 · **Đầu ra:** 1 file Parquet building (tool nội bộ tự convert sang PMTiles cho mobile)

**Tài liệu nguồn:** [B-01 Hub Conflation](https://vtii-jsc.jp.larksuite.com/wiki/SCHiwwjtEiTTXUkGebGj2MFJpAc) · Building Conflation Algorithm Release Note v1.3 · B-03 Building Height · 3D Buildings: hiện trạng và lộ trình · Báo cáo GPMB 20261017 Summary v2 · Mẫu Release note production

---

## 0. TL;DR

Ngày 25/11 phát hành **một file Parquet building cho 48 ô H3 r8 ở Hà Nội** (khoảng 36.912 toà, 38,74 km²). File chỉ được release khi qua cả **3 cổng**:

| # | Cổng | Đạt khi |
|---|---|---|
| G1 | **Conflation** đa nguồn (OSM > GOB > GBA-ms, thuật toán v1.3) | Mọi guardrail ở mục 7 của v1.3 đều pass. Không pass trước 17/11 thì **fallback về footprint OSM** đang chạy trên Production |
| G2 | **Blacklist GPMB**: toà nằm trong vùng giải phóng mặt bằng | Mọi toà giao với vùng GPMB được gắn cờ `is_blacklisted`. **Toà vẫn nằm trong Parquet**; tool convert lọc toà có cờ trước khi lên mobile |
| G3 | **Rule check Building Height** theo quy ước B-03 | Áp dụng 3,5 m/tầng và mặc định 3,5 m cho **mọi toà**; chiều cao hợp lệ 2–500 m; toà T2 không còn cờ mức Cao chưa xử lý; mẫu QC sai số ≤ ±10% |

---

## 1. Problem Statement

Building data trên Production phủ toàn quốc (1.271.894 toà) nhưng **97% toà đang dùng chiều cao mặc định**. Trong 48 ô ưu tiên, chỉ khoảng 2% toà T2 (20/989) có chiều cao thật từ OSM, khoảng 50% đang là mặc định. Hậu quả trên bản đồ 3D: chung cư cao tầng hiện thành khối 1 tầng, có toà cao vô lý, và người dùng dữ liệu không phân biệt được số nào đo thật, số nào là ước.

Có thêm hai vấn đề:

1. **Footprint lấy từ nhiều nguồn chồng lên nhau.** OSM, Google Open Buildings và GBA/Microsoft lệch nhau; có polygon hỏng; có cả bể nước bị nhận nhầm thành nhà.
2. **Một số toà đã hoặc đang bị phá dỡ.** Báo cáo GPMB có 60 tuyến / 22 khu vực nội thành, trong đó nhiều tuyến đi qua AOI (Metro 2 ở Hoàn Kiếm, cầu Trần Hưng Đạo, Nguyễn Sơn ở Long Biên). Các toà này vẫn hiện trên app.

Mốc 25/11 là cam kết trong lộ trình 3D Buildings: 48 ô, chiều cao có nguồn cho toà T2, 9 landmark trên mobile. Nếu không đạt, mốc 25/12 (mở rộng Hà Nội) và 25/01/2027 (sang TP.HCM) sẽ bị đẩy lùi theo, vì cả hai dùng chung pipeline và chung quy ước này.

---

## 2. Phạm vi (Scope)

| Hạng mục | Nội dung |
|---|---|
| Khu vực | **48 ô H3 resolution 8**, tổng 38,74 km². Đã kiểm tra: 48 ID hợp lệ, không trùng, đều ở r8. Danh sách ở [Phụ lục A](#phụ-lục-a--48-ô-h3-r8) |
| Cụm | 4 cụm nghiệp vụ, gồm 3 vùng liền kề: **Tasco Tower / Từ Liêm** (21 ô), **Tasco Mall / Long Biên** (18 ô), **Ba Đình + Hoàn Kiếm** (9 ô, liền nhau) |
| Đối tượng | Toàn bộ building outline và building part trong 48 ô. Toà được gán vào ô theo **tâm toà (centroid)** |
| Số lượng (baseline EDA 28/9) | 36.912 toà: Nhóm 1 có 62 · Nhóm 2 có 1.306 · Nhóm 3 có 35.544. Theo B-03 có **989 toà T2** cần rà soát chiều cao (xem OQ-1) |
| Đầu ra | **1 file Parquet** (EPSG:4326, geometry WKB) kèm QC report và Release note. PMTiles do tool nội bộ convert |

---

## 3. Quyết định đã chốt (Decision log)

| ID | Quyết định | Ngày | Người chốt | Hệ quả |
|---|---|---|---|---|
| D1 | Quy ước chiều cao **B-03 cho tất cả toà**: 3,5 m/tầng; mặc định **3,5 m** (coi là 1 tầng), luôn kèm `height_flag` | 02/11 | BaoNHG | **Bỏ** cách tính 4,0 m/tầng, bỏ trung vị lân cận và mức fallback 7 m ở Step 6–7 của v1.3. Thứ tự ưu tiên nguồn chiều cao theo mục 3.2 của B-03 (GBA chỉ còn là `satellite_est`, xếp thứ 7) |
| D2 | Chiều cao hợp lệ **2–500 m**. Từ ≥ 150 m gắn cờ `very_tall` để review, **không loại** | 02/11 | BaoNHG | Thay ngưỡng `1–150 m` của v1.3. Giới hạn số tầng hợp lệ là 1–100 (thay cho 37,5) |
| D3 | Nếu Conflation v1.3 chưa qua guardrail lúc freeze thì **fallback về footprint OSM** (luật Production hiện tại). Blacklist và height rule vẫn áp dụng | 02/11 | BaoNHG | Phải build sẵn **bản Parquet dự phòng chỉ dùng OSM** trước 17/11 |
| D4 | Toà GPMB **vẫn giữ trong Parquet**, thêm cột cờ | 02/11 | BaoNHG | **Phụ thuộc bắt buộc:** tool convert sang PMTiles (hoặc app) phải lọc theo `is_blacklisted` |
| D5 | Đầu ra chỉ là **Parquet**; tool nội bộ convert sang PMTiles | 02/11 | BaoNHG | Hợp đồng dữ liệu là schema Parquet ở mục 7.4 |
| D6 | **Freeze data toàn bộ từ 17/11**; BaoNHG sign-off release | 02/11 | BaoNHG | Sau 17/11 chỉ sửa lỗi chặn (blocker), mọi thay đổi phải có Release note |

---

## 4. Goals

| # | Goal | Đo bằng | Mục tiêu |
|---|---|---|---|
| G-1 | Mỗi toà trong 48 ô là **một thực thể duy nhất, có nguồn rõ ràng** | Guardrail v1.3, mục 7.1–7.2 | 0 raw source ID bị dùng lại; 100% geometry hợp lệ; 100% toà có `provenance_footprint` |
| G-2 | **Không hiện toà đã hoặc đang bị giải toả** trên app mobile | Đếm toà có `is_blacklisted = true` trong PMTiles sau convert | **0** |
| G-3 | Toà T2 có **chiều cao có nguồn và hợp lý** | Tiêu chí nghiệm thu B-03 | 100% toà T2 đủ field bắt buộc; ưu tiên 1–2 không còn `default`; 0 cờ mức Cao chưa xử lý |
| G-4 | Chiều cao **tin được khi kiểm thực tế** | QC mẫu phân tầng (≥ 10 toà mỗi cụm, đủ 3 tier) | Sai số ≤ ±10%; toà iconic (B-06) sai số ≤ 5% |
| G-5 | **Release truy vết được và có đường lui** | Release note và rollback plan | 100% lần đẩy lên Production từ 21/11 có Release note; rollback ≤ 1 giờ |

---

## 5. Non-Goals

| Không làm | Lý do |
|---|---|
| Dựng hoặc release **landmark 3D** (GLB, 4 style ngày/đêm, LOD) | Thuộc B-06, có kế hoạch riêng. PRD này chỉ dùng chiều cao B-06 để đối chiếu toà iconic |
| **Convert PMTiles** và cấu hình style trên mobile | Tool nội bộ đảm nhận. PRD chỉ định nghĩa hợp đồng Parquet và yêu cầu lọc theo cờ (D4) |
| **Rà tay chiều cao toà T3** (nhà ở) | 35.544 toà; không đủ nguồn lực trước 25/11. T3 dùng chiều cao tự động theo D1 |
| **Xoá hoặc sửa building trên OSM** cho toà GPMB | Cập nhật OSM là việc của cộng đồng và quy trình riêng (bước 10 trong sheet Tien_do). Release chỉ gắn cờ |
| Mở rộng ngoài 48 ô (phường trung tâm Hà Nội 25/12, TP.HCM 25/01) | Có PRD riêng; dùng lại pipeline và config của bản này |
| **Liên kết POI với toà** như một sản phẩm riêng | Chỉ dùng `building_poi` (B-03 B1) làm đầu vào cho `name`. Nghiệm thu POI linking thuộc tài liệu khác |

---

## 6. User Stories

**Người dùng app mobile (gián tiếp)**
- Là người dùng VMAP xem bản đồ 3D, tôi muốn chung cư cao tầng hiện đúng độ cao tương đối, để nhận ra mình đang đứng ở đâu.
- Là người dùng đi qua khu đã giải toả (vd ga Metro 2 ở Hoàn Kiếm), tôi muốn bản đồ không còn hiện toà đã bị phá, để không bị hiểu sai về hiện trạng.

**Data Release (ChiemPT) và chủ tool convert**
- Là người đẩy dữ liệu lên Production, tôi muốn một file Parquet theo schema cố định, có `build_id` và `config_version`, để tool convert chạy không cần sửa.
- Là chủ tool convert, tôi muốn có một cột boolean `is_blacklisted` rõ ràng, để lọc toà GPMB mà không phải hiểu logic GPMB.
- Là người đẩy dữ liệu, tôi muốn có sẵn bản Parquet trước đó và bản dự phòng chỉ dùng OSM, để rollback trong vòng 1 giờ nếu Production lỗi.

**QA/QC**
- Là QC, tôi muốn mỗi toà có `height_provenance` và `height_flag`, để biết toà nào phải kiểm trước.
- Là QC, tôi muốn một file audit liệt kê các toà bị gắn cờ GPMB kèm mã tuyến và tỷ lệ chồng lấn, để đối soát thực địa và gỡ cờ sai.
- Là QC, tôi muốn report diff so với bản Production (thêm, xoá, đổi hình học, đổi chiều cao), để giải thích được mọi thay đổi trước khi duyệt.

**Backend / Data scientist**
- Là backend, tôi muốn bộ tham số conflation và quy ước chiều cao được version hoá trong một config, để build lại cùng input thì ra cùng output (deterministic).
- Là data scientist, tôi muốn guardrail fail thì build bị đánh dấu không hợp lệ, để không ai vô tình release một bản sai.

**Edge cases**
- Toà nằm vắt qua ranh giới hai ô: gán theo centroid, không nhân bản.
- Toà chỉ chạm mép vùng buffer GPMB (sliver): **không** blacklist tự động; đưa vào hàng chờ review (xem R2.3).
- Building part có toà cha bị blacklist: part **kế thừa cờ** của toà cha.
- Toà iconic (B-06) nằm trong vùng GPMB: chặn release và escalate cho owner.

---

## 7. Requirements

### 7.1 G1 · Conflation (P0)

| ID | Yêu cầu | Acceptance criteria |
|---|---|---|
| R1.1 | Chạy Conflation **v1.3** (spec v1.6) trên 48 ô: GBA lineage pre-pass → overlap graph đa nguồn (OSM ∪ GOB ∪ GBA-ms) → resolve theo connected component → chọn footprint theo thứ tự `MapOps > OSM > GOB > GBA-ms` | Tham số giữ như pilot: `MERGE_MIN_IOU = 0.25`, `MERGE_MIN_COVERAGE = 0.40`, `ONE_TO_ONE_MIN_IOU = 0.50`, `COMPONENT_TRUST_IOU = 0.80`. Mọi giá trị khác phải ghi vào `config_version` |
| R1.2 | **Bỏ phần chiều cao của v1.3** (Step 6–7: 4,0 m/tầng, trung vị lân cận, 7 m). Conflation chỉ quyết định footprint và thu thập các ứng viên chiều cao; chiều cao cuối cùng resolve theo G3 | 0 toà có giá trị chiều cao = levels × 4; 0 toà có `height_method` ∈ {`neighborhood_median`, `predicted`} |
| R1.3 | Pass toàn bộ guardrail ở mục 7.1, 7.2 và 7.4 của v1.3 | 0 raw source ID nằm trong > 1 canonical · 0 `gba_google`/`gba_osm` đứng riêng · 100% geometry hợp lệ, không rỗng · `one_to_one` luôn có `group_iou ≥ 0.50` · `building_relations` không trỏ tới ID không tồn tại · parent_id hợp lệ cho 100% khối (yêu cầu của Hub B-01) |
| R1.4 | Sửa **165 polygon hỏng và 33 chỗ chồng lấn** (ticket LuanDT, hạn 12/11) trước freeze | Lần chạy guardrail sau sửa có 0 invalid và 0 same-source overlap chưa xử lý |
| R1.5 | `building_id` **ổn định**: cùng input thì cùng ID; toà đã có trên Production giữ nguyên ID | Diff với Production: số cặp "xoá + thêm" nằm cùng vị trí được báo cáo và giải thích |
| R1.6 | **Fallback (D3):** build sẵn trước 17/11 bản Parquet chỉ dùng OSM (luật Production), qua G2 và G3 | Ngày 17/11 có 2 bản ứng viên; owner chọn 1 bản và ghi lý do vào Release note |
| R1.7 | Hàng chờ review `conflation_issues` (`ambiguous`, `split`, `aggregate`, GBA-ms lớn hơn 5.000 m²) được xử lý hoặc chấp nhận có lý do | Mọi issue mức Cao trong 48 ô đã được đóng, hoặc có quyết định "chấp nhận" ghi rõ người duyệt |

### 7.2 G2 · Blacklist GPMB (P0)

**Đầu vào** (Báo cáo GPMB 20261017 Summary v2):
- Sheet **DS_Way**: **748 OSM way ID** (đường tim của tuyến GPMB), cột buffer mặc định **50 m (25 m mỗi bên tim đường)**. Hiện cả 748 way đều ở trạng thái *"Cần đối soát"* hoặc *"Chưa xác định"*.
- Sheet **Tuyen**: 60 tuyến (`GPMB_001` … `GPMB_060`) với mặt cắt quy hoạch min/max và phạm vi áp dụng: *Dọc tuyến* (34), *Chỉ nút giao* (14), *Dạng điểm / khu ga* (6), *Dạng vùng* (6).
- Sheet **Khu_vuc**: 22 khu vực, mỗi khu có *Nhóm hiện trạng*: Đã xong GPMB · Đang thi công + GPMB · Đang GPMB · Chuẩn bị GPMB.
- Sheet **ThamKhao_NhomC**: các mục dạng vùng, mới chỉ có OSM relation/way để tham khảo, **chưa gán way**.
- **Danh sách `building_id` thủ công** (bổ sung hoặc ngoại lệ) do Ops nhập. Format do owner chốt (OQ-4).

| ID | Yêu cầu | Acceptance criteria |
|---|---|---|
| R2.1 | Dựng **vùng GPMB** cho từng tuyến: buffer đường tim way theo **½ mặt cắt quy hoạch** nếu có, nếu không thì dùng **25 m mỗi bên** (giá trị mặc định trong DS_Way). Tuyến *Chỉ nút giao* hoặc *Khu ga* thì cắt trong bán kính **200 m** quanh nút hoặc ga (cách làm trong sheet `v2_So_sanh_tuyen`). Tuyến *Dạng vùng* dùng polygon hoặc relation tham khảo sau khi owner duyệt | Có 1 lớp `gpmb_zones` (GPKG) gồm `route_id`, `project`, `status_group`, `buffer_m`, `source_version`; mỗi way trong DS_Way thuộc đúng các tuyến của nó |
| R2.2 | Gắn cờ cho toà theo luật chồng lấn: **`overlap_ratio = area(footprint ∩ zone) / area(footprint)`**. `overlap_ratio ≥ 0.30` **hoặc** centroid nằm trong zone thì `is_blacklisted = true`. Cộng thêm mọi `building_id` trong danh sách thủ công. Ngưỡng 0.30 là đề xuất (OQ-3) | Mỗi toà bị gắn cờ có đủ `gpmb_route_ids`, `gpmb_rule` (`buffer` / `zone` / `manual`), `gpmb_overlap_ratio` |
| R2.3 | Toà có `0 < overlap_ratio < 0.30` và centroid nằm ngoài zone: **không** blacklist; gắn `gpmb_review = true` và đưa vào file audit | QC xử lý hết hàng chờ review của 48 ô trước 20/11 |
| R2.4 | Lọc theo hiện trạng: **chỉ blacklist** khu vực ở nhóm *Đã xong GPMB*, *Đang thi công + GPMB*, *Đang GPMB*. Khu ở nhóm *Chuẩn bị GPMB* (vd trục sông Hồng) chỉ gắn `gpmb_status = planned`, **không** blacklist (đề xuất, OQ-5) | Không có toà `planned` nào bị `is_blacklisted = true` |
| R2.5 | Building part **kế thừa** cờ của toà cha. Toà iconic (B-06) bị gắn cờ thì **chặn release** và escalate | 0 part có cờ khác với toà cha; 0 toà iconic bị blacklist mà không có quyết định của owner |
| R2.6 | **Freeze blacklist** cùng lúc với data freeze 17/11. Version hoá bằng `gpmb_source_version` (vd `GPMB_20261017_v2` cộng ngày đối soát) | File audit `gpmb_blacklist_audit.csv`: `building_id, h3_r8, route_id, project, status_group, rule, overlap_ratio, reviewer, decision` |
| R2.7 | **Phụ thuộc (D4):** tool convert lọc `is_blacklisted = true` trước khi sinh PMTiles | Test: Parquet có N toà bị cờ thì PMTiles sau convert có 0 toà trong số đó. Ghi kết quả test vào Release note |

**Giao giữa GPMB và AOI (kiểm sơ bộ ngày 02/11):** Chỉ các tuyến 037–060 có toạ độ điểm đầu/cuối trong file Excel. Trong nhóm này, các tuyến có điểm nằm **trong** 48 ô gồm: GPMB_041, GPMB_042 (ga Metro 2, Đinh Tiên Hoàng / Hàng Bài, Hoàn Kiếm), GPMB_043 (cầu Trần Hưng Đạo phía Hoàn Kiếm), GPMB_044 (Nguyễn Sơn, Long Biên), GPMB_051 (Metro 5, Trần Duy Hưng). GPMB_049 (Metro 5, ga Văn Cao) nằm sát AOI. Các tuyến 001–036 (vd Vành đai 1 qua Ba Đình) chưa có toạ độ trong file, cần lấy hình học way từ OSM để giao.

### 7.3 G3 · Rule check Building Height (P0)

Áp dụng **B-03 cho mọi toà** (D1, D2).

| ID | Yêu cầu | Acceptance criteria |
|---|---|---|
| R3.1 | Hệ số **3,5 m/tầng** (`height_m = building_levels × 3,5`). Chỉ tính tầng nổi. Mặc định **3,5 m** khi không có cả chiều cao lẫn số tầng | 0 toà có chiều cao tính theo × 4 hoặc mặc định 4 m / 7 m; 100% toà có `m_per_level = 3.5` |
| R3.2 | Resolve `height_m` theo **8 mức ưu tiên** của B-03: `osm_height` → `published` → `manual_measure` → `osm_levels` → `poi_levels` → `manual_count` → `satellite_est` (GBA / Open Buildings 2.5D) → `default` | 100% toà có `height_provenance` thuộc 8 nhãn trên. `published` có `source_url`; `manual_*` có người làm và ngày; `poi_levels` có `poi_id`; `satellite_est` có tên dataset |
| R3.3 | Chiều cao hợp lệ **2–500 m**; số tầng hợp lệ **1–100**. Giá trị ngoài khoảng **không dùng làm nguồn**, chuyển xuống mức ưu tiên tiếp theo | 0 toà có `height_m` ngoài [2; 500], trừ `default` = 3,5 m |
| R3.4 | Gắn `height_flag` **tự động**, chỉ gắn cờ, không tự sửa: `highrise_default` (Cao) · `very_tall` ≥ 150 m (Cao) · `needle` ≥ 60 m và đáy < 300 m² (Cao) · `iconic_mismatch` lệch > 5% so với B-06 (Cao) · `levels_height_conflict` khi < 2,5 hoặc > 6 m/tầng (Trung bình) · `large_default` khi đáy ≥ 3.000 m² và đang default (Trung bình) · `no_name` (Thấp) | Bảng thống kê cờ theo mức và theo cụm có trong QC report |
| R3.5 | Toà T2 có đủ field bắt buộc: `name, building_levels, gross_floor_area_m2, footprint_area_m2, land_use, building_use, height_m, height_provenance, m_per_level` | 100% toà T2 (mẫu số theo OQ-1) |
| R3.6 | Ưu tiên rà 1–2 (chung cư cao tầng đang default, toà ≥ 150 m, toà "cây kim", T2_large đang default) **không còn `default`**; **0 cờ mức Cao chưa xử lý** | Báo cáo [B-03][Method] được duyệt trước 14/11 |
| R3.7 | QC mẫu phân tầng: ≥ 10 toà mỗi cụm × 4 cụm, đủ 3 tier; người kiểm khác người nhập | Sai số ≤ ±10% (nhóm không đạt có ghi lý do); toà iconic ≤ 5% |
| R3.8 | Building part: `0 ≤ min_height < height_m`; part không có chiều cao riêng thì lấy chiều cao toà cha, chỉ để render | 0 vi phạm |
| R3.9 | Toàn bộ chạy trong **một build, một config**. Ghi `build_id` và `config_version` vào mọi dòng | 1 giá trị `build_id` duy nhất trong file release |

### 7.4 Hợp đồng đầu ra Parquet (P0)

| ID | Yêu cầu | Acceptance criteria |
|---|---|---|
| R4.1 | 1 file Parquet, EPSG:4326, cột `geometry` dạng WKB (GeoParquet metadata), mỗi dòng là 1 render feature (outline hoặc part) | Tool convert chạy trên file mẫu không lỗi (dry-run trước 17/11) |
| R4.2 | Schema tối thiểu ở bảng dưới; tên cột chốt theo B-03 (OQ-6) | Kiểm schema tự động: đủ cột, đúng kiểu |
| R4.3 | Đối soát số lượng **Input → Parquet → PMTiles khớp 100%**, sau khi trừ toà bị blacklist | Report đối soát đính kèm Release note |

| Nhóm | Cột |
|---|---|
| Định danh | `building_id`, `source_id`, `source_ids`, `component_id`, `parent_building_id`, `is_part`, `has_parts`, `h3_r8`, `area` (cụm), `building_tier` |
| Footprint | `geometry`, `footprint_area_m2`, `provenance_footprint`, `conflation_relation_type`, `quality_score`, `quality_label` |
| Chiều cao | `height_m`, `min_height`, `building_levels`, `levels_underground`, `height_provenance`, `m_per_level`, `height_flag` |
| Thuộc tính | `name`, `project_id`, `poi_id`, `gross_floor_area_m2`, `land_use`, `building_use`, `address` |
| GPMB | `is_blacklisted` (bool), `gpmb_status` (`cleared` / `in_progress` / `planned` / null), `gpmb_route_ids`, `gpmb_project`, `gpmb_rule`, `gpmb_overlap_ratio`, `gpmb_review`, `gpmb_source_version` |
| Phiên bản | `build_id`, `config_version`, `conflation_version` (`tu-liem-v1.3` hoặc `osm-fallback`) |

### 7.5 Release gate và vận hành (P0)

| ID | Yêu cầu | Acceptance criteria |
|---|---|---|
| R5.1 | QC report tự động cho mỗi build: số toà theo `provenance_footprint`, `provenance_height`, `conflation_relation_type`, `height_flag`, `is_blacklisted`; phân bố chiều cao; kết quả guardrail; version các ngưỡng | Report có cho build ứng viên ngày 17/11 và mọi build sửa lỗi sau đó |
| R5.2 | **Diff với bản Production hiện tại** (tab *So sánh 2 file* của Building Data Studio): thêm / xoá / đổi hình học / đổi chiều cao / đổi nguồn chiều cao; kết luận Pass / Warn / Fail | Kết luận không phải Fail; mọi Warn có giải thích |
| R5.3 | **Release note** theo mẫu cho **mỗi lần** đẩy lên Production từ 21/11: lớp dữ liệu, khu vực, số bản ghi thêm/sửa/xoá, `batch_id`, các kiểm tra và ngưỡng, khác biệt, lỗi đã biết, cách quay lại, báo cho ai (Search, Mobile, B2B) | 100% lần đẩy có Release note do BaoNHG duyệt (go / no-go) |
| R5.4 | **Rollback:** giữ bản Parquet và PMTiles Production trước đó; chuẩn bị phương án cùng Backend trong 21–24/11 | Diễn tập rollback trên staging ≤ 1 giờ |

### 7.6 Nice-to-have (P1)

- **Thẻ điểm theo từng ô H3:** số toà, % có chiều cao thật, số toà bị blacklist, số cờ Cao. Dùng để duyệt hoặc rollback từng ô.
- **Fallback theo từng ô:** ô nào qua guardrail thì dùng v1.3, ô nào không qua thì dùng bản OSM.
- Thêm cột `gpmb_effective_date`: chỉ cần đổi trạng thái GPMB, không phải rebuild cả bản.
- Lớp QGIS (GPKG) cho QC xem cùng lúc `gpmb_zones`, toà bị cờ và hàng chờ review.

### 7.7 Future Considerations (P2)

- Thay buffer ước lượng bằng **ranh chỉ giới đường đỏ / bản đồ thu hồi đất** (bước 7 trong sheet Tien_do). Schema `gpmb_rule = zone` đã có chỗ cho việc này.
- Đồng bộ blacklist với diff OSM hằng ngày, để toà tự gỡ cờ khi OSM đã xoá.
- Script tự kiểm footprint sau AOI (ticket LuanDT, 11/12).
- Đánh giá lại phương án trung vị lân cận cho T3 khi mở rộng sang 25/12. Giữ được nhờ `height_provenance` tách riêng.
- Tái dùng pipeline cho phường trung tâm Hà Nội (25/12) và TP.HCM (25/01/2027).

---

## 8. Success Metrics

**Leading: đo tại release gate (17–25/11)**

| Metric | Success | Stretch | Cách đo |
|---|---|---|---|
| Vi phạm guardrail conflation | 0 | 0 | QC report R5.1 |
| Geometry hợp lệ | 100% | 100% | `ST_IsValid` |
| Toà T2 đủ field bắt buộc | 100% | 100% | Kiểm schema và null check |
| Toà T2 có nguồn chiều cao ≠ `default` | ≥ 80% | ≥ 95% | Đếm `height_provenance` (baseline khoảng 50%) |
| Cờ mức Cao chưa xử lý | 0 | 0 | Đếm `height_flag` |
| Sai số QC mẫu | ≤ ±10% | ≤ ±5% | Mẫu phân tầng R3.7 |
| Toà bị blacklist còn trên PMTiles | 0 | 0 | Test R2.7 |
| Đối soát Input → Parquet → PMTiles | 100% | 100% | R4.3 |

**Lagging: đo 2–4 tuần sau release**

| Metric | Mục tiêu | Cách đo |
|---|---|---|
| Báo lỗi building / 3D trong 48 ô (Mobile, Search, B2B) | ≤ 5 lỗi hợp lệ trong 4 tuần | Ticket theo Release note |
| Số hotfix release cho 48 ô | ≤ 1 | Lịch sử Release note |
| Toà GPMB bị báo sai (đã xoá nhầm hoặc còn sót) | ≤ 2% số toà bị blacklist | Đối soát thực địa thí điểm (Tam Trinh, Lĩnh Nam, QL6 và các ô AOI) |
| Thời gian build và release mỗi ô (đầu vào cho mốc 25/12) | Có số đo baseline | Log pipeline (hiện chưa đo) |

---

## 9. Timeline

| Thời gian | Việc | Owner |
|---|---|---|
| 01–06/11 | Chốt 48 ô, chuẩn nghiệm thu; B-03 B1 gán POI (04/11), B2 bộ tag (05/11), B3 lọc T2 (06/11) | BaoNHG, B-03 team |
| **06/11** | **Chốt OQ chặn (OQ-1 → OQ-5)**; xin hình học way GPMB_001–036 | BaoNHG, QuangNV |
| 07–12/11 | Chạy v1.3 trên 48 ô; sửa 165 polygon và 33 chồng lấn (12/11); dựng `gpmb_zones` và chạy blacklist lần 1; B-03 B4 rà 989 toà T2 (12/11) | QuangNV, LuanDT, QC |
| 13–14/11 | Báo cáo [B-03][Method] (14/11); dry-run tool convert trên Parquet mẫu; build bản dự phòng OSM | BaoNHG, ChiemPT |
| **17/11** | **DATA FREEZE**: chọn v1.3 hay OSM fallback, freeze blacklist, build ứng viên (RC1) | BaoNHG |
| 17–20/11 | QC report, diff với Production, kiểm hiển thị 3D, xử lý hàng chờ GPMB review | QC, ChiemPT |
| 21–24/11 | Chỉ sửa lỗi chặn (mỗi lần đẩy có Release note); diễn tập rollback cùng Backend; xuất PMTiles (24/11) | ChiemPT, Backend |
| **25/11** | **Release** và kiểm tra trên Production; BaoNHG sign-off | BaoNHG |

**Phụ thuộc:** POI nội bộ (squad POI, cho B1) · chiều cao B-06 cho toà iconic · tool convert hỗ trợ lọc `is_blacklisted` · hình học OSM của 748 way GPMB · Backend cho rollback.

---

## 10. Open Questions

| ID | Câu hỏi | Ai trả lời | Chặn? |
|---|---|---|---|
| OQ-1 | Mẫu số nghiệm thu T2 là **989 toà** (B-03, tiêu chí `building_tier` bắt đầu bằng T2, không phải khối cha) hay **1.306 toà Nhóm 2** (lộ trình)? Phần chênh 317 toà là khối cha hay do phiên bản phân nhóm khác nhau? | Data / BaoNHG | **Có**, trước 06/11 |
| OQ-2 | Tool convert đã lọc được `is_blacklisted` chưa? Nếu chưa, ai sửa, trước ngày nào? | Chủ tool convert / ChiemPT | **Có**, trước 13/11 |
| OQ-3 | Chốt ngưỡng `overlap_ratio` để blacklist (đề xuất 0,30 hoặc centroid nằm trong zone)? | BaoNHG / QC | **Có**, trước 07/11 |
| OQ-4 | Danh sách `building_id` GPMB thủ công: ai cung cấp, file gì, hạn chót? | Ops GPMB | **Có**, trước 14/11 |
| OQ-5 | Khu *Chuẩn bị GPMB* (trục sông Hồng) và *Đang GPMB* có blacklist không, hay chỉ blacklist *Đã xong GPMB*? | BaoNHG | **Có**, trước 07/11 |
| OQ-6 | Tên cột cuối cùng: theo B-03 (`height_m`, `height_provenance`) hay theo v1.3 (`height_render`, `provenance_height`)? Tool convert và mobile đang đọc tên nào? | Backend / Mobile | **Có**, trước dry-run 13/11 |
| OQ-7 | 748 way vẫn ở trạng thái *"Cần đối soát"*. Mức đối soát tối thiểu trước freeze là gì: các tuyến giao AOI phải xác minh 100%? | BaoNHG / QC | Không, nhưng cần xong trước 17/11 |
| OQ-8 | GPMB_040, GPMB_059 chưa có way; các mục Nhóm C dạng vùng chỉ có tham khảo. Có tuyến nào trong số này giao 48 ô không? | GPMB team | Không |
| OQ-9 | Với T3, chuyển từ mặc định hiện tại sang 3,5 m có làm thay đổi lớn trong diff (vd khoảng 1,1 triệu dòng đổi chiều cao như v4 → v7)? Có cần báo trước cho Mobile, Search, B2B không? | Data / BaoNHG | Không |

---

## 11. Rủi ro

| Rủi ro | Ảnh hưởng | Giảm thiểu |
|---|---|---|
| Thuật toán gộp nguồn v1.3 chưa chốt chính thức | 48 ô trễ sau 25/11 | D3: bản dự phòng OSM build sẵn trước 17/11 |
| Tool convert **không lọc** `is_blacklisted` | Toà đã giải toả vẫn hiện trên app, vi phạm G-2 | OQ-2. Phương án cuối: xuất thêm một file Parquet đã loại toà bị cờ, dùng riêng để convert |
| Buffer 25 m ước lượng sai so với chỉ giới thực | Blacklist nhầm toà còn tồn tại, hoặc sót toà đã phá | Hàng chờ review R2.3, đối soát thực địa các tuyến giao AOI, P2 dùng chỉ giới đường đỏ |
| Chuyển sang 3,5 m mặc định làm toàn bộ T3 thấp đi | Diff lớn; khu nhà thấp có thể trông "phẳng" | Ghi rõ trong Release note (D1); theo dõi phản hồi của Mobile; xem lại ở P2 |
| GBA (`satellite_est`) bị hạ ưu tiên xuống mức 7 | Mất nguồn chiều cao tự động tốt cho T2 chưa rà | Rà tay B-03 B4 cho ưu tiên 1–2; ghi số toà có `satellite_est` trong QC report |
| Nguồn lực QC dồn vào 17–20/11 | Trễ gate | Chạy blacklist và QC lần 1 từ 07–12/11 |

---

## Phụ lục A · 48 ô H3 r8

```
88415cb4a1fffff 88415cb4a5fffff 88415cb4a9fffff 88415cb4adfffff 88415cb4e7fffff 88415cb5d3fffff
88415cb5dbfffff 88415cb4e5fffff 88415cb4edfffff 88415cb425fffff 88415cb427fffff 88415cb42dfffff
88415cb503fffff 88415cb50bfffff 88415cb511fffff 88415cb513fffff 88415cb515fffff 88415cb519fffff
88415cb51bfffff 88415cb51dfffff 88415cb551fffff 88415cb553fffff 88415cb555fffff 88415cb557fffff
88415cb55bfffff 88415cb5c5fffff 88415cb5cdfffff 8841436901fffff 8841436905fffff 8841436909fffff
884143690dfffff 8841436929fffff 884143692bfffff 8841436941fffff 8841436943fffff 8841436945fffff
8841436947fffff 8841436949fffff 884143694bfffff 884143694dfffff 8841436961fffff 8841436963fffff
8841436967fffff 8841436969fffff 884143696bfffff 8841436b27fffff 88415cb493fffff 88415cb49bfffff
```

Đã kiểm tra: 48 ID hợp lệ, không trùng, đều ở resolution 8, tổng diện tích 38,74 km². Gồm 3 vùng liền kề: tâm khoảng (21,020; 105,774) có 21 ô, (21,046; 105,905) có 18 ô, (21,036; 105,842) có 9 ô.

## Phụ lục B · Tóm tắt GPMB (bản 17/10/2026, v2)

- 60 tuyến / 22 khu vực / 12 quận nội thành cũ; 748 way trong DS_Way (v2). Bản cũ (`v2_DS_Way_cu`) có 1.338 way; sheet `v2_Way_bo` liệt kê 727 way đã bỏ khi chuyển sang v2.
- Nhóm hiện trạng: **Đã xong GPMB** (10 khu) · **Đang thi công + GPMB** (6) · **Đang GPMB** (5) · **Chuẩn bị GPMB** (1, trục sông Hồng).
- Tiến độ trong sheet Tien_do: xong các bước 1–4 và 11. **Chưa xong** các bước 5 (chốt điểm đầu/cuối), 6 (buffer theo mặt cắt), 7 (xin ranh chỉ giới), 8 (thống kê POI), 9 (đối soát thực địa), 10 (chốt blacklist). Bước 6 và 10 nằm trên đường găng của PRD này.
