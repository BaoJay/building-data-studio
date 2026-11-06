# Landmark ban đêm: emissive thay màu nền, ánh hắt LED bake sẵn, kính phản chiếu

Ngày: 03/11/2026 · Nguồn: phiên làm việc trong `TASCO-Landmark-Buildings` · Giao cho: repo building-data-studio (playground),
sau đó chuyển cho team Mobile (renderer landmark LMK3 trên MapLibre Native).

Thay thế mục 3–4 (bảng cường độ) của `HANDOFF-building-data-studio-emissive-group.md` (30/10).
Mục 5.1 (đọc `TEXCOORD_1`) của `HANDOFF-building-data-studio-lit-emissive.md` (28/10) **vẫn bắt buộc**, giờ còn cần hơn (mục 5).

## 1. Đã chốt (03/11/2026)

| Chủ đề | Quyết định |
|---|---|
| Bloom / quầng sáng | **Không làm.** MapLibre (cả Native lẫn GL JS) không có hậu xử lý; tự viết trong custom layer quá nặng cho mobile |
| Ánh LED hắt lên tường | **Bake sẵn trong GLB** bằng Cycles: nhóm mới `led_wash`, lightmap JPEG ở `TEXCOORD_1`. App chỉ cộng vào |
| Đèn cửa sổ ban đêm | **Tắt.** GLB Tasco không còn nhóm `windows_*` |
| Kính ban đêm | Phản chiếu sơ bộ trong **shader app** (Fresnel + dải màu trời), theo `surface_type: "glass"` |
| `TEXCOORD_1` | Mặc định Mobile đọc được |
| Phân vai | GLB mang màu, nhóm, lightmap. App quyết định cường độ theo preset và cách trộn màu |

Mọi thứ dưới đây phải chuyển được sang Mobile: chỉ sửa shader và bảng số, **không thêm lượt vẽ** (render pass) nào.

## 2. File để thử

`D:\OneDrive\Drive\ENGINEER\MAP-OPERATION-ENGINEER\TASCO-Landmark-Buildings\1_Tasco_Tower\GLB\`

| File | Zoom | Dung lượng | Lightmap |
|---|---|---|---|
| `Tasco_Tower_v2_shell_lit_z17.glb` | ≥ 17 | 842.660 B | 1024 px |
| `Tasco_Tower_v2_shell_lit_z16.glb` | 16–17 | 802.708 B | 512 px |
| `Tasco_Tower_v2_shell_lit_z15.glb` | 15–16 | 778.604 B | 256 px |
| `Tasco_Tower_v2_shell_lit_manifest.json` | | | anchor, trục, phạm vi zoom từng file |

**Hướng và vị trí đã đổi so với v3 đang đặt trong studio:**
- Model giờ theo **Bắc thật**: glTF +X = Đông, −Z = Bắc, +Y = lên, đơn vị mét. Gốc toạ độ = anchor `21.0156627, 105.7828616`
  (tâm footprint OSM `way/845002110`), ghi trong `asset.extras.anchor` và manifest.
- Khi đặt bản này: **góc xoay = 0**, toạ độ = anchor. Đừng dùng lại góc xoay đã chỉnh tay cho v3, vì v3 dựng theo trục nhà.

## 3. Contract GLB

Vẫn không có extension nào. Vật liệu phát sáng của Tasco:

| Nhóm (`extras.emissive_group`) | Vật liệu | `emissiveFactor` | `emissiveTexture` | Cách trộn (mục 4) |
|---|---|---|---|---|
| `led_warm` | `D01_Frost_White_LED_WARM` (line LED) | `[1, 0.8, 0.5]` | không | **REPLACE** |
| `led_white` | `TM_SIGN_TT_01_TascoPodium_LED_WHITE` (chữ TASCO) | `[1, 1, 1]` | không | **REPLACE** |
| `led_wash` | 10 vật liệu `<X>_BAKED` (tường, ốp, đá, mái cỏ quanh line LED) | `[1, 1, 1]` | lightmap JPEG, `texCoord: 1` | **ADD** |

- `<X>_BAKED` là bản sao của vật liệu `<X>`: cùng ảnh màu nền ở `TEXCOORD_0`, thêm lightmap ở `TEXCOORD_1`, chỉ gồm các mặt nằm trong 1,5 m quanh line LED.
  Ban ngày (mức emissive = 0) trông y hệt `<X>`. `extras.emissive_suffix` = `"BAKED"`.
- Lightmap là **ánh sáng LED chiếu tới mặt** (không nhân màu tường), đã chuẩn hoá: chỗ sáng nhất ≈ 0,6 so với chính line LED = 1.
  Nó thay cho quầng sáng của ảnh chụp, nên tường tối cũng hắt sáng rõ.
- Chỉ primitive `<X>_BAKED` có `TEXCOORD_1`. Primitive khác không có.
- Các nhóm khác pipeline còn có thể sinh cho công trình khác: `glow`, `led_line` (mask, `TEXCOORD_1`), `windows_*` (mask, `TEXCOORD_1`), `lobby`, `sign*`.

## 4. Việc A (bắt buộc): cách trộn emissive theo nhóm, sửa line LED đang hiện trắng

**Nguyên nhân:** shader đang lấy `màu chiếu sáng + emissive`. `D01_Frost_White` có màu nền gần trắng (255, 242, 206), cộng thêm `[1, 0.8, 0.5]`
nên bị kẹp thành trắng. Dải đèn thật là bề mặt tự phát sáng: ban đêm màu của nó **là** màu đèn, không cộng thêm.

Hai cách trộn:

| Chế độ | Nhóm | Công thức |
|---|---|---|
| `REPLACE` | `led_warm`, `led_white`, `glow`, `sign*`, `logo*`, `led_line`, `windows*`, `lobby` | `color = mix(lit, E, gain * m)` |
| `ADD` | `led_wash` | `color = lit + E * gain` |

- `E` = màu emissive của pixel: `emissiveFactor × emissiveTexture`, hoặc chỉ `emissiveFactor` nếu không có texture.
- `m` = độ phủ của mask: `max(E.r, E.g, E.b) / max(factor)` khi có mask, = 1 khi không có texture. Ô đen của mask giữ nguyên màu tường.
- Nhóm lạ: `REPLACE`.

```glsl
// landmark-light.js (fragment). u_emissive_mode: 0 = none, 1 = REPLACE, 2 = ADD
uniform int   u_emissive_mode;
uniform float u_emissive_gain;   // gain(preset, group) x extras.emissive_strength (mục 5 bảng gain)
uniform vec3  u_emissive_factor; // glTF emissiveFactor (linear)
...
vec3 E = u_emissive_factor;
float m = 1.0;
#ifdef LMK_EMISSIVE_MAP
  vec3 t = texture(u_emissive_map, v_uv_emissive).rgb;   // emissiveTexture (sRGB, decode như ảnh màu nền)
  E *= t;
  m = max(max(t.r, t.g), t.b);
#endif
if (u_emissive_mode == 1) color = mix(color, E, clamp(u_emissive_gain * m, 0.0, 1.0));
else if (u_emissive_mode == 2) color += E * u_emissive_gain;
```

- **Không gian màu:** studio đang tính trên màu sRGB thô (theo `landmark-light.js` đọc ngày 28/10). Khi đó phải đổi `emissiveFactor`
  (linear) sang sRGB trước khi trộn, ví dụ `[1, 0.8, 0.5]` → `[1, 0.906, 0.735]`. Nếu không, line sẽ ngả cam đậm. `emissiveTexture` vốn đã là sRGB.
- Chế độ trộn gắn với nhóm (đọc từ `material.userData.emissive_group` khi đổi sang ShaderMaterial), không suy từ tên vật liệu.

```js
// landmark-light.js
export function emissiveMode(group) {
  if (!group) return 0;
  return group === "led_wash" ? 2 : 1;
}
```

## 5. Việc B (bắt buộc): hiển thị lightmap `led_wash` (`TEXCOORD_1`)

- Làm mục 5.1 của file 28/10 (thuộc tính `lmk_uv1`, define `LMK_EMISSIVE_UV1` khi `emissiveMap.channel === 1`).
- Các primitive `<X>_BAKED` có cả ảnh màu nền (`TEXCOORD_0`) lẫn lightmap (`TEXCOORD_1`). Không được lấy lightmap bằng `uv`,
  nếu không vệt sáng sẽ lặp theo tile.
- Ảnh lightmap là JPEG. Bật mipmap và lọc tuyến tính (mặc định của three.js) cho vệt sáng mịn.

## 6. Việc C: bảng cường độ theo preset × nhóm

Thay bảng ở file 30/10:

| Nhóm | dawn | day | dusk | night |
|---|---|---|---|---|
| `led_warm`, `led_white`, `led_line`, `glow`, `sign*`, `logo*` | 0,4 | 0 | 0,9 | 1,0 |
| `led_wash` | 0,3 | 0 | 0,8 | 1,0 |
| `lobby` | 0,3 | 0 | 0,6 | 0,8 |
| `windows*` | 0 | 0 | 0 | 0 (Tasco không còn nhóm này; công trình khác muốn bật thì app chốt số) |
| _(khác)_ | 0,3 | 0 | 0,6 | 1,0 |

- `extras.emissive_strength` (chỉ có khi người dựng đặt mức trên 100%): nhân vào `gain`. Chỉ có tác dụng với `ADD`; với `REPLACE`, giá trị trên 1 bị kẹp.
- Ban ngày mọi nhóm = 0: tòa nhà phải giống SketchUp. Có thể kiểm bằng chế độ QA "tô landmark một màu".

## 7. Việc D: kính phản chiếu sơ bộ ban đêm (`surface_type: "glass"`)

Phản chiếu đổi theo góc nhìn nên GLB không chứa sẵn được. Cách rẻ, không cần cubemap hay texture:

```glsl
// chỉ cho vật liệu có material.userData.surface_type === "glass"
uniform float u_glass_reflect;            // theo preset, bảng dưới
uniform vec3  u_sky_zenith, u_sky_horizon, u_ground;
...
vec3 N = normalize(v_normal_world);
vec3 V = normalize(u_camera_pos_world - v_pos_world);
vec3 R = reflect(-V, N);
float F = 0.04 + 0.96 * pow(1.0 - max(dot(N, V), 0.0), 5.0);           // Schlick, F0 = 0.04 (kính)
vec3 sky = R.y >= 0.0 ? mix(u_sky_horizon, u_sky_zenith, smoothstep(0.0, 0.6, R.y))
                      : mix(u_sky_horizon, u_ground, smoothstep(0.0, 0.3, -R.y));
color = mix(color, sky, clamp(F * u_glass_reflect, 0.0, 1.0));
```

| Preset | `u_glass_reflect` | zenith | horizon (ánh thành phố) | ground |
|---|---|---|---|---|
| night | 0,9 | `#0B1424` | `#3A3A52` | `#0A0C12` |
| dusk | 0,7 | `#2B3A66` | `#C98E6A` | `#1A1A22` |
| dawn | 0,6 | `#5A7AB0` | `#F2C49A` | `#2A2A30` |
| day | 0,3 (tuỳ chọn) | `#7FA8E0` | `#DCE6F2` | `#5A5E66` |

- Màu trên là gợi ý, lấy theo tông `light` của style (night `#3f4455`). App chốt.
- Cần `v_normal_world`, `v_pos_world`, `u_camera_pos_world` trong cùng hệ toạ độ. Trong custom layer của MapLibre,
  vị trí camera tính từ nghịch đảo ma trận model-view-projection của layer.
- GLB đã có `NORMAL` cho mọi primitive và roughness 0,15 cho kính. Có thể làm mờ dải màu theo roughness, nhưng không bắt buộc.
- Trên mobile chỉ tốn vài phép tính mỗi pixel kính, không thêm lượt vẽ.

## 8. Không làm

- Không bloom, không render ra texture riêng.
- Không sửa `baseColor` / `emissiveFactor` trong GLB để bù sáng: cường độ thuộc về app.
- Không suy nhóm hay chế độ trộn từ tên vật liệu: đọc `extras`.

## 9. Chuyển sang Mobile (LMK3) — checklist

1. Go baker phải giữ: `TEXCOORD_1`, `emissiveTexture` + `texCoord`, `emissiveFactor`, ảnh JPEG, `material.extras`
   (`emissive_group`, `surface_type`, `emissive_strength`), `asset.extras` (`anchor`, `lod_zoom`, `zoom_range`).
   Nếu baker bỏ `extras`, cần một chỗ khác mang `emissive_group` và `surface_type`.
2. Shader: đúng 3 thay đổi của mục 4, 5, 7, dùng chung bảng số mục 6. Không thêm lượt vẽ.
3. Đặt model: góc xoay 0, anchor từ `asset.extras` hoặc manifest; đổi file theo zoom 15 / 16 / 17 theo manifest.
4. Kiểm trên máy yếu: ban đêm khung hình có FPS ngang ban ngày (chỉ thêm phép tính trong shader).

## 10. Cách thử trên studio

1. Đặt `Tasco_Tower_v2_shell_lit_z17.glb` ở anchor, góc xoay 0. So với footprint `way/845002110`: cạnh thẳng phía Tây Nam phải trùng.
   Footprint OSM rộng hơn khối đế khoảng 9 m ở phía Đông Bắc, đó là chuyện bình thường.
2. Console:
   `root.traverse(o => o.material?.userData?.emissive_group && console.log(o.material.name, o.material.userData.emissive_group))`
   → 1 dòng `led_warm`, 1 dòng `led_white`, 10 dòng `led_wash` (`*_BAKED`).
3. **Night:** line LED vàng ấm (không trắng), chữ TASCO trắng, tường quanh line có quầng vàng ấm loang ra, cửa sổ tối,
   kính có ánh xanh tím nhạt ở góc nhìn xiên.
4. **Day:** không có gì sáng; vật liệu `*_BAKED` trông y như vật liệu gốc bên cạnh, không có đường ranh giới.
5. **Dusk / Dawn:** LED gần sáng hết / mờ, quầng tường theo bảng mục 6.
6. Nếu quầng sáng lặp thành sọc hoặc ô theo tile: shader đang lấy lightmap bằng `TEXCOORD_0` (chưa làm mục 5).
7. Ảnh tham chiếu render bằng Blender (đêm toàn cảnh, đêm cận, ngày) có trong phiên làm việc 03/11.

## 11. Câu hỏi còn mở

1. Bảng số mục 6 và màu trời mục 7 là đề xuất; team app chốt.
2. Go baker / LMK3 có giữ `extras` và ảnh JPEG không?
3. Dung lượng tăng so với bản không bake: z17 +71 KB, z16 +38 KB, z15 +25 KB (lightmap + `TEXCOORD_1`). Có cần giảm thêm không
   (lightmap 512 px còn khoảng 15 KB, vệt sáng thô hơn)?

## 12. Trạng thái trong studio (03/11/2026)

Đã làm trong building-data-studio. Kiểm bằng bộ `Tasco_Tower_v2_shell_lit_z15/z16/z17.glb`: 1 `led_warm`, 1 `led_white`, 10 `led_wash`, 4 vật liệu kính.

| Việc | Ở đâu | Ghi chú |
|---|---|---|
| A. REPLACE / ADD theo nhóm | `app/static/landmark-light.js` (`emissiveMode`, `materialEmissive`, `FRAGMENT_SHADER`) | `emissiveFactor` đổi sang sRGB trên CPU. **Không có `emissive_group` → không phát sáng** (kể cả GLB cũ như Tasco v3). `emissiveFactor` = 0 → không phát sáng |
| B. Lightmap `TEXCOORD_1` | `landmark-model.js` (`lmk_uv1`, `LMK_EMISSIVE_UV1`) | Vệt sáng mịn, không lặp theo tile |
| C. Bảng gain | `EMISSIVE_GAIN` | Đúng bảng mục 6. `emissive_strength` có thì dùng, không thì `KHR_materials_emissive_strength`, rồi 1 |
| D. Kính | `GLASS`, `glassUniforms`, `cameraInModel` | **Ban ngày tắt** (reflect 0), màu trời giữ như bảng mục 7 để bật thử 0,3 |
| Đặt model | `app/landmark_library.py` | GLB có `asset.extras.anchor` → hướng 0, tỉ lệ 1, gốc GLB tại anchor, không lấy placement đã xoay tay của version trước |
| Đổi file theo zoom | `landmark-store.js`, `/api/landmarks/{id}/versions/{n}/lods` | Một version giữ bộ z17 / z16 / z15 theo `asset.extras.zoom_range` (`min ≤ zoom < max`). Ngoài mọi khoảng: không vẽ, giữ extrusion. Manifest có `lods` |
| Danh sách thuộc tính | Drawer → **Thuộc tính GLB cho Mobile** | Nguồn: `GLB_CONTRACT`. Có nút sao chép Markdown |

QA mode "tô landmark một màu" tắt cả emissive lẫn kính.
