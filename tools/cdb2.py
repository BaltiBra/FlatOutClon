"""Parser de la base de colision track_cdb2.gen de FlatOut 2 / Ultimate Carnage.

Formato (documentado por la comunidad: xob0t/fo2blender docs/formats/collision.md, y comprobado
aqui contra los datos de arena1/a): cabecera de 64 bytes, arbol espacial de nodos de 8 bytes,
triangulos empaquetados en 6 codificaciones ("kind") y vertices como int16 (x, y, z) cuantizados.
Solo se leen las hojas del arbol: el arbol en si (acelerador de consultas) se ignora.

Cabecera:  0 id 21 76 71 98 | 4 version | 8 min int32[3] | 20 max int32[3] | 32 mult f32[3] |
           44 mult inversos | 56 offset tris (desde el byte 64) | 60 offset vertices (idem).
Hoja (nodo de 8 bytes con low&3 == 3):
  low  >> 2: queryMask(4) | kind(3) | offset de triangulos en bytes (relativo a la zona de tris)
  high:      count(7) | baseFlags(6) | vertexScalarOffset (indice de int16, no de vertice)
Cada triangulo recupera tres indices de escalar int16 (i0,i1,i2) y una palabra F de flags:
  surface = (F & 0x3f) + 1  -> fila de data/global/dynamics/surfaces.bed (1..50)
  flags   = F >> 8          -> bits sin nombre documentado (se conservan tal cual)
Un mismo triangulo aparece en varias hojas (el arbol lo duplica): se deduplica por (i0,i1,i2).

Uso:  python cdb2.py <track_cdb2.gen>     (imprime resumen y verificaciones)
"""
import re
import struct
import sys
from pathlib import Path

ID = b"!vq\x98"
# bytes por triangulo de cada kind (el 0 y el 1 tienen un primer triangulo de 6 bytes)
KIND_FIRST = {0: 6, 1: 6, 2: 5, 3: 4, 4: 5, 5: 3}
KIND_NEXT = {0: 9, 1: 8, 2: 5, 3: 4, 4: 5, 5: 3}


def decode_leaf(kind, count, buf, off, base, bflags):
    """Devuelve [(i0, i1, i2, F)] de una hoja. buf[off:] empieza en su primer triangulo."""
    out = []
    p = off
    for n in range(count):
        if kind in (0, 1) and n == 0:
            b0, b1, b2, b3, b4, b5 = buf[p:p + 6]
            f = bflags | ((b0 & 0x3F) << 8) | ((b0 >> 6) << 16)
            out.append((base, b1 | (b2 << 8) | ((b3 & 0x07) << 16),
                        (b3 >> 3) | (b4 << 5) | (b5 << 13), f))
            p += 6
        elif kind == 0:
            b0, b1, b2, b3, b4, b5, b6, b7, b8 = buf[p:p + 9]
            f = (b1 & 0x3F) | ((b0 & 0x3F) << 8) | ((b0 >> 6) << 16)
            out.append(((b1 >> 7) | (b2 << 1) | (b3 << 9) | ((b4 & 0x03) << 17),
                        (b4 >> 2) | (b5 << 6) | ((b6 & 0x1F) << 14),
                        (b6 >> 5) | (b7 << 3) | (b8 << 11), f))
            p += 9
        elif kind == 1:
            b0, b1, b2, b3, b4, b5, b6, b7 = buf[p:p + 8]
            f = bflags | ((b0 & 0x3F) << 8) | (((b0 >> 6) & 1) << 16)
            out.append(((b0 >> 7) | (b1 << 1) | (b2 << 9) | ((b3 & 0x03) << 17),
                        (b3 >> 2) | (b4 << 6) | ((b5 & 0x1F) << 14),
                        (b5 >> 5) | (b6 << 3) | (b7 << 11), f))
            p += 8
        elif kind == 2:
            b0, b1, b2, b3, b4 = buf[p:p + 5]
            f = (b1 & 0x3F) | ((b0 & 0x3F) << 8) | ((b0 >> 6) << 16)
            out.append((base + b2, base + b3, base + b4, f))
            p += 5
        elif kind == 3:
            b0, b1, b2, b3 = buf[p:p + 4]
            f = bflags | ((b0 & 0x3F) << 8) | ((b0 >> 6) << 16)
            out.append((base + b1, base + b2, base + b3, f))
            p += 4
        elif kind == 4:
            b0, b1, b2, b3, b4 = buf[p:p + 5]
            f = bflags | ((b0 & 0x3F) << 8) | (((b0 >> 6) & 1) << 16)
            out.append((base + ((b0 >> 7) | (b1 << 1) | ((b2 & 0x03) << 9)),
                        base + ((b2 >> 2) | ((b3 & 0x1F) << 6)),
                        base + ((b3 >> 5) | (b4 << 3)), f))
            p += 5
        else:  # kind 5
            b0, b1, b2 = buf[p:p + 3]
            f = bflags | ((b0 & 0x3F) << 8) | ((b0 >> 6) << 16)
            out.append((base + (b1 & 0x1F), base + ((b1 >> 5) | ((b2 & 0x03) << 3)),
                        base + (b2 >> 2), f))
            p += 3
    return out, p


def parse(path):
    """Lee un track_cdb2.gen. Devuelve dict con cabecera, triangulos unicos y vertices.

    tris: lista de (i0, i1, i2, surface, flags, queryMask); i* son indices de escalar int16.
    verts: dict indice_escalar -> (x, y, z) en metros del juego (Y arriba, mano izquierda).
    """
    d = Path(path).read_bytes()
    if d[:4] != ID:
        raise ValueError("id de cdb2 incorrecto")
    mn = struct.unpack_from("<3i", d, 8)
    mx = struct.unpack_from("<3i", d, 20)
    mult = struct.unpack_from("<3f", d, 32)
    off_tri, off_vert = struct.unpack_from("<II", d, 56)
    tree_end, tri_start, vert_start = 64 + off_tri, 64 + off_tri, 64 + off_vert
    if not (64 <= tri_start <= vert_start <= len(d)) or off_tri % 8 or (len(d) - vert_start) % 2:
        raise ValueError("offsets de cdb2 incoherentes")
    nscalar = (len(d) - vert_start) // 2
    scal = struct.unpack_from("<%dh" % nscalar, d, vert_start)
    tri_buf = d[tri_start:vert_start]

    seen, tris = {}, []
    nleaf = nempty = 0
    kinds = {}
    used_end = 0  # fin del ultimo triangulo leido (para detectar bytes sobrantes)
    for p in range(64, tree_end, 8):
        low, high = struct.unpack_from("<II", d, p)
        if low & 3 != 3:
            continue
        nleaf += 1
        mask = (low >> 2) & 0x0F
        kind = (low >> 6) & 0x07
        toff = low >> 9
        count = high & 0x7F
        bflags = (high >> 7) & 0x3F
        base = high >> 13
        if count == 0:
            nempty += 1
            continue
        if kind > 5:
            raise ValueError("kind de hoja desconocido %d" % kind)
        leaf, end = decode_leaf(kind, count, tri_buf, toff, base, bflags)
        used_end = max(used_end, end)
        kinds[kind] = kinds.get(kind, 0) + len(leaf)
        for i0, i1, i2, f in leaf:
            key = (i0, i1, i2)
            if key not in seen:
                seen[key] = len(tris)
                tris.append((i0, i1, i2, (f & 0x3F) + 1, f >> 8, mask))

    verts = {}
    for t in tris:
        for i in t[:3]:
            if i not in verts:
                verts[i] = (scal[i] * mult[0], scal[i + 1] * mult[1], scal[i + 2] * mult[2])
    return {"min": mn, "max": mx, "mult": mult, "tris": tris, "verts": verts, "nscalar": nscalar,
            "leaves": nleaf, "empty": nempty, "kinds": kinds, "scal": scal,
            "tri_bytes": len(tri_buf), "tri_used": used_end, "size": len(d)}


def parse_surfaces(path):
    """data/global/dynamics/surfaces.bed -> {id: {name, dynamics, friction, reset, body, camera}}.

    El archivo es texto estilo Lua en cp1252; solo se extraen los campos que se usan aqui.
    """
    txt = Path(path).read_bytes().decode("cp1252")
    txt = re.sub(r"--[^\n]*", "", txt)
    body = txt[txt.index("Surfaces"):]
    out = {}
    for m in re.finditer(r"\[(\d+)\]\s*=\s*\{(.*?)\n\s*\},", body, re.S):
        t = m.group(2)

        def get(k, cast=str, default=None):
            r = re.search(r"\b" + k + r"\s*=\s*\"?([^,\"\n]+)\"?", t)
            return cast(r.group(1).strip()) if r else default
        out[int(m.group(1))] = {
            "name": get("Name"), "dynamics": get("Dynamics"),
            "friction": get("Friction", float, 0.7), "reset": get("Reset", int, 0),
            "body": get("BodyCollision", lambda s: s == "true", False),
            "camera": get("CameraCollision", lambda s: s == "true", False)}
    return out


def main():
    g = parse(sys.argv[1])
    print("hojas %d (vacias %d), triangulos unicos %d, vertices unicos %d, kinds %s" %
          (g["leaves"], g["empty"], len(g["tris"]), len(g["verts"]), g["kinds"]))
    print("zona de tris: %d bytes, consumidos hasta %d" % (g["tri_bytes"], g["tri_used"]))


if __name__ == "__main__":
    main()
