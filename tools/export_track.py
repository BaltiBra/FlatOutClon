"""Exporta una pista de FlatOut UC a formato web (JSON + BIN).

Uso:  python export_track.py [pista]     (por defecto arena/arena1/a)
Salida: web/tracks/<nombre>/track.{json,bin}   (las texturas se leen de extracted/)

Contenido:
  * primitives: una por surface de static batch (geometria del mundo) y una por material para
    los props (modelos dyn_* colocados por las MESH del w32, con la matriz ya aplicada).
    Se omiten: shader 13 (sombras falsas) y la vegetacion (shaders 19/20/21: arboles, sprites,
    LOD de arbustos), que se dibuja con tree meshes/billboards especiales no soportados aqui.
  * collision: aqui se genera solo el FALLBACK (geometria de render opaca de los static batches:
    suelo, muros, edificios, sin props moviles, alpha/decals ni vegetacion). Al final se llama a
    export_collision.build(), que lo mueve a "collisionFallback" y lo sustituye por la malla real
    de track_cdb2.gen con material por triangulo (ver export_collision.py), y anade "splits".
    Los indices de collision son uint32; los de las primitivas uint16.
  * start: de startpoints.bed.

Coordenadas: igual que export_car.py (juego zurdo +z adelante -> three.js diestro: z negada
y giro de triangulos invertido).
"""
import json
import math
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import w32  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TRACKS = ROOT / "extracted" / "data" / "tracks"
OUT = ROOT / "web" / "tracks"

SHADER_SHADOW = 13
SHADERS_VEG = (19, 20, 21)


class Blob:
    def __init__(self):
        self.buf = bytearray()

    def add(self, fmt, values):
        while len(self.buf) % 4:
            self.buf.append(0)
        off = len(self.buf)
        self.buf += struct.pack("<%d%s" % (len(values), fmt), *values)
        return {"offset": off, "count": len(values)}


def flip_z(v):
    v = list(v)
    v[2::3] = [-z for z in v[2::3]]
    return v


def flip_winding(idx):
    idx = list(idx)
    for k in range(0, len(idx) - 2, 3):
        idx[k + 1], idx[k + 2] = idx[k + 2], idx[k + 1]
    return idx


def apply_matrix(m, pos, nrm):
    """Matriz D3D fila-mayor (vector fila): p' = p*M. Devuelve listas nuevas (coords del juego)."""
    p2, n2 = [], []
    for i in range(0, len(pos), 3):
        x, y, z = pos[i:i + 3]
        p2 += (x * m[0] + y * m[4] + z * m[8] + m[12],
               x * m[1] + y * m[5] + z * m[9] + m[13],
               x * m[2] + y * m[6] + z * m[10] + m[14])
    for i in range(0, len(nrm), 3):
        x, y, z = nrm[i:i + 3]
        n2 += (x * m[0] + y * m[4] + z * m[8], x * m[1] + y * m[5] + z * m[9],
               x * m[2] + y * m[6] + z * m[10])
    return p2, n2


def convert_matrix(m):
    """Fila-mayor D3D zurdo -> Matrix4.elements de three.js diestro (igual que export_car.py)."""
    s = (1, 1, -1, 1)
    return [m[i * 4 + j] * s[i] * s[j] for i in range(4) for j in range(4)]


def material_texture(m):
    """Textura difusa: la 0, salvo shader 2 (terreno/carretera) cuya 0 es el colormap generico."""
    names = m["textures"]
    t = names[1] if m["shader"] == 2 and names[1] else names[0]
    return t.rsplit(".", 1)[0].lower() + ".dds" if t else None


def export_track(rel):
    base = TRACKS / rel
    w = w32.parse(base / "geometry" / "track_geom_w2.w32")
    print("trailing:", w["trailing"])
    blob = Blob()

    materials = []
    for m in w["materials"]:
        materials.append({"name": m["name"], "shader": m["shader"], "alpha": m["alpha"],
                          "texture": material_texture(m)})

    def renderable(si):
        mat = w["materials"][w["surfaces"][si]["material"]]
        return mat["shader"] != SHADER_SHADOW and mat["shader"] not in SHADERS_VEG

    bbox = [math.inf] * 3 + [-math.inf] * 3
    prims, coll_pos, coll_idx = [], [], []
    nbad = 0

    def grow(pos):
        for k in range(3):
            v = pos[k::3]
            bbox[k] = min(bbox[k], min(v))
            bbox[k + 3] = max(bbox[k + 3], max(v))

    # --- static batches ---
    for b in w["batches"]:
        si = b["surface"]
        if si >= len(w["surfaces"]) or not renderable(si):
            continue
        s = w["surfaces"][si]
        g = w32.surface_geometry(w, s)
        if not g["idx"]:
            continue
        pos = flip_z(g["pos"])
        grow(pos)
        p = {"material": s["material"], "surface": si, "position": blob.add("f", pos)}
        if g["nrm"]:
            p["normal"] = blob.add("f", flip_z(g["nrm"]))
        if g["uv"]:
            p["uv"] = blob.add("f", g["uv"])
        if g["uv2"]:
            p["uv2"] = blob.add("f", g["uv2"])
        idx = flip_winding(g["idx"])
        p["index"] = blob.add("H", idx)
        prims.append(p)
        mat = w["materials"][s["material"]]
        if not mat["alpha"]:
            base_v = len(coll_pos) // 3
            coll_pos += pos
            coll_idx += [i + base_v for i in idx]
    nstatic = len(prims)

    # --- props: MESH -> assoc -> collidable -> modelo (LOD 0) -> surfaces, matriz aplicada ---
    props = {}  # material -> acumulador
    for mesh in w["meshes"]:
        assoc = w["damageAssoc"][mesh["assoc"]]
        col = w["collidables"][assoc["ids"][0]]
        if not col["models"]:
            continue
        for si in w["models"][col["models"][0]]["surfaces"]:
            if not renderable(si):
                continue
            s = w["surfaces"][si]
            g = w32.surface_geometry(w, s)
            pos, nrm = apply_matrix(mesh["matrix"], g["pos"], g["nrm"])
            chunks = props.setdefault(s["material"], [])
            if not chunks or len(chunks[-1]["pos"]) // 3 + len(pos) // 3 > 65000:
                chunks.append({"pos": [], "nrm": [], "uv": [], "uv2": [], "idx": []})
            acc = chunks[-1]  # tope uint16 por primitiva: se trocea por instancias
            base_v = len(acc["pos"]) // 3
            acc["pos"] += pos
            acc["nrm"] += nrm
            acc["uv"] += g["uv"]
            acc["uv2"] += g["uv2"]
            acc["idx"] += [i + base_v for i in g["idx"]]
    nprops = 0
    for mi, acc in [(mi, c) for mi, cs in sorted(props.items()) for c in cs]:
        pos = flip_z(acc["pos"])
        grow(pos)
        p = {"material": mi, "dynamic": True, "position": blob.add("f", pos),
             "normal": blob.add("f", flip_z(acc["nrm"])), "uv": blob.add("f", acc["uv"])}
        if acc["uv2"]:
            p["uv2"] = blob.add("f", acc["uv2"])
        p["index"] = blob.add("H", flip_winding(acc["idx"]))
        prims.append(p)
        nprops += len(acc["idx"]) // 3

    # verificacion: indices en rango (uint16 por primitiva)
    for p in prims:
        nv = p["position"]["count"] // 3
        raw = blob.buf[p["index"]["offset"]:p["index"]["offset"] + p["index"]["count"] * 2]
        if max(struct.unpack("<%dH" % p["index"]["count"], raw)) >= nv:
            nbad += 1
    print("primitivas con indices fuera de rango:", nbad)

    collision = {"position": blob.add("f", coll_pos), "index": blob.add("I", coll_idx),
                 "indexType": "uint32"}

    # --- start points ---
    start = []
    for pos, (ax, ay, az) in w32.parse_startpoints(base / "data" / "startpoints.bed"):
        fwd = [az[0], az[1], -az[2]]  # eje z del juego = adelante, en three.js
        # Object3D.rotation.y = yaw gira un modelo que mira a -z hacia fwd
        start.append({"position": [pos[0], pos[1], -pos[2]],
                      "yaw": math.atan2(-fwd[0], -fwd[2]), "forward": fwd})

    track_dir = base.parent.parent  # .../tracks/arena
    # rutas relativas a web/ (contrato)
    tex_dirs = ["../extracted/data/tracks/%s/textures/" % track_dir.name,
                "../extracted/data/tracks/%s/lighting/" % rel.replace("\\", "/")]

    name = Path(rel).parts[-2]
    dst = OUT / name
    dst.mkdir(parents=True, exist_ok=True)
    (dst / "track.bin").write_bytes(bytes(blob.buf))
    (dst / "track.json").write_text(json.dumps(
        {"materials": materials, "primitives": prims, "collision": collision, "start": start,
         "textureDirs": tex_dirs, "bbox": bbox}, separators=(",", ":")))
    print(f"{name}: {len(prims)} primitivas ({nstatic} static, {len(prims) - nstatic} props), "
          f"collision {len(coll_idx) // 3} triangulos, track.bin {len(blob.buf) / 1024:.0f} KB")
    print("bbox three.js:", [round(v, 1) for v in bbox])
    import export_collision  # sustituye la colision de fallback por la del cdb2
    export_collision.build(rel)
    for s in start[:2]:
        print("start:", [round(v, 2) for v in s["position"]], "yaw", round(s["yaw"], 3))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    rel = args[0] if args else "arena/arena1/a"
    export_track(rel)


if __name__ == "__main__":
    main()
