"""Exporta un coche de FlatOut UC a formato web (JSON + BIN + texturas DDS).

Uso:  python export_car.py <N>      (por ejemplo 1 -> extracted/data/cars/car_1)
      python export_car.py all      (todos los coches)
Salida: web/cars/car_N/{body,tire}.{json,bin}  (las texturas se leen de extracted/)

Coordenadas: el juego usa sistema zurdo (+z adelante). Se convierte a three.js (diestro)
negando z en posiciones/normales/matrices e invirtiendo el orden de los triangulos.
"""
import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bgm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CARS = ROOT / "extracted" / "data" / "cars"
OUT = ROOT / "web" / "cars"

SHADER_SHADOW = 13  # sombra proyectada falsa: no se dibuja


def convert_matrix(m):
    """Fila-mayor D3D (vector fila) zurdo -> Matrix4.elements de three.js (columna-mayor) diestro."""
    # Las filas D3D (ejes de base + traslacion) son las columnas de three.js, asi que el
    # orden de los 16 floats se conserva; solo hay que reflejar z en ambos lados (s_i * s_j).
    s = (1, 1, -1, 1)
    return [m[i * 4 + j] * s[i] * s[j] for i in range(4) for j in range(4)]


class Blob:
    def __init__(self):
        self.buf = bytearray()

    def add(self, fmt, values):
        while len(self.buf) % 4:
            self.buf.append(0)
        off = len(self.buf)
        self.buf += struct.pack("<%d%s" % (len(values), fmt), *values)
        return {"offset": off, "count": len(values)}


def export_bgm(src, dst_dir, name):
    b = bgm.parse(src)
    blob = Blob()
    materials = []
    for m in b["materials"]:
        tex = m["textures"][0].rsplit(".", 1)[0] + ".dds" if m["textures"][0] else None
        materials.append({"name": m["name"], "shader": m["shader"], "alpha": m["alpha"],
                          "texture": tex, "diffuse": m["diffuse"], "ambient": m["ambient"],
                          "specular": m["specular"], "power": m["power"]})

    nodes = []
    for mesh in b["meshes"]:
        prims = []
        for mi in mesh["models"]:
            for si in b["models"][mi]["surfaces"]:
                s = b["surfaces"][si]
                if b["materials"][s["material"]]["shader"] == SHADER_SHADOW:
                    continue
                g = bgm.surface_geometry(b, s)
                pos = g["pos"][:]
                pos[2::3] = [-z for z in pos[2::3]]
                p = {"material": s["material"], "surface": si,
                     "position": blob.add("f", pos)}
                if g["nrm"]:
                    nrm = g["nrm"][:]
                    nrm[2::3] = [-z for z in nrm[2::3]]
                    p["normal"] = blob.add("f", nrm)
                if g["uv"]:
                    p["uv"] = blob.add("f", g["uv"])
                if g["col"]:
                    p["color"] = blob.add("B", list(g["col"]))
                idx = g["idx"][:]
                for k in range(0, len(idx) - 2, 3):  # invertir giro por el reflejo en z
                    idx[k + 1], idx[k + 2] = idx[k + 2], idx[k + 1]
                p["index"] = blob.add("H", idx)
                prims.append(p)
        nodes.append({"name": mesh["name1"], "matrix": convert_matrix(mesh["matrix"]),
                      "primitives": prims})

    objects = [{"name": o["name1"], "matrix": convert_matrix(o["matrix"])} for o in b["objects"]]

    dst_dir.mkdir(parents=True, exist_ok=True)
    (dst_dir / f"{name}.bin").write_bytes(bytes(blob.buf))
    (dst_dir / f"{name}.json").write_text(
        json.dumps({"materials": materials, "nodes": nodes, "objects": objects}, separators=(",", ":")))
    return len(blob.buf), sum(len(n["primitives"]) for n in nodes)


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "all":
        cars = sorted(p.name for p in CARS.glob("car_*") if (p / "body.bgm").exists())
    else:
        cars = [f"car_{int(sys.argv[1])}"]
    for car in cars:
        src, dst = CARS / car, OUT / car
        for name in ("body", "tire"):
            try:
                size, prims = export_bgm(src / f"{name}.bgm", dst, name)
                print(f"{car}/{name}: {prims} primitivas, {size / 1024:.0f} KB")
            except Exception as ex:  # un coche roto no debe frenar al resto
                print(f"ERROR {car}/{name}: {ex}")
    # las texturas no se copian: el visor las lee de extracted/data/cars/<car> y de cars/shared


if __name__ == "__main__":
    main()
