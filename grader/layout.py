"""
Géométrie canonique de la fiche de réponses (A4, coordonnées en mm).

Le même module est utilisé par :
  - sheet_pdf.py  : pour dessiner le PDF à imprimer
  - omr.py        : pour savoir où lire les cases après recalage de l'image

La disposition est figée dans quiz.layout_json au moment de la génération
du PDF, pour que la lecture reste correcte même si le quiz est modifié après.
"""

import hashlib
import json

import cv2

from . import fonts

PAGE_W = 210.0
PAGE_H = 297.0
PX_PER_MM = 8          # résolution de l'image canonique (1680 x 2376 px)

MARKER_SIZE = 14.0     # côté des repères ArUco (mm)
MARKER_MARGIN = 8.0    # marge entre le bord de page et les repères (mm)

# Dictionnaire ArUco des repères de calibration. Chaque page consomme 4
# identifiants (voir marker_positions), ce qui plafonne le nombre de pages
# d'une fiche. DICT_4X4_250 porte ce plafond à 62 pages ; ses 50 premiers
# marqueurs sont identiques à ceux de DICT_4X4_50 utilisé auparavant, donc
# les fiches déjà imprimées (jusqu'à 12 pages) restent lisibles.
ARUCO_DICT_ID = cv2.aruco.DICT_4X4_250
MARKERS_PER_PAGE = 4
MAX_SHEET_PAGES = 250 // MARKERS_PER_PAGE      # 62 pages


class TooManyPages(ValueError):
    """Fiche dépassant le nombre de pages que les repères peuvent numéroter."""

    def __init__(self, pages):
        super().__init__(
            f"Cette fiche ferait {pages} pages, au-delà de la limite de "
            f"{MAX_SHEET_PAGES} pages imposée par les repères de calibration. "
            "Réduisez le nombre de questions ou la hauteur des zones de "
            "réponse manuscrite, ou répartissez l'épreuve sur plusieurs quiz.")
        self.pages = pages


# ---------------------------------------------------------------------------
# Calage par CADRE (depuis le 29 octobre 2026, à la demande de l'utilisateur :
# plus de carrés noirs dans les coins). Un trait fin encadre la page ; ses
# quatre coins recalent le scan. Une rangée de petits tirets centrée en haut
# donne le n° de page et le sens (bit de départ + parité : une page à l'envers
# ne se lit pas comme une page à l'endroit). Les copies imprimées avant, avec
# les repères ArUco, se lisent toujours (omr.detect_and_warp les essaie
# d'abord). Rien de ce qui est lu (cases, grilles, QR) ne bouge.
# ---------------------------------------------------------------------------
CADRE_MARGE = 10.0     # axe du trait du cadre, depuis le bord de la page (mm)
CADRE_TRAIT = 1.2      # épaisseur du trait (mm)
CODE_Y = 12.6          # haut des tirets du code de page (mm)
CODE_W, CODE_H, CODE_PAS = 4.0, 2.2, 6.0
CODE_N = 8             # départ (toujours plein) + 6 bits de page + parité


# Copie à TALON (Quiz.talon_nom) : le cadre commence plus bas, la bande du
# haut (nom de l'étudiant) est hors du cadre et se découpe au trait pointillé.
# Le code de page reste à la même distance sous le haut du cadre ; un 9e tiret
# (« variante ») est plein sur ces copies : sans lui, une copie normale et une
# copie à talon recalées l'une comme l'autre donneraient toutes deux un code
# valide (seule la hauteur du cadre change).
TALON_CADRE_HAUT = 38.0    # axe du haut du cadre sur une copie à talon (mm)
TALON_COUPE = 30.0         # trait « couper ici » (mm du haut de la page)
CADRES_HAUTS = (CADRE_MARGE, TALON_CADRE_HAUT)


def code_cases(haut=CADRE_MARGE):
    """Rectangles [x, y, w, h] (mm) des tirets du code de page, sous le haut
    du cadre (10 mm sur une copie normale, plus bas sur une copie à talon)."""
    y = round(CODE_Y - CADRE_MARGE + haut, 2)
    x0 = PAGE_W / 2 - (CODE_N * CODE_PAS - (CODE_PAS - CODE_W)) / 2
    return [[round(x0 + i * CODE_PAS, 2), y, CODE_W, CODE_H]
            for i in range(CODE_N)]


def case_variante(haut=CADRE_MARGE):
    """9e tiret, à droite des huit : plein sur une copie à talon."""
    x, y, w, h = code_cases(haut)[-1]
    return [round(x + CODE_PAS, 2), y, w, h]


def code_page(page_index):
    """Tirets pleins (True) codant le n° de page : départ, 6 bits, parité."""
    if not 0 <= page_index < 64:
        raise TooManyPages(page_index + 1)
    bits = [bool((page_index >> (5 - i)) & 1) for i in range(6)]
    return [True] + bits + [sum(bits) % 2 == 1]


def lire_code_page(pleins):
    """Inverse de code_page : n° de page, ou None si le code n'est pas valide
    (page à l'envers, tirets mal lus)."""
    if len(pleins) != CODE_N or not pleins[0]:
        return None
    bits = pleins[1:7]
    if (sum(bits) % 2 == 1) != pleins[7]:
        return None
    return sum(1 << (5 - i) for i, b in enumerate(bits) if b)


CONTENT_X0 = 26.0
CONTENT_X1 = PAGE_W - 26.0
CONTENT_Y1 = PAGE_H - MARKER_MARGIN - MARKER_SIZE - 4.0

BUBBLE_R = 2.6         # rayon des cases à noircir (mm)
BUBBLE_STEP = 9.0      # espacement horizontal entre cases (mm)
QCM_ROW_H = 8.5        # hauteur d'une ligne de question (mm)

NAME_BOX_H = 11.0

ID_BUBBLE_R = 2.1      # rayon des cases de la grille de n° d'inscription (mm)
ID_COL_STEP = 7.0      # espacement horizontal des chiffres 0..9 (mm)
ID_ROW_STEP = 6.2      # espacement vertical entre positions de chiffre (mm)

# --- Étiquette autocollante du candidat (concours) -------------------------
# La planche (labels_pdf.py) imprime, pour chaque candidat, son n° d'inscription
# et son nom à côté de son QR code : c'est ce qui permet de savoir à qui
# remettre quelle étiquette. SEUL LE QR est découpé et collé sur la copie —
# le bloc nom/numéro reste sur la planche, il ne sert qu'au tri.
# L'emplacement réservé sur la copie (_add_qr_zone) est donc dimensionné pour
# le QR découpé, pas pour la vignette entière.
STICKER_COLS = 2                     # vignettes par ligne sur la planche A4
STICKER_ROWS = 8                     # lignes de vignettes -> 16 par page
STICKER_MARGIN_X = 8.0               # marge latérale de la planche (mm)
STICKER_MARGIN_Y = 12.0              # marge haute / basse de la planche (mm)
STICKER_W = (PAGE_W - 2 * STICKER_MARGIN_X) / STICKER_COLS      # ~97.0 mm
STICKER_H = (PAGE_H - 2 * STICKER_MARGIN_Y) / STICKER_ROWS      # ~34.1 mm
STICKER_QR_MM = 22.0                 # côté du QR : la partie qu'on colle
STICKER_QR_PAD = 2.5                 # marge blanche autour du QR à découper

# Emplacement réservé sur la copie : le QR découpé, plus une tolérance
# confortable de découpe et de collage (l'étiquette est posée à la main).
STICKER_GLUE_PAD = 7.0
STICKER_ZONE_W = round(STICKER_QR_MM + 2 * STICKER_GLUE_PAD + 8.0, 1)   # 44 mm
STICKER_ZONE_H = round(STICKER_QR_MM + 2 * STICKER_GLUE_PAD, 1)         # 36 mm


def layout_signature(quiz):
    """Empreinte de tout ce qui est imprimé sur la fiche (questions, choix,
    barème, réglages). Enregistrée à la génération du PDF : si elle change,
    la page du quiz signale que la fiche doit être régénérée. La bonne
    réponse n'en fait pas partie (elle n'est pas imprimée : le corrigé peut
    être modifié après l'épreuve, les notes sont recalculées)."""
    data = {
        "settings": [quiz.title, quiz.class_group.name, quiz.language,
                     quiz.sheet_mode, quiz.id_mode,
                     quiz.id_digits, quiz.auto_enroll],
        "questions": [[q.order, q.qtype, q.text, list(q.choices or []),
                       q.num_bubbles, q.points, q.open_height_mm]
                      for q in quiz.questions.order_by("order")],
    }
    if getattr(quiz, "talon_nom", False):
        data["talon"] = True        # seulement si coché : empreintes inchangées
    if getattr(quiz, "place_anonyme", False):
        # une copie par étudiant : un étudiant ajouté demande une régénération
        data["places"] = sorted(quiz.class_group.students.values_list("pk", flat=True))
    if getattr(quiz, "type_epreuve", "qcm") in ("ecrit", "oral"):
        # seulement pour l'écrit et l'oral : les empreintes des fiches QCM
        # existantes ne doivent pas changer (toutes seraient « périmées »)
        data["ecrit"] = [quiz.note_sur, quiz.pages_redaction]
        if quiz.type_epreuve == "oral":
            data["oral"] = [s.pk for s in quiz.class_group.students.all()]
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _add_id_grid(page, y, digits):
    """Ajoute la grille de numéro d'inscription (une ligne par chiffre,
    colonnes 0..9) et retourne l'ordonnée disponible sous la grille."""
    y_label = y + 4.0
    y0 = y_label + 9.0
    x0 = CONTENT_X0 + 26.0
    rows = []
    boxes = []
    for d in range(digits):
        cy = y0 + d * ID_ROW_STEP
        rows.append([[round(x0 + k * ID_COL_STEP, 2), round(cy, 2)]
                     for k in range(10)])
        boxes.append([CONTENT_X0 + 14.0, round(cy - 2.6, 2), 6.5, 5.2])
    page["id_grid"] = {
        "digits": digits, "r": ID_BUBBLE_R, "rows": rows,
        "write_boxes": boxes, "label_y": round(y_label, 2),
        "header_y": round(y0 - 4.6, 2),
    }
    return y0 + (digits - 1) * ID_ROW_STEP + 7.0


def marker_positions(page_index):
    """Retourne {id_aruco: (x, y)} du coin supérieur gauche de chaque repère.
    Chaque page utilise 4 identifiants qui lui sont propres, ce qui permet
    de reconnaître le numéro de page sur le scan."""
    if page_index >= MAX_SHEET_PAGES:
        raise TooManyPages(page_index + 1)
    base = page_index * MARKERS_PER_PAGE
    right = PAGE_W - MARKER_MARGIN - MARKER_SIZE
    bottom = PAGE_H - MARKER_MARGIN - MARKER_SIZE
    return {
        base + 0: (MARKER_MARGIN, MARKER_MARGIN),          # haut gauche
        base + 1: (right, MARKER_MARGIN),                  # haut droite
        base + 2: (right, bottom),                         # bas droite
        base + 3: (MARKER_MARGIN, bottom),                 # bas gauche
    }


def _checked(layout):
    """Refuse une disposition qui dépasse le nombre de pages numérotables."""
    if len(layout["pages"]) > MAX_SHEET_PAGES:
        raise TooManyPages(len(layout["pages"]))
    # fiche dessinée avec le cadre de calage (et non les carrés ArUco) : les
    # PDF enregistrés sans cette marque sont refaits (services.moderniser_calage)
    layout["calage"] = "cadre"
    return layout


def build_layout(quiz):
    """Disposition de la fiche ; copie à talon : cadre abaissé (voir
    TALON_CADRE_HAUT), rien de ce qui est lu ne bouge."""
    layout = _build_sans_talon(quiz)
    if getattr(quiz, "talon_nom", False):
        layout["talon"] = True
        layout["cadre_haut"] = TALON_CADRE_HAUT
    return layout


def _build_sans_talon(quiz):
    """Calcule la disposition complète de la fiche pour un quiz.

    Retourne un dictionnaire sérialisable en JSON :
    {
      "num_choices": 4,
      "pages": [
        {
          "page_index": 0,
          "name_boxes": {"last": [x,y,w,h], "first": [x,y,w,h]},   # {} en mode grille
          "qcm": [{"order": 1, "bubbles": [[cx,cy], ...]}],
          "open": [{"order": 5, "rect": [x,y,w,h]}]
        }, ...
      ]
    }
    """
    if getattr(quiz, "type_epreuve", "qcm") in ("ecrit", "oral"):
        return _build_ecrit_layout(quiz)
    if getattr(quiz, "sheet_mode", "grid") == "full":
        return _build_full_layout(quiz)

    questions = list(quiz.questions.order_by("order"))
    ncho = quiz.num_choices
    rtl = getattr(quiz, "language", "fr") == "ar"
    qcm_qs = [q for q in questions if q.qtype == "qcm"]
    open_qs = [q for q in questions if q.qtype == "open"]

    id_mode = getattr(quiz, "id_mode", "name")
    id_digits = getattr(quiz, "id_digits", 6) or 6
    pages = []

    def new_page():
        idx = len(pages)
        page = {
            "page_index": idx,
            "name_boxes": _name_boxes(id_mode),
            "qcm": [],
            "open": [],
        }
        pages.append(page)
        # première ordonnée disponible sous l'en-tête
        y0 = 58.0 if idx == 0 else 46.0
        if id_mode == "grid":
            y0 = _add_id_grid(page, y0 - (10.0 if idx == 0 else 0.0), id_digits)
            y0 = _add_qr_corner(page, y0)
        elif id_mode in ("qr", "sticker"):
            y0 = _add_qr_zone(page, idx, sticker=_uses_sticker(quiz))
        return page, y0

    page, y = new_page()

    # ---- Grille QCM, sur deux colonnes si nécessaire ----
    # chaque question a son propre nombre de cases ; la largeur des colonnes
    # suit la question qui en a le plus
    ncho = max([q.num_bubbles for q in qcm_qs] or [ncho])
    qcm_block_w = 14.0 + ncho * BUBBLE_STEP        # numéro + cases
    two_cols = qcm_block_w * 2 + 12.0 <= (CONTENT_X1 - CONTENT_X0)
    col_x = [CONTENT_X0]
    if two_cols:
        col_x.append(CONTENT_X0 + (CONTENT_X1 - CONTENT_X0) / 2 + 4.0)

    if qcm_qs:
        i = 0
        while i < len(qcm_qs):
            rows_left = int((CONTENT_Y1 - y) // QCM_ROW_H)
            if rows_left <= 0:
                page, y = new_page()
                continue
            capacity = rows_left * len(col_x)
            chunk = qcm_qs[i:i + capacity]
            rows_used = -(-len(chunk) // len(col_x))  # plafond
            for j, q in enumerate(chunk):
                col = j // rows_used
                row = j % rows_used
                cy = y + row * QCM_ROW_H + QCM_ROW_H / 2
                x0 = col_x[col] + 14.0
                # en arabe (droite -> gauche) : bloc aligné à droite de la
                # colonne, le choix n° 0 (أ) à droite, le n° de question encore
                # plus à droite (miroir de la disposition latine).
                if rtl:
                    xr = (CONTENT_X0 + CONTENT_X1) - x0   # miroir du bord gauche
                    bubbles = [[round(xr - k * BUBBLE_STEP, 2), cy]
                               for k in range(q.num_bubbles)]
                else:
                    bubbles = [[round(x0 + k * BUBBLE_STEP, 2), cy]
                               for k in range(q.num_bubbles)]
                page["qcm"].append({"order": q.order, "bubbles": bubbles})
            y += rows_used * QCM_ROW_H + 6.0
            i += len(chunk)

    # ---- Zones de réponses manuscrites ----
    for q in open_qs:
        h = float(q.open_height_mm) + 7.0   # + place pour le libellé
        if y + h > CONTENT_Y1:
            page, y = new_page()
        page["open"].append({
            "order": q.order,
            "rect": [CONTENT_X0, y + 6.0, CONTENT_X1 - CONTENT_X0, float(q.open_height_mm)],
        })
        y += h + 4.0

    return _checked({"mode": "grid", "num_choices": ncho, "pages": pages})


# ---------------------------------------------------------------------------
# Mode « questionnaire complet » : les questions et leurs choix de réponse
# sont imprimés sur la fiche, une case à noircir devant chaque choix.
# ---------------------------------------------------------------------------

Q_SIZE = 10        # taille du texte des questions (pt)
C_SIZE = 10        # taille du texte des choix (pt)
Q_LINE_H = 5.2     # interligne du texte de question (mm)
C_LINE_H = 6.6     # hauteur de la 1re ligne d'un choix (mm)
C_CONT_H = 4.8     # hauteur des lignes suivantes d'un choix long (mm)
BLOCK_GAP = 4.5    # espace entre questions (mm)


def _build_full_layout(quiz):
    fonts.load_fonts()
    questions = list(quiz.questions.order_by("order"))
    ncho = quiz.num_choices
    lang = getattr(quiz, "language", "fr")
    rtl = lang == "ar"
    W = CONTENT_X1 - CONTENT_X0
    id_mode = getattr(quiz, "id_mode", "name")
    id_digits = getattr(quiz, "id_digits", 6) or 6

    pages = []

    def new_page():
        idx = len(pages)
        pages.append({"page_index": idx, "name_boxes": _name_boxes(id_mode),
                      "qcm": [], "open": [], "texts": []})
        y0 = 58.0 if idx == 0 else 46.0
        if id_mode == "grid":
            y0 = _add_id_grid(pages[-1], y0 - (10.0 if idx == 0 else 0.0), id_digits)
            y0 = _add_qr_corner(pages[-1], y0)
        elif id_mode in ("qr", "sticker"):
            y0 = _add_qr_zone(pages[-1], idx, sticker=_uses_sticker(quiz))
        return pages[-1], y0

    def fmt_num(n):
        # Numérotation des questions toujours en chiffres occidentaux (1, 2, 3…)
        return f"{n}."

    def fmt_pts(points):
        if rtl:
            unit = "نقطة" if points <= 1 else "نقاط"
            return f"({points:g} {unit})"
        return f"({points:g} pt{'s' if points > 1 else ''})"

    page, y = new_page()

    for q in questions:
        # ----- intitulé de la question (avec n° et barème), avec retour à la ligne
        header = f"{fmt_num(q.order)} {q.text}".strip() + f" {fmt_pts(q.points)}"
        header_lines = fonts.wrap(header, Q_SIZE, W, bold=True)

        if q.qtype == "qcm":
            n = q.num_bubbles
            choice_lines = [fonts.wrap(str(c), C_SIZE, W - 14.0)
                            for c in (q.choices or [])]
            if choice_lines:
                choices_h = sum(C_LINE_H + (len(ls) - 1) * C_CONT_H
                                for ls in choice_lines)
            else:
                choices_h = 8.5   # simple rangée de cases
            block_h = len(header_lines) * Q_LINE_H + 1.5 + choices_h
        else:
            block_h = len(header_lines) * Q_LINE_H + 1.5 + float(q.open_height_mm)

        if y + block_h > CONTENT_Y1 and (page["qcm"] or page["open"] or page["texts"]):
            page, y = new_page()

        for line in header_lines:
            y += Q_LINE_H
            page["texts"].append({
                "x": CONTENT_X1 if rtl else CONTENT_X0, "y": round(y, 2),
                "text": line, "size": Q_SIZE, "bold": True,
                "align": "right" if rtl else "left"})
        y += 1.5

        if q.qtype == "qcm":
            if choice_lines:
                bubbles = []
                for ls in choice_lines:
                    y += C_LINE_H
                    if rtl:
                        bx = CONTENT_X1 - 6.0
                        tx = CONTENT_X1 - 12.0
                    else:
                        bx = CONTENT_X0 + 6.0
                        tx = CONTENT_X0 + 12.0
                    bubbles.append([round(bx, 2), round(y - 1.3, 2)])
                    for j, line in enumerate(ls):
                        if j > 0:
                            y += C_CONT_H
                        page["texts"].append({
                            "x": tx, "y": round(y, 2), "text": line,
                            "size": C_SIZE, "bold": False,
                            "align": "right" if rtl else "left"})
                page["qcm"].append({"order": q.order, "bubbles": bubbles})
            else:
                y += 5.5
                if rtl:
                    # rangée alignée à droite, le choix أ (n° 0) à droite
                    x0 = CONTENT_X1 - 10.0
                    bubbles = [[round(x0 - k * BUBBLE_STEP, 2), round(y, 2)]
                               for k in range(n)]
                else:
                    x0 = CONTENT_X0 + 10.0
                    bubbles = [[round(x0 + k * BUBBLE_STEP, 2), round(y, 2)]
                               for k in range(n)]
                page["qcm"].append({"order": q.order, "bubbles": bubbles})
                y += 3.0
        else:
            page["open"].append({
                "order": q.order,
                "rect": [CONTENT_X0, round(y, 2), W, float(q.open_height_mm)],
            })
            y += float(q.open_height_mm)

        y += BLOCK_GAP

    return _checked({"mode": "full", "num_choices": ncho, "pages": pages})


# ---------------------------------------------------------------------------
# Épreuve écrite : page de garde de la copie. Identification (comme les
# fiches QCM), grille de la note « réservé au correcteur », puis des lignes
# pour rédiger. Seule cette page porte des repères et se scanne.
# ---------------------------------------------------------------------------

NOTE_LIGNE = 8.0       # interligne de la zone de rédaction (mm)


def _note_rows(note_sur):
    """Lignes de la grille de note : chiffres de la partie entière (selon le
    barème : 20 -> dizaines, unités), puis dixièmes et centièmes — quatre
    champs pour une note sur 20, ex. 15,75 (demande de l'utilisateur, 29
    octobre 2026). Les centièmes s'ajoutent SOUS les dixièmes : une copie
    imprimée avec trois lignes se lit toujours (ligne absente = vide = 0)."""
    n = len(str(int(note_sur)))
    entiers = ["centaines", "dizaines", "unites"][-n:] if n <= 3 else \
        ["chiffre"] * n
    return entiers + ["dixiemes", "centiemes"]


def _add_note_grid(page, y, note_sur, order):
    """Cadre « réservé au correcteur » : une ligne de cases 0..9 par chiffre
    de la note (même dessin et même lecture que la grille de n°)."""
    kinds = _note_rows(note_sur)
    x_box, x0 = CONTENT_X0 + 30.0, CONTENT_X0 + 42.0
    y_title = y + 6.0
    y_rows = y + 20.5
    rows, boxes = [], []
    for i in range(len(kinds)):
        cy = y_rows + i * ID_ROW_STEP
        rows.append([[round(x0 + k * ID_COL_STEP, 2), round(cy, 2)]
                     for k in range(10)])
        boxes.append([x_box, round(cy - 2.6, 2), 6.5, 5.2])
    h = round(y_rows - y + (len(kinds) - 1) * ID_ROW_STEP + 6.0, 2)
    page["note_grid"] = {
        "order": order, "r": ID_BUBBLE_R, "rows": rows, "write_boxes": boxes,
        "kinds": kinds, "decimales": 2, "sur": note_sur,
        "rect": [CONTENT_X0, round(y, 2), CONTENT_X1 - CONTENT_X0, h],
        "title_y": round(y_title, 2), "header_y": round(y_rows - 4.6, 2),
    }
    return y + h


def _build_ecrit_layout(quiz):
    """Page de garde d'une copie écrite, ou fiche d'évaluation orale.

    Oral : une fiche NOMINATIVE par étudiant (nom et QR imprimés, l'examinateur
    n'a rien à identifier), la grille de la note, quelques lignes pour ses
    observations ; pas de pages de rédaction."""
    oral = getattr(quiz, "type_epreuve", "qcm") == "oral"
    id_mode = "qr" if oral else getattr(quiz, "id_mode", "name")
    page = {"page_index": 0, "name_boxes": _name_boxes(id_mode),
            "qcm": [], "open": []}
    if oral:
        y = _add_qr_zone(page, 0, sticker=False)
    elif id_mode == "grid":
        y = _add_id_grid(page, 48.0, getattr(quiz, "id_digits", 6) or 6)
        y = _add_qr_corner(page, y)
    elif id_mode in ("qr", "sticker"):
        y = _add_qr_zone(page, 0, sticker=_uses_sticker(quiz))
    else:
        y = NAME_BOXES_Y + NAME_BOX_H + 6.0
    note_q = quiz.questions.filter(qtype="open").order_by("order").first()
    y = _add_note_grid(page, y + 3.0, quiz.note_sur, note_q.order if note_q else 1)
    if oral:
        page["lignes"] = {"y0": round(y + 16.0, 2), "y1": min(y + 16.0 + 6 * NOTE_LIGNE,
                                                              CONTENT_Y1),
                          "pas": NOTE_LIGNE, "titre": "observations"}
        return _checked({"mode": "oral", "num_choices": 0, "pages": [page],
                         "pages_redaction": 0})
    page["lignes"] = {"y0": round(y + 12.0, 2), "y1": CONTENT_Y1, "pas": NOTE_LIGNE}
    return _checked({"mode": "ecrit", "num_choices": 0, "pages": [page],
                     "pages_redaction": int(getattr(quiz, "pages_redaction", 1) or 0)})


def _uses_sticker(quiz):
    """Identification par étiquette autocollante (QR du candidat collé sur
    la copie) : mode « étiquette », ou concours identifié par QR."""
    if getattr(quiz, "talon_nom", False) or getattr(quiz, "place_anonyme", False):
        return False                # QR imprimé (nom sur le talon, ou n° de place)
    return (getattr(quiz, "id_mode", "name") == "sticker"
            or getattr(quiz, "auto_enroll", False))


def _add_qr_zone(page, page_index, sticker=False):
    """Réserve la zone du QR code et du bandeau nominatif (fiches
    pré-imprimées par étudiant). Retourne l'ordonnée de début du contenu.

    En mode étiquette / concours (sticker=True), aucun QR n'est imprimé : la
    zone entière est réservée à l'étiquette autocollante, et la lecture du QR
    (celui de l'étiquette collée) balaie tout cet emplacement."""
    y0 = 45.5
    if sticker:
        # Cadre dimensionné pour le QR découpé (et non pour la vignette
        # entière, dont le bloc nom/numéro reste sur la planche), avec de la
        # tolérance : l'étiquette est posée à la main et ne doit jamais
        # recouvrir la première question.
        w, h = STICKER_ZONE_W, STICKER_ZONE_H
        page["qr"] = {
            # emplacement réservé à l'étiquette (c'est là qu'est lu le QR collé)
            "rect": [CONTENT_X0, y0, w, h],
            "band": [CONTENT_X0, y0, w, h],
            "sticker": True,
        }
        return round(y0 + h + 6.0, 2)
    page["qr"] = {
        "rect": [CONTENT_X1 - 19.0, y0, 19.0, 19.0],       # QR code
        "band": [CONTENT_X0, y0, CONTENT_X1 - CONTENT_X0 - 23.0, 19.0],
    }
    return y0 + 23.0


def _add_qr_corner(page, y_after_grid):
    """Mode grille : emplacement de l'étiquette QR, à la place des cases
    NOM/PRÉNOM, en haut à droite à côté de la grille de n° d'inscription.

    L'élève colle le QR de sa planche d'étiquettes (labels_pdf, disponible
    pour toute classe) ; s'il n'en a pas, il noircit son n° dans la grille.
    Le scan lit d'abord le QR, puis la grille (services._process_sheet).

    La grille et les questions gardent exactement leur place d'avant : une
    fiche imprimée avec les cases NOM/PRÉNOM se lit toujours, même après
    régénération de la fiche. Le cadre se loge dans la bande laissée libre
    par ces cases et à droite de la grille, qui s'arrête vers x = 118 mm."""
    w, h = STICKER_ZONE_W, STICKER_ZONE_H
    x, y = CONTENT_X1 - w, NAME_BOXES_Y
    page["qr"] = {"rect": [x, y, w, h], "band": [x, y, w, h], "sticker": True}
    # libellé imprimé sous le cadre (sheet_pdf) avant la première question
    return max(y_after_grid, round(y + h + 8.0, 2))


NAME_BOXES_Y = 32.0


def _name_boxes(id_mode="name"):
    """Cases NOM/PRÉNOM manuscrites (lues par OCR en secours).

    Seulement en mode « nom manuscrit », où elles sont le seul moyen
    d'identifier la copie. Partout où il y a un code QR (étiquette collée,
    fiche nominative) ou une grille de n°, la feuille de réponses ne porte
    que lui : demande de l'utilisateur, 11 octobre 2026. Grille, QCM et
    emplacement du QR ne bougent pas : les fiches déjà imprimées, avec leurs
    cases, se lisent toujours."""
    if id_mode in ("grid", "sticker", "qr"):
        return {}
    w = (CONTENT_X1 - CONTENT_X0 - 8.0) / 2
    y = NAME_BOXES_Y
    return {
        "last": [CONTENT_X0 + 14.0, y, w - 14.0, NAME_BOX_H],
        "first": [CONTENT_X0 + w + 8.0 + 18.0, y, w - 18.0, NAME_BOX_H],
    }
