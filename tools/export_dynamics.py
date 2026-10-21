"""Exporta los modelos de neumatico POR SUPERFICIE de data/database/game.db a web/surface_dynamics.json.

En game.db hay 11 nodos consecutivos (982..992 en FOUC) con las claves RollingResistance, PneumaticTrail,
ZStiffness/XStiffness [a,b,c], ZFriction/XFriction [mu, k], FrictionBoost, SlideUnderSteer, SlideControl,
UnderSteer, SlowDown, AntiSpin: son los "Dynamics" que referencia surfaces.bed (Tarmac, Gravel...).
Los nombres de nodo del PDB son punteros imprecisos al final de la tabla de textos; se asignan por orden
con la secuencia de la tabla ("DerbyGravel, DerbyTarmac, Forest, Gravel, Hazard, Ice, Sand, Snow,
StuntTarmac, Tarmac, ...") cruzada con los sufijos que dejan los punteros y con los valores. Seguros: los que
coinciden con su puntero; "inferred": True marca los asignados solo por descarte.
"grip" = XFriction[0] / XFriction[0] de Tarmac (1.0 = asfalto).

Uso:  python export_dynamics.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gamedb  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "web" / "surface_dynamics.json"
# (nodo, nombre, inferido)
NODES = [(982, "DerbyTarmac", False), (983, "Forest", False), (984, "Gravel", False),
         (985, "Hazard", False), (986, "Sand", False), (987, "Snow", False),
         (988, "Ice", True), (989, "StuntTarmac", False), (990, "DerbyGravel", True),
         (991, "Tarmac", False), (992, "Object", True)]
KEYS = ("RollingResistance", "InducedDragCoeff", "PneumaticTrail", "PneumaticOffset", "ZStiffness",
        "XStiffness", "CStiffness", "ZFriction", "XFriction", "FrictionBoost", "SlideUnderSteer",
        "SlideControl", "UnderSteer", "SlowDown", "AntiSpin")


def main():
    db = gamedb.GameDB()
    out = {}
    for k, name, inferred in NODES:
        n = db.node(k)
        if "XFriction" not in n:
            raise SystemExit("nodo %d no es un Dynamics (game.db distinto?)" % k)
        out[name] = {key: n[key] for key in KEYS if key in n}
        out[name]["node"] = k
        out[name]["inferred"] = inferred
    ref = out["Tarmac"]["XFriction"][0]
    for v in out.values():
        v["grip"] = round(v["XFriction"][0] / ref, 3)
    OUT.write_text(json.dumps(out, indent=1))
    for name, v in out.items():
        print("%-12s grip %.2f  XStiff %s  trail %.2f  under %.1f  slideCtl %.2f  antiSpin %.2f" % (
            name, v["grip"], v["XStiffness"], v["PneumaticTrail"], v["UnderSteer"],
            v["SlideControl"], v["AntiSpin"]))


if __name__ == "__main__":
    main()
