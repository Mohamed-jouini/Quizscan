"""Moteur de lecture des fiches scannées : recalage ArUco, lecture des
cases (OMR), OCR du nom et rapprochement avec la liste de classe."""
import logging
import re
import unicodedata

import cv2
import numpy as np
import pytesseract
from rapidfuzz import fuzz, process as rf_process, utils as rf_utils

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


# ---------------------------------------------------------------- recalage

def detect_and_warp(image_bgr):
    """Détecte les repères ArUco, identifie la page et recale l'image
    dans le repère canonique. Retourne (image_recalée, page_index)."""
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
            x, y = _mm(cx), _mm(cy)
            mask = np.zeros_like(binary)
            cv2.circle(mask, (x, y), r, 255, -1)
            filled = cv2.countNonZero(cv2.bitwise_and(binary, mask))
            area = cv2.countNonZero(mask)
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


def read_id_grid(warped_bgr, page_layout):
    """Lit la grille du numéro d'inscription.
    Retourne la chaîne du numéro, ou "" si une ligne est illisible/ambiguë."""
    grid = page_layout.get("id_grid")
    if not grid:
        return ""
    gray = cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                   cv2.THRESH_BINARY_INV, 41, 12)
    r = _mm(grid["r"] * 0.75)
    digits = []
    for row in grid["rows"]:
        ratios = []
        for cx, cy in row:
            x, y = _mm(cx), _mm(cy)
            mask = np.zeros_like(binary)
            cv2.circle(mask, (x, y), r, 255, -1)
            filled = cv2.countNonZero(cv2.bitwise_and(binary, mask))
            ratios.append(filled / max(cv2.countNonZero(mask), 1))
        marked = [i for i, v in enumerate(ratios) if v >= FILL_THRESHOLD]
        if len(marked) == 1:
            digits.append(str(marked[0]))
        elif len(marked) > 1:
            top = sorted(marked, key=lambda i: -ratios[i])
            if ratios[top[0]] >= 1.8 * ratios[top[1]]:
                digits.append(str(top[0]))
            else:
                return ""
        else:
            return ""
    return "".join(digits)


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
    if not data:
        return None
    parts = data.split("|")
    if (len(parts) == 3 and parts[0] in ("QS", "QSA")
            and parts[1].isdigit() and parts[2].isdigit()):
        return parts[0], int(parts[1]), int(parts[2])
    return None


def read_qr(warped_bgr, page_layout):
    """Décode le QR code d'une fiche (QR imprimé ou étiquette collée).
    Retourne (kind, quiz_id, valeur) : kind "QS" = fiche nominative
    (valeur = id étudiant), "QSA" = fiche anonyme de concours
    (valeur = n° de série) ; (None, None, None) si aucun QR lisible."""
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
