"""Parser de .bgm de FlatOut (version 0x20000, PC / Ultimate Carnage).

Layout deducido de la herramienta open source gaycoderprincess/FlatOutW32BGMTool (MPL-2.0).
Secciones en orden: materiales (MATC), streams (vertex/index), surfaces, models (BMOD),
meshes (MESH), objects (OBJC).

Uso:  python bgm.py dump <archivo.bgm>
"""
import struct
import sys

VERTEX_POSITION = 0x2
VERTEX_NORMAL = 0x10
VERTEX_COLOR = 0x40
VERTEX_UV = 0x100
VERTEX_UV2 = 0x200
VERTEX_INT16 = 0x2000


class Reader:
    def __init__(self, data):
        self.d = data
        self.p = 0

    def u32(self):
        v = struct.unpack_from("<I", self.d, self.p)[0]
        self.p += 4
        return v

    def f32s(self, n):
        v = struct.unpack_from("<%df" % n, self.d, self.p)
        self.p += 4 * n
        return list(v)

    def raw(self, n):
        v = self.d[self.p:self.p + n]
        if len(v) != n:
            raise ValueError("fin de archivo inesperado")
        self.p += n
        return v

    def cstr(self):
        end = self.d.index(b"\0", self.p)
        s = self.d[self.p:end].decode("latin-1")
        self.p = end + 1
        return s

    def magic(self, expected):
        got = self.raw(4)
        if got != expected:
            raise ValueError(f"magic {got!r} != {expected!r} en {self.p - 4:#x}")


def parse(path):
    r = Reader(open(path, "rb").read())
    version = r.u32()
    if version != 0x20000:
        raise ValueError(f"version bgm no soportada: {version:#x}")

    materials = []
    for _ in range(r.u32()):
        r.magic(b"MATC")
        m = {"name": r.cstr()}
        m["alpha"], m["v92"], m["numTextures"], m["shader"], m["useColormap"], m["v74"] = \
            struct.unpack_from("<6I", r.raw(24))
        m["v108"] = r.f32s(3)
        m["v109"] = r.f32s(3)
        m["ambient"], m["diffuse"], m["specular"], m["emissive"] = (r.f32s(4) for _ in range(4))
        m["power"] = r.f32s(1)[0]
        m["textures"] = [r.cstr() for _ in range(3)]
        materials.append(m)

    vbufs, ibufs = {}, {}
    fouc = False  # PC Ultimate Carnage: vertices int16 empaquetados (formato extra 22/23/24)
    for i in range(r.u32()):
        kind = r.u32()
        if kind in (1, 3):
            extra, count, size = r.u32(), r.u32(), r.u32()
            flags = r.u32() if kind == 1 else 0
            if extra:
                fouc = True
                if extra not in (22, 23, 24):
                    raise ValueError(f"formato de vertices FOUC {extra} no soportado")
            vbufs[i] = {"count": count, "size": size, "flags": flags, "veg": kind == 3,
                        "extra": extra, "data": r.raw(count * size)}
        elif kind == 2:
            extra, count = r.u32(), r.u32()
            if extra:
                fouc = True
            ibufs[i] = {"count": count, "data": r.raw(count * 2)}
        else:
            raise ValueError(f"tipo de stream desconocido {kind}")

    surfaces = []
    for _ in range(r.u32()):
        s = {}
        s["isVeg"], s["material"], s["vertexCount"], s["flags"], s["polyCount"], s["polyMode"], \
            s["indicesUsed"] = struct.unpack_from("<7I", r.raw(28))
        if fouc:
            s["mult"] = r.f32s(4)  # (offset x, y, z, escala)
        n = r.u32()
        if n not in (1, 2):
            raise ValueError(f"numero de streams invalido: {n}")
        s["streams"] = [(r.u32(), r.u32()) for _ in range(n)]  # (id, offset en bytes)
        surfaces.append(s)

    models = []
    for _ in range(r.u32()):
        r.magic(b"BMOD")
        m = {"unk": r.u32(), "name": r.cstr()}
        m["center"] = r.f32s(3)
        m["radius"] = r.f32s(3)
        m["fRadius"] = r.f32s(1)[0]
        m["surfaces"] = [r.u32() for _ in range(r.u32())]
        models.append(m)

    meshes = []
    for _ in range(r.u32()):
        r.magic(b"MESH")
        m = {"name1": r.cstr(), "name2": r.cstr(), "flags": r.u32(), "group": r.u32()}
        m["matrix"] = r.f32s(16)
        m["models"] = [r.u32() for _ in range(r.u32())]
        meshes.append(m)

    objects = []
    for _ in range(r.u32()):
        r.magic(b"OBJC")
        o = {"name1": r.cstr(), "name2": r.cstr(), "flags": r.u32(), "matrix": r.f32s(16)}
        objects.append(o)

    return {"version": version, "fouc": fouc, "materials": materials, "vbufs": vbufs, "ibufs": ibufs,
            "surfaces": surfaces, "models": models, "meshes": meshes, "objects": objects,
            "trailing": len(r.d) - r.p}


def surface_geometry(bgm, s):
    """Devuelve dict con listas planas (coordenadas del juego, sin convertir) para una surface."""
    vid, voff = s["streams"][0]
    vb = bgm["vbufs"][vid]
    size, flags = vb["size"], vb["flags"]
    n = s["vertexCount"]
    d = vb["data"]
    pos, nrm, col, uv, uv2 = [], [], [], [], []
    if flags & VERTEX_INT16:
        # vertice FOUC: pos int16[3], pad u16, tangente u8[4], bitangente u8[4], normal u8[4],
        # [color u8[4]], uv1 i16[2], [uv2 i16[2]]  (las UV son siempre los ultimos 4/8 bytes)
        mx, my, mz, scale = s["mult"]
        uv_off = size - 4 - (4 if flags & VERTEX_UV2 else 0)
        for k in range(n):
            o = voff + k * size
            x, y, z = struct.unpack_from("<3h", d, o)
            pos += ((x + mx) * scale, (y + my) * scale, (z + mz) * scale)
            bz, by, bx = d[o + 16], d[o + 17], d[o + 18]
            nrm += ((bx - 127) / 127, (by - 127) / 127, (bz - 127) / 127)
            if flags & VERTEX_COLOR:
                col += d[o + 20:o + 24]
            u, v = struct.unpack_from("<2h", d, o + uv_off)
            uv += (u / 2048, v / 2048)
            if flags & VERTEX_UV2:
                u, v = struct.unpack_from("<2h", d, o + uv_off + 4)
                uv2 += (u / 2048, v / 2048)
    else:
        for k in range(n):
            o = voff + k * size
            pos += struct.unpack_from("<3f", d, o)
            o += 12
            if flags & VERTEX_NORMAL:
                nrm += struct.unpack_from("<3f", d, o)
                o += 12
            if flags & VERTEX_COLOR:
                col += d[o:o + 4]
                o += 4
            if flags & (VERTEX_UV | VERTEX_UV2):
                uv += struct.unpack_from("<2f", d, o)
                o += 8
            if flags & VERTEX_UV2:
                uv2 += struct.unpack_from("<2f", d, o)
    idx = []
    if len(s["streams"]) == 2:
        iid, ioff = s["streams"][1]
        ib = bgm["ibufs"][iid]
        base = voff // size  # los indices son absolutos dentro del buffer de vertices
        idx = [i - base for i in struct.unpack_from("<%dH" % s["indicesUsed"], ib["data"], ioff)]
        if idx and (min(idx) < 0 or max(idx) >= n):
            raise ValueError(f"indices fuera de rango ({min(idx)}..{max(idx)} de {n} vertices)")
    return {"pos": pos, "nrm": nrm, "col": col, "uv": uv, "uv2": uv2, "idx": idx}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "dump":
        print(__doc__)
        sys.exit(1)
    b = parse(sys.argv[2])
    print("bytes sin leer al final:", b["trailing"])
    print("materiales:")
    for i, m in enumerate(b["materials"]):
        print(f"  [{i}] {m['name']!r} shader={m['shader']} alpha={m['alpha']} tex={m['textures']}")
    print("vbufs:", {k: (v["count"], v["size"], hex(v["flags"])) for k, v in b["vbufs"].items()})
    print("ibufs:", {k: v["count"] for k, v in b["ibufs"].items()})
    print("surfaces:", len(b["surfaces"]))
    print("models:")
    for i, m in enumerate(b["models"]):
        print(f"  [{i}] {m['name']!r} surfaces={m['surfaces']}")
    print("meshes:")
    for i, m in enumerate(b["meshes"]):
        mm = m["matrix"]
        print(f"  [{i}] {m['name1']!r}/{m['name2']!r} flags={m['flags']:#x} group={m['group']} "
              f"models={m['models']} pos=({mm[12]:.2f},{mm[13]:.2f},{mm[14]:.2f})")
    print("objects:")
    for i, o in enumerate(b["objects"]):
        mm = o["matrix"]
        print(f"  [{i}] {o['name1']!r}/{o['name2']!r} flags={o['flags']:#x} "
              f"pos=({mm[12]:.2f},{mm[13]:.2f},{mm[14]:.2f})")
