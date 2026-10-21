// Props dinamicos de la pista (vallas, barriles, neumaticos, cajas...) como cuerpos rigidos de Rapier.
//
//   import { loadProps } from './props.js';
//   const props = await loadProps(RAPIER, world, scene, 'tracks/arena1/props.json');
//   ...en cada paso de fisica o fotograma:  props.update(carPositionVector3);
//
// Datos: los genera tools/export_props.py (modelos locales + collider simple + instancias) con la fisica
// de dynamic_objects.bed (tools/dynobjects.py): masa, restitucion y tipo de inercia salen del bed.
// El bed no trae friccion ni damping: se usan opts.friction (0.6), linearDamping (0.02) y angularDamping (0.3).
//
// GRUPOS DE COLISION (Rapier: (membresia << 16) | filtro). Se chocan A y B si (mA & fB) && (mB & fA):
//   coche  : membresia 0x0001, filtro 0x0005 -> setCollisionGroups(0x00010005)  (suelo + props)
//   props  : membresia 0x0004, filtro 0x0005 -> 0x00040005                      (coche, suelo y entre si)
//   suelo/pista (trimesh): grupos por defecto (todos) -> choca con ambos; no hace falta tocarlo.
//
// COSTE: los 1593 props de arena1 estan siempre dibujados (un THREE.InstancedMesh por modelo y primitiva),
// pero solo tienen cuerpo los que estan a menos de activeRadius del coche (maximo maxBodies, y como mucho
// `createPerUpdate` altas por llamada). Los cuerpos nacen DORMIDOS (no caen ni consumen hasta que algo los
// toca, como en el juego) y, cuando se alejan mas de 1.25*activeRadius y estan quietos, se retiran del mundo
// congelando su pose. Los props con masa >= 5000 kg o con Joint/Hinge del bed son fijos (cuerpo fixed).
import * as THREE from 'three';

export const PROP_GROUPS = 0x00040005;

const SHADER = { WINDOW: 6, LIGHT: 10 };

async function defaultTextureLoader() {
  try {
    const { DDSLoader } = await import('three/addons/loaders/DDSLoader.js');
    const loader = new DDSLoader();
    return (dirs, file) => new Promise(resolve => {
      const next = i => {
        if (i >= dirs.length) return resolve(null);
        loader.load(dirs[i] + file, t => {
          t.wrapS = t.wrapT = THREE.RepeatWrapping;
          t.colorSpace = THREE.SRGBColorSpace;
          resolve(t);
        }, undefined, () => next(i + 1));
      };
      next(0);
    });
  } catch { return async () => null; }
}

function makeMaterial(m, map) {
  const mat = new THREE.MeshStandardMaterial({ map, roughness: 0.6, metalness: 0.1, color: map ? 0xffffff : 0x555555 });
  if (m.shader === SHADER.WINDOW) Object.assign(mat, { transparent: true, opacity: 0.55, depthWrite: false, roughness: 0.1, metalness: 0.6 });
  else if (m.shader === SHADER.LIGHT) Object.assign(mat, { transparent: true, depthWrite: false });
  else if (m.alpha) Object.assign(mat, { transparent: true, alphaTest: 0.4, side: THREE.DoubleSide });
  return mat;
}

export async function loadProps(RAPIER, world, scene, url,
  { maxBodies = 400, activeRadius = 120, createPerUpdate = 60, friction = 0.6,
    linearDamping = 0.02, angularDamping = 0.3, textureLoader = null, groups = PROP_GROUPS } = {}) {
  const binUrl = url.replace(/\.json$/, '.bin');
  const [json, bin] = await Promise.all([
    fetch(url).then(r => { if (!r.ok) throw new Error(`${url}: ${r.status}`); return r.json(); }),
    fetch(binUrl).then(r => { if (!r.ok) throw new Error(`${binUrl}: ${r.status}`); return r.arrayBuffer(); }),
  ]);

  // --- materiales (solo los que se usan) ---
  const loadTex = textureLoader || await defaultTextureLoader();
  const usedMats = new Set(json.models.flatMap(m => m.primitives.map(p => p.material)));
  const materials = [];
  await Promise.all([...usedMats].map(async mi => {
    const m = json.materials[mi];
    materials[mi] = makeMaterial(m, m.texture ? await loadTex(json.textureDirs || [], m.texture) : null);
  }));

  // --- instancias por modelo ---
  const inst = json.instances.map((d, i) => ({
    i, model: d.m, bed: json.beds[d.b] || { mass: 50, restitution: 0.2, inertia: 1, fixed: false },
    p: new THREE.Vector3(...d.p), q: new THREE.Quaternion(...d.q), body: null, slot: 0,
  }));
  const perModel = json.models.map(() => []);
  for (const it of inst) { it.slot = perModel[it.model].length; perModel[it.model].push(it); }

  const group = new THREE.Group();
  group.name = 'props';
  const meshes = json.models.map((md, mi) => md.primitives.map(p => {
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(bin, p.position.offset, p.position.count), 3));
    if (p.normal) geo.setAttribute('normal', new THREE.BufferAttribute(new Float32Array(bin, p.normal.offset, p.normal.count), 3));
    else geo.computeVertexNormals();
    if (p.uv) geo.setAttribute('uv', new THREE.BufferAttribute(new Float32Array(bin, p.uv.offset, p.uv.count), 2));
    geo.setIndex(new THREE.BufferAttribute(new Uint16Array(bin, p.index.offset, p.index.count), 1));
    const mesh = new THREE.InstancedMesh(geo, materials[p.material], Math.max(1, perModel[mi].length));
    mesh.frustumCulled = false;   // la esfera englobante de un InstancedMesh no cubre las instancias
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    group.add(mesh);
    return mesh;
  }));
  scene.add(group);

  const tmpM = new THREE.Matrix4(), one = new THREE.Vector3(1, 1, 1);
  const dirty = new Set();
  const writeMatrix = it => {
    tmpM.compose(it.p, it.q, one);
    for (const mesh of meshes[it.model]) mesh.setMatrixAt(it.slot, tmpM);
    dirty.add(it.model);
  };
  const flush = () => {
    for (const mi of dirty) for (const mesh of meshes[mi]) mesh.instanceMatrix.needsUpdate = true;
    dirty.clear();
  };
  for (const it of inst) writeMatrix(it);
  flush();

  // --- cuerpos ---
  const bodies = [];      // instancias con cuerpo vivo
  const hullCache = new Map();
  const colliderDesc = md => {
    const c = md.collider;
    if (c.type === 'hull') {
      if (!hullCache.has(md)) hullCache.set(md, new Float32Array(c.points));
      const d = RAPIER.ColliderDesc.convexHull(hullCache.get(md));
      if (d) return d;
    }
    const b = md.bbox;   // caja alineada al bbox (caja del exportador o respaldo del casco)
    const half = c.half || [0, 1, 2].map(k => (b.max[k] - b.min[k]) / 2);
    const ctr = c.center || [0, 1, 2].map(k => (b.max[k] + b.min[k]) / 2);
    return RAPIER.ColliderDesc.cuboid(...half).setTranslation(...ctr);
  };

  function createBody(it) {
    const md = json.models[it.model], bed = it.bed;
    const fixed = bed.fixed;
    const desc = (fixed ? RAPIER.RigidBodyDesc.fixed() : RAPIER.RigidBodyDesc.dynamic().setSleeping(true))
      .setTranslation(it.p.x, it.p.y, it.p.z).setRotation({ x: it.q.x, y: it.q.y, z: it.q.z, w: it.q.w })
      .setLinearDamping(linearDamping).setAngularDamping(angularDamping);
    const rb = world.createRigidBody(desc);
    const cd = colliderDesc(md).setFriction(friction).setRestitution(bed.restitution ?? 0.2)
      .setCollisionGroups(groups);
    world.createCollider(cd, rb);
    if (!fixed) {
      // masa del bed; inercia principal segun el tipo INERTIA_* (caja, cilindro, esfera, cilindro hueco)
      const u = md.inertiaUnit[String(bed.inertia)] || md.inertiaUnit[Object.keys(md.inertiaUnit)[0]];
      const b = md.bbox, m = bed.mass;
      const com = { x: (b.max[0] + b.min[0]) / 2, y: (b.max[1] + b.min[1]) / 2, z: (b.max[2] + b.min[2]) / 2 };
      rb.setAdditionalMassProperties(m, com, { x: u[0] * m, y: u[1] * m, z: u[2] * m }, { x: 0, y: 0, z: 0, w: 1 }, false);
    }
    it.body = rb;
    bodies.push(it);
  }

  function removeBody(it, k) {
    world.removeRigidBody(it.body);
    it.body = null;
    bodies.splice(k, 1);
  }

  const stats = { created: 0, removed: 0, awake: 0 };
  const R2 = activeRadius * activeRadius, R2out = R2 * 1.5625;   // 1.25^2

  function update(car) {
    // 1) sincroniza instancias con cuerpos despiertos; retira los dormidos lejanos
    let awake = 0;
    for (let k = bodies.length - 1; k >= 0; k--) {
      const it = bodies[k], rb = it.body;
      const sleeping = rb.isSleeping() || rb.isFixed();
      if (!sleeping) {
        const t = rb.translation(), r = rb.rotation();
        it.p.set(t.x, t.y, t.z); it.q.set(r.x, r.y, r.z, r.w);
        writeMatrix(it);
        awake++;
      }
      const dx = it.p.x - car.x, dz = it.p.z - car.z;
      if (sleeping && dx * dx + dz * dz > R2out) { removeBody(it, k); stats.removed++; }
    }
    // 2) da de alta cuerpos de props cercanos (dormidos), los mas cercanos primero; con el cupo lleno se
    //    expulsan los dormidos mas lejanos (solo si estan mas lejos que el candidato)
    const near = [];
    for (const it of inst) {
      if (it.body) continue;
      const dx = it.p.x - car.x, dz = it.p.z - car.z, d2 = dx * dx + dz * dz;
      if (d2 < R2) near.push([d2, it]);
    }
    if (near.length) {
      near.sort((a, b) => a[0] - b[0]);
      let n = Math.min(near.length, createPerUpdate);
      let free = maxBodies - bodies.length;
      if (free < n) {
        const far = [];
        bodies.forEach((it, k) => {
          if (it.body.isSleeping()) { const dx = it.p.x - car.x, dz = it.p.z - car.z; far.push([dx * dx + dz * dz, it]); }
        });
        far.sort((a, b) => b[0] - a[0]);
        for (const [d2, it] of far) {
          if (free >= n) break;
          if (d2 <= near[free][0]) break;          // el siguiente candidato no es mas cercano que este cuerpo
          removeBody(it, bodies.indexOf(it)); stats.removed++; free++;
        }
        n = Math.min(n, free);
      }
      for (let k = 0; k < n; k++) { createBody(near[k][1]); stats.created++; }
    }
    stats.awake = awake;
    flush();
  }

  /** Despierta un prop concreto (indice de instancia) con un impulso, p. ej. para explosiones. */
  function impulse(index, v) {
    const it = inst[index];
    if (!it.body) createBody(it);
    it.body.applyImpulse(v, true);
  }

  const counts = { ...json.counts, bodies: 0 };
  return {
    update, impulse, group, stats, instances: inst, model: json,
    get bodies() { return bodies.map(it => it.body); },
    get count() { return inst.length; },
    get counts() { counts.bodies = bodies.length; return counts; },
  };
}
