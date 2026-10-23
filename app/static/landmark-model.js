// A .glb landmark drawn in a MapLibre custom layer, lit by the style's own `light` exactly like the
// fill-extrusions around it (the formula and shaders are in landmark-light.js). Shared by the 3D
// preview (landmark.js) and the landmark library page (landmarks.js).
//
// Placement: the model point `anchor` ("origin": the GLB origin, "bbox": the bottom centre of its
// box) stands on the ground at lon / lat, turned `heading` degrees clockwise seen from above (at 0,
// GLB +X points east and the front, +Z, faces south) and scaled by `scale`.
/* global maplibregl */
import * as THREE from "three";
import { GLTFLoader } from "/static/vendor/three/examples/jsm/loaders/GLTFLoader.js";
import * as LL from "/static/landmark-light.js";

export { LL };

export class LandmarkLayer {
  // opts: id, before() (the layer to draw under, so translucent buildings in front blend over the
  // model; the model waits until it exists; null: on top), visible(), referenceColor() and flat()
  // (one-colour QA look), onLight() (the light changed).
  constructor(map, opts = {}) {
    this.map = map;
    this.opts = {
      id: "landmark",
      before: () => "bldg-3d",
      visible: () => true,
      referenceColor: () => "#ffffff",
      flat: () => false,
      onLight: () => {},
      ...opts,
    };
    this.model = null;  // { root, bounds, name, materials, textures, meshes, triangles }
    this.placement = null;  // { lon, lat, heading, scale, anchor }
    this.light = LL.resolveLight(styleLight(map));
    this.night = false;  // read again on style.load
    try { this.night = LL.isNightStyle(map.getStyle()?.metadata); } catch { /* style not loaded yet */ }
    this.shared = sharedUniforms();
    this.scene = new THREE.Scene();
    this.camera = new THREE.Camera();
    this.renderer = null;
    this.layer = { id: this.opts.id, type: "custom", renderingMode: "3d", onAdd: (m, gl) => this.onAdd(gl), render: (gl, args) => this.render(gl, args) };
    // Light: re-read on every style change, so a preset switch relights the model at once.
    map.on("styledata", () => this.readLight());
    map.on("style.load", () => this.readNight());
  }

  // ---------------------------------------------------------------- model

  // Parse a .glb and make it the model shown. Throws with a readable message.
  async load(buffer, name) {
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
      const convert = (m) => {
        const key = `${m.uuid}:${vertexColors}`;
        if (!materials.has(key)) materials.set(key, landmarkMaterial(m, this.shared, vertexColors, textures));
        return materials.get(key);
      };
      obj.material = Array.isArray(obj.material) ? obj.material.map(convert) : convert(obj.material);
      obj.frustumCulled = false;  // the camera is MapLibre's matrix, three cannot cull with it
    });
    if (!meshes) throw new Error(`${name} không có mesh nào.`);
    this.disposeModel();
    const bounds = new THREE.Box3().setFromObject(root);
    this.model = { root, bounds, name, materials: [...materials.values()], textures: [...textures], meshes, triangles };
    this.scene.add(root);
    this.update();
    return this.model;
  }

  disposeModel() {
    if (!this.model) return;
    this.scene.remove(this.model.root);
    this.model.root.traverse((obj) => { if (obj.isMesh) obj.geometry.dispose(); });
    this.model.materials.forEach((m) => m.dispose());
    this.model.textures.forEach((t) => t.dispose());
    this.model = null;
  }

  // Remove the model and the layer.
  clear() {
    this.disposeModel();
    this.placement = null;
    if (this.map.getLayer(this.opts.id)) this.map.removeLayer(this.opts.id);
    this.map.triggerRepaint();
  }

  // ---------------------------------------------------------------- placement

  setPlacement(placement) {
    this.placement = placement ? { ...placement } : null;
    this.update();
  }

  // Call after every (re)build of the style's own layers: a basemap or preset switch replaces
  // the whole style, custom layers included.
  ensureLayer() {
    if (!this.model || !this.placement || this.map.getLayer(this.opts.id)) return;
    const before = this.opts.before();
    if (before && !this.map.getLayer(before)) return;  // style still loading: called again once it is
    this.map.addLayer(this.layer, before || undefined);
  }

  update() {
    this.ensureLayer();
    this.map.triggerRepaint();
  }

  // The model point that goes on the ground, in model coordinates.
  anchorModel() {
    const b = this.model.bounds;
    if (this.placement?.anchor === "bbox") return new THREE.Vector3((b.min.x + b.max.x) / 2, b.min.y, (b.min.z + b.max.z) / 2);
    return new THREE.Vector3(0, 0, 0);
  }

  // Height of the model above its anchor point, scaled.
  modelHeight() {
    return (this.model.bounds.max.y - this.anchorModel().y) * (this.placement?.scale ?? 1);
  }

  // Footprint of the model box, scaled: [x, z] in metres.
  modelSize() {
    const size = new THREE.Vector3();
    this.model.bounds.getSize(size);
    const scale = this.placement?.scale ?? 1;
    return [size.x * scale, size.z * scale];
  }

  // Model frame -> ENU metres around the anchor -> Mercator, multiplied into MapLibre's matrix.
  // three's camera has no view transform, so this is the only placement.
  matrix() {
    const { lon, lat, heading, scale } = this.placement;
    const a = this.anchorModel();
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

  // ---------------------------------------------------------------- light

  // The night switch lives in the style's metadata, which only changes with a new style.
  readNight() {
    this.night = LL.isNightStyle(this.map.getStyle()?.metadata);
    this.readLight(true);
  }

  readLight(force = false) {
    const light = LL.resolveLight(styleLight(this.map));
    if (!force && JSON.stringify(light) === JSON.stringify(this.light)) return;
    this.light = light;
    this.opts.onLight();
    this.map.triggerRepaint();
  }

  // The uniforms of the current light, heading and map bearing (also for readouts).
  uniforms() {
    return LL.landmarkUniforms(this.light, { heading: this.placement?.heading ?? 0, bearing: this.map.getBearing(), night: this.night });
  }

  setUniforms() {
    const u = this.uniforms();
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
    const ref = LL.parseColor(this.opts.referenceColor()) || [1, 1, 1, 1];
    s.u_override.value.set(ref[0], ref[1], ref[2], this.opts.flat() ? 1 : 0);
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
    if (!this.model || !this.placement || !this.opts.visible()) return;
    this.setUniforms();
    this.camera.projectionMatrix = new THREE.Matrix4().fromArray(args.defaultProjectionData.mainMatrix).multiply(this.matrix());
    this.renderer.resetState();
    this.renderer.setViewport(0, 0, gl.drawingBufferWidth, gl.drawingBufferHeight);
    this.renderer.render(this.scene, this.camera);
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
