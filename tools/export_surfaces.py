"""Exporta a JSON los datos reales de superficies y dinamicas de neumatico del juego.

  web/data/tire_dynamics.json  <- game.db, nodo "TireDynamics": un grupo de parametros por tipo de
                                  suelo (Tarmac, Gravel, Sand, Ice...). 11 nodos consecutivos con
                                  FrictionBoost; sus nombres no se pueden leer del nodo, asi que se
                                  asignan por el orden alfabetico en que aparecen en la tabla de
                                  textos del db (DerbyGravel, DerbyTarmac, Forest, Gravel, Hazard, Ice,
                                  Object, Sand, Snow, StuntTarmac, Tarmac). SUPUESTO verificado solo por
                                  coherencia de los valores (p. ej. StuntTarmac tiene SlideControl alto).
  web/data/surfaces.json       <- data/global/dynamics/surfaces.bed (texto estilo Lua): por id de
                                  superficie, su "Dynamics", friccion, rugosidad, chispas, etc.

Uso:  python export_surfaces.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gamedb import GameDB  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SURFACES_BED = ROOT / "extracted" / "data" / "global" / "dynamics" / "surfaces.bed"
OUT = ROOT / "web" / "data"

DYNAMICS_ORDER = ["DerbyGravel", "DerbyTarmac", "Forest", "Gravel", "Hazard", "Ice", "Object",
                  "Sand", "Snow", "StuntTarmac", "Tarmac"]


def export_tire_dynamics():
    db = GameDB()
    nodes = []
    for k in range(db.num_nodes):
        if db.node_ptr[k] not in db.recs:
            continue
        n = db.node(k)
        if "FrictionBoost" in n:
            nodes.append(n)
    if len(nodes) != len(DYNAMICS_ORDER):
        raise SystemExit(f"se esperaban {len(DYNAMICS_ORDER)} nodos TireDynamics y hay {len(nodes)}")
    return dict(zip(DYNAMICS_ORDER, nodes))


def _value(raw):
    raw = raw.strip().rstrip(",").strip()
    if raw.startswith('"'):
        return raw.strip('"')
    if raw in ("true", "false"):
        return raw == "true"
    if raw.startswith("{"):
        return [_value(x) for x in raw.strip("{} ").split(",") if x.strip()]
    try:
        return float(raw) if ("." in raw or "e" in raw.lower()) else int(raw)
    except ValueError:
        return raw   # constante simbolica (COLLISIONSOUND_*, RUMBLE_*...)


def export_surfaces():
    text = SURFACES_BED.read_text(encoding="latin-1")
    text = re.sub(r"--[^\n]*", "", text)                                   # comentarios Lua
    body = text[text.index("Surfaces"):]
    surfaces = {}
    # cada entrada: [id]= { campo = valor, ... }   (sin tablas anidadas salvo {a,b})
    for m in re.finditer(r"\[(\d+)\]\s*=\s*\{(.*?)\n\s*\}\s*,?", body, re.S):
        fields = {}
        for line in m.group(2).splitlines():
            fm = re.match(r"\s*(\w+)\s*=\s*(.+?)\s*$", line)
            if fm:
                fields[fm.group(1)] = _value(fm.group(2))
        surfaces[int(m.group(1))] = fields
    return surfaces


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    dyn = export_tire_dynamics()
    (OUT / "tire_dynamics.json").write_text(json.dumps(dyn, indent=1))
    surf = export_surfaces()
    (OUT / "surfaces.json").write_text(json.dumps(surf, indent=1))
    used = sorted({s.get("Dynamics") for s in surf.values()})
    missing = [u for u in used if u not in dyn]
    print(f"{len(dyn)} dinamicas de neumatico, {len(surf)} superficies; dinamicas usadas: {used}")
    print("dinamicas sin datos:", missing or "ninguna")
    for name in DYNAMICS_ORDER:
        d = dyn[name]
        print(f"  {name:12s} mu X/Z {d['XFriction'][0]}/{d['ZFriction'][0]} boost {d['FrictionBoost']} "
              f"slowDown {d['SlowDown']} antiSpin {d['AntiSpin']} slideCtl {d['SlideControl']}")


if __name__ == "__main__":
    main()
