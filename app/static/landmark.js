// Landmark check in the 3D preview: a local .glb placed on one building_id, drawn in a MapLibre
// custom layer and lit by the style's own `light` exactly like the fill-extrusions around it
// (the formula and shaders are in landmark-light.js). preview.js imports this module the first
// time the "Landmark 3D" section is opened.
/* global maplibregl */
import * as THREE from "three";
import { GLTFLoader } from "/static/vendor/three/examples/jsm/loaders/GLTFLoader.js";
import * as LL from "/static/landmark-light.js";

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const LAYER_ID = "landmark";
const REF_LAYER_ID = "landmark-ref";
const EARTH_M = 40075016.686;  // equator length: metres per pixel = EARTH_M · cos(lat) / (512 · 2^zoom)
const HEIGHT_TOLERANCE = { m: 2, ratio: 0.05 };  // the height check warns past both
const COMPASS = ["bắc", "đông", "nam", "tây"];

// preview.js hands over the map and a few hooks (see openLandmark there).
export function init(api) {
  const lmk = new Landmark(api);
  lmk.bindUI();
  return lmk;
}

class Landmark {
  constructor(api) {
    this.api = api;
    this.map = api.map;
    this.model = null;  // { root, bounds, name, materials, textures }
    this.place = null;  // /api/building answer for the building the model stands on
    this.light = LL.resolveLight(this.map.getLight());
    this.night = false;
    this.shared = sharedUniforms();
    this.scene = new THREE.Scene();
    this.camera = new THREE.Camera();
    this.renderer = null;
    this.layer = { id: LAYER_ID, type: "custom", renderingMode: "3d", onAdd: (map, gl) => this.onAdd(gl), render: (gl, args) => this.render(gl, args) };
  }

  // ---------------------------------------------------------------- UI

  bindUI() {
    $("#lmkFile").addEventListener("change", (e) => this.loadFile(e.target.files[0]));
    $("#lmkPick").addEventListener("click", () => {
      if (this.api.selectedId()) $("#lmkId").value = this.api.selectedId();
      this.syncButtons();
    });
    $("#lmkId").addEventListener("input", () => this.syncButtons());
    $("#lmkPlace").addEventListener("click", () => this.placeOn($("#lmkId").value.trim()));
    $("#lmkRemove").addEventListener("click", () => this.remove());
    ["#lmkHeading", "#lmkScale", "#lmkAnchor"].forEach((s) => $(s).addEventListener("input", () => this.update()));
    $("#lmkCompare").addEventListener("change", () => this.syncReference());
    $("#lmkFlat").addEventListener("change", () => this.update());
    // Light: re-read on every style change, so a preset switch relights the model at once.
    this.map.on("styledata", () => this.readLight());
    this.map.on("style.load", () => this.readNight());
    this.map.on("zoom", () => this.syncReferenceOffset());
    this.map.on("rotate", () => { if (this.light.anchor === "viewport") this.renderInfo(); });
    if (!$("#lmkId").value && this.api.selectedId()) $("#lmkId").value = this.api.selectedId();
    this.readNight();
    this.syncButtons();
  }

  syncButtons() {
    $("#lmkPick").disabled = !this.api.selectedId();
    $("#lmkPlace").disabled = !$("#lmkId").value.trim() || !this.api.ready();
    $("#lmkRemove").disabled = !this.place && !this.model;
  }

  params() {
    const num = (s, d) => (Number.isFinite(Number($(s).value)) && $(s).value !== "" ? Number($(s).value) : d);
    const scale = num("#lmkScale", 1);
    return { heading: num("#lmkHeading", 0), scale: scale > 0 ? scale : 1, anchor: $("#lmkAnchor").value, flat: $("#lmkFlat").checked };
  }

  note(text, warn = false) {
    $("#lmkStatus").textContent = text;
    $("#lmkStatus").classList.toggle("warn", warn);
  }

  // ---------------------------------------------------------------- model

  async loadFile(file) {
    if (!file) return;
    this.note(`Đang đọc ${file.name}…`);
    let gltf;
    try {
      gltf = await new GLTFLoader().parseAsync(await file.arrayBuffer(), "");
    } catch (err) {
      const msg = String(err?.message || err);
      const hint = /draco|meshopt|ktx2|basisu/i.test(msg) ? " File dùng nén Draco / meshopt / KTX2 — xuất lại GLB không nén để xem thử." : "";
      this.note(`Không đọc được ${file.name}: ${msg}.${hint}`, true);
      return;
    }
    this.disposeModel();
    const root = gltf.scene;
    root.updateMatrixWorld(true);
    const materials = new Map();
    const textures = new Set();
    let meshes = 0;
    root.traverse((obj) => {
      if (!obj.isMesh) return;
      meshes++;
      if (!obj.geometry.attributes.normal) {
        // glTF: no normals means flat shading.
        obj.geometry = obj.geometry.index ? obj.geometry.toNonIndexed() : obj.geometry;
        obj.geometry.computeVertexNormals();
      }
      const vertexColors = Boolean(obj.geometry.attributes.color);
      const convert = (m) => {
        const key = `${m.uuid}:${vertexColors}`;
        if (!materials.has(key)) materials.set(key, landmarkMaterial(m, this.shared, vertexColors, textures));
        return materials.get(key);
      };
      obj.material = Array.isArray(obj.material) ? obj.material.map(convert) : convert(obj.material);
      obj.frustumCulled = false;  // the camera is MapLibre's matrix, three cannot cull with it
    });
    if (!meshes) {
      this.note(`${file.name} không có mesh nào.`, true);
      return;
    }
    const bounds = new THREE.Box3().setFromObject(root);
    this.model = { root, bounds, name: file.name, materials: [...materials.values()], textures: [...textures], meshes };
    this.scene.add(root);
    this.note("");
    this.update();
    this.syncButtons();
  }

  disposeModel() {
    if (!this.model) return;
    this.scene.remove(this.model.root);
    this.model.root.traverse((obj) => { if (obj.isMesh) obj.geometry.dispose(); });
    this.model.materials.forEach((m) => m.dispose());
    this.model.textures.forEach((t) => t.dispose());
    this.model = null;
  }

  // ---------------------------------------------------------------- placement

  async placeOn(id) {
    if (!id) return;
    const s = this.api.state;
    const c = this.api.lookAt(id);
    const q = new URLSearchParams({ f: this.api.token, layer: s.layer, idcol: s.idCol, id, lon: c.lng.toFixed(7), lat: c.lat.toFixed(7) });
    if (s.maxzoom != null) q.set("z", String(s.maxzoom));
    if (s.heightAttr) q.set("h", s.heightAttr);
    if (s.baseAttr) q.set("base", s.baseAttr);
    if (s.parentAttr) q.set("parent", s.parentAttr);
    this.note("Đang tìm footprint của building…");
    let place;
    try {
      const res = await fetch(`/api/building?${q}`);
      place = await res.json();
      if (!res.ok) throw new Error(place.error || `HTTP ${res.status}`);
    } catch (err) {
      this.note(`Không đặt được: ${err.message}`, true);
      return;
    }
    this.place = place;
    this.api.setHidden(id);  // hides its extrusion (and its parts); its ground shadow stays
    this.note("");
    this.ensureLayers();
    this.update();
    this.syncButtons();
  }

  remove() {
    this.place = null;
    this.disposeModel();
    this.api.setHidden(null);
    if (this.map.getLayer(REF_LAYER_ID)) this.map.removeLayer(REF_LAYER_ID);
    if (this.map.getLayer(LAYER_ID)) this.map.removeLayer(LAYER_ID);
    $("#lmkFile").value = "";
    $("#lmkInfo").innerHTML = "";
    $("#lmkUniforms").textContent = "";
    this.note("");
    this.syncButtons();
  }

  // Called by preview.js after every (re)build of its building layers: a basemap or preset
  // switch replaces the whole style, custom layers included.
  ensureLayers() {
    if (!this.place || !this.map.getLayer("bldg-3d")) return;
    if (!this.map.getLayer(LAYER_ID)) {
      // Under the extrusions: translucent buildings in front of the landmark blend over it.
      this.map.addLayer(this.layer, "bldg-3d");
    }
    this.syncReference();
  }

  // Model frame -> ENU metres around the anchor -> Mercator, multiplied into MapLibre's matrix.
  // three's camera has no view transform, so this is the only placement.
  anchorModel() {
    const b = this.model.bounds;
    if (this.params().anchor === "bbox") return new THREE.Vector3((b.min.x + b.max.x) / 2, b.min.y, (b.min.z + b.max.z) / 2);
    return new THREE.Vector3(0, 0, 0);
  }

  placement() {
    const { heading, scale } = this.params();
    const a = this.anchorModel();
    const merc = maplibregl.MercatorCoordinate.fromLngLat(this.place.centroid, 0);
    const m = merc.meterInMercatorCoordinateUnits();
    return new THREE.Matrix4()
      .makeTranslation(merc.x, merc.y, merc.z)
      .multiply(new THREE.Matrix4().makeScale(m, -m, m))                   // ENU metres -> Mercator (y south)
      .multiply(new THREE.Matrix4().makeRotationZ((-heading * Math.PI) / 180))  // heading: clockwise seen from above
      .multiply(new THREE.Matrix4().makeRotationX(Math.PI / 2))            // glTF Y-up -> Z-up
      .multiply(new THREE.Matrix4().makeScale(scale, scale, scale))
      .multiply(new THREE.Matrix4().makeTranslation(-a.x, -a.y, -a.z));
  }

  // ---------------------------------------------------------------- light

  // The night switch lives in the style's metadata, which only changes with a new style.
  readNight() {
    this.night = LL.isNightStyle(this.map.getStyle()?.metadata);
    this.readLight(true);
  }

  readLight(force = false) {
    const light = LL.resolveLight(this.map.getLight());
    if (!force && JSON.stringify(light) === JSON.stringify(this.light)) return;
    this.light = light;
    this.renderInfo();
    this.map.triggerRepaint();
  }

  setUniforms() {
    const { heading, flat } = this.params();
    const u = LL.landmarkUniforms(this.light, { heading, bearing: this.map.getBearing(), night: this.night });
    const s = this.shared;
    s.u_lightpos.value.fromArray(u.u_lightpos);
    s.u_lightcolor.value.fromArray(u.u_lightcolor);
    s.u_lightintensity.value = u.u_lightintensity;
    s.u_north.value.fromArray(u.u_north);
    s.u_emissive_on.value = u.u_emissive_on;
    s.u_vertical_gradient.value = this.map.getLayer("bldg-3d") && this.map.getPaintProperty("bldg-3d", "fill-extrusion-vertical-gradient") === false ? 0 : 1;
    const a = this.anchorModel();
    s.u_ground.value = a.y;
    s.u_top.value = this.model.bounds.max.y;
    s.u_height_m.value = this.modelHeight();
    const ref = LL.parseColor(this.api.referenceColor()) || [1, 1, 1, 1];
    s.u_override.value.set(ref[0], ref[1], ref[2], flat ? 1 : 0);
    return u;
  }

  modelHeight() {
    return (this.model.bounds.max.y - this.anchorModel().y) * this.params().scale;
  }

  // ---------------------------------------------------------------- custom layer

  onAdd(gl) {
    if (!this.renderer) {
      // One renderer for the map's lifetime: the layer is removed and re-added on every
      // style switch, the GL context stays.
      this.renderer = new THREE.WebGLRenderer({ canvas: this.map.getCanvas(), context: gl, antialias: true });
      this.renderer.autoClear = false;
    }
  }

  render(gl, args) {
    if (!this.model || !this.place || !$("#threeD").checked) return;
    this.setUniforms();
    this.camera.projectionMatrix = new THREE.Matrix4().fromArray(args.defaultProjectionData.mainMatrix).multiply(this.placement());
    this.renderer.resetState();
    this.renderer.setViewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
    this.renderer.render(this.scene, this.camera);
  }

  update() {
    if (this.model && this.place) this.ensureLayers();
    this.syncReferenceOffset();
    this.renderInfo();
    this.map.triggerRepaint();
  }

  // ---------------------------------------------------------------- QA: the building's own extrusion beside the model

  syncReference() {
    const map = this.map;
    const on = $("#lmkCompare").checked && this.place && map.getLayer("bldg-3d");
    if (!on) {
      if (map.getLayer(REF_LAYER_ID)) map.removeLayer(REF_LAYER_ID);
      return;
    }
    if (!map.getLayer(REF_LAYER_ID)) {
      map.addLayer({ id: REF_LAYER_ID, type: "fill-extrusion", source: "bldg", "source-layer": this.api.state.layer, paint: {} }, LAYER_ID);
    }
    // Same geometry, height and gradient as bldg-3d, in the comparison colour, opaque (the
    // model is opaque too), moved sideways on screen.
    map.setFilter(REF_LAYER_ID, this.api.buildingFilter(this.place.id));
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-color", this.api.referenceColor());
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-opacity", 1);
    for (const p of ["fill-extrusion-height", "fill-extrusion-base", "fill-extrusion-vertical-gradient"]) {
      map.setPaintProperty(REF_LAYER_ID, p, map.getPaintProperty("bldg-3d", p));
    }
    map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-translate-anchor", "viewport");
    this.syncReferenceOffset();
    this.renderInfo();
  }

  // fill-extrusion-translate is in pixels: keep the copy one footprint diagonal (+ 15 m) to the
  // right of the building at every zoom.
  syncReferenceOffset() {
    if (!this.place || !this.map.getLayer(REF_LAYER_ID)) return;
    const [w, d] = this.place.size_m;
    const metres = Math.hypot(w, d) + 15;
    const mpp = (EARTH_M * Math.cos((this.place.centroid[1] * Math.PI) / 180)) / (512 * 2 ** this.map.getZoom());
    this.map.setPaintProperty(REF_LAYER_ID, "fill-extrusion-translate", [metres / mpp, 0]);
  }

  // ---------------------------------------------------------------- readouts

  renderInfo() {
    const info = $("#lmkInfo");
    if (!info) return;
    const rows = [];
    if (this.model) {
      const b = this.model.bounds;
      const size = new THREE.Vector3();
      b.getSize(size);
      const { scale } = this.params();
      rows.push(`<div><b>${esc(this.model.name)}</b>: ${this.model.meshes} mesh, ${this.model.materials.length} vật liệu. `
        + `Hộp bao ${fmt(size.x * scale)} × ${fmt(size.z * scale)} m mặt bằng, cao ${fmt(this.modelHeight())} m trên điểm neo.</div>`);
      const center = new THREE.Vector3((b.min.x + b.max.x) / 2, b.min.y, (b.min.z + b.max.z) / 2);
      const off = center.length() * scale;
      if (off > 1) rows.push(`<div>Gốc GLB cách tâm đáy hộp bao ${fmt(off)} m.</div>`);
    }
    if (this.place) {
      const p = this.place;
      rows.push(`<div>Neo tại ${p.centroid[1].toFixed(6)}, ${p.centroid[0].toFixed(6)}: tâm footprint ${p.matched === "parent" ? "các khối con của " : ""}building `
        + `${esc(p.id)} (${fmt(p.area_m2, 0)} m², ${p.pieces} mảnh tile). Extrusion của building đã ẩn, bóng vẫn giữ.</div>`);
    }
    if (this.model && this.place) rows.push(heightCheck(this.modelHeight(), this.place, this.params().scale));
    if (this.model || this.place) rows.push(this.lightRows());
    info.innerHTML = rows.join("");
    $("#lmkUniforms").textContent = this.model ? this.uniformDump() : "";
  }

  lightRows() {
    const l = this.light;
    const [r, az, polar] = l.position;
    const extra = l.anchor === "viewport" ? ` + bearing ${fmt(this.map.getBearing(), 0)}°` : "";
    const warn = l.problems.length ? ` <span class="warn">Không dùng được ${l.problems.join(", ")} của light (expression?): dùng giá trị mặc định.</span>` : "";
    const emissive = this.night ? "Emissive: bật (style ban đêm)." : "Emissive: tắt (chỉ bật với style ban đêm).";
    return `<div>Light của style (anchor ${l.anchor}): màu ${rgb(l.color)}, cường độ ${l.intensity}, `
      + `[${r}, ${az}°${extra}, ${polar}°]. ${emissive}${warn}</div>`
      + `<div class="lmk-swatches">Màu mong đợi với màu so sánh ${esc(this.api.referenceColor())}: ${this.expectedSwatches()}</div>`;
  }

  // Roof and walls facing N / E / S / W, top of the wall, through the CPU copy of the shader.
  expectedSwatches() {
    const base = (LL.parseColor(this.api.referenceColor()) || [1, 1, 1]).slice(0, 3);
    const opts = { bearing: this.map.getBearing(), heightM: this.place?.height ?? (this.model ? this.modelHeight() : 0), t: 1 };
    const faces = [["mái", [0, 0, 1]], ...COMPASS.map((name, i) => [`tường ${name}`, [Math.sin((i * Math.PI) / 2), Math.cos((i * Math.PI) / 2), 0]])];
    return faces.map(([name, n]) => {
      const c = LL.shade(base, n.map((v) => (Math.abs(v) < 1e-12 ? 0 : v)), this.light, opts);
      return `<span class="lmk-sw" title="${esc(name)}: ${rgb(c)}"><i style="background:${rgb(c)}"></i>${esc(name)}</span>`;
    }).join("");
  }

  uniformDump() {
    const { heading, scale } = this.params();
    const u = LL.landmarkUniforms(this.light, { heading, bearing: this.map.getBearing(), night: this.night });
    const v = (a) => `[${a.map((x) => x.toFixed(3)).join(", ")}]`;
    return [
      `heading_deg = ${heading}`,
      `scale = ${scale}`,
      `L_enu = ${v(u.lightposEnu)}`,
      `u_lightpos = ${v(u.u_lightpos)}`,
      `u_lightcolor = ${v(u.u_lightcolor)}`,
      `u_lightintensity = ${u.u_lightintensity}`,
      `u_north = ${v(u.u_north)}`,
      `u_height_m = ${this.modelHeight().toFixed(2)}`,
      `u_emissive_on = ${u.u_emissive_on}`,
    ].join("\n");
  }
}

function heightCheck(modelH, place, scale) {
  if (place.height == null) return `<div>Building không có cột chiều cao để so.</div>`;
  const diff = modelH - place.height;
  const bad = Math.abs(diff) > HEIGHT_TOLERANCE.m && Math.abs(diff) > HEIGHT_TOLERANCE.ratio * place.height;
  const fit = modelH > 0 ? (scale * place.height) / modelH : null;
  return `<div class="${bad ? "warn" : ""}">Chiều cao: mô hình ${fmt(modelH)} m, building ${fmt(place.height)} m (${esc(place.height_field)}) → lệch `
    + `${diff >= 0 ? "+" : ""}${fmt(diff)} m (${diff >= 0 ? "+" : ""}${fmt((100 * diff) / (place.height || 1), 1)}%).`
    + `${bad && fit ? ` Tỉ lệ khớp chiều cao: ${fit.toPrecision(4)}.` : ""}</div>`;
}

function sharedUniforms() {
  return {
    u_lightpos: { value: new THREE.Vector3() },
    u_lightcolor: { value: new THREE.Vector3(1, 1, 1) },
    u_lightintensity: { value: 0.5 },
    u_vertical_gradient: { value: 1 },
    u_height_m: { value: 0 },
    u_north: { value: new THREE.Vector3(0, 0, -1) },
    u_ground: { value: 0 },
    u_top: { value: 1 },
    u_emissive_on: { value: 0 },
    u_override: { value: new THREE.Vector4() },
  };
}

// glTF material (as three's GLTFLoader built it) -> the landmark ShaderMaterial. three keeps
// colour factors linear (fine: the shader expects the glTF factors) and marks the base colour /
// emissive textures sRGB, which would make WebGL linearise them on sampling: they are switched
// to NoColorSpace so the shader gets the raw bytes.
function landmarkMaterial(src, shared, vertexColors, textures) {
  const tex = (t) => {
    if (!t) return null;
    t.colorSpace = THREE.NoColorSpace;
    t.updateMatrix();
    t.needsUpdate = true;
    textures.add(t);
    return t;
  };
  const map = tex(src.map);
  const emissiveMap = tex(src.emissiveMap);
  const color = src.color || new THREE.Color(1, 1, 1);
  const emissive = src.emissive || new THREE.Color(0, 0, 0);
  return new THREE.ShaderMaterial({
    name: `landmark:${src.name || src.uuid}`,
    glslVersion: THREE.GLSL3,
    vertexShader: LL.VERTEX_SHADER,
    fragmentShader: LL.FRAGMENT_SHADER,
    defines: vertexColors ? { LMK_VERTEX_COLOR: "" } : {},
    uniforms: {
      ...shared,
      u_base_factor: { value: new THREE.Vector4(color.r, color.g, color.b, src.opacity ?? 1) },
      u_base_map: { value: map },
      u_has_base_map: { value: map ? 1 : 0 },
      u_map_transform: { value: map ? map.matrix : new THREE.Matrix3() },
      u_emissive: { value: new THREE.Vector3(emissive.r, emissive.g, emissive.b) },
      u_emissive_map: { value: emissiveMap },
      u_has_emissive_map: { value: emissiveMap ? 1 : 0 },
      u_emissive_transform: { value: emissiveMap ? emissiveMap.matrix : new THREE.Matrix3() },
      u_emissive_intensity: { value: src.emissiveIntensity ?? 1 },
      u_alpha_cutoff: { value: src.alphaTest > 0 ? src.alphaTest : -1 },
    },
    side: src.side ?? THREE.FrontSide,
    transparent: Boolean(src.transparent),
    depthWrite: src.depthWrite ?? true,
  });
}

function fmt(v, digits = 1) {
  return Number(v).toLocaleString("vi-VN", { maximumFractionDigits: digits, minimumFractionDigits: 0 });
}

function rgb(c) {
  return `rgb(${c.map((v) => Math.round(v * 255)).join(", ")})`;
}
