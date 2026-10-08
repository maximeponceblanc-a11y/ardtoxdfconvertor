"""Conversion d'un fichier ArtiosCAD .ARD vers DXF R12 (lignes, arcs, textes, cotes),
par rétro-ingénierie du format. Usage CLI : python ard2dxf.py fichier.ARD [...]

Structure du fichier (big-endian, float32, unités = mm) :
  0x0E          table d'en-tête : enregistrements (id u16, début u32, fin u32), décalage +0x400
  0x26          table des attributs : [id][type] nom\\0 ...   (taille de la valeur = type - 1)
  section 0x0B  flux de tokens géométriques (blocs de 512 octets, bourrage 00)
  section 0x0C  index des groupes : enregistrements de 35 octets
                (niveau u8, nom 10 car., bbox 4f, début u32, fin u32) -> sert à ignorer REBUILD/PASTE

Tokens du flux :
  F3 x y        -> moveto            F4 x y   -> lineto
  F6 cx cy a    -> arc de centre (cx,cy), balayage a en radians (CCW > 0)
  E8 n          -> type de ligne (1 = coupe, 2 = rainage, ...)
  ED h          -> hauteur de texte (mm) pour les textes / cotes qui suivent
  FE L ...      -> texte : x y (coin bas-gauche du cadre), 1 octet, chaîne (L = longueur totale)
  FF L ...      -> cote : T(x,y) centre du texte, 1 octet, P1 P2 (ligne de cote),
                   P3 P4 (points mesurés), g1 g2 (longueurs de ligne avant/après le texte),
                   valeur, 2 octets
  EA id v       -> déclaration d'attribut (id u16 + valeur), puis token court <id> v
                   (ex. D9 = « Bridges », valeur u16)
  E3/E5/E9 1o, EB 2o, EE/EF/F0 4o, F9 16o -> attributs (non interprétés)
  00            -> fin de bloc de 512 octets (bourrage)
"""
import math
import struct
import sys
from pathlib import Path

ARGS = {0xF3: 8, 0xF4: 8, 0xF6: 12, 0xE3: 1, 0xE5: 1, 0xE8: 1, 0xE9: 1, 0xEB: 2,
        0xED: 4, 0xEE: 4, 0xEF: 4, 0xF0: 4, 0xF9: 16}
LAYERS = {1: "Cut", 2: "Crease"}  # autres valeurs -> "Type_<n>"
DIM_LAYER, TEXT_LAYER = "Dimension", "Text"
BRIDGE_ATTR = 0xD9
SKIP_GROUPS = {"REBUILD", "PASTE"}  # reconstruction paramétrique / presse-papiers : non visibles
BASE = 0x400                  # décalage des pointeurs de la table d'en-tête
GEOM_START_FALLBACK = 0x1AD
MAGIC = b"I-code MS"
BOX_RATIO = 1.7               # hauteur du cadre de texte ArtiosCAD / hauteur du texte
ARROW_RATIO = 0.6             # longueur de flèche / hauteur du texte de cote
ARROW_ANGLE = math.radians(15)
EXT_OVERSHOOT = 2.0           # dépassement des lignes d'attache (mm)


class ArdError(ValueError):
    pass


def _u32(d, p):
    return struct.unpack_from(">I", d, p)[0]


def _attr_sizes(data):
    """Table des attributs de l'en-tête : id -> taille de la valeur (None = inconnue)."""
    sizes, p = {}, 0x26
    while p < len(data) - 2 and data[p] != 0:
        aid, typ = data[p], data[p + 1]
        sizes[aid] = typ - 1 if typ >= 2 else None
        end = data.index(b"\0", p + 2)
        p = end + 1
    return sizes


def _sections(data):
    """Table d'en-tête -> {id: (début, fin)} en offsets fichier, ou None si non reconnue."""
    try:
        if struct.unpack_from(">H", data, 0x0E)[0] != 0x0F:
            return None
        p = _u32(data, 0x14) - BASE           # fin du nom de fichier = début de la table
        secs = {}
        while p + 10 <= len(data):
            sid = struct.unpack_from(">H", data, p)[0]
            a, b = _u32(data, p + 2) - BASE, _u32(data, p + 6) - BASE
            secs[sid] = (a, b)
            p += 10
            if 0x0B in secs and p >= secs[0x0B][0]:
                break
        if 0x0B not in secs or p != secs[0x0B][0]:
            return None
        return secs
    except (struct.error, ValueError):
        return None


def _skip_ranges(data, secs):
    """Plages de la section géométrie à ignorer (groupes REBUILD et PASTE, non affichés)."""
    if not secs or 0x0C not in secs:
        return []
    out, (p, end) = [], secs[0x0C]
    while p + 35 <= end:
        if data[p] == 0:
            p = (p // 512 + 1) * 512
            continue
        name = data[p + 1:p + 11].decode("latin-1").strip()
        a, b = _u32(data, p + 27) - BASE, _u32(data, p + 31) - BASE
        if data[p] == 2 and name in SKIP_GROUPS:
            out.append((a, b))
        p += 35
    return out


def _fmt_dim(v):
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s == "-0" else s


def _dimension(rec, h):
    """Décompose une cote en lignes + flèches + texte (comme l'export DXF d'ArtiosCAD)."""
    tx, ty = struct.unpack_from(">2f", rec, 0)
    x1, y1, x2, y2, x3, y3, x4, y4, g1, g2, val = struct.unpack_from(">11f", rec, 9)
    ents, L = [], DIM_LAYER
    dx, dy = x2 - x1, y2 - y1
    n = math.hypot(dx, dy)
    if n < 1e-9:
        return ents
    ux, uy = dx / n, dy / n
    # ligne de cote, interrompue autour du texte
    if g1 + g2 < n - 1e-6 and g1 > 0 and g2 > 0:
        ents.append(("LINE", L, (x1, y1), (x1 + g1 * ux, y1 + g1 * uy)))
        ents.append(("LINE", L, (x2 - g2 * ux, y2 - g2 * uy), (x2, y2)))
    else:
        ents.append(("LINE", L, (x1, y1), (x2, y2)))
    # flèches (pointe sur P1 et P2, tournées vers l'extérieur)
    a = h * ARROW_RATIO
    for (px, py), s in (((x1, y1), 1), ((x2, y2), -1)):
        for k in (1, -1):
            ang = math.atan2(s * uy, s * ux) + k * ARROW_ANGLE
            ents.append(("LINE", L, (px, py), (px + a * math.cos(ang), py + a * math.sin(ang))))
    # lignes d'attache : point mesuré -> ligne de cote (+ dépassement)
    for (fx, fy), (px, py) in (((x3, y3), (x1, y1)), ((x4, y4), (x2, y2))):
        ex, ey = px - fx, py - fy
        m = math.hypot(ex, ey)
        if m > 1e-6:
            ents.append(("LINE", L, (fx, fy),
                         (px + EXT_OVERSHOOT * ex / m, py + EXT_OVERSHOOT * ey / m)))
    ents.append(("TEXT", L, (tx, ty), h, _fmt_dim(val), "center"))
    return ents


def parse(data, warnings=None):
    """Octets d'un .ARD -> liste d'entités. `warnings` (liste) reçoit les avertissements."""
    if warnings is None:
        warnings = []
    if not data.startswith(MAGIC):
        raise ArdError("En-tête ArtiosCAD absent : ce n'est pas un fichier .ARD reconnu.")
    secs = _sections(data)
    if secs:
        p, geom_end = secs[0x0B]
    else:
        warnings.append("Table d'en-tête non reconnue : lecture à partir de l'offset par défaut.")
        p, geom_end = GEOM_START_FALLBACK, len(data)
    skips = _skip_ranges(data, secs)
    attr_sizes = _attr_sizes(data)
    attrs, bridged = {}, 0
    ents, cur, ltype, th = [], None, 1, 5.0

    def set_attr(aid, raw):
        nonlocal bridged
        attrs[aid] = int.from_bytes(raw, "big")
        if aid == BRIDGE_ATTR and attrs[aid]:
            bridged += 1

    while p < geom_end:
        skip = next((b for a, b in skips if a <= p < b), None)
        if skip is not None:
            p = skip
            continue
        t = data[p]
        if t == 0:
            p = (p // 512 + 1) * 512
            continue
        if t in (0xFE, 0xFF):                       # texte / cote : longueur en 2e octet
            n = data[p + 1]
            rec = data[p + 2:p + n]
            if t == 0xFE and len(rec) >= 9:
                x, y = struct.unpack_from(">2f", rec, 0)
                s = rec[9:].split(b"\0")[0].decode("cp1252", errors="replace")
                if s.strip():
                    ents.append(("TEXT", TEXT_LAYER, (x, y + th * BOX_RATIO / 2), th, s, "left"))
            elif t == 0xFF and len(rec) >= 53:
                ents += _dimension(rec, th)
            p += max(n, 2)
            continue
        if t == 0xEA:                               # déclaration d'attribut
            aid = struct.unpack_from(">H", data, p + 1)[0]
            size = attr_sizes.get(aid)
            if size is None:
                warnings.append(f"Attribut 0x{aid:X} de taille inconnue à 0x{p:X} : arrêt.")
                break
            set_attr(aid, data[p + 3:p + 3 + size])
            p += 3 + size
            continue
        if t in attr_sizes and t not in ARGS:       # token court d'attribut (ex. D9 = ponts)
            size = attr_sizes[t]
            if size is None:
                warnings.append(f"Attribut 0x{t:X} de taille inconnue à 0x{p:X} : arrêt.")
                break
            set_attr(t, data[p + 1:p + 1 + size])
            p += 1 + size
            continue
        if t not in ARGS:
            warnings.append(f"Token inconnu 0x{t:02X} à 0x{p:X} : fin de lecture anticipée.")
            break
        a = data[p + 1:p + 1 + ARGS[t]]
        layer = LAYERS.get(ltype, f"Type_{ltype}")
        if t == 0xE8:
            ltype = a[0]
        elif t == 0xED:
            v = struct.unpack(">f", a)[0]
            if 0 < v < 1e4:
                th = v
        elif t == 0xF3:
            cur = struct.unpack(">2f", a)
        elif t == 0xF4:
            f = struct.unpack(">2f", a)
            if cur and f != cur:
                ents.append(("LINE", layer, cur, f))
            cur = f
        elif t == 0xF6 and cur:
            cx, cy, sweep = struct.unpack(">3f", a)
            r = math.hypot(cur[0] - cx, cur[1] - cy)
            a0 = math.atan2(cur[1] - cy, cur[0] - cx)
            a1 = a0 + sweep
            if r > 1e-6 and abs(sweep) > 1e-9:
                ents.append(("ARC", layer, (cx, cy), r, a0, a1))
            cur = (cx + r * math.cos(a1), cy + r * math.sin(a1))
        p += 1 + ARGS[t]
    if bridged:
        warnings.append(f"{bridged} tracé(s) avec ponts : l'encodage des ponts n'est pas encore "
                        "décodé, ces lignes sont exportées continues.")
    if not any(e[0] in ("LINE", "ARC") for e in ents):
        raise ArdError("Aucune géométrie trouvée dans le fichier.")
    return ents


def _layer_table(names):
    colors = {"Cut": 1, "Crease": 5, DIM_LAYER: 3, TEXT_LAYER: 3}
    out = ["0", "TABLE", "2", "LTYPE", "70", "1",
           "0", "LTYPE", "2", "CONTINUOUS", "70", "64", "3", "Solid line", "72", "65",
           "73", "0", "40", "0.0", "0", "ENDTAB",
           "0", "TABLE", "2", "LAYER", "70", str(len(names))]
    for n in names:
        out += ["0", "LAYER", "2", n, "70", "0", "62", str(colors.get(n, 7)), "6", "CONTINUOUS"]
    return out + ["0", "ENDTAB"]


def to_dxf(ents):
    layers = list(dict.fromkeys(e[1] for e in ents))
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009",
           "9", "$DWGCODEPAGE", "3", "ANSI_1252", "0", "ENDSEC",
           "0", "SECTION", "2", "TABLES"] + _layer_table(layers) + ["0", "ENDSEC",
           "0", "SECTION", "2", "ENTITIES"]
    for e in ents:
        if e[0] == "LINE":
            _, ly, (x1, y1), (x2, y2) = e
            out += ["0", "LINE", "8", ly, "10", f"{x1:.4f}", "20", f"{y1:.4f}",
                    "11", f"{x2:.4f}", "21", f"{y2:.4f}"]
        elif e[0] == "ARC":
            _, ly, (cx, cy), r, a0, a1 = e
            if a1 < a0:  # DXF : arcs toujours CCW
                a0, a1 = a1, a0
            out += ["0", "ARC", "8", ly, "10", f"{cx:.4f}", "20", f"{cy:.4f}", "40", f"{r:.4f}",
                    "50", f"{math.degrees(a0):.4f}", "51", f"{math.degrees(a1):.4f}"]
        else:  # TEXT, ancré au milieu (gauche ou centre) du cadre
            _, ly, (x, y), h, s, just = e
            s = s.replace("\r", " ").replace("\n", " ")
            hj = "1" if just == "center" else "0"
            out += ["0", "TEXT", "8", ly, "10", f"{x:.4f}", "20", f"{y:.4f}", "40", f"{h:.4f}",
                    "1", s, "72", hj, "11", f"{x:.4f}", "21", f"{y:.4f}", "73", "2"]
    out += ["0", "ENDSEC", "0", "EOF"]
    return "\n".join(out) + "\n"


def convert(data, warnings=None):
    """Octets d'un .ARD -> (texte DXF, nombre d'entités)."""
    ents = parse(data, warnings)
    return to_dxf(ents), len(ents)


if __name__ == "__main__":
    for src in sys.argv[1:]:
        warns = []
        dxf, n = convert(Path(src).read_bytes(), warns)
        dst = Path(src).with_suffix(".dxf").name
        Path(dst).write_text(dxf, encoding="cp1252", errors="replace")
        print(f"{src} -> {dst} ({n} entités)")
        for w in warns:
            print("  attention :", w)
