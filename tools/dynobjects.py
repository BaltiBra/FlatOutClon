"""Lector de dynamic_objects.bed (props dinamicos de FlatOut UC) -> JSON por modelo.

Uso:  python -I dynobjects.py [salida.json]        (por defecto web/dynamic_objects.json)
      python -I dynobjects.py show <nombre>        (imprime un objeto)

El .bed es texto estilo Lua (latin-1, comentarios con '--' y '#'). No se ejecuta Lua: un tokenizador
propio lee las asignaciones de cabecera (COLLISIONSOUND_*, BONUSTYPE_*, ...: constantes), las tablas
BonusTypeMayhem / BonusTypePrice (indexadas por constante) y una tabla por objeto
(`nombre = { Campo = valor, ... }`). `function ... end` se salta; `SetCategory(a, b)` se resuelve a
la lista de categorias. Si un nombre aparece dos veces gana el ultimo (semantica de Lua; "overrides"
cuenta las veces). Las comas finales, comentarios intercalados y `#` sueltos se toleran.

Campos que EXISTEN de verdad en los objetos (180 tablas; 178 definiciones de objeto = 173 nombres distintos; conteos entre parentesis):
  Mass (178, kg) · Restitution (191: 178 del cuerpo + los de Joint/Hinge) · Inertia (178, tipo 1 caja,
  2 cilindro solido, 3 esfera, 4 cilindro hueco) · Type (178: 1 normal, 2 puerta, 3 poste) ·
  Category (178: lista de CATEGORY_*) · CollisionSound / ParticleCollision (178, ids de sonido) ·
  BonusType (173: tipo para nitro y dinero; ver BonusTypeMayhem/Price) · MaxVisibility (166, m) ·
  WakeupVelocity (81, m/s: velocidad de impacto para despertar el objeto) · ReactivationVelocity (15) ·
  DestroyFx (5) · ExplosionForce (11) · EmitterFx (2) ·
  Joint {XAxisLimits, ZAxisLimits (grados), Restitution{a,b}, Velocity, MaxForce, BreakForce, BreakTorque}
  (7: postes que se inclinan) · Hinge {AngleLimits, Restitution, Velocity, MaxForce, BreakForce,
  BreakTorque} (6: puertas y vallas con bisagra).
NO existen: friccion, amortiguamiento (damping), formas de colision (el juego usa la malla de
colision del .w32), "durabilidad"/"breakable" (lo mas parecido es Joint/Hinge.BreakForce). Quien
use estos datos debe elegir friccion/damping por su cuenta.

Salida (por nombre): campos normalizados en minuscula (mass, restitution, inertia, inertiaName, type,
typeName, categories, collisionSound(+Name), particleCollision(+Name), bonusType(+Name), nitro
(fraccion de nitro por objeto, BonusTypeMayhem), price (dinero, BonusTypePrice), maxVisibility,
wakeupVelocity, reactivationVelocity, explosionForce, destroyFx(+Name), emitterFx, joint, hinge,
hasJoint) mas "raw" (tabla leida tal cual, con constantes ya resueltas) y "overrides".
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BED = ROOT / "extracted" / "data" / "global" / "dynamics" / "dynamic_objects.bed"

TOKEN = re.compile(r'''\s*(?:
    (?P<num>[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)
  | (?P<id>[A-Za-z_][A-Za-z_0-9]*)
  | (?P<str>"[^"]*"|'[^']*')
  | (?P<sym>[{}\[\](),=.])
)''', re.X)


def strip_comments(txt):
    out = []
    for line in txt.replace("\r", "").split("\n"):
        # '--' y '#' inician comentario (no hay cadenas con esos caracteres en el archivo)
        line = re.sub(r'(--|#).*$', '', line)
        out.append(line)
    return "\n".join(out)


def tokenize(txt):
    pos, toks = 0, []
    while pos < len(txt):
        if txt[pos:].strip() == "":
            break
        m = TOKEN.match(txt, pos)
        if not m or m.end() == pos:
            pos += 1  # caracter raro (ej. restos de '#' o bytes sueltos): se salta
            continue
        pos = m.end()
        kind = m.lastgroup
        toks.append((kind, m.group(kind)))
    return toks


class Parser:
    def __init__(self, toks):
        self.t, self.i = toks, 0
        self.consts = {}
        self.objects = {}      # nombre -> dict (ultimo gana)
        self.overrides = {}
        self.tables = {}       # tablas con claves-constante (BonusTypeMayhem, BonusTypePrice)

    def peek(self, k=0):
        j = self.i + k
        return self.t[j] if j < len(self.t) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def skip_function(self):
        depth = 0
        while self.i < len(self.t):
            kind, v = self.next()
            if kind == "id" and v in ("function", "if", "for", "while", "do"):
                depth += 1 if v != "do" else 0
            elif kind == "id" and v == "end":
                depth -= 1
                if depth <= 0:
                    return

    def value(self):
        kind, v = self.peek()
        if kind == "num":
            self.next()
            return float(v) if re.search(r'[.eE]', v) else int(v)
        if kind == "str":
            self.next()
            return v[1:-1]
        if kind == "sym" and v == "{":
            return self.table()
        if kind == "id":
            self.next()
            if self.peek() == ("sym", "("):          # llamada: SetCategory(a, b) -> lista de args
                self.next()
                args = []
                while self.peek() != ("sym", ")") and self.peek()[0] is not None:
                    args.append(self.value())
                    if self.peek() == ("sym", ","):
                        self.next()
                self.next()
                return sorted(int(a) for a in args if str(a).lstrip("-").isdigit()) \
                    if v == "SetCategory" else args
            return self.consts.get(v, v)             # constante resuelta (o el nombre si es desconocida)
        self.next()
        return None

    def table(self):
        self.next()  # {
        d, arr = {}, []
        while True:
            kind, v = self.peek()
            if kind is None:
                break
            if (kind, v) == ("sym", "}"):
                self.next()
                break
            if (kind, v) == ("sym", ","):
                self.next()
                continue
            if (kind, v) == ("sym", "["):            # [CONSTANTE] = valor
                self.next()
                key = self.value()
                if self.peek() == ("sym", "]"):
                    self.next()
                if self.peek() == ("sym", "="):
                    self.next()
                d[key] = self.value()
                continue
            if kind == "id" and self.peek(1) == ("sym", "="):
                self.next(); self.next()
                d[v] = self.value()
                continue
            arr.append(self.value())
        return d if d or not arr else arr

    def parse(self):
        while self.i < len(self.t):
            kind, v = self.next()
            if kind == "id" and v == "function":
                self.i -= 1
                self.skip_function()
                continue
            if kind == "id" and self.peek() == ("sym", "="):
                self.next()
                val = self.value()
                if isinstance(val, dict):
                    if "Mass" in val:
                        if v in self.objects:
                            self.overrides[v] = self.overrides.get(v, 0) + 1
                        self.objects[v] = val
                    else:
                        self.tables[v] = val
                elif isinstance(val, (int, float, str)):
                    self.consts[v] = val


def const_names(consts, prefix):
    return {v: k[len(prefix):].lower() for k, v in consts.items()
            if k.startswith(prefix) and isinstance(v, int)}


def parse_bed(path=BED):
    txt = Path(path).read_bytes().decode("latin-1")
    p = Parser(tokenize(strip_comments(txt)))
    p.parse()
    c = p.consts
    names = {k: const_names(c, k + "_") for k in
             ("COLLISIONSOUND", "PARTICLECOLLISION", "BONUSTYPE", "OBJECT", "INERTIA", "DESTROYFX")}
    mayhem = {int(k): v for k, v in p.tables.get("BonusTypeMayhem", {}).items() if isinstance(k, int)}
    price = {int(k): v for k, v in p.tables.get("BonusTypePrice", {}).items() if isinstance(k, int)}

    def nm(group, v):
        return names[group].get(v) if isinstance(v, int) else None

    out = {}
    for name, o in p.objects.items():
        bt = o.get("BonusType")
        r = {
            "mass": o.get("Mass"), "restitution": o.get("Restitution"),
            "inertia": o.get("Inertia"), "inertiaName": nm("INERTIA", o.get("Inertia")),
            "type": o.get("Type"), "typeName": nm("OBJECT", o.get("Type")),
            "categories": o.get("Category", []),
            "collisionSound": o.get("CollisionSound"), "collisionSoundName": nm("COLLISIONSOUND", o.get("CollisionSound")),
            "particleCollision": o.get("ParticleCollision"),
            "bonusType": bt, "bonusTypeName": nm("BONUSTYPE", bt),
            "nitro": mayhem.get(bt), "price": price.get(bt),
            "maxVisibility": o.get("MaxVisibility"),
            "wakeupVelocity": o.get("WakeupVelocity"), "reactivationVelocity": o.get("ReactivationVelocity"),
            "explosionForce": o.get("ExplosionForce"),
            "destroyFx": o.get("DestroyFx"), "destroyFxName": nm("DESTROYFX", o.get("DestroyFx")),
            "emitterFx": o.get("EmitterFx"),
            "joint": o.get("Joint"), "hinge": o.get("Hinge"),
            "hasJoint": "Joint" in o or "Hinge" in o,
            "overrides": p.overrides.get(name, 0), "raw": o,
        }
        out[name] = r
    return out


def main():
    a = sys.argv[1:]
    objs = parse_bed()
    if a and a[0] == "show":
        print(json.dumps(objs.get(a[1]), indent=1))
        return
    dst = Path(a[0]) if a else ROOT / "web" / "dynamic_objects.json"
    dst.write_text(json.dumps(objs, indent=1))
    mass = sorted(o["mass"] for o in objs.values())
    print(f"{len(objs)} objetos -> {dst}; masa {mass[0]}..{mass[-1]} kg; "
          f"con Joint/Hinge: {sum(o['hasJoint'] for o in objs.values())}; "
          f"duplicados: {[k for k, o in objs.items() if o['overrides']]}")


if __name__ == "__main__":
    main()
