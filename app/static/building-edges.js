// SketchUp-style "Edges" for the LoD1 extrusions: each building's roof outline, its vertical
// corners and its footprint, drawn as 1px lines by a custom WebGL layer right above bldg-3d.
// fill-extrusion has no outline of its own, so a block of buildings sharing walls (Hà Nội's
// Old Quarter) reads as one solid mass; the edges split it back into its buildings.
//
// The lines are rebuilt from the loaded vector tiles whenever the view settles. They share the
// map's depth buffer, so an edge hidden behind a building stays hidden, pulled a hair towards
// the camera so the edges of the faces in view are not lost in z-fighting with those faces.

const MIN_ZOOM = 15;          // below it the view holds too many buildings for one rebuild
const DEPTH_BIAS = 0.00002;   // in clip-space z (× w): enough against z-fighting, small enough to keep occlusion
const CORNER_COS = Math.cos((8 * Math.PI) / 180);  // a vertex turning less than 8° is not a corner

const VS = `
attribute vec3 a_pos;
uniform mat4 u_matrix;
uniform float u_bias;
void main() {
  vec4 p = u_matrix * vec4(a_pos, 1.0);
  p.z -= u_bias * p.w;
  gl_Position = p;
}`;
const FS = `
precision mediump float;
uniform vec4 u_color;
void main() { gl_FragColor = u_color; }`;

export class BuildingEdges {
  // opts: id, before() (layer id to insert under), sourceLayer(), filter(), height(props),
  // base(props), enabled(), color() → [r, g, b, a] in 0..1, onStats({ buildings, segments, ms } | { tooLow } | null)
  constructor(map, opts) {
    this.map = map;
    this.opts = opts;
    this.gl = null;
    this.prog = null;
    this.buf = null;
    this.vao = null;
    this.count = 0;      // vertices in buf
    this.origin = null;  // { x, y, mpu }: mercator origin of the local metre frame
    this.timer = 0;
    this.layer = { id: opts.id, type: "custom", renderingMode: "3d", onAdd: (m, gl) => this.onAdd(gl), render: (gl, args) => this.render(gl, args) };
    map.on("moveend", () => this.schedule());
    map.on("sourcedata", (e) => { if (e.sourceId === "bldg" && e.tile) this.schedule(); });
  }

  // Call after every (re)build of the building layers: a style switch drops custom layers.
  ensureLayer() {
    if (this.map.getLayer(this.opts.id)) return;
    const before = this.opts.before();
    if (before && !this.map.getLayer(before)) return;
    this.map.addLayer(this.layer, before || undefined);
  }

  // The toggle, the filter, the heights or the theme changed: rebuild now.
  update() {
    this.ensureLayer();
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.rebuild(), 0);  // one rebuild for a burst of changes
  }

  schedule() {
    if (!this.opts.enabled()) return;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.rebuild(), 120);
  }

  rebuild() {
    clearTimeout(this.timer);
    const map = this.map;
    if (!this.opts.enabled() || !this.gl) {
      this.count = 0;
      this.opts.onStats(null);
      map.triggerRepaint();
      return;
    }
    if (map.getZoom() < MIN_ZOOM) {
      this.count = 0;
      this.opts.onStats({ tooLow: MIN_ZOOM });
      map.triggerRepaint();
      return;
    }
    const t0 = performance.now();
    const filter = this.opts.filter();
    const feats = map.querySourceFeatures("bldg", { sourceLayer: this.opts.sourceLayer(), ...(filter ? { filter } : {}) });
    const c = map.getCenter();
    const ox = (c.lng + 180) / 360;
    const oy = mercY(c.lat);
    const mpu = 1 / (40075016.68557849 * Math.cos((c.lat * Math.PI) / 180));  // mercator units per metre at the centre
    const out = new Growable();
    const seen = new Set();
    let buildings = 0;
    for (const f of feats) {
      const vt = f._vectorTileFeature;
      if (!vt || vt.type !== 3) continue;
      const top = Number(this.opts.height(f.properties)) || 0;
      const bottom = Number(this.opts.base(f.properties)) || 0;
      if (!(top > bottom)) continue;
      const rings = vt.loadGeometry();
      // The same tile can come twice (a tile and its overzoomed copy): draw it once.
      const sig = `${vt._z}/${vt._x}/${vt._y}|${f.id ?? ""}|${rings[0]?.[0]?.x},${rings[0]?.[0]?.y}`;
      if (seen.has(sig)) continue;
      seen.add(sig);
      const n = 2 ** vt._z;
      const ext = vt.extent;
      // Tile pixel → local metres (x east, y north).
      const sx = 1 / (ext * n * mpu);
      const tx = (vt._x / n - ox) / mpu;
      const ty = (oy - vt._y / n) / mpu;
      buildings++;
      for (const ring of rings) addRing(out, ring, ext, sx, tx, ty, bottom, top);
    }
    this.origin = { x: ox, y: oy, mpu };
    this.upload(out.view());
    this.opts.onStats({ buildings, segments: this.count / 2, ms: Math.round(performance.now() - t0) });
    map.triggerRepaint();
  }

  // ---------------------------------------------------------------- GL

  onAdd(gl) {
    if (this.gl === gl) return;
    this.gl = gl;
    this.prog = program(gl, VS, FS);
    this.loc = {
      pos: gl.getAttribLocation(this.prog, "a_pos"),
      matrix: gl.getUniformLocation(this.prog, "u_matrix"),
      bias: gl.getUniformLocation(this.prog, "u_bias"),
      color: gl.getUniformLocation(this.prog, "u_color"),
    };
    this.buf = gl.createBuffer();
    if (typeof WebGL2RenderingContext !== "undefined" && gl instanceof WebGL2RenderingContext) {
      this.vao = gl.createVertexArray();
      gl.bindVertexArray(this.vao);
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.enableVertexAttribArray(this.loc.pos);
      gl.vertexAttribPointer(this.loc.pos, 3, gl.FLOAT, false, 0, 0);
      gl.bindVertexArray(null);
    }
    this.schedule();
  }

  upload(data) {
    const gl = this.gl;
    if (this.vao) gl.bindVertexArray(null);
    gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
    gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
    this.count = data.length / 3;
  }

  render(gl, args) {
    if (!this.count || !this.origin || !this.opts.enabled()) return;
    const { x, y, mpu } = this.origin;
    // main × translate(origin) × scale(mpu, -mpu, mpu), in doubles: the big mercator offsets
    // cancel here, the float32 vertices stay small (metres around the origin).
    const m = mul(args.defaultProjectionData.mainMatrix, [mpu, 0, 0, 0, 0, -mpu, 0, 0, 0, 0, mpu, 0, x, y, 0, 1]);
    gl.useProgram(this.prog);
    gl.uniformMatrix4fv(this.loc.matrix, false, new Float32Array(m));
    gl.uniform1f(this.loc.bias, DEPTH_BIAS);
    gl.uniform4fv(this.loc.color, this.opts.color());
    if (this.vao) {
      gl.bindVertexArray(this.vao);
    } else {
      gl.bindBuffer(gl.ARRAY_BUFFER, this.buf);
      gl.enableVertexAttribArray(this.loc.pos);
      gl.vertexAttribPointer(this.loc.pos, 3, gl.FLOAT, false, 0, 0);
    }
    gl.enable(gl.DEPTH_TEST);
    gl.depthFunc(gl.LEQUAL);
    gl.depthMask(false);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    gl.drawArrays(gl.LINES, 0, this.count);
    if (this.vao) gl.bindVertexArray(null);
    gl.depthMask(true);
  }
}

// One ring of a footprint: its outline at the roof and at the base, and a vertical edge at each
// corner. Vector tiles clip a building at the tile's border plus a buffer: edges lying wholly
// outside the tile proper (the clip line among them) are the neighbour tile's to draw.
function addRing(out, ring, ext, sx, tx, ty, bottom, top) {
  const len = ring.length - (ring.length > 1 && ring[0].x === ring[ring.length - 1].x && ring[0].y === ring[ring.length - 1].y ? 1 : 0);
  if (len < 3) return;
  const X = (p) => tx + p.x * sx;
  const Y = (p) => ty - p.y * sx;
  const inside = (p) => p.x >= 0 && p.x <= ext && p.y >= 0 && p.y <= ext;
  for (let i = 0; i < len; i++) {
    const a = ring[i];
    const b = ring[(i + 1) % len];
    const outside = (a.x < 0 && b.x < 0) || (a.x > ext && b.x > ext) || (a.y < 0 && b.y < 0) || (a.y > ext && b.y > ext);
    if (!outside) {
      out.push(X(a), Y(a), top, X(b), Y(b), top);
      out.push(X(a), Y(a), bottom, X(b), Y(b), bottom);
    }
    if (inside(a) && isCorner(ring[(i - 1 + len) % len], a, b)) out.push(X(a), Y(a), bottom, X(a), Y(a), top);
  }
}

function isCorner(p, a, b) {
  const ux = a.x - p.x, uy = a.y - p.y, vx = b.x - a.x, vy = b.y - a.y;
  const d = Math.hypot(ux, uy) * Math.hypot(vx, vy);
  return !d || (ux * vx + uy * vy) / d < CORNER_COS;
}

function mercY(lat) {
  const s = Math.sin((lat * Math.PI) / 180);
  return 0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI);
}

// Column-major 4×4 product a × b.
function mul(a, b) {
  const o = new Array(16);
  for (let c = 0; c < 4; c++) {
    for (let r = 0; r < 4; r++) {
      o[c * 4 + r] = a[r] * b[c * 4] + a[4 + r] * b[c * 4 + 1] + a[8 + r] * b[c * 4 + 2] + a[12 + r] * b[c * 4 + 3];
    }
  }
  return o;
}

function program(gl, vs, fs) {
  const sh = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src);
    gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  };
  const p = gl.createProgram();
  gl.attachShader(p, sh(gl.VERTEX_SHADER, vs));
  gl.attachShader(p, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  return p;
}

class Growable {
  constructor() { this.a = new Float32Array(1 << 16); this.n = 0; }
  push(...v) {
    if (this.n + v.length > this.a.length) {
      const b = new Float32Array(this.a.length * 2);
      b.set(this.a);
      this.a = b;
    }
    for (const x of v) this.a[this.n++] = x;
  }
  view() { return this.a.subarray(0, this.n); }
}
