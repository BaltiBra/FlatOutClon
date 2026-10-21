// Consulta de suelo con material sobre la colision cdb2 de web/tracks/<pista>/track.json.
// Uso (ES module):
//   import { loadCollision } from './collision_materials.js';
//   const col = await loadCollision('tracks/arena1/');
//   const hit = col.groundAt(x, z, yDesde);  // -> {y, tri, material:{name,grip,solid,...}, grip} | null
//   // yDesde: altura desde la que se lanza el rayo hacia abajo (p. ej. y de la rueda + 1)
// Coordenadas three.js (las del json). Solo lectura: no depende de drive.html ni vehicle.js.
export async function loadCollision(base) {
  const [json, bin] = await Promise.all([
    fetch(base + 'track.json').then(r => r.json()),
    fetch(base + 'track.bin').then(r => r.arrayBuffer())]);
  const c = json.collision;
  const pos = new Float32Array(bin, c.position.offset, c.position.count);
  const idx = new Uint32Array(bin, c.index.offset, c.index.count);
  const tmat = c.triMaterial ? new Uint8Array(bin, c.triMaterial.offset, c.triMaterial.count) : null;
  const mats = c.materials || [];
  const CELL = 8, cells = new Map();
  const key = (cx, cz) => cx * 100003 + cz;
  for (let t = 0; t < idx.length / 3; t++) {
    let x0 = 1e9, x1 = -1e9, z0 = 1e9, z1 = -1e9;
    for (let k = 0; k < 3; k++) {
      const v = idx[t * 3 + k] * 3;
      x0 = Math.min(x0, pos[v]); x1 = Math.max(x1, pos[v]);
      z0 = Math.min(z0, pos[v + 2]); z1 = Math.max(z1, pos[v + 2]);
    }
    for (let cx = Math.floor(x0 / CELL); cx <= Math.floor(x1 / CELL); cx++)
      for (let cz = Math.floor(z0 / CELL); cz <= Math.floor(z1 / CELL); cz++) {
        const k = key(cx, cz);
        (cells.get(k) || cells.set(k, []).get(k)).push(t);
      }
  }
  // Rayo vertical hacia abajo: devuelve el primer triangulo SOLIDO por debajo de yFrom.
  function groundAt(x, z, yFrom = 1e9) {
    let best = null;
    for (const t of cells.get(key(Math.floor(x / CELL), Math.floor(z / CELL))) || []) {
      const m = tmat ? mats[tmat[t]] : null;
      if (m && !m.solid) continue;
      const a = idx[t * 3] * 3, b = idx[t * 3 + 1] * 3, cc = idx[t * 3 + 2] * 3;
      const d = (pos[b + 2] - pos[cc + 2]) * (pos[a] - pos[cc]) + (pos[cc] - pos[b]) * (pos[a + 2] - pos[cc + 2]);
      if (Math.abs(d) < 1e-12) continue;
      const u = ((pos[b + 2] - pos[cc + 2]) * (x - pos[cc]) + (pos[cc] - pos[b]) * (z - pos[cc + 2])) / d;
      const v = ((pos[cc + 2] - pos[a + 2]) * (x - pos[cc]) + (pos[a] - pos[cc]) * (z - pos[cc + 2])) / d;
      const w = 1 - u - v;
      if (u < -1e-6 || v < -1e-6 || w < -1e-6) continue;
      const y = u * pos[a + 1] + v * pos[b + 1] + w * pos[cc + 1];
      if (y <= yFrom && (!best || y > best.y)) best = { y, tri: t, material: m, grip: m ? m.grip : 1 };
    }
    return best;
  }
  return { json, groundAt, materials: mats };
}
