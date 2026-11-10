# Handoff cho team Mobile: landmark 3D theo 4 chế độ ánh sáng VMAP (MVP Tasco Tower)

Ngày: **10/11/2026** · Từ: Building Data Studio · Gửi: **team Mobile** (renderer landmark LMK3 trên MapLibre Native, iOS và Android) và người phụ trách Go baker (GLB → LMK3)

**Phạm vi:** bản MVP cho **Tasco Tower**. GLB đã có vật liệu lit, đèn ban đêm, lightmap, kính và 3 mức LOD. Các landmark khác sẽ được làm lại theo đúng chuẩn trong tài liệu này sau khi Mobile chạy được bản Tasco.

**Đây là tài liệu đầy đủ và duy nhất.** Nó thay mọi handoff trước. Mọi số liệu lấy từ code đang chạy trong studio (bản sao ở `js/`) và từ chính các file trong gói.

---

## Nội dung gói

```text
handoff/
  HANDOFF-mobile-3d-landmark-2026-11-10.md   tài liệu này
  manifest.json                              anchor, đặt model, LOD theo zoom, sha256, file bóng
  SHA256SUMS                                 kiểm file sau khi giải nén: sha256sum -c SHA256SUMS
  Tasco_Tower_v2_shell_lit_z17.glb           LOD gần   (zoom ≥ 17)
  Tasco_Tower_v2_shell_lit_z16.glb           LOD giữa  (16 ≤ zoom < 17)
  Tasco_Tower_v2_shell_lit_z15.glb           LOD xa    (15 ≤ zoom < 16)
  shadows/Tasco_Tower_shadow_{dawn,day,dusk}.geojson   bóng đổ tính sẵn (night không có nắng)
  js/landmark-light.js                       công thức chiếu sáng, emissive, kính, bảng số, shader: NGUỒN CHUẨN để port
  js/landmark-model.js                       nạp GLB vào three.js, TEXCOORD_1, ma trận đặt model (tham khảo)
  js/landmark-store.js                       chọn file LOD theo zoom, tải và giải phóng model (tham khảo)
```

- `js/landmark-light.js` không phụ thuộc gì, chạy được trong Node và port được từng dòng.
- `js/landmark-model.js` cần three.js và import theo đường dẫn của studio (`/static/...`), nên chỉ để đọc, không chạy riêng được.

---

## 0. Tóm tắt: việc Mobile cần làm

| # | Việc | Mục | Bắt buộc |
|---|---|---|---|
| M1 | Go baker giữ đủ thuộc tính GLB khi bake sang LMK3 | 3, 10 | Có |
| M2 | Đặt model tại anchor, góc xoay 0; đổi file theo zoom | 4 | Có |
| M3 | Chiếu sáng màu nền bằng đúng công thức fill-extrusion của MapLibre, lấy theo `light` của style | 5 | Có |
| M4 | Emissive theo nhóm (REPLACE / ADD) và gain theo preset | 6 | Có |
| M5 | Đọc lightmap trên `TEXCOORD_1`, mask cửa sổ trên `TEXCOORD_0`, theo `texCoord` của từng vật liệu | 7 | Có |
| M6 | Kính phản chiếu trong shader (`surface_type: "glass"`) | 8 | Có |
| M7 | Bóng đổ của landmark | 9 | Chốt cách làm |
| M8 | Chốt budget thiết bị và trả lời các câu hỏi mở | 11, 14 | Có |
| M9 | Nghiệm thu trên thiết bị | 12 | Có |

Ràng buộc chung: chỉ sửa shader và bảng số, **không thêm lượt vẽ** (render pass) nào. Ban đêm phải giữ FPS ngang ban ngày.

---

## 1. Phân vai và các quyết định đã chốt

| Hạng mục | Team 3D / pipeline GLB | Team Mobile |
|---|---|---|
| Geometry, UV, normals, LOD | Dựng, tối ưu và kiểm chất lượng | Nạp file, chọn LOD theo zoom, chuyển LOD |
| Vật liệu, texture | Dựng theo contract (mục 3) | Giữ nguyên thuộc tính khi bake; viết shader |
| Đèn, logo, biển hiệu, cửa sổ sáng | Mask hoặc lightmap, màu, nhóm (`emissive_group`) | Cường độ theo preset, cách trộn, chuyển mượt khi đổi preset |
| Kính | Gắn `surface_type: "glass"` | Tính phản chiếu trong shader |
| Bóng đổ | Normals và winding đúng | Cách vẽ bóng, giới hạn GPU |
| Budget | Báo số liệu, tối ưu theo budget | **Đề xuất budget theo thiết bị**, đo và xác nhận |
| Nghiệm thu | Hình dáng, vật liệu, ảnh tham chiếu | Kiểm file thật trên iOS và Android |
| Phát hành | Bàn giao bản ứng viên có version | Tích hợp, kiểm thử, promote hoặc rollback |

GLB mang **màu, nhóm và lightmap**. App quyết định **cường độ theo preset và cách trộn màu**.

| Chủ đề | Quyết định |
|---|---|
| Vật liệu | **Lit**, không dùng extension nào (không còn `KHR_materials_unlit`). App chiếu sáng như fill-extrusion (mục 5) |
| Bloom, quầng sáng | **Không làm.** MapLibre không có hậu xử lý; tự viết quá nặng cho mobile |
| Ánh LED hắt lên tường | Bake sẵn trong GLB: nhóm `led_wash`, lightmap JPEG trên `TEXCOORD_1`. App **cộng** vào |
| Đèn cửa sổ, sảnh | Mask PNG trên vật liệu `_WIN` (`TEXCOORD_0`), sáng tắt theo từng ô. Không sinh ngẫu nhiên, không đổi theo thời gian |
| Kính | Opaque. Phản chiếu sơ bộ trong shader app (Fresnel + dải màu trời) |
| LOD | 3 file theo zoom (z17 / z16 / z15), ghi trong `asset.extras.zoom_range` |
| Hệ trục, anchor | +X Đông, −Z Bắc, +Y lên, mét; gốc GLB = anchor; góc xoay 0 |
| Nén | GLB không nén, ảnh PNG/JPEG. Chưa dùng Draco, meshopt, KTX2 |

---

## 2. Ba file LOD của Tasco Tower

Anchor `21.0156627, 105.7828616` (tâm footprint OSM `way/845002110`). Cả 3 file: 24 vật liệu, tất cả `OPAQUE`, **không có vật liệu `doubleSided`**, mọi primitive đều có `NORMAL`, 9 primitive có `TEXCOORD_1`.

| File | Zoom | Dung lượng | Tam giác | Ảnh màu nền | Mask cửa sổ | Lightmap |
|---|---|---|---|---|---|---|
| `Tasco_Tower_v2_shell_lit_z17.glb` | ≥ 17 | 885.864 B | 13.198 | tối đa 512 px | PNG 512 px | JPEG 1024 px |
| `Tasco_Tower_v2_shell_lit_z16.glb` | 16–17 | 829.056 B | 13.198 | 256 px | PNG 256 px | JPEG 512 px |
| `Tasco_Tower_v2_shell_lit_z15.glb` | 15–16 | 803.804 B | 13.171 | 128 px | PNG 128 px | JPEG 256 px |

Các LOD hiện khác nhau chủ yếu ở **độ phân giải ảnh**, geometry gần như giữ nguyên. Nếu budget của Mobile (mục 11) cần giảm tam giác ở z15/z16, team 3D sẽ làm ở các bản sau.

**Vật liệu phát sáng:**

| Vật liệu | `emissive_group` | Cách trộn | `emissiveFactor` | `emissiveTexture` |
|---|---|---|---|---|
| `D01_Frost_White_LED_WARM` (line LED) | `led_warm` | REPLACE | `[1, 0.8, 0.5]` | không |
| `TM_SIGN_TT_01_TascoPodium_LED_WHITE` (chữ TASCO) | `led_white` | REPLACE | `[1, 1, 1]` | không |
| `TM_FACADE_TT_03_TowerGlass_WIN` (cửa sổ tháp) | `windows_tower` | REPLACE | `[1, 0.8, 0.55]` | mask PNG, `texCoord 0` |
| `TM_FACADE_TT_09_Storefront_WIN` (sảnh) | `lobby` | REPLACE | `[1, 0.88, 0.7]` | mask PNG, `texCoord 0` |
| 9 vật liệu `<X>_BAKED` (tường, ốp, đá quanh line LED) | `led_wash` | ADD | `[1, 1, 1]` | lightmap JPEG, `texCoord 1` |

`surface_type` của 24 vật liệu: stone 10, glass 5, metal 5, sign 2, concrete 2.

---

## 3. Contract GLB: các thuộc tính renderer phải đọc

Nguồn: `GLB_CONTRACT` trong `js/landmark-light.js`.

| Thuộc tính | Dùng để | Quy tắc |
|---|---|---|
| `asset.extras.anchor {lat, lon}` | Đặt model | Góc xoay 0, gốc GLB đặt tại lat/lon (glTF +X Đông, −Z Bắc, +Y lên, mét) |
| `asset.extras.lod_zoom`, `zoom_range [min, max\|null]` | Đổi file theo zoom | Vẽ file có `min ≤ zoom < max` (`max` null: không giới hạn). Ngoài mọi khoảng: không vẽ, giữ extrusion LoD1 |
| `materials[].extras.emissive_group` | Chế độ trộn và gain | `led_wash`: ADD; nhóm khác: REPLACE; không có nhóm: không phát sáng. Không suy từ tên vật liệu |
| `materials[].extras.emissive_strength` | Nhân vào gain | Chỉ có khi người dựng đặt trên 100%. Có tác dụng với ADD; với REPLACE thì `gain × m` bị kẹp ≤ 1. Thiếu thì dùng `KHR_materials_emissive_strength`, nếu cũng thiếu thì 1 |
| `materials[].emissiveFactor` | Màu E | Linear: đổi sang sRGB trên CPU trước khi trộn. Bằng 0: không phát sáng |
| `materials[].emissiveTexture {index, texCoord}` | Lightmap hoặc mask | Texel sRGB thô nhân vào E; `m = max(r, g, b)`. Giữ `texCoord`; bật mipmap và lọc tuyến tính |
| `meshes[].primitives[].attributes.TEXCOORD_1` | UV của lightmap | Đọc khi `emissiveTexture.texCoord = 1` (chỉ primitive `*_BAKED` có). Lấy bằng `TEXCOORD_0` thì vệt sáng lặp theo tile |
| `materials[].extras.surface_type` | Kính phản chiếu | `"glass"`: Fresnel Schlick F0 0,04 + dải màu trời theo preset. Giá trị khác: không phản chiếu |
| `baseColorFactor × COLOR_0 × baseColorTexture` (`TEXCOORD_0`) | Màu nền | Đổi sang sRGB một lần rồi chiếu sáng bằng công thức fill-extrusion. Không sửa trong GLB để bù sáng |
| `meshes[].primitives[].attributes.NORMAL` | Ánh sáng, Fresnel | Cùng hệ toạ độ với vị trí camera. Thiếu thì tính pháp tuyến phẳng theo mặt |
| `alphaMode MASK / alphaCutoff`, `doubleSided` | Cắt alpha, hai mặt | MASK: bỏ pixel dưới `alphaCutoff`. `doubleSided`: lật pháp tuyến ở mặt sau (không nhân đôi geometry) |
| `KHR_texture_transform` (baseColor, emissive) | UV | Áp vào UV trước khi lấy mẫu |

**Cố ý không đọc:** `metallicFactor` / `roughnessFactor` / `metallicRoughnessTexture` (extrusion không có), `normalTexture`, `occlusionTexture`, `extras.emissive_suffix`, `extras.lit` (thông tin nội bộ của pipeline).

Các nhóm khác pipeline có thể sinh cho công trình sau này: `glow`, `led_line`, `sign*`, `logo*`, `windows_*`. Quy tắc trộn và gain của chúng đã có sẵn ở mục 6.

---

## 4. Đặt model và đổi LOD theo zoom (M2)

- **Hệ trục:** glTF +X = Đông, −Z = Bắc, +Y = lên, đơn vị mét.
- **Đặt:** gốc toạ độ (0, 0, 0) của GLB đặt tại `asset.extras.anchor`, **góc xoay 0, tỉ lệ 1**. Không dùng tâm hộp bao làm điểm đặt.
- **LOD:** chọn file có `min ≤ zoom < max`. Ngoài mọi khoảng (zoom < 15): không vẽ model, giữ extrusion LoD1 của building.
- **Ẩn extrusion gốc:** khi model đã tải xong, ẩn extrusion của building đó, kể cả các khối con (`parent_building_id`). Trước khi model tải xong, giữ extrusion để không bị hở.
- **`manifest.json`** trong gói ghi đủ anchor, placement, 3 file LOD (`zoom_range`, sha256, dung lượng) và file bóng:

```json
"placement": { "lon": 105.7828616, "lat": 21.0156627, "heading": 0, "scale": 1, "anchor": "origin" },
"lods": [
  { "lod_zoom": 17, "zoom_range": [17, null], "file": "Tasco_Tower_v2_shell_lit_z17.glb" },
  { "lod_zoom": 16, "zoom_range": [16, 17],   "file": "Tasco_Tower_v2_shell_lit_z16.glb" },
  { "lod_zoom": 15, "zoom_range": [15, 16],   "file": "Tasco_Tower_v2_shell_lit_z15.glb" }
]
```

`heading` tính bằng độ, theo chiều kim đồng hồ nhìn từ trên xuống; 0 nghĩa là +X của GLB hướng Đông. `anchor: "origin"` nghĩa là gốc GLB nằm tại lon/lat.

Cách đổi giữa các file khi zoom, tải và giải phóng model: xem `js/landmark-store.js`. Ma trận đặt model: `placementMatrix` trong `js/landmark-model.js`.

---

## 5. Chiếu sáng màu nền: giống fill-extrusion (M3)

Landmark phải sáng tối giống các extrusion xung quanh ở cả 4 preset. Công thức lấy từ shader fill-extrusion của MapLibre 5.13. Mọi giá trị là sRGB thô 0..1, không linear hoá, không tone map. Tính cho từng kênh:

```text
lum = 0.2126 r + 0.7152 g + 0.0722 b         của màu nền, trước ambient
c   = base + 0.03                            ambient
d   = clamp(dot(N, L), 0, 1)                 N: pháp tuyến ngoài, đơn vị. L: vector đèn dài r (1.15), không chuẩn hoá
d   = mix(1 - I, max(1 - lum + I, 1), d)     I: light.intensity
d  *= wall gradient                          fill-extrusion-vertical-gradient
out = clamp(c * d * lightColor, 0.3 * (1 - lightColor), 1)

L_enu = r * (sin α sin p, cos α sin p, cos p)   α = azimuth (+ bearing nếu light.anchor = "viewport")
```

- **Màu nền:** `baseColorFactor` (linear) × `COLOR_0` (linear) × `baseColorTexture` (đã mã hoá sRGB), đổi sang sRGB một lần.
- **Upload texture:** dạng RGBA8 thường. Không dùng `SRGB8_ALPHA8` / `.rgba8Unorm_srgb`, không dùng framebuffer sRGB.
- **Đọc `light` của style:** làm lại mỗi khi đổi style hoặc preset.
  - Android: `Position` không có getter, nên parse object `light` trong `Style.getJson()`.
  - iOS: đọc `MLNStyle.light`. Màu là `UIColor`: lấy thành phần sRGB rồi nhân alpha.
  - Màu đèn nhân sẵn alpha như MapLibre: `rgba(255,255,255,0.5)` thành `[0.5, 0.5, 0.5]`.
- **Preset** đọc từ metadata `tasco:light-preset` của style (`dawn` / `day` / `dusk` / `night`). Style không có preset thì không có emissive và không có phản chiếu kính.
- Wall gradient, ma trận đổi hệ trục và các trường hợp đặc biệt được giải thích trong comment đầu file `js/landmark-light.js`. Shader đầy đủ: `VERTEX_SHADER` và `FRAGMENT_SHADER` trong cùng file.

---

## 6. Emissive theo nhóm và preset (M4)

| Chế độ | Nhóm | Công thức |
|---|---|---|
| `REPLACE` | `led_warm`, `led_white`, `led_line`, `glow`, `sign*`, `logo*`, `windows*`, `lobby`, nhóm lạ | `color = mix(lit, E, clamp(gain × m, 0, 1))` |
| `ADD` | `led_wash` | `color = lit + E × gain` |

- `E` = `sRGB(emissiveFactor) × emissiveTexture` (texel sRGB thô). Không có texture thì chỉ `sRGB(emissiveFactor)`.
- Ví dụ đổi màu: `[1, 0.8, 0.5]` thành `[1, 0.906, 0.735]`. Nếu không đổi sang sRGB, line LED sẽ ngả cam đậm.
- `m` = `max(texel.r, texel.g, texel.b)` khi có texture, bằng 1 khi không có. Ô đen của mask giữ nguyên màu tường.
- `gain` = bảng dưới × `emissive_strength` (mục 3).
- Lý do có REPLACE: line LED có màu nền gần trắng. Cộng thêm emissive sẽ kẹp thành trắng. Bề mặt tự phát sáng thì ban đêm màu của nó **là** màu đèn.
- Emissive áp **sau** phản chiếu kính.

**Bảng gain** (nguồn: `EMISSIVE_GAIN` trong `js/landmark-light.js`):

| Nhóm | dawn | day | dusk | night |
|---|---|---|---|---|
| `led_warm`, `led_white`, `led_line`, `glow`, `sign*`, `logo*` | 0,4 | 0 | 0,9 | 1,0 |
| `led_wash` (ADD) | 0,3 | 0 | 0,8 | 1,0 |
| `lobby` | 0,3 | 0 | 0,8 | 1,0 |
| `windows*` | 0,3 | 0 | 0,8 | 1,0 |
| nhóm khác | 0,3 | 0 | 0,6 | 1,0 |

Ban ngày mọi nhóm bằng 0: toà nhà phải trông giống model SketchUp. Bảng này là đề xuất của studio; **team Mobile chốt số cuối** sau khi xem trên thiết bị.

```glsl
uniform int   u_emissive_mode;   // 0 none, 1 REPLACE, 2 ADD
uniform float u_emissive_gain;   // gain(preset, group) × emissive_strength
uniform vec3  u_emissive_factor; // sRGB(emissiveFactor), tính trên CPU
vec3 E = u_emissive_factor; float m = 1.0;
#ifdef LMK_EMISSIVE_MAP
  vec3 t = texture(u_emissive_map, v_uv_emissive).rgb;
  E *= t; m = max(max(t.r, t.g), t.b);
#endif
if (u_emissive_mode == 1) color = mix(color, E, clamp(u_emissive_gain * m, 0.0, 1.0));
else if (u_emissive_mode == 2) color += E * u_emissive_gain;
```

Hàm tương ứng trên CPU để viết test cho bản port: `materialEmissive`, `applyEmissive`, `emissiveGain`, `srgbEmissive` trong `js/landmark-light.js`.

---

## 7. Lightmap và mask (M5)

- Mỗi `emissiveTexture` đọc **bộ UV mà `texCoord` của nó chỉ định**. Phải giữ `texCoord` theo từng vật liệu.
- **Lightmap `led_wash`** (vật liệu `<X>_BAKED`, `texCoord = 1`, JPEG):
  - `<X>_BAKED` là bản sao của vật liệu `<X>`: cùng ảnh màu nền trên `TEXCOORD_0`, thêm lightmap trên `TEXCOORD_1`.
  - Nó chỉ gồm các mặt nằm trong 1,5 m quanh line LED. Ban ngày trông y hệt `<X>`.
  - Lightmap là ánh sáng LED chiếu tới mặt (không nhân màu tường), đã chuẩn hoá: chỗ sáng nhất khoảng 0,6 so với chính line LED.
- **Mask cửa sổ và sảnh** (vật liệu `_WIN`, `texCoord = 0`, PNG): cùng UV với màu nền. Các primitive này không có `TEXCOORD_1`.
- Bật mipmap và lọc tuyến tính.
- Dấu hiệu lỗi: quầng sáng lặp thành sọc hoặc ô theo tile nghĩa là shader đang lấy lightmap bằng `TEXCOORD_0`.
- Tham khảo: thuộc tính `lmk_uv1` và define `LMK_EMISSIVE_UV1` trong `js/landmark-model.js`.

---

## 8. Kính phản chiếu (M6)

Chỉ áp cho vật liệu có `extras.surface_type === "glass"`. Không dùng cubemap, không dùng texture, không thêm lượt vẽ.

```glsl
vec3 N = normalize(v_normal); vec3 V = normalize(u_camera_pos - v_pos);
vec3 R = reflect(-V, N);
float F = 0.04 + 0.96 * pow(1.0 - max(dot(N, V), 0.0), 5.0);
vec3 sky = R.y >= 0.0 ? mix(u_sky_horizon, u_sky_zenith, smoothstep(0.0, 0.6, R.y))
                      : mix(u_sky_horizon, u_ground,     smoothstep(0.0, 0.3, -R.y));
color = mix(color, sky, clamp(F * u_glass_reflect, 0.0, 1.0));
```

| Preset | reflect | zenith | horizon | ground |
|---|---|---|---|---|
| night | 0,9 | `#0B1424` | `#3A3A52` | `#0A0C12` |
| dusk | 0,7 | `#2B3A66` | `#C98E6A` | `#1A1A22` |
| dawn | 0,6 | `#5A7AB0` | `#F2C49A` | `#2A2A30` |
| day | **0** (tắt; 0,3 nếu muốn thử) | `#7FA8E0` | `#DCE6F2` | `#5A5E66` |

- `N`, `v_pos` và vị trí camera phải cùng một hệ toạ độ. Studio dùng hệ gốc của glTF.
- Vị trí camera = nghịch đảo ma trận model-view-projection của layer nhân với `(0, 0, 1, 0)`, rồi chia cho `w` (hàm `cameraInModel`).
- Màu trời là đề xuất theo tông `light` của từng style; team Mobile chốt.
- Nguồn: `GLASS`, `glassUniforms`, `glassReflect` trong `js/landmark-light.js`.

---

## 9. Bóng đổ (M7)

Cách studio đang làm: bóng là một đa giác phẳng trên mặt đất, chiếu từ chính mesh GLB theo hướng mặt trời của từng preset, rồi gộp lại. Bóng chỉ phụ thuộc GLB, cách đặt model và mặt trời, nên tính sẵn một lần.

| File | Preset | Mặt trời (hướng, độ cao) |
|---|---|---|
| `shadows/Tasco_Tower_shadow_dawn.geojson` | dawn | 120°, 40° |
| `shadows/Tasco_Tower_shadow_day.geojson` | day | 180°, 70° |
| `shadows/Tasco_Tower_shadow_dusk.geojson` | dusk | 240°, 10° |
| (không có) | night | không có nắng, không đổ bóng |

- Tính từ file z17, đặt theo `origin` tại anchor như mục 4. Toạ độ là lon/lat (CRS84).
- Vẽ như một layer fill trên mặt đất, cùng màu và độ mờ với bóng của các building xung quanh.
- **Cần Mobile chốt:** dùng các file tính sẵn này (rẻ, không tốn GPU) hay tự vẽ bóng trong app.

---

## 10. Go baker (GLB → LMK3) phải giữ (M1)

Đây là điểm rủi ro lớn nhất: pipeline cũ đã từng làm mất thuộc tính vật liệu khi bake. Baker phải giữ:

- `TEXCOORD_1`, `emissiveTexture` cùng `texCoord`, `emissiveFactor`, ảnh JPEG và PNG.
- `material.extras`: `emissive_group`, `surface_type`, `emissive_strength`. Nếu LMK3 không giữ được `extras`, cần một chỗ khác trong LMK3 mang hai trường `emissive_group` và `surface_type`.
- `asset.extras`: `anchor`, `lod_zoom`, `zoom_range`.
- `NORMAL`, `alphaMode` / `alphaCutoff`, `doubleSided`, `KHR_texture_transform`.
- Tên vật liệu và cách gắn vật liệu với primitive.

Thêm hai điểm:

- **Hai mặt:** pipeline cũ tạo mặt ngược cho mọi vật liệu `doubleSided`, làm số tam giác gần gấp đôi. Bản Tasco này không còn vật liệu `doubleSided`. Với GLB sau này, lật pháp tuyến ở mặt sau trong shader thay vì nhân đôi geometry.
- **Nén:** giữ GLB không nén và ảnh PNG/JPEG cho tới khi Mobile xác nhận hỗ trợ Draco, meshopt hoặc KTX2.

---

## 11. Budget và contract Mobile cần chốt (M8)

Team 3D sẽ làm các landmark tiếp theo theo số này, nên cần Mobile cung cấp hoặc xác nhận:

- Thiết bị mục tiêu iOS và Android, kèm máy yếu nhất cần chạy.
- Budget cho mỗi LOD và cho tập landmark cùng hiển thị: số tam giác, draw call và vật liệu, texture sau giải nén, RAM và VRAM.
- LOD z15 / z16 có cần giảm geometry không, hay chỉ giảm ảnh như hiện tại là đủ.
- Bảng gain (mục 6) và màu trời (mục 8) cuối cùng.
- Cách chuyển preset: chuyển mượt hay đổi ngay, và mất bao lâu.
- Extension được chấp nhận. Hiện tại là không có extension nào; transparency chỉ MASK.
- Định dạng đóng gói LOD và manifest trong app, cách đặt tên và version.

---

## 12. Nghiệm thu trên thiết bị (M9)

- [ ] Bake GLB → LMK3 giữ đúng các thuộc tính, tên và cách gắn vật liệu (so với danh sách mục 10).
- [ ] **Day:** không có gì phát sáng. Vật liệu `*_BAKED` trông y như vật liệu gốc bên cạnh, không có đường ranh giới.
- [ ] **Night:** line LED vàng ấm (không trắng), chữ TASCO trắng, tường quanh line có quầng vàng ấm, cửa sổ tháp và sảnh sáng theo mask (không sáng cả mặt kính), kính có ánh xanh tím nhạt khi nhìn xiên.
- [ ] **Dawn / Dusk:** cường độ theo bảng gain.
- [ ] Mái và tường của landmark sáng tối giống extrusion bên cạnh ở cả 4 preset.
- [ ] Không có vệt sáng lặp theo tile (lightmap đọc đúng `TEXCOORD_1`).
- [ ] Đổi LOD ở zoom 15 / 16 / 17, về extrusion dưới zoom 15, tải lại style: không hở, không chồng, không mất model.
- [ ] Model đúng vị trí: cạnh Tây Nam của Tasco trùng footprint `way/845002110`. Footprint OSM rộng hơn khối đế khoảng 9 m ở phía Đông Bắc là bình thường.
- [ ] Hiệu năng trên thiết bị: frame pacing, CPU/GPU, bộ nhớ, tải, cache, vòng đời. Mục tiêu 59–60 FPS, xét theo từng giai đoạn, không chỉ số trung bình. Ban đêm ngang ban ngày.
- [ ] Publish và rollback đúng file đang phát hành.

---

## 13. Không làm

- Không bloom, không render ra texture riêng, không thêm lượt vẽ.
- Không sửa `baseColor` hay `emissiveFactor` trong GLB để bù sáng: cường độ thuộc về app.
- Không suy nhóm emissive hay chế độ trộn từ tên vật liệu. Đọc `extras`.
- Không sinh đèn cửa sổ ngẫu nhiên hay theo thời gian. Ánh sáng cửa sổ chỉ lấy từ GLB.
- Không ghi đè hoặc rút bản production hiện tại khi đang thử. Dùng version mới.

---

## 14. Câu hỏi cần Mobile trả lời

1. LMK3 có giữ được `material.extras` và ảnh JPEG/PNG không? Nếu không, giữ `emissive_group` và `surface_type` ở đâu?
2. Bảng gain (mục 6) và màu trời (mục 8): giữ nguyên hay chỉnh?
3. Bóng đổ: dùng GeoJSON tính sẵn hay tự vẽ trong app?
4. Budget thiết bị (mục 11): khi nào có? LOD z15 / z16 có cần giảm geometry không?
5. Lightmap và `TEXCOORD_1` làm dung lượng tăng khoảng 25–71 KB mỗi LOD. Có cần giảm thêm không?

### Mẫu phản hồi

```text
Người phụ trách:
Đã nhận gói (sha256sum -c SHA256SUMS: OK / lỗi):
Go baker giữ được extras / TEXCOORD_1 / JPEG / PNG (có / không / cần sửa):
Ước lượng M1–M9:
Thời điểm có bản chạy thử trên iOS / Android:
Budget thiết bị đề xuất:
Trả lời câu hỏi 1–5:
Rủi ro:
```
