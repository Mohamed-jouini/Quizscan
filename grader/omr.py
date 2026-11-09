"""Moteur de lecture des fiches scannées : recalage ArUco, lecture des
cases (OMR), OCR du nom et rapprochement avec la liste de classe."""
import functools
import logging
import re
import threading
import unicodedata

import cv2
import numpy as np
import pytesseract
import qrcode
from rapidfuzz import fuzz, utils as rf_utils

from . import layout as L

log = logging.getLogger(__name__)

# Dictionnaire des repères : voir layout.ARUCO_DICT_ID (les 50 premiers
# marqueurs sont identiques à DICT_4X4_50, les fiches déjà imprimées par une
# version antérieure restent donc lisibles).
ARUCO_DICT = cv2.aruco.getPredefinedDictionary(L.ARUCO_DICT_ID)

CANON_W = int(L.PAGE_W * L.PX_PER_MM)
CANON_H = int(L.PAGE_H * L.PX_PER_MM)

FILL_THRESHOLD = 0.30      # proportion de pixels sombres pour "case cochée"
MATCH_THRESHOLD = 55       # score rapidfuzz minimal pour associer un étudiant


class MarkerError(Exception):
    pass


def _mm(v):
    return int(round(v * L.PX_PER_MM))


def page_blanche(image_bgr):
    """Verso vide d'une feuille imprimée d'un seul côté, passée au scanner en recto-verso.
    Bords exclus (ombre du chargeur). Seuil de gris bas : le recto vu par transparence est
    gris clair. Une page imprimée dépasse largement cette part d'encre, ne serait-ce que
    par son cadre, ou par la moitié noire d'un QR collé."""
    h, w = image_bgr.shape[:2]
    gris = cv2.cvtColor(image_bgr[h // 30:h - h // 30, w // 30:w - w // 30], cv2.COLOR_BGR2GRAY)
    return np.count_nonzero(gris < 110) < 0.002 * gris.size


# ---------------------------------------------------------------- recalage

def detect_and_warp(image_bgr):
    """Recale le scan dans le repère canonique et identifie la page.
    Retourne (image_recalée, page_index).

    Deux générations de fiches : les nouvelles ont un cadre fin et un code de
    page en haut (voir layout.CADRE_MARGE), cherchés d'abord ; les copies
    d'avant le 29 octobre 2026, quatre repères ArUco dans les coins. Les
    chercher en premier coûtait 0,2 s à chaque copie nouvelle, qui n'en a pas ;
    une copie ancienne n'a ni cadre ni code de page, elle passe aux repères."""
    resultat = _caler_par_cadre(image_bgr)
    if resultat is not None:
        return resultat
    # Impression pâle (toner faible) : le noir imprimé reste gris clair, au-dessus des
    # seuils du code de page et du trait ; contraste étiré, on recommence (9 novembre 2026).
    contraste = _etirer_contraste(image_bgr)
    if contraste is not None:
        resultat = _caler_par_cadre(contraste)
        if resultat is not None:
            return resultat
    try:
        return _caler_aruco(image_bgr)
    except MarkerError:
        raise MarkerError("Cadre de la page non détecté (page mal scannée, "
                          "coupée ou sans cadre ?)") from None


def _etirer_contraste(image_bgr):
    """La page au contraste étiré, son encre la plus sombre ramenée au noir ; None si
    l'encre est déjà assez noire (l'échec du cadre a une autre cause, inutile de
    recommencer) ou s'il n'y a presque rien d'imprimé (page blanche)."""
    gris = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    noir, papier = np.percentile(gris[::4, ::4], (0.3, 60))
    if noir < 100 or papier - noir < 25:
        return None
    lut = np.clip((np.arange(256) - noir) * 255.0 / (papier - noir), 0, 255).astype(np.uint8)
    return cv2.LUT(image_bgr, lut)


# Cadre : bord extérieur et bord intérieur du trait (le contour trouvé peut
# être l'un ou l'autre) ; la bande de contrôle est l'axe du trait.
_CADRE_BORDS = (L.CADRE_MARGE - L.CADRE_TRAIT / 2, L.CADRE_MARGE + L.CADRE_TRAIT / 2)


def _ordre_horaire(points):
    """Quatre points dans l'ordre des aiguilles d'une montre (à l'écran)."""
    centre = points.mean(axis=0)
    angles = np.arctan2(points[:, 1] - centre[1], points[:, 0] - centre[0])
    return points[np.argsort(angles)]


def _cadres_candidats(gray):
    """Quadrilatères assez grands pour être le cadre, du plus grand au plus
    petit."""
    h, w = gray.shape
    flou = cv2.GaussianBlur(gray, (5, 5), 0)
    encre = cv2.adaptiveThreshold(flou, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                  cv2.THRESH_BINARY_INV, 51, 15)
    encre = cv2.morphologyEx(encre, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours, _ = cv2.findContours(encre, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    quads = []
    for contour in contours:
        aire = cv2.contourArea(contour)
        if aire < 0.25 * h * w:
            continue
        approx = cv2.approxPolyDP(contour, 0.02 * cv2.arcLength(contour, True), True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            quads.append((aire, _ordre_horaire(approx.reshape(4, 2).astype(np.float32))))
    quads.sort(key=lambda q: -q[0])
    return [q for _, q in quads]


def _gris_moyen(gray_canon, rect_mm, marge=0.2):
    x, y, w, h = rect_mm
    x0, y0 = _mm(x + w * marge), _mm(y + h * marge)
    x1, y1 = _mm(x + w * (1 - marge)), _mm(y + h * (1 - marge))
    zone = gray_canon[max(y0, 0):y1, max(x0, 0):x1]
    return float(zone.mean()) if zone.size else 255.0


def _score_trait(gray_canon, haut=L.CADRE_MARGE):
    """Part des points de l'axe du cadre qui sont de l'encre (recalage juste).
    haut : axe du haut du cadre (plus bas sur une copie à talon)."""
    m = L.CADRE_MARGE
    pts = ([(m, y) for y in np.linspace(haut + 5, L.PAGE_H - m - 5, 40)]
           + [(L.PAGE_W - m, y) for y in np.linspace(haut + 5, L.PAGE_H - m - 5, 40)]
           + [(x, haut) for x in np.linspace(m + 5, L.PAGE_W - m - 5, 30)]
           + [(x, L.PAGE_H - m) for x in np.linspace(m + 5, L.PAGE_W - m - 5, 30)])
    sombres = sum(1 for x, y in pts
                  if 0 <= _mm(y) < CANON_H and 0 <= _mm(x) < CANON_W
                  and gray_canon[_mm(y), _mm(x)] < 140)
    return sombres / len(pts)


def _bas_du_code(haut):
    """Lignes de l'image redressée qui contiennent le code de page et le 9e tiret
    (avec la marge que lit _gris_moyen) : la bande à redresser pour les lire."""
    cases = L.code_cases(haut) + [L.case_variante(haut)]
    return min(_mm(max(y + h for _, y, _, h in cases)) + 2, CANON_H)


def _caler_par_cadre(image_bgr):
    """Recalage par le cadre fin. Essaie chaque quadrilatère candidat dans ses
    quatre orientations (page à l'endroit, à l'envers, tournée) et pour les
    deux hauteurs de cadre (copie normale, copie à talon — découpé ou non) :
    retient celle dont le code de page est valide, dont le 9e tiret dit le
    même gabarit, et dont le trait tombe sur l'axe du cadre.
    Retourne (image_recalée, page_index) ou None."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    for quad in _cadres_candidats(gray)[:6]:
        meilleur = None
        for k in range(4):
            pts = np.roll(quad, -k, axis=0)          # haut-gauche, haut-droit, ...
            largeur = np.linalg.norm(pts[1] - pts[0])
            hauteur = np.linalg.norm(pts[3] - pts[0])
            if hauteur < largeur:                    # portrait attendu
                continue
            for haut in L.CADRES_HAUTS:
                for bord in _CADRE_BORDS:
                    ecart = bord - L.CADRE_MARGE          # bord extérieur / intérieur
                    dst = np.array([[_mm(bord), _mm(haut + ecart)],
                                    [_mm(L.PAGE_W - bord), _mm(haut + ecart)],
                                    [_mm(L.PAGE_W - bord), _mm(L.PAGE_H - bord)],
                                    [_mm(bord), _mm(L.PAGE_H - bord)]], dtype=np.float32)
                    H = cv2.getPerspectiveTransform(pts.astype(np.float32), dst)
                    # Le code de page et le 9e tiret sont en haut : on ne
                    # redresse que cette bande pour les lire (les mêmes lignes,
                    # au pixel près, que la page entière redressée), la page
                    # entière seulement pour l'hypothèse qui les a valides.
                    # Huit pages entières par copie coûtaient 0,2 s.
                    bande = cv2.warpPerspective(gray, H, (CANON_W, _bas_du_code(haut)),
                                                borderValue=255)
                    page = L.lire_code_page([_gris_moyen(bande, r) < 128
                                             for r in L.code_cases(haut)])
                    if page is None:
                        continue
                    talon = _gris_moyen(bande, L.case_variante(haut)) < 128
                    if talon != (haut != L.CADRE_MARGE):
                        continue                          # pas ce gabarit
                    canon = cv2.warpPerspective(gray, H, (CANON_W, CANON_H),
                                                borderValue=255)
                    score = _score_trait(canon, haut)
                    if score >= 0.6 and (meilleur is None or score > meilleur[0]):
                        meilleur = (score, H, page)
        if meilleur:
            _, H, page = meilleur
            warped = cv2.warpPerspective(image_bgr, H, (CANON_W, CANON_H),
                                         flags=cv2.INTER_CUBIC,
                                         borderMode=cv2.BORDER_CONSTANT,
                                         borderValue=(255, 255, 255))
            return warped, page
    return None


def _caler_aruco(image_bgr):
    """Recalage par les quatre repères ArUco des copies d'avant le cadre."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    params = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(ARUCO_DICT, params)
    corners, ids, _ = detector.detectMarkers(gray)
    if ids is None or len(ids) < 3:
        # nouvelle tentative sur image améliorée
        eq = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8)).apply(gray)
        corners, ids, _ = detector.detectMarkers(eq)
    if ids is None or len(ids) < 3:
        raise MarkerError("Repères de calibration non détectés (page mal scannée ?)")

    ids = ids.flatten().tolist()
    # page = majorité des id // nombre de repères par page
    votes = {}
    for i in ids:
        p = i // L.MARKERS_PER_PAGE
        votes[p] = votes.get(p, 0) + 1
    page_index = max(votes, key=votes.get)

    canon = L.marker_positions(page_index)
    src_pts, dst_pts = [], []
    for det_corners, mid in zip(corners, ids):
        if mid not in canon:
            continue
        x, y = canon[mid]
        s = L.MARKER_SIZE
        # ordre ArUco : haut-gauche, haut-droite, bas-droite, bas-gauche
        dst = [(x, y), (x + s, y), (x + s, y + s), (x, y + s)]
        for (cx, cy), (dx, dy) in zip(det_corners.reshape(4, 2), dst):
            src_pts.append([cx, cy])
            dst_pts.append([_mm(dx), _mm(dy)])
    if len(src_pts) < 12:
        raise MarkerError("Repères insuffisants pour recaler la page")

    H, _ = cv2.findHomography(np.array(src_pts, dtype=np.float32),
                              np.array(dst_pts, dtype=np.float32), cv2.RANSAC, 5.0)
    if H is None:
        raise MarkerError("Échec du recalage de la page")
    warped = cv2.warpPerspective(image_bgr, H, (CANON_W, CANON_H),
                                 flags=cv2.INTER_CUBIC,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    return warped, page_index


# ---------------------------------------------------------------- lecture QCM

def _encre_disque(binary, x, y, r):
    """(pixels d'encre, pixels) du disque de centre (x, y) et de rayon r.

    Compté dans un petit carré autour de la case et non sur toute la page : un
    masque de la taille de la page par case coûtait une seconde par copie de 30
    questions. Même résultat au pixel près : le disque est tracé de la même
    façon, simplement décalé, et le carré s'arrête aux mêmes bords de l'image."""
    h, w = binary.shape
    x0, y0 = max(x - r - 2, 0), max(y - r - 2, 0)
    x1, y1 = min(x + r + 3, w), min(y + r + 3, h)
    if x0 >= x1 or y0 >= y1:
        return 0, 0
    zone = binary[y0:y1, x0:x1]
    masque = np.zeros_like(zone)
    cv2.circle(masque, (x - x0, y - y0), r, 255, -1)
    return cv2.countNonZero(cv2.bitwise_and(zone, masque)), cv2.countNonZero(masque)


def read_bubbles(warped_bgr, page_layout):
    """Lit toutes les cases QCM de la page.
    Retourne {order: {"choice": int|None, "multiple": bool, "ratios": [...]}}"""
    gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    # binarisation locale : robuste aux variations d'éclairage du scanner
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY_INV, 41, 12)
    r = _mm(L.BUBBLE_R * 0.78)   # on échantillonne l'intérieur de la case
    results = {}
    for item in page_layout["qcm"]:
        ratios = []
        for cx, cy in item["bubbles"]:
            filled, area = _encre_disque(binary, _mm(cx), _mm(cy), r)
            ratios.append(round(filled / max(area, 1), 3))
        marked = [i for i, v in enumerate(ratios) if v >= FILL_THRESHOLD]
        choice, multiple = None, False
        if len(marked) == 1:
            choice = marked[0]
        elif len(marked) > 1:
            top = sorted(marked, key=lambda i: -ratios[i])
            # si une case est nettement plus noircie que les autres, on la garde
            if ratios[top[0]] >= 1.8 * ratios[top[1]]:
                choice = top[0]
            else:
                multiple = True
                choice = top[0]
        results[item["order"]] = {"choice": choice, "multiple": multiple,
                                  "ratios": ratios}
    return results


def _binaire(warped_bgr):
    """Encre en blanc sur fond noir, seuil local (ombres et papier gris tolérés)."""
    gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                 cv2.THRESH_BINARY_INV, 41, 12)


def _lire_chiffres(warped_bgr, grid):
    """Lit une grille de chiffres (une ligne par position, colonnes 0..9) :
    grille du n° d'inscription ou grille de la note (écrit).
    Retourne une liste, un élément par ligne : le chiffre (int), None si la
    ligne est vide, "?" si plusieurs cases sont noircies sans net gagnant."""
    binary = _binaire(warped_bgr)
    r = _mm(grid["r"] * 0.75)
    lus = []
    for row in grid["rows"]:
        ratios = []
        for cx, cy in row:
            filled, area = _encre_disque(binary, _mm(cx), _mm(cy), r)
            ratios.append(filled / max(area, 1))
        marked = [i for i, v in enumerate(ratios) if v >= FILL_THRESHOLD]
        if len(marked) == 1:
            lus.append(marked[0])
        elif len(marked) > 1:
            top = sorted(marked, key=lambda i: -ratios[i])
            lus.append(top[0] if ratios[top[0]] >= 1.8 * ratios[top[1]] else "?")
        else:
            lus.append(None)
    return lus


def read_id_grid(warped_bgr, page_layout):
    """Lit la grille du numéro d'inscription.
    Retourne la chaîne du numéro, ou "" si une ligne est illisible/ambiguë."""
    grid = page_layout.get("id_grid")
    if not grid:
        return ""
    lus = _lire_chiffres(warped_bgr, grid)
    if any(d is None or d == "?" for d in lus):
        return ""
    return "".join(str(d) for d in lus)


def lire_note(warped_bgr, grille, note_sur):
    """Note noircie par le correcteur dans la grille de la page de garde
    (épreuve écrite, fiche d'oral) : une ligne par chiffre — dizaines, unités,
    dixièmes, centièmes (« decimales » de la grille : 2 ; 1 sur les premières copies).

    Chaque ligne doit avoir sa case noircie, 0 compris (« 8,5 » : 0 · 8 · 5 · 0). Une
    ligne vide n'est pas lue 0 : la note est à vérifier (demande de l'utilisateur, 8
    novembre 2026, après une vraie fiche d'oral où le « 1 » des dizaines, écrit mais pas
    noirci, donnait 9 au lieu de 19). Une ligne ambiguë (plusieurs cases) ou une note
    au-dessus du barème aussi : None, la note reste à saisir à l'écran.
    Retourne (note | None, chiffres lus : un chiffre, ou « ? » pour une ligne à vérifier)."""
    lus = ["?" if d is None else d for d in _lire_chiffres(warped_bgr, grille)]
    if "?" in lus:
        return None, lus
    chiffres = lus
    decimales = grille.get("decimales", 1)
    entier = int("".join(str(d) for d in chiffres[:len(chiffres) - decimales]) or 0)
    fraction = chiffres[len(chiffres) - decimales:]
    note = entier + sum(d / 10 ** (k + 1) for k, d in enumerate(fraction))
    note = round(note, decimales)
    if note > note_sur:
        return None, lus
    return note, lus


def dessiner_note(image, grille, lus, note, note_sur):
    """Sur l'image de contrôle : cases de la note retenues (vert), ligne
    ambiguë (orange), et la note lue écrite à côté de la grille."""
    r = _mm(grille["r"]) + 3
    for row, d in zip(grille["rows"], lus):
        if d is None:
            continue
        if d == "?":
            cx, cy = row[0]
            cv2.rectangle(image, (_mm(cx) - r, _mm(cy) - r),
                          (_mm(row[-1][0]) + r, _mm(cy) + r), (0, 140, 255), 3)
            continue
        cx, cy = row[d]
        cv2.circle(image, (_mm(cx), _mm(cy)), r, (40, 170, 60), 4)
    x, y, w, h = grille["rect"]
    texte = (f"Note lue : {note:g} / {note_sur:g}" if note is not None
             else "Note illisible : a saisir")
    couleur = (40, 150, 60) if note is not None else (0, 110, 230)
    cv2.putText(image, texte, (_mm(x + 2), _mm(y + h + 7)),
                cv2.FONT_HERSHEY_SIMPLEX, 1.4, couleur, 3, cv2.LINE_AA)
    return image


def _qr_variants(crop):
    """Versions successives d'un recadrage à présenter au décodeur.

    Le décodeur d'OpenCV échoue sur des QR pourtant nets : le passage par
    une image strictement noir et blanc (Otsu) agrandie d'un facteur entier
    (plus proche voisin, qui garde les modules carrés et nets) rattrape ces
    échecs. On va du moins cher au plus cher — dès qu'une variante décode,
    les suivantes ne sont pas calculées."""
    yield crop
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255,
                              cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield cv2.cvtColor(cv2.resize(binary, None, fx=2, fy=2,
                                  interpolation=cv2.INTER_NEAREST),
                       cv2.COLOR_GRAY2BGR)
    yield cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
    yield cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)


def _decode_qr(detector, img):
    """Charge utile « QS|<id>|<valeur> » lue dans l'image, ou None."""
    try:
        data, _, _ = detector.detectAndDecode(img)
    except cv2.error:
        return None
    return _charge_utile(data)


# ------------------------------------------------- QR abîmés (9 novembre 2026)
# Le décodeur WeChat d'OpenCV-contrib (dérivé de ZXing), employé sans ses modèles :
# bien plus tolérant que QRCodeDetector aux taches, traits de stylo, effacements et
# froissures (banc d'essai : 120 -> 153 lectures sur 201 copies abîmées, avec les QR
# imprimés en correction H). Un décodeur par fil de lecture.
_LOCAL = threading.local()


def _decode_wechat(img):
    if not hasattr(cv2, "wechat_qrcode_WeChatQRCode"):
        return None
    detecteur = getattr(_LOCAL, "wechat", None)
    if detecteur is None:
        detecteur = _LOCAL.wechat = cv2.wechat_qrcode_WeChatQRCode()
    try:
        textes, _ = detecteur.detectAndDecode(img)
    except cv2.error:
        return None
    for texte in textes:
        trouve = _charge_utile(texte)
        if trouve:
            return trouve
    return None


def _variantes_tolerantes(crop):
    """Pour le décodeur WeChat : gris, contraste rehaussé, agrandi, seuil aux trous
    bouchés. Les quatre qui lisaient le plus de QR abîmés, sur 7 essayées (70 ms
    chacune quand elle échoue) ; la relecture par la grille fait le reste."""
    gris = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    yield gris
    yield cv2.createCLAHE(clipLimit=3.0, tileGridSize=(4, 4)).apply(gris)
    yield cv2.resize(gris, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    _, otsu = cv2.threshold(cv2.GaussianBlur(gris, (3, 3), 0), 0, 255,
                            cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    yield cv2.morphologyEx(otsu, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))


def _zone_encree(crop):
    """Il y a quelque chose dans la zone (QR imprimé, étiquette collée) : sinon les
    essais coûteux ne servent à rien (fiche à grille dont l'élève a noirci son n°
    au lieu de coller son étiquette)."""
    gris = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return np.count_nonzero(gris < 128) > 0.05 * gris.size


@functools.lru_cache(maxsize=8)
def _gabarit_qr(version):
    """Ce que l'on sait d'un QR de cette version sans le lire : ses modules fixes
    communs (motifs de position, d'alignement, de synchronisation : -1 = module de
    données ou de format, 0 blanc, 1 noir), les modules de l'information de format,
    et, pour chaque correction (H, M) et chaque masque, la grille de ses modules fixes.
    Construit par la bibliothèque qui imprime les QR."""
    combinaisons = []
    for correction in (qrcode.constants.ERROR_CORRECT_H, qrcode.constants.ERROR_CORRECT_M):
        for masque in range(8):
            qr = qrcode.QRCode(version=version, error_correction=correction, border=0)
            n = qr.modules_count = 4 * version + 17
            qr.modules = [[None] * n for _ in range(n)]
            qr.setup_position_probe_pattern(0, 0)
            qr.setup_position_probe_pattern(n - 7, 0)
            qr.setup_position_probe_pattern(0, n - 7)
            qr.setup_position_adjust_pattern()
            qr.setup_timing_pattern()
            qr.setup_type_info(False, masque)
            grille = np.array([[-1 if m is None else int(m) for m in ligne] for ligne in qr.modules])
            combinaisons.append(grille)
    pile = np.stack(combinaisons)
    fixes = (pile >= 0).all(axis=0)
    communs = np.where(fixes & (pile == pile[0]).all(axis=0), pile[0], -1)
    format_ = fixes & (communs < 0)
    return communs, format_, combinaisons


def _qr_par_la_grille(warped_bgr, rect):
    """Dernier recours pour un QR IMPRIMÉ (jamais une étiquette, posée à la main) :
    sa place et sa taille sont connues au dixième de millimètre (l'image du QR, marge
    d'un module comprise, remplit sa zone). Chaque module est relu à sa place ; la
    version est celle dont les motifs fixes collent le mieux, la correction et le
    masque ceux que dit l'information de format lue ; un QR propre est redessiné
    (motifs fixes tracés à neuf) et donné au décodeur, dont la correction d'erreur
    fait le reste. Lit un QR dont un coin est taché ou effacé, que les détecteurs ne
    savent plus situer. Huit essais au plus (0,1 s), et non plus 48 (8 s)."""
    x, y, w, h = rect
    gris = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    lectures = []
    for version in (1, 2, 3):
        n = 4 * version + 17
        pas = w / (n + 2)                                  # mm par module
        rayon = max(1, int(pas * L.PX_PER_MM * 0.25))
        moyennes = cv2.blur(gris, (2 * rayon + 1, 2 * rayon + 1))
        ys = np.clip([_mm(y + (r + 1.5) * pas) for r in range(n)], 0, gris.shape[0] - 1)
        xs = np.clip([_mm(x + (c + 1.5) * pas) for c in range(n)], 0, gris.shape[1] - 1)
        valeurs = moyennes[np.ix_(ys, xs)]
        seuil, _ = cv2.threshold(valeurs.reshape(-1, 1), 0, 255,
                                 cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        sombres = (valeurs < seuil).astype(np.int64)
        communs, format_, combinaisons = _gabarit_qr(version)
        accord = float((sombres == communs)[communs >= 0].mean())
        lectures.append((accord, version, sombres))
    lectures.sort(key=lambda lu: -lu[0])
    cote = 6                                               # px par module redessiné
    for _, version, sombres in lectures[:2]:
        communs, format_, combinaisons = _gabarit_qr(version)
        classees = sorted(combinaisons, key=lambda g: -int((sombres == g)[format_].sum()))
        for grille in classees[:4]:
            noirs = np.where(grille >= 0, grille, sombres).astype(np.uint8)
            image = np.kron(1 - noirs, np.ones((cote, cote), np.uint8)) * 255
            image = cv2.copyMakeBorder(image, 4 * cote, 4 * cote, 4 * cote, 4 * cote,
                                       cv2.BORDER_CONSTANT, value=255)
            # image propre : WeChat la lit seul ; QRCodeDetector (30 ms) s'il manque
            trouve = (_decode_wechat(image) if hasattr(cv2, "wechat_qrcode_WeChatQRCode")
                      else _decode_qr(cv2.QRCodeDetector(), image))
            if trouve:
                return trouve
    return None


def _charge_utile(data):
    """Le contenu d'un QR de QuizScan : (« QS » | « QSA », id, valeur), (« QSI »,
    None, n° d'inscription), ou None pour tout autre texte."""
    if not data:
        return None
    parts = data.split("|")
    # carte d'inscription : « QSI|<n°>|<NOM>|<Prénom> » — le n° identifie
    if len(parts) >= 2 and parts[0] == "QSI" and parts[1].strip():
        numero = "".join(parts[1].split())
        # un n° d'inscription : lettres, chiffres, tiret ; rien d'autre ne
        # doit entrer dans la base (export Excel, pages) depuis un QR collé
        if len(numero) <= 40 and all(c.isalnum() or c in "-_/." for c in numero):
            return "QSI", None, numero
        return None
    # longueur bornée : un QR collé par un candidat ne doit pas faire entrer dans
    # la base un numéro qu'elle refuse (une telle page bloquerait son lot)
    if (len(parts) == 3 and parts[0] in ("QS", "QSA")
            and parts[1].isdigit() and parts[2].isdigit()
            and len(parts[1]) <= 12 and len(parts[2]) <= 12):
        return parts[0], int(parts[1]), int(parts[2])
    return None


def read_qr(warped_bgr, page_layout):
    """Décode le QR code d'une fiche (QR imprimé ou étiquette collée).
    Retourne (kind, quiz_id, valeur) : kind "QS" = fiche nominative
    (valeur = id étudiant), "QSA" = fiche anonyme de concours
    (valeur = n° de série), "QSI" = carte d'inscription (valeur = n°
    d'inscription, texte) ; (None, None, None) si aucun QR lisible."""
    zone = page_layout.get("qr")
    detector = cv2.QRCodeDetector()
    if zone:
        x, y, w, h = zone["rect"]
        pad = 3.0
        crop = warped_bgr[max(_mm(y - pad), 0):_mm(y + h + pad),
                          max(_mm(x - pad), 0):_mm(x + w + pad)]
        if crop.size:
            for img in _qr_variants(crop):
                found = _decode_qr(detector, img)
                if found:
                    return found
            # L'autre détecteur d'OpenCV : scanner réglé en noir et blanc à 300 ppp, le
            # premier ratait jusqu'à 2 étiquettes sur 5, celui-ci les lit toutes (20 ms).
            found = _decode_qr(cv2.QRCodeDetectorAruco(), crop)
            if found:
                return found
            # QR abîmé (tache, trait de stylo, morceau effacé, feuille froissée, scan
            # pâle) : le décodeur WeChat, puis, pour un QR imprimé, la relecture module
            # par module. Seulement s'il y a de l'encre dans la zone.
            if _zone_encree(crop):
                for img in _variantes_tolerantes(crop):
                    found = _decode_wechat(img)
                    if found:
                        return found
                if not zone.get("sticker"):
                    found = _qr_par_la_grille(warped_bgr, zone["rect"])
                    if found:
                        return found
    # secours : l'étiquette a pu être collée de travers, on balaie la page
    found = _decode_qr(detector, warped_bgr)
    return found or (None, None, None)


def match_student_by_id(id_string, students):
    """Associe un numéro lu dans la grille à un étudiant (comparaison exacte,
    zéros de tête ignorés)."""
    if not id_string:
        return None
    normalized = id_string.lstrip("0") or "0"
    for s in students:
        num = (s.student_number or "").strip()
        if not num:
            continue
        if num == id_string or (num.lstrip("0") or "0") == normalized:
            return s
    return None


# ---------------------------------------------------------------- OCR du nom

def _crop_rect(img, rect, inset_mm=1.6):
    x, y, w, h = rect
    x0, y0 = _mm(x + inset_mm), _mm(y + inset_mm)
    x1, y1 = _mm(x + w - inset_mm), _mm(y + h - inset_mm)
    return img[max(y0, 0):y1, max(x0, 0):x1]


class _TesseractAbsent(Exception):
    pass


def _variantes_image(crop):
    """Plusieurs mises au propre d'une case NOM ou PRÉNOM manuscrite.

    Tesseract est fait pour l'imprimé : sur une écriture à la main, une
    seule binarisation lit souvent n'importe quoi (« DIWOSACCU SATLH » pour
    « BEN SALEN SAMIA »). On lui donne trois versions de la même case et on
    garde, plus loin, celle qui ressemble le mieux à un étudiant de la classe.
      1. agrandie, adoucie, seuil d'Otsu (la lecture historique) ;
      2. agrandie davantage, traits du cadre effacés, écriture épaissie,
         marge blanche — un trait de stylo fin devient lisible ;
      3. seuil adaptatif (éclairage ou encre inégaux), écriture épaissie."""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    v1 = cv2.GaussianBlur(cv2.resize(gray, None, fx=2.0, fy=2.0,
                                     interpolation=cv2.INTER_CUBIC), (3, 3), 0)
    _, v1 = cv2.threshold(v1, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    g3 = cv2.resize(gray, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
    _, encre = cv2.threshold(g3, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = encre.shape
    # traits longs = bords de la case, pas l'écriture
    horizontaux = cv2.getStructuringElement(cv2.MORPH_RECT, (max(w // 3, 1), 1))
    verticaux = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(h * 2 // 3, 1)))
    lignes = (cv2.morphologyEx(encre, cv2.MORPH_OPEN, horizontaux)
              | cv2.morphologyEx(encre, cv2.MORPH_OPEN, verticaux))
    encre = cv2.dilate(cv2.subtract(encre, lignes), np.ones((3, 3), np.uint8))
    v2 = cv2.copyMakeBorder(255 - encre, 24, 24, 24, 24,
                            cv2.BORDER_CONSTANT, value=255)

    g2 = cv2.resize(gray, None, fx=2.5, fy=2.5, interpolation=cv2.INTER_CUBIC)
    adapt = cv2.adaptiveThreshold(g2, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                  cv2.THRESH_BINARY, 41, 15)
    adapt = cv2.erode(adapt, np.ones((2, 2), np.uint8))
    v3 = cv2.copyMakeBorder(adapt, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    return [v1, v2, v3]


def _ocr_crop(crop, ocr_lang, config):
    """OCR d'un recadrage (première variante). Voir _ocr_image."""
    try:
        return _ocr_image(_variantes_image(crop)[0], ocr_lang, config)
    except _TesseractAbsent:
        return ""


def _ocr_image(th, ocr_lang, config):
    """OCR d'une image déjà binarisée. Retourne "" plutôt que de lever :
    l'absence de Tesseract sur le serveur ne doit pas faire perdre la lecture
    de la copie (elle est alors simplement « à identifier »). Lève
    _TesseractAbsent pour que l'appelant n'insiste pas sur les variantes."""
    try:
        txt = pytesseract.image_to_string(th, lang=ocr_lang, config=config)
    except pytesseract.TesseractNotFoundError:
        log.warning("Tesseract introuvable : OCR du nom désactivé "
                    "(installez tesseract-ocr, tesseract-ocr-fra et "
                    "tesseract-ocr-ara). Les copies non identifiées par QR "
                    "ou par grille de n° restent à affecter à la main.")
        raise _TesseractAbsent()
    except Exception as exc:  # noqa: BLE001 — paquet de langue absent, image illisible…
        log.warning("OCR du nom impossible (%s: %s)", type(exc).__name__, exc)
        return ""
    return re.sub(r"\s+", " ", txt).strip()


def _reglages_ocr(language):
    if language == "ar":
        return "ara+fra", "--psm 7"
    return "fra+eng", ("--psm 7 -c tessedit_char_whitelist="
                       "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                       "ÀÂÄÇÉÈÊËÎÏÔÖÙÛÜàâäçéèêëîïôöùûü-")


def lire_nom(warped_bgr, page_layout, language="fr"):
    """Lectures de la zone NOM / PRÉNOM, une par variante d'image.

    Retourne (lectures, image_recadrée) ; lectures = [(nom, prénom), …], une
    par variante (voir _variantes_image), sans doublon ni lecture vide.
    Tesseract absent : liste vide, la copie reste « à identifier »."""
    nb = page_layout.get("name_boxes")
    vide = np.full((10, 10, 3), 255, dtype=np.uint8)
    if not nb:
        return [], vide
    ocr_lang, config = _reglages_ocr(language)
    crops, colonnes = [], []
    try:
        for key in ("last", "first"):
            crop = _crop_rect(warped_bgr, nb[key])
            if not crop.size:                  # zone hors de l'image recalée
                crops.append(vide)
                colonnes.append(["", "", ""])
                continue
            crops.append(crop)
            colonnes.append([_ocr_image(v, ocr_lang, config)
                             for v in _variantes_image(crop)])
    except _TesseractAbsent:
        colonnes = []
    lectures = []
    if len(colonnes) == 2:
        for nom, prenom in zip(*colonnes):
            if (nom or prenom) and (nom, prenom) not in lectures:
                lectures.append((nom, prenom))
    while len(crops) < 2:
        crops.append(vide)
    h = max(c.shape[0] for c in crops)
    sep = np.full((h, 12, 3), 255, dtype=np.uint8)
    padded = [cv2.copyMakeBorder(c, 0, h - c.shape[0], 0, 0,
                                 cv2.BORDER_CONSTANT, value=(255, 255, 255))
              for c in crops]
    return lectures, cv2.hconcat([padded[0], sep, padded[1]])


def read_name(warped_bgr, page_layout, language="fr"):
    """OCR de la zone NOM / PRÉNOM.
    Retourne (texte_complet, image_recadrée, (texte_nom, texte_prénom)).

    language "ar" : OCR arabe (nécessite le paquet tesseract-ocr-ara).

    L'OCR n'est qu'un secours d'identification : s'il échoue (Tesseract ou
    son paquet de langue absent du serveur, image illisible…), on rend un
    texte vide et le recadrage du nom. La copie reste lue et notée, et
    s'affecte en deux clics dans l'écran de vérification — une copie n'est
    jamais perdue à cause de l'OCR."""
    if language == "ar":
        ocr_lang, config = "ara+fra", "--psm 7"
    else:
        ocr_lang = "fra+eng"
        config = ("--psm 7 -c tessedit_char_whitelist="
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                  "ÀÂÄÇÉÈÊËÎÏÔÖÙÛÜàâäçéèêëîïôöùûü-")
    nb = page_layout.get("name_boxes")
    if not nb:
        # fiche sans zone nom/prénom imprimée : rien à lire
        return "", np.full((10, 10, 3), 255, dtype=np.uint8), ("", "")
    crops = []
    texts = []
    for key in ("last", "first"):
        crop = _crop_rect(warped_bgr, nb[key])
        if not crop.size:                      # zone hors de l'image recalée
            crops.append(np.full((10, 10, 3), 255, dtype=np.uint8))
            texts.append("")
            continue
        crops.append(crop)
        texts.append(_ocr_crop(crop, ocr_lang, config))
    h = max(c.shape[0] for c in crops)
    sep = np.full((h, 12, 3), 255, dtype=np.uint8)
    padded = [cv2.copyMakeBorder(c, 0, h - c.shape[0], 0, 0,
                                 cv2.BORDER_CONSTANT, value=(255, 255, 255))
              for c in crops]
    combined = cv2.hconcat([padded[0], sep, padded[1]])
    return (" ".join(t for t in texts if t).strip(), combined,
            (texts[0], texts[1]))


_AR_NORM = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
                          "ة": "ه", "ى": "ي", "ـ": ""})


def _normalize_arabic(text):
    text = text.translate(_AR_NORM)
    # suppression des diacritiques (harakat)
    return re.sub(r"[ً-ٰٟ]", "", text)


def _forme(texte):
    """Forme comparable d'un nom : arabe normalisé, sans accents, sans
    casse ni ponctuation. Sans cela, rapidfuzz (≥ 3) compte « SAMIA » et
    « Samia » comme différents : une lecture PARFAITE en capitales ne
    faisait que 73 % de ressemblance avec la fiche de l'étudiant."""
    texte = unicodedata.normalize("NFKD", _normalize_arabic(texte or ""))
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return rf_utils.default_process(texte)


def _ressemblance(lecture, etudiant):
    """Meilleure ressemblance d'une lecture (nom, prénom) avec un étudiant :
    nom complet dans un ordre ou l'autre, ou case par case."""
    nom, prenom = (_forme(t) for t in lecture)
    nom_e, prenom_e = _forme(etudiant.last_name), _forme(etudiant.first_name)
    lu = f"{nom} {prenom}".strip()
    if not lu:
        return 0.0
    scores = [fuzz.token_sort_ratio(lu, f"{nom_e} {prenom_e}")]
    if nom and prenom:
        scores.append((fuzz.ratio(nom, nom_e) + fuzz.ratio(prenom, prenom_e)) / 2)
    return float(max(scores))


# Acceptation d'un rapprochement : au-dessus de MATCH_THRESHOLD, ou nettement
# devant le deuxième de la classe (la liste est fermée : un nom mal lu mais
# qui ne ressemble qu'à un seul élève désigne cet élève).
SEUIL_ECART = 45.0
ECART_MIN = 15.0


def identifier(lectures, students, deja_pris=()):
    """Choisit l'étudiant d'une copie d'après ses lectures OCR.

    deja_pris : étudiants qui ont déjà leur copie pour cette page. Ils ne
    sont plus candidats — sauf ressemblance écrasante (≥ 85), signe d'une
    page scannée deux fois : la copie reste alors « à identifier ». Quand un
    seul étudiant reste sans copie, une ressemblance même faible le désigne.
    Retourne (étudiant|None, score, meilleure lecture, classement)."""
    if not lectures or not students:
        return None, 0.0, ("", ""), []
    classement = []
    for s in students:
        meilleure = max(lectures, key=lambda lec: _ressemblance(lec, s))
        classement.append((s, _ressemblance(meilleure, s), meilleure))
    classement.sort(key=lambda c: -c[1])
    pris = {getattr(s, "pk", s) for s in deja_pris}
    tete = classement[0]
    if tete[0].pk in pris and tete[1] >= 85:
        return None, tete[1], tete[2], classement
    libres = [c for c in classement if c[0].pk not in pris] or classement
    premier = libres[0]
    second = libres[1][1] if len(libres) > 1 else 0.0
    accepte = (premier[1] >= MATCH_THRESHOLD
               or (premier[1] >= SEUIL_ECART and premier[1] - second >= ECART_MIN)
               or (len(libres) == 1 and bool(pris) and premier[1] >= 25))
    return (premier[0] if accepte else None), premier[1], premier[2], classement


def match_student(ocr_text, students):
    """Associe un texte OCR (« NOM Prénom ») à un étudiant de la classe.
    Retourne (étudiant|None, score). Voir identifier()."""
    if not ocr_text or not students:
        return None, 0.0
    etudiant, score, _, _ = identifier([(ocr_text, "")], students)
    return etudiant, score


# ---------------------------------------------------------------- extras

def crop_open_zone(warped_bgr, rect):
    return _crop_rect(warped_bgr, rect, inset_mm=0.8)


def draw_overlay(warped_bgr, page_layout, bubble_results, questions_by_order,
                 zone_nom=True):
    """Image de contrôle : cases détectées entourées en couleur.

    zone_nom : encadrer aussi la zone NOM/PRÉNOM lue par OCR. Faux quand la
    fiche n'en imprime pas (concours à étiquette, corrigé) : la disposition
    garde ces coordonnées, et l'image montrait deux cadres bleus sur une
    zone vide, sans rôle — voir services._has_name_zone."""
    img = warped_bgr.copy()
    r = _mm(L.BUBBLE_R) + 4
    for item in page_layout["qcm"]:
        res = bubble_results.get(item["order"])
        if res is None:
            continue
        q = questions_by_order.get(item["order"])
        for i, (cx, cy) in enumerate(item["bubbles"]):
            x, y = _mm(cx), _mm(cy)
            if q is not None and q.correct_choice == i:
                cv2.circle(img, (x, y), r + 4, (255, 160, 0), 2)   # bonne réponse (bleu)
            if i == res["choice"]:
                ok = q is not None and q.correct_choice == i and not res["multiple"]
                color = (0, 170, 0) if ok else (0, 0, 230)
                if res["multiple"]:
                    color = (0, 140, 255)
                cv2.circle(img, (x, y), r, color, 3)
    for key, rect in ((page_layout.get("name_boxes") or {}) if zone_nom
                      else {}).items():
        if key not in ("last", "first"):
            continue
        x, y, w, h = rect
        cv2.rectangle(img, (_mm(x), _mm(y)), (_mm(x + w), _mm(y + h)), (200, 80, 0), 2)
    return img
