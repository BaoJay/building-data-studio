// .glb landmarks drawn in one MapLibre custom layer, lit by the style's own `light` exactly like
// the fill-extrusions around them (the formula and shaders are in landmark-light.js). Used by the
// 3D preview (landmark-workspace.js), which draws every landmark in use on the PMTiles it shows.
//
// Each model is parsed once (parseModel) into its own scene, with its own uniforms, and drawn with
// its own placement: the model point `anchor` ("origin": the GLB origin, "bbox": the bottom centre
// of its box) stands on the ground at lon / lat, turned `heading` degrees clockwise seen from above
// (at 0, GLB +X points east and the front, +Z, faces south) and scaled by `scale`. Every model
// reads the same light; heading, scale, anchor and height are their own.
/* global maplibregl */
import * as THREE from "three";
import { GLTFLoader } from "/static/vendor/three/examples/jsm/loaders/GLTFLoader.js";
import * as LL from "/static/landmark-light.js";

export { LL };

// ------------------------------------------------------------------ models

// Parse a .glb into a model ready to draw. Throws with a readable message.
export async function parseModel(buffer, name) {
  let gltf;
  try {
    gltf = await new GLTFLoader().parseAsync(buffer, "");
  } catch (err) {
    const msg = String(err?.message || err);
    const hint = /draco|meshopt|ktx2|basisu/i.test(msg) ? " File dùng nén Draco / meshopt / KTX2 — xuất lại GLB không nén để xem thử." : "";
    throw new Error(`Không đọc được ${name}: ${msg}.${hint}`);
  }
  const root = gltf.scene;
  root.updateMatrixWorld(true);
  const shared = sharedUniforms();
  const materials = new Map();
  const textures = new Set();
  let meshes = 0;
  let triangles = 0;
  root.traverse((obj) => {
    if (!obj.isMesh) return;
    meshes++;
    if (!obj.geometry.attributes.normal) {
      // glTF: no normals means flat shading.
      obj.geometry = obj.geometry.index ? obj.geometry.toNonIndexed() : obj.geometry;
      obj.geometry.computeVertexNormals();
    }
    triangles += Math.floor((obj.geometry.index ? obj.geometry.index.count : obj.geometry.attributes.position.count) / 3);
    const vertexColors = Boolean(obj.geometry.attributes.color);
    // TEXCOORD_1 under its own name for the shader (an emissive mask with texCoord 1).
    if (obj.geometry.attributes.uv1) obj.geometry.setAttribute("lmk_uv1", obj.geometry.attributes.uv1);
    const convert = (m) => {
      const uv1 = Boolean(obj.geometry.attributes.uv1);
      const key = `${m.uuid}:${vertexColors}:${uv1}`;
      if (!materials.has(key)) materials.set(key, landmarkMaterial(m, shared, vertexColors, uv1, textures));
      return materials.get(key);
    };
    obj.material = Array.isArray(obj.material) ? obj.material.map(convert) : convert(obj.material);
    obj.frustumCulled = false;  // the camera is MapLibre's matrix, three cannot cull with it
  });
  if (!meshes) throw new Error(`${name} không có mesh nào.`);
  const scene = new THREE.Scene();
  scene.add(root);
  const bounds = new THREE.Box3().setFromObject(root);
  return { root, scene, shared, bounds, name, materials: [...materials.values()], textures: [...textures], meshes, triangles };
}

export function disposeModel(model) {
  if (!model) return;
  model.scene.remove(model.root);
  model.root.traverse((obj) => { if (obj.isMesh) obj.geometry.dispose(); });
  model.materials.forEach((m) => m.dispose());
  model.textures.forEach((t) => t.dispose());
}

// The model point that goes on the ground, in model coordinates.
export function anchorPoint(model, placement) {
  const b = model.bounds;
  if (placement?.anchor === "bbox") return new THREE.Vector3((b.min.x + b.max.x) / 2, b.min.y, (b.min.z + b.max.z) / 2);
  return new THREE.Vector3(0, 0, 0);
}

// Height of the model above its anchor point, scaled.
export function modelHeight(model, placement) {
  return (model.bounds.max.y - anchorPoint(model, placement).y) * (placement?.scale ?? 1);
}

// Footprint of the model box, scaled: [x, z] in metres.
export function modelSize(model, placement) {
  const size = new THREE.Vector3();
  model.bounds.getSize(size);
  const scale = placement?.scale ?? 1;
  return [size.x * scale, size.z * scale];
}

// Model frame -> ENU metres around the anchor -> Mercator, multiplied into MapLibre's matrix.
// three's camera has no view transform, so this is the only placement.
function placementMatrix(model, placement) {
  const { lon, lat, heading, scale } = placement;
  const a = anchorPoint(model, placement);
  const merc = maplibregl.MercatorCoordinate.fromLngLat([lon, lat], 0);
  const m = merc.meterInMercatorCoordinateUnits();
  return new THREE.Matrix4()
    .makeTranslation(merc.x, merc.y, merc.z)
    .multiply(new THREE.Matrix4().makeScale(m, -m, m))                   // ENU metres -> Mercator (y south)
    .multiply(new THREE.Matrix4().makeRotationZ((-heading * Math.PI) / 180))  // heading: clockwise seen from above
    .multiply(new THREE.Matrix4().makeRotationX(Math.PI / 2))            // glTF Y-up -> Z-up
    .multiply(new THREE.Matrix4().makeScale(scale, scale, scale))
    .multiply(new THREE.Matrix4().makeTranslation(-a.x, -a.y, -a.z));
}

// ------------------------------------------------------------------ layer

export class LandmarkLayer {
  // opts: id, before() (the layer to draw under, so translucent buildings in front blend over the
  // models; the layer waits until it exists; null: on top), models() (the models to draw: [{ model,
  // placement, flat }], flat: one-colour QA look), visible(), referenceColor(), onLight() (the
  // light changed).
  constructor(map, opts = {}) {
    this.map = map;
    this.opts = {
      id: "landmarks",
      before: () => "bldg-3d",
      models: () => [],
      visible: () => true,
      referenceColor: () => "#ffffff",
      onLight: () => {},
      ...opts,
    };
    this.light = LL.resolveLight(styleLight(map));
    this.night = false;  // read again on style.load
    this.emissive = 0;
    try { this.readPreset(map.getStyle()?.metadata); } catch { /* style not loaded yet */ }
    this.camera = new THREE.Camera();
    this.renderer = null;
    this.layer = { id: this.opts.id, type: "custom", renderingMode: "3d", onAdd: (m, gl) => this.onAdd(gl), render: (gl, args) => this.render(gl, args) };
    // Light: re-read on every style change, so a preset switch relights every model at once.
    map.on("styledata", () => this.readLight());
    map.on("style.load", () => this.readNight());
  }

  // Call after every (re)build of the style's own layers: a basemap or preset switch replaces
  // the whole style, custom layers included. The models stay parsed.
  ensureLayer() {
    if (this.map.getLayer(this.opts.id)) return;
    const before = this.opts.before();
    if (before && !this.map.getLayer(before)) return;  // style still loading: called again once it is
    this.map.addLayer(this.layer, before || undefined);
  }

  update() {
    this.ensureLayer();
    this.map.triggerRepaint();
  }

  // ---------------------------------------------------------------- light

  // The preset (night switch, emissive level) lives in the style's metadata, which only
  // changes with a new style.
  readNight() {
    this.readPreset(this.map.getStyle()?.metadata);
    this.readLight(true);
  }

  readPreset(metadata) {
    this.night = LL.isNightStyle(metadata);
    this.emissive = LL.emissiveLevel(metadata);
  }

  readLight(force = false) {
    const light = LL.resolveLight(styleLight(this.map));
    if (!force && JSON.stringify(light) === JSON.stringify(this.light)) return;
    this.light = light;
    this.opts.onLight();
    this.map.triggerRepaint();
  }

  // The uniforms of the current light for a heading and the map bearing (also for readouts).
  uniforms(heading = 0) {
    return LL.landmarkUniforms(this.light, { heading, bearing: this.map.getBearing(), emissive: this.emissive });
  }

  setUniforms(model, placement, flat) {
    const u = this.uniforms(placement.heading);
    const s = model.shared;
    s.u_lightpos.value.fromArray(u.u_lightpos);
    s.u_lightcolor.value.fromArray(u.u_lightcolor);
    s.u_lightintensity.value = u.u_lightintensity;
    s.u_north.value.fromArray(u.u_north);
    s.u_emissive_on.value = u.u_emissive_on;
    s.u_vertical_gradient.value = this.map.getLayer("bldg-3d") && this.map.getPaintProperty("bldg-3d", "fill-extrusion-vertical-gradient") === false ? 0 : 1;
    s.u_ground.value = anchorPoint(model, placement).y;
    s.u_top.value = model.bounds.max.y;
    s.u_height_m.value = modelHeight(model, placement);
    const ref = LL.parseColor(this.opts.referenceColor()) || [1, 1, 1, 1];
    s.u_override.value.set(ref[0], ref[1], ref[2], flat ? 1 : 0);
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
    if (!this.opts.visible()) return;
    const models = this.opts.models();
    if (!models.length) return;
    const main = new THREE.Matrix4().fromArray(args.defaultProjectionData.mainMatrix);
    this.renderer.resetState();
    this.renderer.setViewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
    for (const { model, placement, flat } of models) {
      this.setUniforms(model, placement, flat);
      this.camera.projectionMatrix = main.clone().multiply(placementMatrix(model, placement));
      this.renderer.render(model.scene, this.camera);
    }
  }
}

// The style's root light; undefined (MapLibre's default light) while no style is loaded yet.
function styleLight(map) {
  try {
    return map.getLight();
  } catch {
    return undefined;
  }
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
function landmarkMaterial(src, shared, vertexColors, hasUv1, textures) {
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
  const defines = vertexColors ? { LMK_VERTEX_COLOR: "" } : {};
  if (emissiveMap && emissiveMap.channel === 1) {
    if (hasUv1) defines.LMK_EMISSIVE_UV1 = "";
    else console.warn(`landmark: ${src.name || src.uuid}: emissiveTexture dùng TEXCOORD_1 nhưng mesh không có TEXCOORD_1, dùng TEXCOORD_0.`);
  }
  return new THREE.ShaderMaterial({
    name: `landmark:${src.name || src.uuid}`,
    glslVersion: THREE.GLSL3,
    vertexShader: LL.VERTEX_SHADER,
    fragmentShader: LL.FRAGMENT_SHADER,
    defines,
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
