"""Lector de data/database/game.db (formato PDB de Bugbear, FlatOut Ultimate Carnage).

Formato deducido por analisis de datos (no hay documentacion publica):

  cabecera : "PDB\\x1a", version 0x200, u32 numero de nodos
  nodos    : tabla de N entradas de 20 bytes desde 0x0c; la palabra 5 de cada entrada es un
             puntero relativo (desde el inicio de la entrada) al primer registro del nodo
  registros: [i32 ref][u8 tipo][u8 tam][u16 extra][u32 marca][carga util de 'tam' bytes]
             ref = desplazamiento relativo (desde el propio campo) al nombre de la propiedad
             en la tabla de textos que ocupa el final del archivo
             tipos: 5 bool/int, 6 int, 7 float, 9 vec2, 10 vec3, 11 vec4, 2 cadena,
                    12 enlace a otro nodo (carga util: u16 indice de nodo; la 'marca' no es 0x0a)
  los registros de un nodo van seguidos hasta el inicio del siguiente nodo.

Uso:
    python gamedb.py cars            -> escribe web/cars/car_N/physics.json de todos los coches
    python gamedb.py car <N>         -> imprime los parametros del coche N
"""
import bisect
import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "extracted" / "data" / "database" / "game.db"
OUT = ROOT / "web" / "cars"

TABLE_START = 0x0C
SIZES = {5: 4, 6: 4, 7: 4, 9: 8, 10: 12, 11: 16}
SKIP_LINKS = {"Visuals", "EngineSound", "Upgrades"}   # no aportan a la fisica


class GameDB:
    def __init__(self, path=DB):
        self.d = open(path, "rb").read()
        d = self.d
        if d[:3] != b"PDB":
            raise ValueError("no es un game.db (PDB)")
        self.num_nodes = struct.unpack_from("<I", d, 8)[0]
        self.str_start = d.index(b"null\0root\0")          # inicio de la tabla de textos
        self.recs = self._scan_records()
        self.node_ptr = []
        for k in range(self.num_nodes):
            base = TABLE_START + 20 * k
            rel = struct.unpack_from("<i", d, base + 16)[0]
            self.node_ptr.append(base + rel if rel else None)
        # un nodo termina donde empieza el siguiente nodo (no el siguiente registro)
        self.starts = sorted(p for p in set(self.node_ptr) if p in self.recs)

    # ---- registros ----
    def _name(self, field_pos, ref):
        a = field_pos + ref
        d = self.d
        if not (self.str_start <= a < len(d)) or d[a - 1] != 0:
            return None
        e = d.index(b"\0", a)
        return d[a:e].decode("latin-1") if 0 < e - a < 48 else None

    def _scan_records(self):
        d, recs = self.d, {}
        p = TABLE_START
        while p < self.str_start:
            ref = struct.unpack_from("<i", d, p)[0]
            name = self._name(p, ref)
            total = 0
            if name:
                t, sz = d[p + 4], d[p + 5]
                extra, mark = struct.unpack_from("<HI", d, p + 6)
                if t in SIZES and sz == SIZES[t] and extra == 0 and mark == 0x0A:
                    total = 12 + sz
                elif t == 2 and mark == 0x0A and sz and d[p + 12 + sz - 1] == 0:
                    total = 12 + sz
                elif t == 12 and sz == 2 and extra == 0:
                    total = 14
            if total:
                recs[p] = (name, d[p + 4], d[p + 5], total)
                p += total
            else:
                p += 1
        return recs

    def _value(self, pos):
        name, t, sz, _ = self.recs[pos]
        d = self.d
        if t == 2:
            return d[pos + 12:pos + 12 + sz - 1].decode("latin-1")
        if t == 12:
            return ("link", struct.unpack_from("<H", d, pos + 12)[0])
        if t in (5, 6):
            return struct.unpack_from("<i", d, pos + 12)[0]
        vals = [round(v, 6) for v in struct.unpack_from("<%df" % (sz // 4), d, pos + 12)]
        return vals[0] if t == 7 else vals

    def node(self, k, depth=0, seen=()):
        """Propiedades del nodo k como dict; los enlaces se resuelven recursivamente."""
        p = self.node_ptr[k]
        if p not in self.recs:
            return {}
        i = bisect.bisect_right(self.starts, p)
        end = self.starts[i] if i < len(self.starts) else self.str_start
        out = {}
        while p < end and p in self.recs:
            name = self.recs[p][0]
            v = self._value(p)
            if isinstance(v, tuple):
                if name in SKIP_LINKS or depth >= 3 or v[1] in seen or v[1] >= self.num_nodes:
                    p += self.recs[p][3]
                    continue
                v = self.node(v[1], depth + 1, seen + (k,))
            out[name] = v
            p += self.recs[p][3]
        return out

    def cars(self):
        """{numero_de_coche: dict de parametros} para todos los nodos con DataPath data/Cars/Car_N/."""
        cars = {}
        for k in range(self.num_nodes):
            p = self.node_ptr[k]
            if p not in self.recs:
                continue
            props = self.node(k)
            dp = props.get("DataPath")
            if isinstance(dp, str) and dp.startswith("data/Cars/Car_") and "Engine" in props:
                cars[int(dp.rstrip("/").rsplit("_", 1)[1])] = props
        return cars


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("cars", "car"):
        print(__doc__)
        sys.exit(1)
    db = GameDB()
    cars = db.cars()
    if sys.argv[1] == "car":
        print(json.dumps(cars[int(sys.argv[2])], indent=1))
        return
    for n, props in sorted(cars.items()):
        dst = OUT / f"car_{n}"
        dst.mkdir(parents=True, exist_ok=True)
        (dst / "physics.json").write_text(json.dumps(props, separators=(",", ":")))
    print(f"{len(db.recs)} registros, {db.num_nodes} nodos, {len(cars)} coches exportados")


if __name__ == "__main__":
    main()
