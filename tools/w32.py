"""Parser de .w32 de pista de FlatOut (version 0x20002, PC / Ultimate Carnage).

Layout deducido de la herramienta open source gaycoderprincess/FlatOutW32BGMTool (MPL-2.0).
Orden de secciones: version, N + (N-1) u32 saltados, materiales (MATC), streams, surfaces,
static batches, tree LODs, tree meshes, matriz de colision, models (BMOD), objects (OBJC),
collidable models, mesh damage assoc, compact meshes (MESH).

Los materiales, streams y surfaces son iguales que en bgm.py (se reutilizan sus Reader y
surface_geometry); aqui solo se anade lo propio de pistas.

Uso:  python w32.py dump <track_geom_w2.w32>
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bgm  # noqa: E402
from bgm import Reader  # noqa: E402


def parse(path):
    r = Reader(open(path, "rb").read())
    version = r.u32()
    if version != 0x20002:
        raise ValueError(f"version w32 no soportada: {version:#x}")
    n = r.u32()
    for _ in range(n - 1):  # valores sin uso tras la version
        r.u32()

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

    # streams: en FOUC el tamano en bytes es siempre count*size (las formulas "en grupos de 8/6"
    # de la herramienta original solo reordenan lecturas); el formato 25 son 8 bytes por vertice (2*count u32), el 26 no aparece
    vbufs, ibufs = {}, {}
    for i in range(r.u32()):
        kind = r.u32()
        if kind in (1, 3):
            extra, count, size = r.u32(), r.u32(), r.u32()
            flags = r.u32() if kind == 1 else 0
            if extra not in (0, 22, 23, 24, 25, 26):
                raise ValueError(f"formato de vertices FOUC {extra} no soportado")
            vbufs[i] = {"count": count, "size": size, "flags": flags, "veg": kind == 3,
                        "extra": extra, "data": r.raw(count * size)}
        elif kind == 2:
            extra, count = r.u32(), r.u32()
            ibufs[i] = {"count": count, "data": r.raw(count * 2)}
        else:
            raise ValueError(f"tipo de stream desconocido {kind}")

    surfaces = []
    for _ in range(r.u32()):
        s = {}
        s["isVeg"], s["material"], s["vertexCount"], s["flags"], s["polyCount"], s["polyMode"], \
            s["indicesUsed"] = struct.unpack_from("<7I", r.raw(28))
        s["mult"] = r.f32s(4)  # (offset x, y, z, escala)
        n = r.u32()
        if n not in (1, 2):
            raise ValueError(f"numero de streams invalido: {n}")
        s["streams"] = [(r.u32(), r.u32()) for _ in range(n)]
        surfaces.append(s)

    # static batches: (id, surface, id2, centro[3], radio[3])
    batches = []
    for _ in range(r.u32()):
        b = {"id": r.u32(), "surface": r.u32(), "id2": r.u32()}
        b["center"], b["radius"] = r.f32s(3), r.f32s(3)
        batches.append(b)

    # (en 0x20002 no hay tree colors)
    tree_lods = []
    for _ in range(r.u32()):
        tree_lods.append({"pos": r.f32s(3), "scale": r.f32s(2),
                          "values": struct.unpack_from("<2I", r.raw(8))})

    tree_meshes = []
    for _ in range(r.u32()):
        t = {"isBush": r.u32(), "unk": r.u32(), "bvh1": r.u32(), "bvh2": r.u32()}
        t["matrix"] = r.f32s(16)
        t["scale"] = r.f32s(3)
        # trunk/branch: 9 i32; hoja: 4 i32; extra: 3 i32 (sin interpretar)
        t["trunk"] = struct.unpack_from("<9i", r.raw(36))
        t["branch"] = struct.unpack_from("<9i", r.raw(36))
        t["leaf"] = struct.unpack_from("<4i", r.raw(16))
        t["extra"] = struct.unpack_from("<3i", r.raw(12))
        tree_meshes.append(t)

    collision_matrix = r.f32s(16)

    models = []
    for _ in range(r.u32()):
        r.magic(b"BMOD")
        m = {"unk": r.u32(), "name": r.cstr()}
        m["center"] = r.f32s(3)
        m["radius"] = r.f32s(3)
        m["fRadius"] = r.f32s(1)[0]
        m["surfaces"] = [r.u32() for _ in range(r.u32())]
        models.append(m)

    objects = []
    for _ in range(r.u32()):
        r.magic(b"OBJC")
        o = {"name1": r.cstr(), "name2": r.cstr(), "flags": r.u32(), "matrix": r.f32s(16)}
        objects.append(o)

    collidables = []
    for _ in range(r.u32()):
        c = {"models": [r.u32() for _ in range(r.u32())]}
        c["center"], c["radius"] = r.f32s(3), r.f32s(3)
        collidables.append(c)

    damage_assoc = []
    for _ in range(r.u32()):
        damage_assoc.append({"name": r.cstr(), "ids": struct.unpack_from("<2I", r.raw(8))})

    group_count = r.u32()
    meshes = []
    for _ in range(r.u32()):
        r.magic(b"MESH")
        m = {"name1": r.cstr(), "name2": r.cstr(), "flags": r.u32(), "group": r.u32()}
        m["matrix"] = r.f32s(16)
        m["unk1"], m["assoc"] = r.u32(), r.u32()
        meshes.append(m)

    return {"version": version, "materials": materials, "vbufs": vbufs, "ibufs": ibufs,
            "surfaces": surfaces, "batches": batches, "treeLods": tree_lods,
            "treeMeshes": tree_meshes, "collisionMatrix": collision_matrix, "models": models,
            "objects": objects, "collidables": collidables, "damageAssoc": damage_assoc,
            "groupCount": group_count, "meshes": meshes, "fouc": True,
            "trailing": len(r.d) - r.p}


def surface_geometry(w, s):
    """Igual que bgm.surface_geometry (los dicts de w32 tienen las mismas claves)."""
    return bgm.surface_geometry(w, s)


def parse_startpoints(path):
    """startpoints.bed es texto estilo Lua: Position = { x, y, z } y Orientation x/y/z (filas).
    Devuelve [(pos, (ejeX, ejeY, ejeZ))] en coordenadas del juego."""
    import re
    txt = Path(path).read_text(errors="replace")
    out = []
    for blk in re.split(r"\n\t\[\d+\] = \{", txt)[1:]:
        pos = re.search(r"Position = \{([^}]*)\}", blk)
        axes = [re.search(r'\["%s"\]=\{([^}]*)\}' % a, blk) for a in "xyz"]
        if not pos or not all(axes):
            continue
        f = lambda m: [float(v) for v in m.group(1).split(",")]
        out.append((f(pos), tuple(f(a) for a in axes)))
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "dump":
        print(__doc__)
        sys.exit(1)
    w = parse(sys.argv[2])
    print("trailing:", w["trailing"])
    print("materiales:", len(w["materials"]), "streams v/i:", len(w["vbufs"]), len(w["ibufs"]))
    print("surfaces:", len(w["surfaces"]), "batches:", len(w["batches"]),
          "treeLods:", len(w["treeLods"]), "treeMeshes:", len(w["treeMeshes"]))
    print("models:", len(w["models"]), "objects:", len(w["objects"]),
          "collidables:", len(w["collidables"]), "assoc:", len(w["damageAssoc"]),
          "meshes:", len(w["meshes"]), "groups:", w["groupCount"])
