"""Exporta los props dinamicos (dyn_*) de una pista como cuerpos rigidos: modelos + instancias + fisica.

Uso:  python -I export_props.py [pista]        (por defecto arena/arena1/a)
Salida: web/tracks/<nombre>/props.{json,bin}  y  track.noprops.json (copia de track.json sin los props
        fusionados; comparte track.bin). track.json NO se modifica (copia de seguridad: track.json.bak).

Como se colocan los props en el w32 (deducido): cada MESH (1593 en arena1) trae name1 = nombre de
instancia, name2 = clave de dynamic_objects.bed (ej. "racing_rubber_tire", "metal_light"), una matriz
4x4 D3D fila-mayor (rotacion + traslacion, sin escala: se comprobo) y `assoc` -> damageAssoc ->
collidable -> lista de modelos BMOD (LOD 0, 1, 2...). 42 modelos distintos en arena1. Se usa el LOD 0.
La fisica sale de tools/dynobjects.py a traves de name2 (un mismo modelo puede usar distintas claves,
por eso cada instancia lleva su "bed").

props.json:
  materials, textureDirs      como track.json (textura = material_texture)
  beds        {clave: {mass, restitution, inertia, fixed, wakeupVelocity, nitro, price, bonusType, ...}}
  models[]    {name, primitives[{material, position, normal, uv, index}] (local, mundo three.js, ref. a
               props.bin), collider {type:'box', half, center} | {type:'hull', points:[x,y,z,...]},
               bbox{min,max}, inertiaUnit {tipo: [ix,iy,iz]} (inercia principal por kg en ejes locales)}
  instances[] {m: indice de modelo, b: clave de bed, p:[x,y,z], q:[x,y,z,w]}  (three.js, sin escala)
  counts, alsoInTrack: true (track.json aun contiene los props fusionados)
Coordenadas: igual que export_track.py (z negada, triangulos invertidos); la matriz de instancia se
conjuga con diag(1,1,-1), asi que el origen local del modelo coincide con el origen del cuerpo.
Collider: caja si los 8 vertices de la caja del modelo existen (vallas, cajas, carteles) o casco
convexo = vertices extremos en ~70 direcciones (<= 64 puntos; Rapier construye el casco).
"""
import json
import math
import shutil
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import w32  # noqa: E402
import dynobjects  # noqa: E402
from export_track import (Blob, flip_z, flip_winding, material_texture, SHADER_SHADOW,  # noqa: E402
                          SHADERS_VEG, TRACKS, OUT)

FIXED_MASS = 5000.0   # masa a partir de la cual un prop se considera inamovible (en arena1 ninguno)
MAX_HULL = 64


def directions(n=60):
    d = []
    for i in range(n):  # espiral de Fibonacci
        y = 1 - 2 * (i + 0.5) / n
        r = math.sqrt(max(0.0, 1 - y * y))
        a = i * math.pi * (3 - math.sqrt(5))
        d.append((math.cos(a) * r, y, math.sin(a) * r))
    for k in range(3):
        for s in (1, -1):
            v = [0, 0, 0]
            v[k] = s
            d.append(tuple(v))
    return d


DIRS = directions()


def collider_for(pts):
    """pts: lista de (x,y,z) del modelo (three.js, local). -> (collider, bbox)"""
    lo = [min(p[k] for p in pts) for k in range(3)]
    hi = [max(p[k] for p in pts) for k in range(3)]
    dims = [hi[k] - lo[k] for k in range(3)]
    diag = math.sqrt(sum(d * d for d in dims)) or 1.0
    center = [(lo[k] + hi[k]) / 2 for k in range(3)]
    bbox = {"min": lo, "max": hi}
    # caja si cada esquina del bbox tiene un vertice muy cerca
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    boxlike = all(min(math.dist(c, p) for p in pts) < 0.08 * diag for c in corners)
    if boxlike:
        return {"type": "box", "half": [d / 2 for d in dims], "center": center}, bbox
    chosen = {}
    for d in DIRS:
        best = max(pts, key=lambda p: p[0] * d[0] + p[1] * d[1] + p[2] * d[2])
        chosen[tuple(round(v, 4) for v in best)] = best
    sel = list(chosen.values())[:MAX_HULL]
    flat = [round(v, 4) for p in sel for v in p]
    return {"type": "hull", "points": flat}, bbox


def inertia_unit(kind, dims):
    """Inercia principal por kg (ejes locales) segun el tipo INERTIA_* del bed."""
    a, b, c = dims
    if kind == 3:
        r = (a + b + c) / 6
        return [0.4 * r * r] * 3
    if kind in (2, 4):
        # eje del cilindro: el que deja las otras dos dimensiones mas parecidas
        k = min(range(3), key=lambda i: abs(dims[(i + 1) % 3] - dims[(i + 2) % 3]))
        h = dims[k]
        r = (dims[(k + 1) % 3] + dims[(k + 2) % 3]) / 4
        axis, perp = (r * r / 2, (3 * r * r + h * h) / 12) if kind == 2 else (r * r, r * r / 2 + h * h / 12)
        out = [perp] * 3
        out[k] = axis
        return out
    return [(b * b + c * c) / 12, (a * a + c * c) / 12, (a * a + b * b) / 12]


def quat_from_matrix(m):
    """m[fila][col] rotacion (vector columna). Devuelve (x,y,z,w)."""
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = math.sqrt(t + 1) * 2
        q = ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, s / 4)
    elif m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1 + m[0][0] - m[1][1] - m[2][2]) * 2
        q = (s / 4, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    elif m[1][1] > m[2][2]:
        s = math.sqrt(1 + m[1][1] - m[0][0] - m[2][2]) * 2
        q = ((m[0][1] + m[1][0]) / s, s / 4, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    else:
        s = math.sqrt(1 + m[2][2] - m[0][0] - m[1][1]) * 2
        q = ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, s / 4, (m[1][0] - m[0][1]) / s)
    n = math.sqrt(sum(v * v for v in q))
    return [v / n for v in q]


def instance_transform(M):
    """Matriz D3D fila-mayor del juego -> (posicion, cuaternion) three.js."""
    # columna-mayor: Rg = transpuesta del bloque 3x3; p' = Rg p + t. Conjugar con F=diag(1,1,-1).
    R = [[M[c * 4 + r] for c in range(3)] for r in range(3)]
    F = (1, 1, -1)
    Rt = [[F[i] * R[i][j] * F[j] for j in range(3)] for i in range(3)]
    det = (Rt[0][0] * (Rt[1][1] * Rt[2][2] - Rt[1][2] * Rt[2][1])
           - Rt[0][1] * (Rt[1][0] * Rt[2][2] - Rt[1][2] * Rt[2][0])
           + Rt[0][2] * (Rt[1][0] * Rt[2][1] - Rt[1][1] * Rt[2][0]))
    return [M[12], M[13], -M[14]], quat_from_matrix(Rt), det


def export_props(rel, strip_track=True):
    base = TRACKS / rel
    w = w32.parse(base / "geometry" / "track_geom_w2.w32")
    beds = dynobjects.parse_bed()
    blob = Blob()
    materials = [{"name": m["name"], "shader": m["shader"], "alpha": m["alpha"],
                  "texture": material_texture(m)} for m in w["materials"]]

    def renderable(si):
        mat = w["materials"][w["surfaces"][si]["material"]]
        return mat["shader"] != SHADER_SHADOW and mat["shader"] not in SHADERS_VEG

    # --- instancias y modelos usados ---
    used = {}      # assoc -> indice de modelo
    models, instances, bed_used = [], [], {}
    bed_by_model = {}
    for mesh in w["meshes"]:
        assoc = w["damageAssoc"][mesh["assoc"]]
        col = w["collidables"][assoc["ids"][0]]
        if not col["models"]:
            continue
        key = mesh["assoc"]
        if key not in used:
            used[key] = len(models)
            models.append({"assoc": key, "name": assoc["name"], "model": col["models"][0]})
        p, q, det = instance_transform(mesh["matrix"])
        if det < 0.99:
            print("AVISO: instancia con reflexion/escala", mesh["name1"], det)
        bed = mesh["name2"]
        bed_used[bed] = beds.get(bed)
        bed_by_model.setdefault(used[key], set()).add(bed)
        instances.append({"m": used[key], "b": bed, "p": [round(v, 4) for v in p],
                          "q": [round(v, 6) for v in q]})
    missing = [b for b, v in bed_used.items() if v is None]
    if missing:
        print("AVISO: claves sin entrada en el bed:", missing)

    # --- geometria y collider por modelo ---
    for mi, md in enumerate(models):
        prims, pts = [], []
        for si in w["models"][md["model"]]["surfaces"]:
            if not renderable(si):
                continue
            s = w["surfaces"][si]
            g = w32.surface_geometry(w, s)
            if not g["idx"]:
                continue
            pos = flip_z(g["pos"])
            pts += [tuple(pos[i:i + 3]) for i in range(0, len(pos), 3)]
            p = {"material": s["material"], "position": blob.add("f", pos)}
            p["normal"] = blob.add("f", flip_z(g["nrm"])) if g["nrm"] else None
            if p["normal"] is None:
                del p["normal"]
            if g["uv"]:
                p["uv"] = blob.add("f", g["uv"])
            p["index"] = blob.add("H", flip_winding(g["idx"]))
            prims.append(p)
        if not pts:
            raise SystemExit("modelo sin geometria: " + md["name"])
        pts = list({(round(x, 3), round(y, 3), round(z, 3)) for x, y, z in pts})
        collider, bbox = collider_for(pts)
        dims = [bbox["max"][k] - bbox["min"][k] for k in range(3)]
        kinds = {beds[b]["inertia"] for b in bed_by_model[mi] if beds.get(b)}
        md.update(primitives=prims, collider=collider, bbox=bbox,
                  inertiaUnit={str(k): inertia_unit(k, dims) for k in kinds},
                  vertices=len(pts))
        del md["assoc"], md["model"]

    bed_out = {}
    for b, o in bed_used.items():
        if not o:
            continue
        bed_out[b] = {k: o[k] for k in ("mass", "restitution", "inertia", "inertiaName", "type",
                                        "bonusType", "bonusTypeName", "nitro", "price", "collisionSoundName",
                                        "wakeupVelocity", "reactivationVelocity", "explosionForce",
                                        "destroyFx", "joint", "hinge", "hasJoint")}
        bed_out[b]["fixed"] = bool(o["mass"] >= FIXED_MASS or o["hasJoint"])

    nfixed = sum(1 for i in instances if bed_out.get(i["b"], {}).get("fixed"))
    track_dir = base.parent.parent
    name = Path(rel).parts[-2]
    dst = OUT / name
    info = {"materials": materials, "textureDirs": ["../extracted/data/tracks/%s/textures/" % track_dir.name,
                                                     "../extracted/data/tracks/%s/lighting/" % rel.replace("\\", "/")],
            "beds": bed_out, "models": models, "instances": instances,
            "counts": {"models": len(models), "instances": len(instances), "fixed": nfixed,
                       "dynamic": len(instances) - nfixed},
            "alsoInTrack": True}
    (dst / "props.bin").write_bytes(bytes(blob.buf))
    (dst / "props.json").write_text(json.dumps(info, separators=(",", ":")))
    print(f"{name}: {len(models)} modelos, {len(instances)} instancias ({nfixed} fijas), "
          f"props.bin {len(blob.buf) / 1024:.0f} KB")
    for m in models:
        c = m["collider"]
        print(f"  {m['name']:<34} {c['type']:<5}{len(c.get('points', [])) // 3 or '':<4} v={m['vertices']}")

    # --- track.noprops.json: track.json sin primitivas "dynamic" (track.json no se toca) ---
    tj = dst / "track.json"
    bak = dst / "track.json.bak"
    if not bak.exists():
        shutil.copy2(tj, bak)
    t = json.loads(bak.read_text())
    n0 = len(t["primitives"])
    t["primitives"] = [p for p in t["primitives"] if not p.get("dynamic")]
    (dst / "track.noprops.json").write_text(json.dumps(t, separators=(",", ":")))
    print(f"track.noprops.json: {n0} -> {len(t['primitives'])} primitivas (track.json intacto)")


def main():
    export_props(sys.argv[1] if len(sys.argv) > 1 else "arena/arena1/a")


if __name__ == "__main__":
    main()
