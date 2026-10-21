"""Sustituye la colision de web/tracks/<pista>/track.json por la malla REAL de track_cdb2.gen.

Uso:  python export_collision.py [pista]        (por defecto arena/arena1/a)
      python export_collision.py [pista] --verify   (ademas lanza las comprobaciones y las imprime)

Lee extracted/data/tracks/<pista>/geometry/track_cdb2.gen (parser en cdb2.py) y
data/global/dynamics/surfaces.bed. Modifica SOLO track.json y track.bin de la pista, sin tocar las
primitivas ni los offsets existentes (los blobs nuevos se anaden al final de track.bin):
  * "collisionFallback": la colision antigua (generada desde la geometria de render), intacta.
  * "collision": malla del cdb2. Mismas claves de siempre (position / index / indexType uint32) mas
      "triMaterial" {offset,count}: uint8 por triangulo = indice en "materials"
      "triFlags"    {offset,count}: uint16 por triangulo = palabra de flags del cdb2 (F >> 8),
                    sin significado documentado; se conserva tal cual (en arena1 el bit 256
                    aparece solo en el terreno/suelo)
      "triMask"     {offset,count}: uint8 por triangulo = mascara de consulta de la hoja (4 bits).
                    Observado en arena1: 11 (0b1011) en suelo y carretera, 3 (0b0011) en muros
                    y objetos; el bit 8 parece marcar "suelo conducible" (deduccion, no verificada)
      "materials": [{"id","surface","name","dynamics","grip","solid","body","ray","camera",
                     "reset","friction"}]   (solo las usadas; id == indice == valor de triMaterial;
                     "surface" es el ID 1..50 de surfaces.bed)
  * "splits": puntos de control de vuelta de data/splitpoints.bed.
Es idempotente: si ya hay collisionFallback, descarta la colision cdb2 previa y la regenera.

Coordenadas: igual que export_track.py (juego zurdo, Y arriba -> three.js: z negada y giro de
triangulos invertido).
"""
import json
import math
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import cdb2  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GAME = ROOT / "extracted" / "data"
OUT = ROOT / "web" / "tracks"

# Agarre relativo (1.0 = asfalto). surfaces.bed da Friction=0.7 casi en todas las filas, asi que no
# discrimina; la diferencia real del juego esta en el modelo de neumatico ("Dynamics"), que no se
# ha decodificado. Valores estimados por nombre, segun la guia del proyecto:
# asfalto/cemento 1.0, hard .95, medium .85, soft .7, hielo/aceite .25, nieve .45, terreno blando
# .55-.65, objetos duros .9, agua .4.
GRIP_BY_KEYWORD = [  # (subcadena en minusculas, grip); gana la primera que coincida
    ("oil", 0.25), ("curling", 0.25), ("bowling", 0.25), ("ice", 0.25),
    ("snow", 0.45),
    ("water", 0.4), ("river", 0.4), ("puddle", 0.4),
    ("derby gravel", 0.8), ("dirt", 0.8),
    ("soft", 0.7), ("medium", 0.85), ("hard", 0.95),
    ("tarmac", 1.0), ("asphalt", 1.0), ("cement", 1.0), ("curb", 0.95), ("bridge", 0.95),
    ("conveyer", 1.0), ("bouncer", 1.0),
    ("bank sand", 0.6), ("sand", 0.55), ("grass", 0.6), ("forest", 0.6), ("mould", 0.6),
    ("field", 0.6), ("wet", 0.5), ("rock (terrain)", 0.65),
    ("concrete", 0.9), ("rock", 0.9), ("metal", 0.9), ("wood", 0.9), ("tree", 0.9),
    ("rubber", 0.9), ("bush", 0.9), ("no camera", 0.9),
]
NON_DRIVABLE = ("nocollision", "camera only", "reset")  # grip 0 y solid false


def grip_of(name):
    n = name.lower()
    if any(n.startswith(k) for k in NON_DRIVABLE):
        return 0.0
    for key, g in GRIP_BY_KEYWORD:
        if key in n:
            return g
    return 0.7


class Blob:
    """Anade datos al final de un track.bin existente (alineado a 4 bytes)."""

    def __init__(self, buf):
        self.buf = bytearray(buf)

    def add(self, fmt, values):
        while len(self.buf) % 4:
            self.buf.append(0)
        off = len(self.buf)
        self.buf += struct.pack("<%d%s" % (len(values), fmt), *values)
        return {"offset": off, "count": len(values)}


def parse_splitpoints(path):
    """splitpoints.bed (texto estilo Lua): Splitpoints = { [n] = { Position/Left/Right = {x,y,z} } }."""
    txt = path.read_text(encoding="cp1252")
    out = []
    for m in re.finditer(r"\[(\d+)\]\s*=\s*\{(.*?)\}\s*,?\s*\}", txt, re.S):
        body = m.group(2)
        vec = {}
        for k in ("Position", "Left", "Right"):
            r = re.search(k + r"\s*=\s*\{\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)\s*,\s*([-\d.eE+]+)",
                          body)
            if r:
                vec[k] = [float(r.group(i)) for i in (1, 2, 3)]
        if len(vec) == 3:
            out.append((int(m.group(1)), vec))
    return sorted(out)


def build(rel="arena/arena1/a", verify=False):
    base = GAME / "tracks" / rel
    name = Path(rel).parts[-2]
    dst = OUT / name
    tj = json.loads((dst / "track.json").read_text())
    buf = (dst / "track.bin").read_bytes()

    # --- si ya hay una colision cdb2 previa: recupera el fallback y recorta sus blobs del final ---
    if "collisionFallback" in tj:
        old = tj["collision"]
        cut = min(v["offset"] for v in old.values() if isinstance(v, dict) and "offset" in v)
        buf = buf[:cut]
        tj["collision"] = tj["collisionFallback"]
    tj["collisionFallback"] = tj["collision"]
    blob = Blob(buf)

    # --- cdb2 ---
    g = cdb2.parse(base / "geometry" / "track_cdb2.gen")
    surfaces = cdb2.parse_surfaces(GAME / "global" / "dynamics" / "surfaces.bed")
    tris, verts = g["tris"], g["verts"]
    print("cdb2: %d triangulos unicos, %d vertices, bytes de tris sin leer: %d" %
          (len(tris), len(verts), g["tri_bytes"] - g["tri_used"]))

    remap, pos = {}, []
    for i in verts:  # orden de primera aparicion
        remap[i] = len(pos) // 3
        x, y, z = verts[i]
        pos += (x, y, -z)  # z negada (three.js)
    idx, tmat, tflags, tmask = [], [], [], []
    used = sorted({t[3] for t in tris})
    mindex = {sid: k for k, sid in enumerate(used)}
    for i0, i1, i2, sid, flags, mask in tris:
        idx += (remap[i0], remap[i2], remap[i1])  # giro invertido
        tmat.append(mindex[sid])
        tflags.append(flags)
        tmask.append(mask)

    materials = []
    for sid in used:
        s = surfaces.get(sid, {"name": "surface %d" % sid, "dynamics": "", "friction": 0.7,
                               "reset": 0, "body": True, "camera": True})
        grip = grip_of(s["name"])
        materials.append({"id": mindex[sid], "surface": sid, "name": s["name"],
                          "dynamics": s["dynamics"], "grip": grip,
                          "solid": grip > 0 and (s["body"] or "water" in s["name"].lower()
                                                 or "river" in s["name"].lower()
                                                 or "puddle" in s["name"].lower()),
                          "body": s["body"], "camera": s["camera"], "reset": s["reset"],
                          "friction": s["friction"]})

    coll = {"position": blob.add("f", pos), "index": blob.add("I", idx), "indexType": "uint32",
            "triMaterial": blob.add("B", tmat), "triFlags": blob.add("H", tflags),
            "triMask": blob.add("B", tmask), "materials": materials, "source": "track_cdb2.gen"}
    tj["collision"] = coll

    # --- splits ---
    sp = base / "data" / "splitpoints.bed"
    splits = []
    if sp.exists():
        ground = Grid(pos, idx)
        for n, v in parse_splitpoints(sp):
            p, l, r = v["Position"], v["Left"], v["Right"]
            pts = []
            for q in (p, l, r):  # el .bed trae y=0: se sustituye por la altura del suelo cdb2
                h = [y for y, t in ground.down(q[0], -q[2]) if materials[tmat[t]]["solid"]]
                pts.append([q[0], h[0] if h else q[1], -q[2]])
            splits.append({"index": n - 1, "position": pts[0], "left": pts[1], "right": pts[2],
                           "radius": math.dist(l, r) / 2, "groundFound": all(
                               ground.down(q[0], -q[2]) for q in (p, l, r))})
    tj["splits"] = splits

    # --- resetmap: 256x256 nibbles (4 bits, nibble bajo primero), fila 0 = z de juego +372.
    # 8 = pista (reset valido), 9 = fuera. Rango en resetmap.bed (ResetMapTopLeft/BottomRight).
    rm = base / "data" / "resetmap.4b"
    if rm.exists():
        raw = rm.read_bytes()
        cells = [v for b in raw for v in (b & 15, b >> 4)]
        side = int(math.isqrt(len(cells)))
        tl = [float(v) for v in re.findall(r"TopLeft\s*=\s*\{\s*([-\d.]+)\s*,\s*([-\d.]+)",
                                         (base / "data" / "resetmap.bed").read_text())[0]]
        br = [float(v) for v in re.findall(r"BottomRight\s*=\s*\{\s*([-\d.]+)\s*,\s*([-\d.]+)",
                                         (base / "data" / "resetmap.bed").read_text())[0]]
        # x de juego = tl.x + (cx+.5)/side*(br.x-tl.x); z de juego = tl.y + (cy+.5)/side*(br.y-tl.y)
        tj["resetmap"] = {"size": side, "topLeft": tl, "bottomRight": br,
                          "cells": blob.add("B", [1 if v == 8 else 0 for v in cells]),
                          "note": "1=pista; z three.js = -z de juego"}

    (dst / "track.bin").write_bytes(bytes(blob.buf))
    (dst / "track.json").write_text(json.dumps(tj, separators=(",", ":")))
    kb = (len(pos) * 4 + len(idx) * 4 + len(tmat) + len(tflags) * 2) / 1024
    print("%s: collision cdb2 %d triangulos, %d vertices, ~%.0f KB nuevos (track.bin %.0f KB)" %
          (name, len(tris), len(verts), kb, len(blob.buf) / 1024))
    if verify:
        run_verify(tj, bytes(blob.buf), g, surfaces)
    return tj


# ---------------------------------------------------------------- verificacion

def read_blob(buf, ref, fmt):
    return struct.unpack_from("<%d%s" % (ref["count"], fmt), buf, ref["offset"])


class Grid:
    """Rejilla en XZ para lanzar rayos verticales contra una malla (three.js, Y arriba)."""

    def __init__(self, pos, idx, cell=8.0):
        self.pos, self.idx, self.cell = pos, idx, cell
        self.cells = {}
        for t in range(len(idx) // 3):
            xs = [pos[3 * idx[3 * t + k]] for k in range(3)]
            zs = [pos[3 * idx[3 * t + k] + 2] for k in range(3)]
            for cx in range(int(min(xs) // cell), int(max(xs) // cell) + 1):
                for cz in range(int(min(zs) // cell), int(max(zs) // cell) + 1):
                    self.cells.setdefault((cx, cz), []).append(t)

    def down(self, x, z):
        """Hits (y, tri) de un rayo vertical en (x, z), de mayor a menor altura."""
        p, ix = self.pos, self.idx
        hits = []
        for t in self.cells.get((int(x // self.cell), int(z // self.cell)), ()):
            a, b, c = (ix[3 * t + k] * 3 for k in range(3))
            ax, ay, az = p[a], p[a + 1], p[a + 2]
            # coordenadas baricentricas en XZ
            d = (p[b + 2] - p[c + 2]) * (ax - p[c]) + (p[c] - p[b]) * (az - p[c + 2])
            if abs(d) < 1e-12:
                continue
            u = ((p[b + 2] - p[c + 2]) * (x - p[c]) + (p[c] - p[b]) * (z - p[c + 2])) / d
            v = ((p[c + 2] - az) * (x - p[c]) + (ax - p[c]) * (z - p[c + 2])) / d
            w = 1 - u - v
            if u >= -1e-6 and v >= -1e-6 and w >= -1e-6:
                hits.append((u * ay + v * p[b + 1] + w * p[c + 1], t))
        return sorted(hits, reverse=True)


def run_verify(tj, buf, g, surfaces):
    c = tj["collision"]
    pos = read_blob(buf, c["position"], "f")
    idx = read_blob(buf, c["index"], "I")
    tmat = read_blob(buf, c["triMaterial"], "B")
    nv, nt = len(pos) // 3, len(idx) // 3
    print("\n== verificacion ==")
    # (1) bytes sobrantes de la zona de triangulos
    print("(1) zona de tris %d B, leidos hasta %d (el byte 0 es relleno: las hojas empiezan en 1)" %
          (g["tri_bytes"], g["tri_used"]))
    # coherencia con la cabecera: extremos de los escalares referenciados
    sc = g["scal"]
    ref = sorted({i for t in g["tris"] for i in t[:3]})
    ext = [(min(sc[i + k] for i in ref), max(sc[i + k] for i in ref)) for k in range(3)]
    print("    min/max cabecera", g["min"], g["max"], "| extremos referenciados",
          [e[0] for e in ext], [e[1] for e in ext])
    # (2) rangos y degenerados
    bad = sum(1 for i in idx if i >= nv)
    deg = 0
    for t in range(nt):
        a, b, cc = (idx[3 * t + k] * 3 for k in range(3))
        ux, uy, uz = (pos[b + k] - pos[a + k] for k in range(3))
        vx, vy, vz = (pos[cc + k] - pos[a + k] for k in range(3))
        cr = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
        if math.sqrt(cr[0] ** 2 + cr[1] ** 2 + cr[2] ** 2) < 1e-6:
            deg += 1
    oob = sum(1 for tr in g["tris"] if max(tr[:3]) + 2 >= g["nscalar"])
    print("(2) indices fuera de rango: %d, triangulos con escalares fuera de tabla: %d, "
          "degenerados (area~0): %d de %d" % (bad, oob, deg, nt))
    # (3) histograma
    hist = {}
    for m in tmat:
        hist[m] = hist.get(m, 0) + 1
    print("(3) triangulos por material:")
    for m, n in sorted(hist.items(), key=lambda kv: -kv[1]):
        mm = c["materials"][m]
        print("    %6d  %5.1f%%  [%2d] %-22s dyn=%-11s grip=%.2f solid=%s" %
              (n, 100.0 * n / nt, mm["surface"], mm["name"], mm["dynamics"], mm["grip"], mm["solid"]))
    # (4) bbox y rayo desde los start points
    lo = [min(pos[k::3]) for k in range(3)]
    hi = [max(pos[k::3]) for k in range(3)]
    rb = tj["bbox"]
    print("(4) bbox colision", [round(v, 1) for v in lo + hi])
    print("    bbox render   ", [round(v, 1) for v in rb])
    old = tj["collisionFallback"]
    fb_pos = read_blob(buf, old["position"], "f")
    fb_idx = read_blob(buf, old["index"], "I")
    print("    triangulos: cdb2 %d vs fallback (render) %d" % (nt, len(fb_idx) // 3))
    grid = Grid(pos, idx)
    for s in tj["start"][:8]:
        x, y, z = s["position"]
        hits = grid.down(x, z)
        txt = ["y=%.2f %s" % (h[0], c["materials"][tmat[h[1]]]["name"]) for h in hits[:3]]
        print("    start (%.1f, %.2f, %.1f) ->" % (x, y, z), txt or "SIN IMPACTO")
    # cobertura: centroides del fallback con un hit del cdb2 a menos de 1 m de altura
    step = max(1, (len(fb_idx) // 3) // 1500)
    ok = tot = 0
    for t in range(0, len(fb_idx) // 3, step):
        a, b, cc = (fb_idx[3 * t + k] * 3 for k in range(3))
        cx = (fb_pos[a] + fb_pos[b] + fb_pos[cc]) / 3
        cy = (fb_pos[a + 1] + fb_pos[b + 1] + fb_pos[cc + 1]) / 3
        cz = (fb_pos[a + 2] + fb_pos[b + 2] + fb_pos[cc + 2]) / 3
        tot += 1
        if any(abs(h[0] - cy) < 1.0 for h in grid.down(cx, cz)):
            ok += 1
    print("    cobertura: %d de %d centroides del render tienen colision cdb2 a <1 m (%.0f%%)" %
          (ok, tot, 100.0 * ok / tot))
    # splits
    print("    splits:", len(tj["splits"]))
    for s in tj["splits"][:3]:
        h = grid.down(s["position"][0], s["position"][2])
        print("      #%d pos %s r=%.1f -> suelo y=%s" % (
            s["index"], [round(v, 1) for v in s["position"]], s["radius"],
            round(h[0][0], 2) if h else None))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    build(args[0] if args else "arena/arena1/a", verify="--verify" in sys.argv)


if __name__ == "__main__":
    main()
