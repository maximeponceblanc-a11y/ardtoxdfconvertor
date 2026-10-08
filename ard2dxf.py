"""Extraction de la géométrie (lignes + arcs) d'un fichier ArtiosCAD .ARD vers DXF R12,
par rétro-ingénierie du format. Usage CLI : python ard2dxf.py fichier.ARD [...]

Tokens identifiés (big-endian, float32, unités = mm) :
  F3 x y        -> moveto            F4 x y   -> lineto
  F6 cx cy a    -> arc de centre (cx,cy), balayage a en radians (CCW > 0)
  E8 n          -> type de ligne (1 = coupe, 2 = rainage, ...)
  E5/E9 n, EA 4o, EB 2o, F9 16o -> attributs (non interprétés)
  00            -> fin de bloc de 512 octets (bourrage)
"""
import math
import struct
import sys
from pathlib import Path

ARGS = {0xF3: 8, 0xF4: 8, 0xF6: 12, 0xE5: 1, 0xE8: 1, 0xE9: 1, 0xEA: 4, 0xEB: 2, 0xF9: 16}
LAYERS = {1: "Cut", 2: "Crease"}  # autres valeurs -> "Type_<n>"
GEOM_START = 0x1AD  # TODO : à lire dans l'en-tête plutôt qu'en dur
MAGIC = b"I-code MS"


class ArdError(ValueError):
    pass


def parse(data):
    if not data.startswith(MAGIC):
        raise ArdError("En-tête ArtiosCAD absent : ce n'est pas un fichier .ARD reconnu.")
    ents, p, cur, ltype = [], GEOM_START, None, 1
    while p < len(data):
        t = data[p]
        if t == 0:
            p = (p // 512 + 1) * 512
            continue
        if t not in ARGS:
            break  # fin de la section géométrie (cotations, textes... non gérés)
        a = data[p + 1:p + 1 + ARGS[t]]
        f = struct.unpack(">" + "f" * (len(a) // 4), a) if t >= 0xF3 else None
        layer = LAYERS.get(ltype, f"Type_{ltype}")
        if t == 0xE8:
            ltype = a[0]
        elif t == 0xF3:
            cur = f
        elif t == 0xF4:
            if cur and f != cur:
                ents.append(("LINE", layer, cur, f))
            cur = f
        elif t == 0xF6 and cur:
            cx, cy, sweep = f
            r = math.hypot(cur[0] - cx, cur[1] - cy)
            a0 = math.atan2(cur[1] - cy, cur[0] - cx)
            a1 = a0 + sweep
            if r > 1e-6 and abs(sweep) > 1e-9:
                ents.append(("ARC", layer, (cx, cy), r, a0, a1))
            cur = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        p += 1 + ARGS[t]
    if not ents:
        raise ArdError("Aucune géométrie trouvée dans le fichier.")
    return ents


def to_dxf(ents):
    out = ["0", "SECTION", "2", "ENTITIES"]
    for e in ents:
        if e[0] == "LINE":
            _, ly, (x1, y1), (x2, y2) = e
            out += ["0", "LINE", "8", ly, "10", f"{x1:.4f}", "20", f"{y1:.4f}",
                    "11", f"{x2:.4f}", "21", f"{y2:.4f}"]
        else:
            _, ly, (cx, cy), r, a0, a1 = e
            if a1 < a0:  # DXF : arcs toujours CCW
                a0, a1 = a1, a0
            out += ["0", "ARC", "8", ly, "10", f"{cx:.4f}", "20", f"{cy:.4f}", "40", f"{r:.4f}",
                    "50", f"{math.degrees(a0):.4f}", "51", f"{math.degrees(a1):.4f}"]
    out += ["0", "ENDSEC", "0", "EOF"]
    return "\n".join(out) + "\n"


def convert(data):
    """Octets d'un .ARD -> (texte DXF, nombre d'entités)."""
    ents = parse(data)
    return to_dxf(ents), len(ents)


if __name__ == "__main__":
    for src in sys.argv[1:]:
        dxf, n = convert(Path(src).read_bytes())
        dst = Path(src).with_suffix(".dxf").name
        Path(dst).write_text(dxf, encoding="ascii")
        print(f"{src} -> {dst} ({n} entités)")
