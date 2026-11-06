"""Lecture par OCR d'un questionnaire en PDF sans texte.

Un PDF imprimé avec « Microsoft Print to PDF » (questionnaire reçu de
l'utilisateur le 6 novembre 2026) ou scanné n'a pas de texte : ses lettres sont
des dessins ou des images. Tesseract (installé dans l'image Docker, arabe et
français) les lit, à ces conditions trouvées sur ce questionnaire de 50
questions :
  - les puces « • » des listes sont effacées de l'image : Tesseract les lisait
    « ٠ », « ء » ou « هه » et sautait alors des lignes entières ;
  - chaque ligne est lue seule (« --psm 7 ») : lue d'un bloc, la page perdait
    une ligne sur dix ;
  - à la bonne taille : à 300 ppp, une ligne sur cinq revenait vide ; ramenée à
    une hauteur d'encre d'environ 22 pixels, toutes sont lues (une ligne encore
    vide est relue à d'autres tailles) ;
  - en arabe seul : « ara+eng » lisait plus mal les choix chiffrés et doublait
    la durée (49 questions au lieu de 50, 32 s au lieu de 14).
Les lignes rendues sont celles du texte, à analyser par
importers.parse_questions_text(lignes, ocr=True), plus tolérant (lettre de choix
perdue ou doublée, choix illisible, n° de question tronqué ou sans point). Le
résultat est à relire : l'OCR se trompe (une formule en lettres latines dans un
questionnaire arabe, surtout).
"""
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

PAGES_MAX = 40          # un questionnaire, pas un livre : 15 s environ par 8 pages
RESOLUTION = 300
HAUTEUR_ENCRE = 22      # pixels d'encre par ligne que Tesseract lit le mieux
LANGUES = {"ar": "ara", "fr": "fra"}


class OcrIndisponible(RuntimeError):
    """Tesseract ou son paquet de langue n'est pas installé (poste de développement)."""


def _sans_puces(gris):
    """Efface les petits disques pleins et isolés : les puces des listes."""
    _, encre = cv2.threshold(gris, 160, 255, cv2.THRESH_BINARY_INV)
    n, etiquettes, stats, _ = cv2.connectedComponentsWithStats(encre, 8)
    for k in range(1, n):
        x, y, w, h, aire = stats[k]
        if 10 <= w <= 40 and 10 <= h <= 40 and abs(w - h) <= 4 and aire >= 0.6 * w * h:
            boite = gris[y:y + h, x:x + w]          # une vue : efface dans la page
            boite[etiquettes[y:y + h, x:x + w] == k] = 255
    return gris


def _rattacher_les_points(bandes):
    """Une bande bien plus basse qu'une ligne (points, hamza, chadda détachés au-dessus
    ou au-dessous du texte) rejoint la ligne la plus proche ; deux lignes entières ne
    se rejoignent jamais, même serrées : lues ensemble, deux choix n'en faisaient qu'un."""
    hauteurs = sorted(bas - haut for haut, bas in bandes if bas - haut >= 12)
    if not hauteurs:
        return bandes
    petite = 0.45 * hauteurs[len(hauteurs) // 2]
    lignes = [list(b) for b in bandes]
    k = 0
    while k < len(lignes):
        haut, bas = lignes[k]
        if bas - haut >= petite or len(lignes) == 1:
            k += 1
            continue
        ecart_avant = haut - lignes[k - 1][1] if k > 0 else None
        ecart_apres = lignes[k + 1][0] - bas if k + 1 < len(lignes) else None
        if ecart_apres is None or (ecart_avant is not None and ecart_avant <= ecart_apres):
            if ecart_avant is not None and ecart_avant < 20:
                lignes[k - 1][1] = bas
                del lignes[k]
                continue
        elif ecart_apres < 20:
            lignes[k + 1][0] = haut
            del lignes[k]
            continue
        k += 1
    return lignes


def _couper_les_doubles(bandes, encre_par_rang):
    """Une bande haute de deux lignes ou plus (les points d'une ligne touchent le bas de
    la précédente : aucun rang blanc entre elles) est coupée au rang le moins encré,
    près de chaque limite attendue."""
    hauteurs = sorted(bas - haut for haut, bas in bandes if bas - haut >= 12)
    if not hauteurs:
        return bandes
    ligne = hauteurs[len(hauteurs) // 2]
    sortie = []
    for haut, bas in bandes:
        n = round((bas - haut) / ligne)
        if n < 2 or (bas - haut) < 1.5 * ligne:
            sortie.append([haut, bas])
            continue
        moyenne = encre_par_rang[haut:bas].mean()
        debut = haut
        for k in range(1, n):
            cible = haut + k * (bas - haut) // n
            fenetre = range(max(debut + ligne // 3, cible - ligne // 3),
                            min(bas - ligne // 3, cible + ligne // 3))
            if not fenetre:
                continue
            coupe = min(fenetre, key=lambda y: encre_par_rang[y])
            # un titre en grande police est haut sans être double : il est encré
            # partout, deux lignes serrées ont entre elles un rang presque blanc
            if encre_par_rang[coupe] > 0.15 * moyenne:
                continue
            sortie.append([debut, coupe])
            debut = coupe
        sortie.append([debut, bas])
    return sortie


def _bandes(gris):
    """Les lignes de texte de la page, une image chacune, à la taille de lecture."""
    encre = gris < 160
    encre_par_rang = encre.sum(axis=1)
    profil = encre_par_rang > 0
    bandes, debut = [], None
    for y, occupee in enumerate(profil):
        if occupee and debut is None:
            debut = y
        elif not occupee and debut is not None:
            bandes.append([debut, y])
            debut = None
    if debut is not None:
        bandes.append([debut, len(profil)])
    lignes = _couper_les_doubles(_rattacher_les_points(bandes), encre_par_rang)
    images = []
    for haut, bas in lignes:
        if bas - haut < 12:      # trait de séparation, poussière
            continue
        bande = gris[haut:bas]
        colonnes = np.where((bande < 160).any(axis=0))[0]
        bande = bande[:, max(colonnes.min() - 10, 0):colonnes.max() + 10]
        images.append((cv2.copyMakeBorder(bande, 20, 20, 30, 30, cv2.BORDER_CONSTANT,
                                          value=255), bas - haut))
    return images


def _lire_bande(bande, hauteur_encre, lang):
    """Le texte d'une ligne. Tesseract rend parfois du vide pour une ligne pourtant
    nette, à une taille donnée : elle est relue à d'autres tailles."""
    import pytesseract
    for cible in (HAUTEUR_ENCRE, 18, 27, 15):
        echelle = min(1.0, max(0.15, cible / hauteur_encre))
        image = cv2.resize(bande, None, fx=echelle, fy=echelle, interpolation=cv2.INTER_AREA)
        texte = pytesseract.image_to_string(image, lang=lang, config="--psm 7").strip()
        if texte:
            return texte
    return ""


def lignes_du_pdf(doc, langue="fr"):
    """Lignes de texte d'un PDF (pymupdf.Document) lues par OCR, dans l'ordre."""
    import pymupdf
    import pytesseract
    lang = LANGUES.get(langue, "fra")
    try:
        installees = set(pytesseract.get_languages())
    except pytesseract.TesseractNotFoundError as exc:
        raise OcrIndisponible("Tesseract n'est pas installé") from exc
    manquantes = set(lang.split("+")) - installees
    if manquantes:
        raise OcrIndisponible(f"paquet de langue de Tesseract absent : {', '.join(manquantes)}")
    bandes = []
    for page in list(doc)[:PAGES_MAX]:
        pix = page.get_pixmap(dpi=RESOLUTION, colorspace=pymupdf.csGRAY)
        gris = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width).copy()
        bandes += _bandes(_sans_puces(gris))

    with ThreadPoolExecutor(max_workers=4) as lecteurs:
        return list(lecteurs.map(lambda b: _lire_bande(b[0], b[1], lang), bandes))
