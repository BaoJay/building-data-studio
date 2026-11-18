# Yêu cầu cập nhật landmark 3D cho Tasco Maps

Ngày: **27/10/2026**  
Đối tượng nhận: **Team 3D / Technical Artist**  
Phạm vi đợt đầu: **Tasco Tower và Keangnam Hanoi Landmark 72**  
Trạng thái: Owner đã chốt phạm vi công việc; budget thiết bị và contract xuất dữ liệu cần team kỹ thuật thống nhất trước khi xuất bản cuối.

## 1. Mục tiêu

Cập nhật các model hiện có để:

- Hiển thị nhất quán trong bốn chế độ **Dawn (bình minh), Day (ban ngày), Dusk (hoàng hôn), Night (ban đêm)**. Tên đúng là Dusk, không phải Dust.
- Có cửa sổ, logo và biển hiệu phát sáng có kiểm soát ở Night; có thể điều chỉnh mức phát sáng khi chuyển chế độ.
- Có LOD cho khoảng nhìn gần, trung bình và xa để giảm tải mobile, giữ đặc điểm nhận diện của công trình.
- Có dữ liệu và tài liệu bàn giao đủ để app/pipeline triển khai, kiểm thử và bảo trì mà không phải đoán ý nghĩa vật liệu.

**Không yêu cầu dựng lại toàn bộ model. Không tạo bốn bộ geometry riêng cho bốn chế độ ánh sáng.** Team giữ lại các phần tốt và sửa đúng các đầu mục bên dưới.

## 2. Đầu vào đã kiểm chứng

Đã lấy GLB nguồn từ MapOps, đối chiếu SHA256/kích thước với phiên bản released và bundle LMK3 tương ứng.

| Thuộc tính | Tasco Tower | Keangnam Hanoi Landmark 72 |
|---|---|---|
| Phiên bản trên MapOps | v10 | v2 |
| File nguồn | `Tasco_Tower_v1_shell_textured_web_nodraco.glb` | `keangnam_hanoi_landmark_tower_v4_shell_textured_web_nodraco.glb` |
| Version ID | `01a14038-c0f3-7c40-add6-c65036341d5b` | `01a143ce-245b-771f-bcee-0e72fca3b933` |
| SHA256 GLB | `3933b85724597435e15e4d85846f0a1695d76e766c0707f524b87deb9e6a92e3` | `65055d986de04a18c57891383dfcb4dac9a7e2cd6c131c1479077a2695b9d2ad` |
| Kích thước GLB | 726.464 byte | 756.600 byte |
| Tam giác GLB | 12.236 | 26.659 |
| Số vật liệu | 16 | 29 |
| Vật liệu gắn `KHR_materials_unlit` | 16/16 | 29/29 |
| Vật liệu `doubleSided` | 16/16 | 29/29 |
| Vật liệu BLEND / MASK | 0 / 0 | 0 / 0 |
| Emissive texture / emissive factor khác 0 | Không / Không | Không / Không |
| Transmission / material variants | Không / Không | Không / Không |
| Vertex normals | Có ở mọi primitive được kiểm | Thiếu ở ít nhất một primitive |

### Ý nghĩa của hiện trạng

- `unlit` nghĩa là vật liệu không chịu ánh sáng trong renderer tuân theo glTF. Pipeline mobile hiện không bảo toàn thuộc tính này và app chiếu sáng lại. **Đây là phần pipeline/app phải sửa hoặc thống nhất chuyển đổi, không quy toàn bộ lỗi màu cho team 3D.**
- Muốn vật liệu phản ứng đúng với đèn của bốn preset, cần author vật liệu lit với màu nền phù hợp; không chỉ xóa cờ unlit một cách máy móc.
- Cửa kính hiện là vật liệu opaque hoặc ảnh màu nền. Đây có thể là lựa chọn hợp lý cho bản đồ; không mặc định coi thiếu transparency là lỗi.
- GLB chưa có dữ liệu emissive tách riêng, nên chưa thể điều khiển vùng cửa phát sáng đúng yêu cầu Night.
- Pipeline hiện tạo mặt ngược cho doubleSided, khiến số tam giác LMK3 gần gấp đôi: Tasco 24.376, Keangnam 53.047. Cần tối ưu dữ liệu mặt hai phía có chủ ý; không dùng các số này làm budget mới.

## 3. Phân công trách nhiệm

| Hạng mục | Team 3D | Team app/backend/rendering |
|---|---|---|
| Geometry, UV, normals, LOD | Author, tối ưu và kiểm chất lượng | Nạp, chọn và chuyển LOD |
| Vật liệu và texture | Author theo contract thống nhất | Bảo toàn thuộc tính khi bake; triển khai shader |
| Cửa/biển hiệu sáng | Mask/texture, màu và mức sáng tương đối | Điều khiển cường độ theo preset, chuyển mượt |
| Bóng đổ | Geometry có normals/winding đúng; shadow proxy nếu hai bên thống nhất | Đèn, shadow pass, nhận/đổ bóng, giới hạn GPU |
| Budget | Báo số liệu và tối ưu theo budget đã chốt | Đề xuất budget theo thiết bị, đo và xác nhận |
| Nghiệm thu | Duyệt hình dáng/vật liệu và hình tham chiếu | Kiểm binary/data thật trên iOS/Android trong phạm vi release |
| Publish | Bàn giao candidate có version | Tích hợp, kiểm thử, promote/rollback theo quy trình |

Team 3D không phải sửa code app hoặc chịu trách nhiệm FPS của toàn app. Team kỹ thuật không tự đoán emissive mask hoặc thay đổi vật liệu ngoài ý đồ đã duyệt.

## 4. Công việc chi tiết của team 3D

### A01 — Chuẩn hóa lit/unlit và màu nền

- Phân loại từng vật liệu: lit, emissive, hoặc ngoại lệ unlit có lý do.
- Chuyển tường/mái/kính và các phần cần nhận ánh sáng sang lit. Đối chiếu màu sau chuyển đổi, tránh cháy sáng hoặc tối bệt.
- Màu nền/baseColor cần thể hiện màu bề mặt; không chứa bóng mặt trời cố định hoặc ánh sáng ngày/đêm đã bake cho phần cần đổi theo thời điểm.
- Nếu có AO, tách hoặc mô tả rõ vị trí lưu và cách áp; tránh nhân AO hai lần. Không yêu cầu loại bỏ AO tiếp xúc chỉ vì không dùng bóng mặt trời bake.
- Phần unlit giữ lại phải được liệt kê trong bảng vật liệu cùng lý do và hình minh họa.

**Đầu ra:** bảng lit/unlit, texture màu nền và hình so sánh trước/sau. Không tự coi việc gỡ `KHR_materials_unlit` là đã hoàn tất.

### A02 — Cửa, logo và biển hiệu phát sáng

- Tạo emissive mask/texture có UV khớp bề mặt. Vùng không phát sáng bằng 0.
- Tách được cửa với khung cửa/tường; không làm cả mặt đứng hoặc kính đều sáng như một tấm đèn.
- Xác định vùng cửa bật/tắt có chủ ý, màu ấm/lạnh và độ sáng tương đối. Phân bố phải ổn định; không ngẫu nhiên thay đổi từng frame.
- Logo/biển hiệu nếu cần điều khiển độc lập phải có material hoặc mask channel nhận diện riêng, theo contract hai bên chốt.
- Bàn giao hình Night tham chiếu cùng đề xuất mức phát sáng tương đối ở Dawn/Day/Dusk/Night. App quyết định giá trị cường độ cuối dựa trên renderer và nghiệm thu.
- Không bake cửa đêm trực tiếp vào baseColor như giải pháp duy nhất.

**Ranh giới:** cửa nhìn phát sáng, bloom/quầng sáng, và ánh sáng thực sự chiếu lên môi trường là ba hiệu ứng riêng. Đợt đầu cần dữ liệu cho cửa phát sáng; bloom/chiếu sáng môi trường không mặc định nằm trong phần team 3D phải sản xuất.

### A03 — Phân loại và chuẩn hóa vật liệu

- Phân biệt tường/đá/bê tông, kim loại, kính, biển hiệu và phần emissive.
- Đặt tên ổn định, có ý nghĩa; ghi rõ mesh/primitive sử dụng từng material.
- Với hướng lit, author roughness/metallic phù hợp. Không mặc định dùng cùng một bộ thông số cho tất cả bề mặt.
- Normal map chỉ bổ sung nếu cần và pipeline đã hỗ trợ; không bắt buộc tạo để đủ checklist.
- Không thêm extension như clearcoat/transmission/volume hoặc texture nén đặc biệt khi chưa có xác nhận hỗ trợ từ team kỹ thuật.

**Đầu ra:** material table, texture liên quan và khai báo extension sử dụng.

### A04 — Kính

- Đề xuất mặc định: kính opaque với màu/độ phản chiếu phù hợp, tránh overdraw và nhìn xuyên vào nội thất không tồn tại.
- Chỉ dùng alpha BLEND khi thực sự cần nhìn xuyên; chỉ dùng MASK cho vùng cần cắt lỗ, theo contract.
- Ghi rõ alphaMode, alphaCutoff nếu có, mặt cần nhìn từ hai phía và yêu cầu nội thất.
- Kiểm kính không làm mất silhouette, không lộ mặt sau sai hoặc phát sáng cả mặt tường.

**Lưu ý:** emissive cho ô cửa không đòi hỏi kính phải BLEND/transmission.

### A05 — Normals, winding và doubleSided

- Kiểm hướng mặt ra ngoài, normals hữu hạn/đúng, cạnh cứng/mềm có chủ ý.
- Bổ sung vertex normals ở các primitive thiếu; đặc biệt kiểm Keangnam.
- Tắt doubleSided cho vỏ tòa nhà kín có thể render một phía.
- Chỉ giữ doubleSided cho các mặt thực sự cần nhìn cả hai phía; liệt kê ngoại lệ.
- Không đảo toàn bộ normals hoặc bật doubleSided toàn bộ để che lỗi topology.

**Đầu ra:** model không mất mặt khi orbit; bảng vật liệu/mặt còn doubleSided và lý do.

### A06 — Ba mức LOD

| Mức | Mục tiêu hình ảnh | Cách xử lý |
|---|---|---|
| Gần | Giữ chi tiết cần thiết khi quan sát tòa nhà | Geometry chi tiết có chọn lọc, vật liệu/texture đủ nét |
| Trung bình | Giữ silhouette, khối chính, mặt đứng và nhận diện | Giảm chi tiết nhỏ, geometry phụ; giảm số material/texture nếu hợp lý |
| Xa | Giữ hình khối nhận diện ở kích thước màn hình nhỏ | Mesh đơn giản, ít draw call và texture; app có thể dùng extrusion fallback theo thiết kế |

- Ba mức dùng cùng điểm neo, hệ trục, đơn vị, hướng, tỷ lệ và footprint phù hợp.
- Không làm đứt mái, mất tháp chính, lệch logo/đặc điểm nhận diện hoặc nhảy vị trí khi chuyển.
- Mỗi LOD cần emissive/UV tương thích với chính LOD đó; tránh cửa nhấp nháy hoặc đổi vị trí rõ rệt khi chuyển mức.
- Có thể dùng công cụ decimation tự động nhưng phải kiểm lại silhouette, UV, normals và material.
- Team 3D bàn giao số liệu trước/sau. **Chưa chốt ngưỡng zoom, tỷ lệ giảm polygon hay số tam giác cứng trong tài liệu này.** App chọn mức theo kích thước trên màn hình và budget thiết bị.

### A07 — UV và texture

- UV không kéo giãn sai, không chồng không chủ ý; có padding đủ cho mipmap và atlas.
- Mật độ chi tiết tương xứng với tầm nhìn: không cấp texture lớn cho chi tiết rất nhỏ.
- Gộp atlas/vật liệu khi phù hợp; bỏ ảnh trùng và tài nguyên không dùng. Không gộp opaque/blend/emissive khác semantics chỉ để giảm số material.
- Giữ bản texture nguồn và bản mobile export. Ghi kích thước, số kênh, vai trò và color space cho từng ảnh.
- Màu baseColor/emissive và dữ liệu mask/normal/roughness phải được khai báo cách đọc đúng; không đóng gói channel tùy ý khi chưa thống nhất.
- Ghi nhận baseline pipeline hiện thường resize texture thường tối đa1024, prelit2048; đây là hiện trạng, **không phải budget mới đã duyệt**.

### A08 — Geometry cho bóng đổ và chất lượng hiển thị

- Bỏ mặt trùng, mặt đồng phẳng chồng nhau gây z-fighting, geometry ẩn không phục vụ hiển thị.
- Sửa lỗi topology/winding ảnh hưởng silhouette và bóng; không bắt buộc mọi chi tiết trang trí phải là khối kín nếu không cần.
- Không bake bóng mặt trời cố định vào baseColor của bề mặt cần phản ứng theo preset.
- Nếu team kỹ thuật cần shadow proxy riêng: thống nhất format/anchor/LOD trước khi làm. Chưa tự sản xuất thêm một bộ proxy chưa có nơi sử dụng.

### A09 — Vị trí, kích thước và điểm neo

- Đơn vị mét; khai báo hệ trục và quy ước export rõ ràng, phù hợp glTF và pipeline hiện hành.
- Điểm neo tại chân tòa theo quy ước thống nhất; giữ đúng chiều cao, kích thước và tỷ lệ.
- Kiểm orientation/heading, không tự bake heading vào geometry rồi app áp thêm lần nữa.
- Các LOD phải cùng anchor. Kiểm model không chìm/nổi khỏi mặt đất hoặc lệch footprint sau khi app chuyển tọa độ.
- Không tự đổi vị trí địa lý/chiều cao hiện hành chỉ dựa trên cảm quan; nếu phát hiện sai thì báo chênh lệch và căn cứ.

### A10 — Tham chiếu hình ảnh và hồ sơ

- Cùng camera/góc nhìn cho ảnh so sánh Dawn/Day/Dusk/Night; ghi điều kiện đèn/viewer dùng để tạo ảnh.
- Ít nhất có: toàn tòa, góc xiên thấy mặt đứng và mái, cận kính/cửa, cận biển hiệu nếu có.
- Ảnh kiểm từng LOD và ảnh normals/wireframe hoặc thống kê tương đương.
- Giữ project nguồn có thể chỉnh sửa, texture nguồn và thiết lập export/bake.
- Ghi rõ nguồn tài sản và quyền sử dụng/phân phối trong app; không đưa texture không rõ quyền vào candidate.

## 5. Contract và budget phải thống nhất trước bản xuất cuối

Team kỹ thuật cung cấp/xác nhận:

- Thiết bị mục tiêu và budget geometry, draw call/material, texture giải nén, RAM/VRAM cho mỗi LOD và tập landmark cùng hiển thị.
- Bộ material hỗ trợ, cách encode lit/unlit, emissive/mask/channel/color space; transparency và extension nào được chấp nhận.
- Kiểu đóng gói LOD/manifest, naming/versioning, anchor và bake transform.
- Preset đèn tham chiếu và cách chuyển mode; cường độ emissive chuẩn; hỗ trợ shadow và shadow proxy nếu có.
- Công cụ validate/export và fixture kiểm trên app.

**Baseline hiện tại:** Go baker chưa hỗ trợ required Draco/meshopt/KTX2 và chưa có luồng emissive hoàn chỉnh. Dùng GLB không nén Draco/meshopt và ảnh PNG/JPEG cho mẫu tích hợp ban đầu, trừ khi kỹ thuật xác nhận hỗ trợ mới. Có thể bàn giao texture emissive rời và metadata trước; không coi dữ liệu đã chạy được trên app khi pipeline chưa nâng cấp.

Nếu budget/contract chưa có, team 3D vẫn làm audit, normals, phân loại vật liệu và mẫu thử; báo rõ phần chờ. Không tự chốt số để tuyên bố đạt production.

## 6. Quy trình làm và review

1. **Nhận việc:** xác nhận file/version đầu vào, người phụ trách, liệt kê vướng mắc và ước lượng theo đầu mục A01–A10.
2. **Mẫu tích hợp nhỏ:** chọn một phần Tasco gồm tường, kính và cửa/biển hiệu emissive; bàn giao GLB + mask + bảng vật liệu. Team kỹ thuật chạy qua pipeline và renderer để xác nhận dữ liệu không bị mất nghĩa.
3. **Chốt mẫu:** hai bên kiểm cùng ánh sáng/góc nhìn; chốt contract và budget. Không mở rộng toàn bộ bộ model khi mẫu chưa được đọc đúng.
4. **Hoàn thiện Tasco:** vật liệu, geometry, LOD và bộ ảnh; review dữ liệu rồi kiểm trên app.
5. **Hoàn thiện Keangnam:** áp chuẩn đã kiểm, chú ý vertex normals, mặt hai phía và độ phức tạp geometry.
6. **Bàn giao candidate:** file có version/hash, checklist đầy đủ, ghi rõ phần còn thiếu. Team app thực hiện nghiệm thu thiết bị trước khi phát hành.

Không ghi đè hoặc release/unrelease bản production hiện tại trong bước sản xuất dữ liệu. Nếu quy trình cần upload draft MapOps, phải dùng version mới và tuân theo quyền/quy trình của dự án.

## 7. Cấu trúc gói bàn giao đề xuất

```text
landmark-3d-delivery-<revision>/
  README.md
  CHANGELOG.md
  material-contract.md
  budget-and-statistics.csv
  known-limitations.md
  SHA256SUMS
  tasco-tower/
    source/                 # project chỉnh sửa được + hướng dẫn mở
    export/                 # GLB LOD gần/trung/xa, ghi rõ tên mức
    textures-source/
    textures-mobile/
    material-table.csv
    model-metadata.json
    reference/              # ảnh 4preset, LOD và góc cận cần thiết
  keangnam/
    ...
```

Metadata cần có: landmark name/ID, source version, delivery revision, đơn vị/hệ trục/anchor, transform đã bake hay chưa, bounds/height, LOD identity, triangle/vertex/material counts, texture dimensions/formats, extensions, lit/unlit/doubleSided/transparency/emissive mapping. Không đặt credential, token hoặc URL có chữ ký vào gói bàn giao.

Gợi ý cột `material-table.csv`: `material_id,name,mesh_or_primitive,surface_type,lit_or_unlit,alpha_mode,double_sided,base_color_texture,normal_texture,emissive_texture,emissive_group,notes`.

## 8. Checklist nghiệm thu dữ liệu team 3D

- [ ] Đúng model nguồn và phiên bản đã thống nhất; có revision mới và changelog.
- [ ] Lit/unlit có chủ ý; mọi ngoại lệ được mô tả.
- [ ] Màu nền không chứa ánh sáng/bóng cố định gây chiếu sáng hai lần.
- [ ] Emissive mask đúng vùng; không sáng lan sang tường/khung cửa; có hình Night tham chiếu.
- [ ] Kính có alpha policy rõ; không bật BLEND đại trà.
- [ ] Normals/winding đúng; doubleSided chỉ giữ nơi cần; Keangnam không còn primitive thiếu normals ngoài ngoại lệ được duyệt.
- [ ] Ba LOD cùng anchor/scale/orientation, giữ hình dáng nhận diện; không rách UV/mất mặt.
- [ ] Texture/UV/atlas được kiểm; số liệu từng LOD và dung lượng đầy đủ.
- [ ] Hình kiểm bốn preset dùng điều kiện đèn/camera được ghi lại; đã xem từ nhiều góc.
- [ ] GLB export mở/validate được; không có tài nguyên ngoài bị thiếu hoặc extension chưa thống nhất.
- [ ] Project/texture nguồn, metadata, checksum và known limitations đầy đủ.
- [ ] Có xác nhận mẫu đã qua pipeline không mất thuộc tính; nếu chưa, ghi **INTEGRATION_PENDING**.

## 9. Ranh giới nghiệm thu production

Hoàn thành checklist dữ liệu **không đồng nghĩa app đã production complete**. Team kỹ thuật còn phải kiểm:

- Bake GLB → LMK3 giữ đúng semantics, identity và material mapping.
- Render đúng lit/unlit/emissive/kính; bóng và landmark đồng bộ bốn preset.
- LOD transitions, fallback nhà gốc, zoom/style reload không hở/chồng/mất model.
- Frame pacing, CPU/GPU, memory, tải/cache/lifecycle trên thiết bị; FPS59–60 theo phase phù hợp, không chỉ số trung bình.
- Publish/rollback và chất lượng đúng artifact đang release.

Chỉ sau hai lớp nghiệm thu dữ liệu và tích hợp thiết bị mới chốt candidate để phát hành.

## 10. Mẫu phản hồi đầu tiên của team

```text
Người phụ trách:
Đã nhận đúng Tasco v10 / Keangnam v2:
Công cụ và phiên bản phần mềm:
Ước lượng A01–A10 (có thể gộp mục liên quan):
Thời điểm bàn giao mẫu Tasco nhỏ:
Thông tin cần team kỹ thuật chốt:
Rủi ro/chất lượng có thể ảnh hưởng:
Các tài sản nguồn/quyền sử dụng còn thiếu:
```
