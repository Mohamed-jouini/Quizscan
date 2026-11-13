"""Copies d'écrit et sujet de QCM en A3 recto-verso, pliés au milieu (Quiz.format_impression).

Modèle de l'institut (2 novembre 2026) : une feuille A3 porte la page de garde et trois pages
lignées, sans les quatre carrés noirs. On vérifie :
1. l'ordre des pages une fois la feuille pliée, en français et en arabe ;
2. aucun carré noir, cadre et code de page conservés ;
3. la note et l'identité lues sur la feuille A3 par le vrai chemin : dépliée, tournée (le
   chargeur prend l'A3 dans le sens de la longueur), à l'envers, pliée, talon coupé ;
4. les copies A4 déjà imprimées restent lues après le passage en A3, et l'A4 ne change pas ;
5. le sujet d'un QCM en A3 sans toucher à la feuille de réponses, qui reste en A4 ;
6. copie nominative (talon, nom imprimé, n° de place) : l'en-tête et le QR de l'étudiant sur la
   page de garde seulement, les pages lignées étant sur la même feuille pliée.
"""
import re
import sys
from unittest import mock

# (django.setup et migrate : à l'import de ce module)
from test_e2e_ecrit_oral import jpg, noircir_note

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import cv2
import numpy as np
import pymupdf
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import layout as L
from grader import omr, services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan
from test_e2e_web import stick_label

PROF = "prof_a3"
ETUDIANTS = [("AMRI", "Sana", "104501"), ("BAKIR", "Omar", "104502"),
             ("CHERIF", "Lina", "104503"), ("DALI", "Rim", "104504"),
             ("ESSID", "Nour", "104505")]
MM = 72 / 25.4


def format_mm(page):
    return round(page.rect.width / MM), round(page.rect.height / MM)


def ouvrir(pdf):
    return pymupdf.open(stream=pdf, filetype="pdf")


def creer_ecrit(c, prof, nom, langue="fr", **reglages):
    groupe = ClassGroup.objects.create(name=nom, owner=prof)
    for ligne in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=ligne[0], first_name=ligne[1],
                               student_number=ligne[2])
    r = c.post(reverse("quiz_create"), {
        "title": f"Redaction {nom}", "type_epreuve": "ecrit", "class_group": groupe.pk,
        "language": langue, "note_sur": "20", "sheet_mode": "grid", "id_mode": "grid",
        "id_digits": "6", "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors if r.context else r
    quiz = Quiz.objects.get(title=f"Redaction {nom}", owner=prof)
    donnees = {"reglages": "1", "title": quiz.title, "sujet_texte": "Racontez un voyage.",
               "note_sur": "20", "id_mode": "grid", "id_digits": "6", "pages_redaction": "1",
               "format_impression": "a3", **reglages}
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), donnees)
    assert r.status_code == 302, r.context["form"].errors if r.context else r
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    assert r.status_code == 302
    quiz.refresh_from_db()
    return quiz, groupe


def moities(page):
    """(moitié gauche, moitié droite) d'un côté de la feuille A3."""
    largeur, hauteur = page.rect.width, page.rect.height
    return (pymupdf.Rect(0, 0, largeur / 2, hauteur),
            pymupdf.Rect(largeur / 2, 0, largeur, hauteur))


def qr_de_la_moitie(image, droite, zone, marge=3.0):
    """QR lu sur une moitié d'un côté de la feuille (rendue ou scannée), recadré à sa place
    (zone de la disposition, à la marge près) comme à la lecture des copies : sur la moitié
    entière, le détecteur d'OpenCV en rate selon leur motif, celui de la page de garde
    compris."""
    milieu = image.shape[1] // 2
    moitie = image[:, milieu:] if droite else image[:, :milieu]
    x, y, w, h = zone["rect"]
    recadre = moitie[mm2px(y - marge):mm2px(y + h + marge),
                     mm2px(x - marge):mm2px(x + w + marge)]
    for detecteur in (cv2.QRCodeDetector(), cv2.QRCodeDetectorAruco()):
        lu = detecteur.detectAndDecode(recadre)[0]
        if lu:
            return lu
    return ""


def pages_des_moities(doc, titre):
    """(n° de page à gauche, à droite) de chaque côté, d'après le pied des pages."""
    cotes = []
    for page in doc:
        moities = (pymupdf.Rect(0, 0, page.rect.width / 2, page.rect.height),
                   pymupdf.Rect(page.rect.width / 2, 0, page.rect.width, page.rect.height))
        cotes.append(tuple(int(re.search(rf"{titre} — page (\d+)", page.get_text(clip=m)).group(1))
                           for m in moities))
    return cotes


def griffonner(img, rect_mm, graine):
    """Écriture à la main sur les lignes, sans toucher au cadre."""
    rng = np.random.default_rng(graine)
    x0, y0, x1, y1 = rect_mm
    for y in np.arange(y0, y1, 9.0):
        x = x0
        while x < x1 - 8:
            mot = rng.uniform(6, 22)
            points = np.array([[mm2px(x + t), mm2px(y - rng.uniform(.5, 4))]
                               for t in np.linspace(0, min(mot, x1 - x), 14)], np.int32)
            cv2.polylines(img, [points], False, (70, 40, 30), 3, cv2.LINE_AA)
            x += mot + 4
    return img


def copie_remplie(pdf, quiz, index_recto, numero, chiffres, gauche, ecrire=True, etiquette=None):
    """Recto A3 scanné d'une copie : n° noirci ou étiquette collée, note noircie, texte écrit."""
    disposition = quiz.layout_json
    page = disposition["pages"][0]
    feuille = render_page(pdf, index_recto)
    decalage = 0 if gauche else mm2px(210)
    moitie = feuille[:, decalage:decalage + mm2px(210)].copy()
    moitie = fill_sheet(moitie, disposition, {}, student_number=numero)
    if etiquette is not None:
        moitie = stick_label(moitie, page, etiquette)
    moitie = noircir_note(moitie, page["note_grid"], chiffres)
    graine = int(numero or 1)
    if ecrire:
        haut = page["lignes"]["y0"]
        moitie = griffonner(moitie, (22, haut + 9, 190, 276), graine)
    feuille[:, decalage:decalage + moitie.shape[1]] = moitie
    if ecrire:
        # l'autre moitié du recto : une page lignée remplie de texte (sans en-tête : talon et
        # QR sont sur la page de garde seulement) ; elle ne doit pas gêner la lecture
        autre = 210 if gauche else 0
        feuille = griffonner(feuille, (autre + 22, debut_du_texte(page), autre + 190, 276),
                             graine + 1)
    return feuille


def debut_du_texte(page):
    """Ordonnée (mm) où l'on commence à écrire sur une page lignée A3 : sans en-tête (elle est
    sur la feuille de la page de garde, pliée au milieu), dès le haut des lignes."""
    return 28.0


def en_pdf(images):
    """Sortie du chargeur : un PDF, chaque page au format réel de la feuille (image à 200 ppp)."""
    doc = pymupdf.open()
    for image in images:
        hauteur, largeur = image.shape[:2]
        petite = cv2.resize(image, (largeur * 2 // 3, hauteur * 2 // 3), interpolation=cv2.INTER_AREA)
        page = doc.new_page(width=largeur * 72 / 300, height=hauteur * 72 / 300)
        page.insert_image(page.rect, stream=jpg(petite))
    return doc.tobytes()


def notes(quiz):
    return {r["student"].last_name: r["score"] for r in services.compute_results(quiz)}


def verifier_ecrit(c, prof):
    quiz, _ = creer_ecrit(c, prof, "A3-FR")
    assert quiz.format_impression == "a3" and quiz.layout_json["format"] == "a3"
    assert quiz.layout_json["pages_redaction"] == 3, "feuille entière : 3 pages lignées"
    pdf = quiz.sheet_pdf.read()
    doc = ouvrir(pdf)
    assert len(doc) == 2 and all(format_mm(p) == (420, 297) for p in doc), [format_mm(p) for p in doc]
    assert pages_des_moities(doc, quiz.title) == [(4, 1), (2, 3)], pages_des_moities(doc, quiz.title)
    moitie_droite = pymupdf.Rect(doc[0].rect.width / 2, 0, doc[0].rect.width, doc[0].rect.height)
    assert "RÉSERVÉ AU CORRECTEUR" in doc[0].get_text(clip=moitie_droite)
    assert "pli au milieu" in doc[0].get_text() and "pli au milieu" in doc[1].get_text()
    assert "Ne pas scanner" not in doc[0].get_text() + doc[1].get_text(), \
        "sur la même feuille que la page de garde, on ne peut pas ne pas la scanner"
    print("  français : feuille A3, 2 côtés, page de garde à droite du pli (4|1, 2|3) OK")

    # Pas de carrés noirs : ni image dans le PDF (mode grille), ni encre aux emplacements.
    assert not doc[0].get_images(), doc[0].get_images()
    recto = cv2.cvtColor(render_page(pdf, 0), cv2.COLOR_BGR2GRAY)
    for x, y in L.marker_positions(0).values():
        zone = recto[mm2px(y):mm2px(y + L.MARKER_SIZE),
                     mm2px(210 + x):mm2px(210 + x + L.MARKER_SIZE)]
        assert (zone < 100).mean() < 0.15, f"encre à un coin ({x}, {y}) : {(zone < 100).mean():.2f}"
    print("  aucun des 4 carrés noirs aux coins de la page de garde OK")

    # L'énoncé reste en A4 : le format choisi est celui des copies.
    assert all(format_mm(p) == (210, 297) for p in ouvrir(quiz.subject_pdf.read()))
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "en A3 recto-verso sur le bord court" in page and "recto seul" in page
    assert "régénérez les documents" not in page

    disposition_page = quiz.layout_json["pages"][0]
    assert disposition_page["lignes"]["x1"] == 196.0 and disposition_page["lignes"]["pas"] == 9.0

    # Lecture : chaque présentation que le scanner peut rendre.
    sens = [("depliee.jpg", "AMRI", [15, 1], lambda f: f),
            ("tournee90.jpg", "BAKIR", [8, 0],
             lambda f: cv2.rotate(f, cv2.ROTATE_90_CLOCKWISE)),
            ("tournee270.jpg", "CHERIF", [12, 1],
             lambda f: cv2.rotate(f, cv2.ROTATE_90_COUNTERCLOCKWISE)),
            ("envers.jpg", "DALI", [9, 1], lambda f: cv2.rotate(f, cv2.ROTATE_180)),
            ("pliee.jpg", "ESSID", [11, 0], lambda f: f[:, mm2px(210):])]
    numeros = {e[0]: e[2] for e in ETUDIANTS}
    envois = []
    for nom, etudiant, chiffres, sens_scan in sens:
        feuille = copie_remplie(pdf, quiz, 0, numeros[etudiant], chiffres, gauche=False)
        envois.append((nom, jpg(simulate_scan(sens_scan(feuille)))))
    # verso, écrit à la main : la page sans cadre est écartée
    verso = griffonner(griffonner(render_page(pdf, 1), (22, 28, 190, 276), 7), (232, 28, 400, 276), 8)
    envois.append(("verso.jpg", jpg(simulate_scan(verso))))
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile(n, d) for n, d in envois], label="A3 images")
    feuilles = {s.source_name: s for s in lot.sheets.all()}
    assert feuilles["verso.jpg"].status == "ignoree", feuilles["verso.jpg"].status
    for nom, etudiant, _, _ in sens:
        s = feuilles[nom]
        assert s.status == "ok" and s.student and s.student.last_name == etudiant, \
            (nom, s.status, s.error_message)
    attendu = {"AMRI": 15.5, "BAKIR": 8.0, "CHERIF": 12.5, "DALI": 9.5, "ESSID": 11.0}
    assert notes(quiz) == attendu, notes(quiz)
    print("  lecture : dépliée, tournée à 90° et 270°, à l'envers, pliée : 5 notes justes ; "
          "verso écrit ignoré OK")

    # Sortie conseillée : un PDF recto-verso à 200 ppp, comme le chargeur la rend.
    quiz2, _ = creer_ecrit(c, prof, "A3-PDF")
    pdf2 = quiz2.sheet_pdf.read()
    images = [copie_remplie(pdf2, quiz2, 0, numeros["AMRI"], [14, 0], gauche=False),
              griffonner(render_page(pdf2, 1), (22, 28, 190, 276), 11),
              cv2.rotate(copie_remplie(pdf2, quiz2, 0, numeros["BAKIR"], [7, 1],
                                       gauche=False), cv2.ROTATE_90_COUNTERCLOCKWISE),
              cv2.rotate(render_page(pdf2, 1), cv2.ROTATE_90_COUNTERCLOCKWISE)]
    lot = services.process_uploaded_files(
        quiz2, [SimpleUploadedFile("paquet.pdf", en_pdf(images))], label="A3 PDF")
    assert sorted(s.status for s in lot.sheets.all()) == ["ignoree", "ignoree", "ok", "ok"], \
        [(s.source_name, s.status, s.error_message) for s in lot.sheets.all()]
    assert notes(quiz2) == {"AMRI": 14.0, "BAKIR": 7.5}, notes(quiz2)
    print("  PDF A3 du chargeur à 200 ppp, recto-verso : 2 notes justes, 2 versos ignorés OK")


def verifier_arabe_et_etiquette(c, prof):
    quiz, groupe = creer_ecrit(c, prof, "A3-AR", langue="ar", id_mode="sticker")
    pdf = quiz.sheet_pdf.read()
    doc = ouvrir(pdf)
    assert pages_des_moities(doc, quiz.title) == [(1, 4), (3, 2)], pages_des_moities(doc, quiz.title)
    assert quiz.layout_json["pages"][0]["lignes"]["marge"] == 190.0, "marge côté début de ligne"
    candidat = groupe.students.get(last_name="AMRI")
    feuille = copie_remplie(pdf, quiz, 0, "", [16, 0], gauche=True, etiquette=candidat)
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile("ar.jpg", jpg(simulate_scan(feuille)))], label="A3 arabe")
    s = lot.sheets.get()
    assert s.status == "ok" and s.student == candidat, (s.status, s.error_message)
    assert notes(quiz)["AMRI"] == 16.0, notes(quiz)
    print("  arabe, étiquette QR : page de garde à gauche comme sur le modèle (1|4, 3|2), "
          "étiquette et note lues OK")


def verifier_talon(c, prof):
    quiz, groupe = creer_ecrit(c, prof, "A3-TALON", id_mode="qr", anonymat="talon")
    assert quiz.talon_nom and quiz.layout_json["format"] == "a3"
    pdf = quiz.sheet_pdf.read()
    doc = ouvrir(pdf)
    assert len(doc) == 2 * groupe.students.count(), len(doc)
    premiere = groupe.students.order_by("pk").first()
    assert premiere.last_name in doc[0].get_text()
    # Le massicot coupe toute la largeur : aucune ligne d'écriture au-dessus du trait, sur
    # aucune page lignée (français : à gauche sur le recto, des deux côtés sur le verso).
    # Traits pleins seulement : le trait de coupe, pointillé, est sur chaque page.
    vues = 0
    for page in doc[:2]:
        for trace in page.get_drawings():
            if "s" not in (trace.get("type") or "") or trace.get("dashes") != "[] 0":
                continue
            for element in trace["items"]:
                if element[0] == "l" and abs(element[1].y - element[2].y) < .1 \
                        and element[2].x - element[1].x > 150 * MM \
                        and (element[1].x < 210 * MM or page.number == 1):
                    assert element[1].y / MM >= L.TALON_COUPE + 1, element
                    vues += 1
    assert vues > 60, vues

    # Pages lignées : sur la feuille de la page de garde, pliée au milieu, elles ne reprennent
    # pas son en-tête (demande de l'utilisateur, 7 novembre 2026) : ni nom, ni talon, ni QR ;
    # leur n° au pied.
    zone = quiz.layout_json["pages"][0]["qr"]
    gauche_droite = moities(doc[0])
    assert "Page 1 / 4" in doc[0].get_text(clip=gauche_droite[1])
    for index, droite, numero in ((0, False, 4), (1, False, 2), (1, True, 3)):
        texte = doc[index].get_text(clip=moities(doc[index])[droite])
        assert premiere.last_name not in texte and "Talon à découper" not in texte, \
            (numero, texte)
        assert f"— page {numero}" in texte, (numero, texte)
        assert qr_de_la_moitie(render_page(pdf, index), droite, zone) == "", numero
    assert "Ne pas scanner" not in doc[0].get_text() + doc[1].get_text()

    # Lecture : la page de garde, à côté de la page 4 et de son QR ; le verso, talon coupé
    # ou non, ignoré (ni cadre ni code de page).
    feuille = copie_remplie(pdf, quiz, 0, "", [13, 0], gauche=False)
    coupee = feuille[mm2px(L.TALON_COUPE):]
    haut = debut_du_texte(quiz.layout_json["pages"][0])
    verso = griffonner(griffonner(render_page(pdf, 1), (22, haut, 190, 276), 5),
                       (232, haut, 400, 276), 6)
    lot = services.process_uploaded_files(quiz, [
        SimpleUploadedFile("talon.jpg", jpg(simulate_scan(coupee))),
        SimpleUploadedFile("verso_coupe.jpg", jpg(simulate_scan(verso[mm2px(L.TALON_COUPE):]))),
        SimpleUploadedFile("verso_entier.jpg", jpg(simulate_scan(verso)))], label="A3 talon")
    feuilles = {s.source_name: s for s in lot.sheets.all()}
    s = feuilles["talon.jpg"]
    assert s.status == "ok" and s.student == premiere, (s.status, s.error_message)
    for nom in ("verso_coupe.jpg", "verso_entier.jpg"):
        assert feuilles[nom].status == "ignoree", (nom, feuilles[nom].status)
    assert notes(quiz) == {premiere.last_name: 13.0}, notes(quiz)
    print("  copie à talon : nom sur le talon, rien à écrire au-dessus du trait, talon coupé "
          "sur toute la largeur, QR et note lus OK")
    print("  copie à talon : talon et QR sur la page de garde seulement (feuille pliée), "
          "verso ignoré au scan OK")


def verifier_entete_nominatif(c, prof):
    """Nom imprimé, puis n° de place : en A3 plié, l'en-tête sur la page de garde seulement."""
    quiz, groupe = creer_ecrit(c, prof, "A3-NOM", id_mode="qr")
    assert not quiz.talon_nom and not quiz.place_anonyme
    pdf = quiz.sheet_pdf.read()
    doc = ouvrir(pdf)
    assert len(doc) == 2 * groupe.students.count(), len(doc)
    etudiants = {s.last_name: s for s in groupe.students.all()}
    amri = etudiants["AMRI"]
    zone = quiz.layout_json["pages"][0]["qr"]
    for index, droite, numero in ((0, False, 4), (1, False, 2), (1, True, 3)):
        texte = doc[index].get_text(clip=moities(doc[index])[droite])
        assert "AMRI" not in texte and "104501" not in texte, (numero, texte)
        assert qr_de_la_moitie(render_page(pdf, index), droite, zone) == "", numero
    assert "Ne pas scanner" not in doc[0].get_text() + doc[1].get_text()
    # la page de garde se lit : dépliée, puis tournée
    recto = simulate_scan(copie_remplie(pdf, quiz, 0, "", [12, 0], gauche=False))
    haut = debut_du_texte(quiz.layout_json["pages"][0])
    verso = griffonner(render_page(pdf, 1), (232, haut, 400, 276), 9)
    lot = services.process_uploaded_files(quiz, [
        SimpleUploadedFile("amri.jpg", jpg(recto)),
        SimpleUploadedFile("bakir.jpg", jpg(simulate_scan(cv2.rotate(
            copie_remplie(pdf, quiz, 2, "", [8, 1], gauche=False),
            cv2.ROTATE_90_CLOCKWISE)))),
        SimpleUploadedFile("verso.jpg", jpg(simulate_scan(verso)))], label="A3 nominatives")
    feuilles = {s.source_name: s for s in lot.sheets.all()}
    for nom, etudiant in (("amri.jpg", amri), ("bakir.jpg", etudiants["BAKIR"])):
        s = feuilles[nom]
        assert s.status == "ok" and s.student == etudiant, (nom, s.status, s.error_message)
    assert feuilles["verso.jpg"].status == "ignoree", feuilles["verso.jpg"].status
    assert notes(quiz) == {"AMRI": 12.0, "BAKIR": 8.5}, notes(quiz)
    print("  copie nominative : nom, n° et QR sur la page de garde seulement ; page de garde "
          "lue dépliée et tournée, verso ignoré OK")

    places, _ = creer_ecrit(c, prof, "A3-PLACES", id_mode="qr", anonymat="place")
    assert places.place_anonyme
    doc = ouvrir(places.sheet_pdf.read())
    assert "PLACE N° 1" in doc[0].get_text(clip=moities(doc[0])[1]), "page de garde"
    for index, droite in ((0, False), (1, False), (1, True)):
        texte = doc[index].get_text(clip=moities(doc[index])[droite])
        assert "PLACE N° 1" not in texte, (index, droite, texte)
        assert not any(nom in texte or num in texte for nom, _, num in ETUDIANTS), texte
    print("  copie à n° de place : « PLACE N° 1 » et QR sur la page de garde seulement, "
          "ni nom ni n° OK")


def verifier_deux_feuilles(c, prof):
    quiz, _ = creer_ecrit(c, prof, "A3-LONG", pages_redaction="6")
    assert quiz.layout_json["pages_redaction"] == 7
    doc = ouvrir(quiz.sheet_pdf.read())
    assert len(doc) == 4
    assert pages_des_moities(doc, quiz.title) == [(8, 1), (2, 7), (6, 3), (4, 5)], \
        pages_des_moities(doc, quiz.title)
    print("  6 pages lignées demandées : 2 feuilles emboîtées, 8 pages dans l'ordre OK")
    assert sheet_pdf._cotes_a3(1, False) == [(4, 1), (2, 3)]
    assert sheet_pdf._cotes_a3(5, True) == [(1, 8), (7, 2), (3, 6), (5, 4)]

    # Copie à talon de 8 pages : talon et QR sur la page de garde SEULEMENT (demande de
    # l'utilisateur, 13 novembre 2026). La page 3, première de la 2e feuille emboîtée (pages
    # 3 à 6), les reprenait depuis le 7 novembre : plus rien, comme les autres pages lignées.
    talon, groupe = creer_ecrit(c, prof, "A3-LONG-TALON", id_mode="qr", anonymat="talon",
                                pages_redaction="6")
    pdf = talon.sheet_pdf.read()
    doc = ouvrir(pdf)
    premiere = groupe.students.order_by("pk").first()
    assert len(doc) == 4 * groupe.students.count(), len(doc)
    assert pages_des_moities([doc[i] for i in range(4)], talon.title) == \
        [(8, 1), (2, 7), (6, 3), (4, 5)]
    avec_nom = {}
    for index in range(4):
        for droite in (False, True):
            texte = doc[index].get_text(clip=moities(doc[index])[droite])
            numero = int(re.search(rf"{talon.title} — page (\d+)", texte).group(1))
            avec_nom[numero] = premiere.last_name in texte
    assert sorted(n for n, oui in avec_nom.items() if oui) == [1], avec_nom
    texte3 = doc[2].get_text(clip=moities(doc[2])[1])
    assert "Talon à découper" not in texte3 and "Page 3 / 8" not in texte3, texte3
    zone = talon.layout_json["pages"][0]["qr"]
    for droite, numero in ((True, 3), (False, 6)):
        assert qr_de_la_moitie(render_page(pdf, 2), droite, zone) == "", f"page {numero}"
    assert qr_de_la_moitie(render_page(pdf, 0), True, zone) == f"QS|{talon.pk}|{premiere.pk}"
    print("  copie à talon de 8 pages : talon et QR sur la page de garde seulement, rien sur "
          "la 2e feuille (page 3 comprise) OK")


def verifier_a4_inchange(c, prof):
    quiz, _ = creer_ecrit(c, prof, "A3-COMPAT", format_impression="a4", pages_redaction="2")
    assert "format" not in quiz.layout_json
    pdf_a4 = quiz.sheet_pdf.read()
    doc = ouvrir(pdf_a4)
    # 2 pages lignées demandées, 3 imprimées : deux feuilles entières en recto-verso
    assert len(doc) == 4 and all(format_mm(p) == (210, 297) for p in doc), len(doc)
    assert "Page non lue au scan" in doc[1].get_text()
    signature_a4 = quiz.layout_json["signature"]
    assert L.layout_signature(quiz) == signature_a4
    zones_a4 = {k: quiz.layout_json["pages"][0].get(k) for k in services._ZONES_LUES}
    page_a4 = quiz.layout_json["pages"][0]
    print("  format A4 : pages séparées, feuilles entières pour le recto-verso OK")

    # Pages lignées imprimées selon le nombre demandé (0 à 6) : feuilles entières, en A4
    # (demande de l'utilisateur, 6 novembre 2026) comme en A3.
    for demandees, a4, a3 in zip(range(7), (1, 1, 3, 3, 5, 5, 7), (3, 3, 3, 3, 7, 7, 7)):
        quiz.pages_redaction = demandees
        for fmt, attendu in (("a4", a4), ("a3", a3)):
            quiz.format_impression = fmt
            assert L.build_layout(quiz)["pages_redaction"] == attendu, (demandees, fmt)
    quiz.refresh_from_db()

    # Copies A4 générées avant les feuilles entières (3 pages : la copie suivante commençait
    # au dos de la dernière) : la page de l'épreuve demande de les régénérer.
    with mock.patch.object(L, "pages_lignees_a4", lambda n: n):
        c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    assert len(ouvrir(quiz.sheet_pdf.read())) == 3
    assert "régénérez les documents" in c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    assert len(ouvrir(quiz.sheet_pdf.read())) == 4
    assert "régénérez les documents" not in \
        c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    print("  copies A4 d'avant, à une page près : régénération demandée, puis 4 pages OK")

    # Passage en A3 : l'alerte de régénération, puis une copie A4 déjà imprimée reste lue.
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "reglages": "1", "title": quiz.title, "sujet_texte": quiz.sujet_texte, "note_sur": "20",
        "id_mode": "grid", "id_digits": "6", "pages_redaction": "2", "format_impression": "a3"})
    assert r.status_code == 302
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "régénérez les documents" in page, "format changé : documents à refaire"
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    assert quiz.layout_json["format"] == "a3"
    assert {k: quiz.layout_json["pages"][0].get(k) for k in services._ZONES_LUES} == zones_a4, \
        "rien de ce qui est lu ne bouge entre A4 et A3"
    ancienne = fill_sheet(render_page(pdf_a4, 0), {"pages": [page_a4]}, {}, student_number="104501")
    ancienne = noircir_note(ancienne, page_a4["note_grid"], [10, 1])
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile("a4.jpg", jpg(simulate_scan(ancienne)))], label="A4 d'avant")
    s = lot.sheets.get()
    assert s.status == "ok" and s.student.last_name == "AMRI", (s.status, s.error_message)
    assert notes(quiz)["AMRI"] == 10.5, notes(quiz)
    print("  copie A4 déjà imprimée, lue après le passage en A3 OK")


def creer_qcm(c, prof, nom, langue, nb):
    groupe = ClassGroup.objects.create(name=nom, owner=prof)
    quiz = Quiz.objects.create(title=f"Sujet {nom}", class_group=groupe, owner=prof,
                               sheet_mode="split", id_mode="grid", id_digits=6,
                               language=langue, num_choices=4)
    Question.objects.bulk_create(
        Question(quiz=quiz, order=o, qtype="qcm", points=1.0, correct_choice=0, num_choices=4,
                 text=f"Question {o}", choices=[f"Choix {x} de {o}" for x in "ABCD"])
        for o in range(1, nb + 1))
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    assert r.status_code == 302
    quiz.refresh_from_db()
    return quiz


def reglages_qcm(quiz, format_impression):
    return {"update_settings": "1", "title": quiz.title, "sheet_mode": quiz.sheet_mode,
            "id_mode": quiz.id_mode, "id_digits": quiz.id_digits, "wrong_penalty": "0",
            "entete": quiz.entete, "duree": quiz.duree, "format_impression": format_impression}


def numeros_sujet(page, total):
    """N° de page imprimés à gauche et à droite (0 : moitié laissée blanche)."""
    resultat = []
    for clip in (pymupdf.Rect(0, 0, page.rect.width / 2, page.rect.height),
                 pymupdf.Rect(page.rect.width / 2, 0, page.rect.width, page.rect.height)):
        trouve = re.search(rf"\b(\d+)/{total}\b", page.get_text(clip=clip))
        resultat.append(int(trouve.group(1)) if trouve else 0)
    return tuple(resultat)


def verifier_sujet(c, prof):
    for langue, rtl in (("fr", False), ("ar", True)):
        quiz = creer_qcm(c, prof, f"A3-QCM-{langue}", langue, 40)
        sujet_a4 = ouvrir(quiz.subject_pdf.read())
        total = len(sujet_a4)
        assert total >= 5 and all(format_mm(p) == (210, 297) for p in sujet_a4), total
        feuille_avant = quiz.sheet_pdf.read()
        signature = quiz.layout_json["signature"]

        # Le réglage refait le sujet aussitôt, sans toucher à la feuille de réponses.
        r = c.post(reverse("quiz_detail", args=[quiz.pk]), reglages_qcm(quiz, "a3"))
        assert r.status_code == 302
        quiz.refresh_from_db()
        assert quiz.format_impression == "a3"
        sujet = ouvrir(quiz.subject_pdf.read())
        attendu = sheet_pdf._cotes_a3(total, rtl)
        assert len(sujet) == len(attendu) == (total + 3) // 4 * 2, (len(sujet), total)
        assert all(format_mm(p) == (420, 297) for p in sujet)
        obtenu = [numeros_sujet(p, total) for p in sujet]
        assert obtenu == [tuple(n if n <= total else 0 for n in cote) for cote in attendu], \
            (obtenu, attendu)
        assert open(quiz.sheet_pdf.path, "rb").read() == feuille_avant, "feuille de réponses retouchée"
        assert quiz.layout_json["signature"] == signature == L.layout_signature(quiz)
        assert "Question 40" in "".join(p.get_text() for p in sujet)
        page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
        assert "Imprimez le sujet en A3 recto-verso" in page and 'name="format_impression"' in page

        # Régénérer : sujet en A3, feuille de réponses (seul document scanné) en A4.
        c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
        quiz.refresh_from_db()
        assert all(format_mm(p) == (420, 297) for p in ouvrir(quiz.subject_pdf.read()))
        assert all(format_mm(p) == (210, 297) for p in ouvrir(quiz.sheet_pdf.read()))
        assert "format" not in quiz.layout_json

        # Retour en A4
        c.post(reverse("quiz_detail", args=[quiz.pk]), reglages_qcm(quiz, "a4"))
        quiz.refresh_from_db()
        assert all(format_mm(p) == (210, 297) for p in ouvrir(quiz.subject_pdf.read()))
        print(f"  sujet QCM {langue} : {total} pages sur {len(sujet) // 2} feuilles A3, dans "
              "l'ordre ; feuille de réponses intacte, en A4 ; retour en A4 OK")

    # Un QCM sans sujet séparé n'a rien à mettre en A3 : le choix n'est pas proposé.
    quiz = creer_qcm(c, prof, "A3-QCM-FULL", "fr", 3)
    quiz.sheet_mode = "full"
    quiz.save(update_fields=["sheet_mode"])
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert 'name="format_impression"' not in page


def verifier_lecteur_ne_confond_pas():
    """Une page lignée seule n'est jamais prise pour une page de garde."""
    blanc = np.full((mm2px(297), mm2px(210), 3), 255, np.uint8)
    for y in np.arange(18, 279, 9.0):
        cv2.line(blanc, (mm2px(14), mm2px(y)), (mm2px(196), mm2px(y)), (199, 199, 199), 2)
    try:
        omr.detect_and_warp(blanc)
    except omr.MarkerError:
        return
    raise AssertionError("page lignée prise pour une page de garde")


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username=PROF).delete()
    ClassGroup.objects.filter(owner__username=PROF).delete()
    User.objects.filter(username=PROF).delete()
    prof = enseignant_complet(User.objects.create_user(PROF, password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    print("Copies d'écrit en A3 :")
    verifier_ecrit(c, prof)
    verifier_arabe_et_etiquette(c, prof)
    verifier_talon(c, prof)
    verifier_entete_nominatif(c, prof)
    verifier_deux_feuilles(c, prof)
    verifier_a4_inchange(c, prof)
    verifier_lecteur_ne_confond_pas()
    print("Sujet de QCM en A3 :")
    verifier_sujet(c, prof)
    print("\n✅ TESTS A3 RÉUSSIS")


if __name__ == "__main__":
    main()
