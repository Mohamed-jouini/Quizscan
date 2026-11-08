"""Sorties d'un scanner à chargeur recto-verso (Canon DR-G2110 à l'institut).

Pas de scanner ici : chaque sortie est fabriquée dans le format réel, puis envoyée par
le vrai chemin. Pannes à ne pas revoir : un TIFF multipage réduit à sa première page, lot
« terminé » sans erreur ; les versos blancs d'un paquet imprimé d'un seul côté, tous comptés
en erreur et en « page non identifiée » ; une étiquette sur trois ratée en noir et blanc.
"""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import io
import logging
from unittest import mock

import cv2
import numpy as np
import pymupdf
from PIL import Image

# (django.setup et migrate : à l'import de ce module)
from test_e2e_etiquette_pages import NB_QCM, PAGE_1, copie, creer_quiz, envoyer, lignes, preparer

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from grader import omr, services
from grader.models import ClassGroup, Quiz
from test_commun import enseignant_complet
from test_e2e import render_page, simulate_scan
from test_e2e_web import stick_label

PROF = "prof_scanner"


def en_tiff(pages, bitonal=False):
    """Toutes les pages dans un seul fichier, comme le propose le logiciel du scanner."""
    images = [Image.fromarray(cv2.cvtColor(p, cv2.COLOR_BGR2RGB)) for p in pages]
    if bitonal:
        images = [i.convert("L").point(lambda v: 255 if v > 128 else 0).convert("1")
                  for i in images]
    tampon = io.BytesIO()
    images[0].save(tampon, format="TIFF", save_all=True, append_images=images[1:],
                   compression="group4" if bitonal else "tiff_lzw", dpi=(300, 300))
    return tampon.getvalue()


def verifier_tiff(teacher):
    groupe, quiz, (a, b) = creer_quiz("SC-TIFF", teacher, id_mode="sticker", auto_enroll=True)
    disposition, pdf = preparer(quiz)
    pages = copie(pdf, disposition, a, {0, 1}) + copie(pdf, disposition, b, {0, 1})

    envoi = SimpleUploadedFile("paquet.tif", en_tiff(pages))
    lot = services.process_uploaded_files(quiz, [envoi], label="tiff")
    feuilles = list(lot.sheets.order_by("pk"))
    assert lot.total_pages == 4, f"pages annoncées : {lot.total_pages}"
    assert [f.source_name for f in feuilles] == [f"paquet.tif (page {i})" for i in range(1, 5)], \
        [f.source_name for f in feuilles]
    assert [f.student_id for f in feuilles] == [a.pk, a.pk, b.pk, b.pk], feuilles
    rangs = lignes(quiz)
    assert rangs[a.pk]["score"] == rangs[b.pk]["score"] == NB_QCM, rangs
    print(f"  TIFF couleur de 4 pages : 4 pages lues, {NB_QCM}/{NB_QCM} pour chacun")

    # Noir et blanc compressé : toutes les pages arrivent à la lecture.
    _, quiz, (a, b) = creer_quiz("SC-TIFF-G4", teacher, id_mode="sticker", auto_enroll=True)
    disposition, pdf = preparer(quiz)
    pages = copie(pdf, disposition, a, {0, 1}) + copie(pdf, disposition, b, {0, 1})
    envoi = SimpleUploadedFile("nb.tiff", en_tiff(pages, bitonal=True))
    lot = services.process_uploaded_files(quiz, [envoi], label="tiff-g4")
    assert lot.sheets.count() == 4 and not lot.sheets.exclude(status__in=("ok", "no_match")), \
        list(lot.sheets.values_list("status", "error_message"))
    print("  TIFF noir et blanc compressé (G4) : 4 pages lues")

    # Une image démesurée dans le fichier : elle seule est refusée, le lot continue.
    tampon = io.BytesIO()
    Image.fromarray(cv2.cvtColor(pages[0], cv2.COLOR_BGR2RGB)).convert("1").save(
        tampon, format="TIFF", save_all=True, compression="group4",
        append_images=[Image.new("1", (9000, 9000), 1)])
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile("demesure.tif", tampon.getvalue())], label="demesure")
    feuilles = list(lot.sheets.order_by("pk"))
    assert lot.status == "done" and len(feuilles) == 2, (lot.status, feuilles)
    assert feuilles[1].status == "error" and "démesurée" in feuilles[1].error_message, feuilles[1]
    assert feuilles[0].status != "error", feuilles[0].error_message
    print("  image démesurée dans le TIFF : refusée seule, le lot continue")

    # Une image seule garde son nom ; un faux .tif reste signalé, sans arrêter le lot.
    _, quiz, (a, _b) = creer_quiz("SC-TIFF-1", teacher, nb_qcm=PAGE_1, id_mode="sticker",
                                  auto_enroll=True)
    disposition, pdf = preparer(quiz)
    envoi = SimpleUploadedFile("seule.tif", en_tiff(copie(pdf, disposition, a, {0})))
    faux = SimpleUploadedFile("faux.tif", b"ceci n'est pas une image")
    lot = services.process_uploaded_files(quiz, [envoi, faux], label="une")
    feuilles = {f.source_name: f for f in lot.sheets.all()}
    assert feuilles["seule.tif"].student_id == a.pk, feuilles
    assert feuilles["faux.tif"].status == "error", feuilles["faux.tif"].status
    print("  TIFF d'une image : nom inchangé ; faux TIFF signalé sans arrêter le lot")


def pdf_bitonal(bitonal):
    """Une page en noir et blanc 1 bit, à 300 ppp, dans le PDF que rend le scanner."""
    tampon = io.BytesIO()
    Image.fromarray(bitonal).convert("1").save(tampon, format="PNG")
    doc = pymupdf.open()
    h, w = bitonal.shape[:2]
    page = doc.new_page(width=w * 72 / 300, height=h * 72 / 300)
    page.insert_image(page.rect, stream=tampon.getvalue())
    octets = doc.tobytes()
    doc.close()
    return octets


def verifier_noir_et_blanc(teacher):
    """Mode « noir et blanc » du scanner à 300 ppp : plusieurs étiquettes, car c'est la
    variété des motifs qui fait apparaître la panne du décodeur (1 sur 3 ratée en PDF)."""
    _, quiz, (a, _b) = creer_quiz("SC-NB", teacher, nb_qcm=PAGE_1, id_mode="sticker",
                                  auto_enroll=True)
    disposition, pdf = preparer(quiz)
    page = disposition["pages"][0]
    vierge = render_page(pdf, 0)

    class Candidat:
        class_group_id = a.class_group_id

        def __init__(self, pk):
            self.pk = pk

    for pk in range(2000, 2000 + 37 * 12, 37):
        scan = simulate_scan(stick_label(vierge.copy(), page, Candidat(pk)))
        gris = cv2.cvtColor(scan, cv2.COLOR_BGR2GRAY)
        bitonal = cv2.adaptiveThreshold(gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                        cv2.THRESH_BINARY, 51, 15)
        with services._open_pages("nb.pdf", pdf_bitonal(bitonal)) as pages:
            warped, _ = omr.detect_and_warp(pages[0][1]())
        lu = omr.read_qr(warped, page)
        assert lu[0] == "QS" and lu[2] == pk, f"étiquette {pk} non lue en noir et blanc : {lu}"
    print("  PDF en noir et blanc à 300 ppp : 12 étiquettes sur 12 lues")


def verifier_page_blanche(fiche):
    blanc = np.full_like(fiche, 255)
    assert omr.page_blanche(simulate_scan(blanc.copy())), "verso blanc non reconnu"
    # le recto vu par transparence : gris clair
    transparence = cv2.addWeighted(fiche, 0.25, blanc, 0.75, 0)
    assert omr.page_blanche(transparence), "transparence du recto prise pour de l'encre"
    assert not omr.page_blanche(simulate_scan(fiche.copy())), "fiche prise pour une page blanche"
    # une page qu'on ne sait pas lire n'est pas une page vide : quelques lignes suffisent
    texte = blanc.copy()
    for i in range(12):
        cv2.putText(texte, "Une page sans cadre mais pas vide", (260, 500 + i * 110),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.2, (20, 20, 20), 5)
    assert not omr.page_blanche(texte), "page écrite prise pour une page blanche"


def verifier_versos_blancs(teacher):
    client = Client()
    client.force_login(teacher)
    groupe, quiz, (a, b) = creer_quiz("SC-VERSO", teacher, id_mode="sticker", auto_enroll=True)
    disposition, pdf = preparer(quiz)
    fiche = render_page(pdf, 0)
    verifier_page_blanche(fiche)

    # Feuilles imprimées d'un seul côté, chargeur en recto-verso : un verso blanc après
    # chaque page. Le second candidat oublie l'étiquette de sa page 2.
    verso = simulate_scan(np.full_like(fiche, 255))
    pages = copie(pdf, disposition, a, {0, 1}) + copie(pdf, disposition, b, {0})
    lot = envoyer(quiz, [v for p in pages for v in (p, verso)], "versos")
    etats = [f.status for f in lot.sheets.order_by("pk")]
    assert etats == ["ok", "ignoree", "ok", "ignoree", "ok", "ignoree", "no_match", "ignoree"], etats
    rangs = lignes(quiz)
    assert rangs[a.pk]["score"] == NB_QCM, rangs[a.pk]
    assert rangs[b.pk]["score"] == PAGE_1 and rangs[b.pk]["questions_manquantes"] == 8, \
        rangs[b.pk]["score"]

    # Seule la vraie page manquante est signalée, pas les 4 versos.
    ecran = client.get(f"/quiz/{quiz.pk}/resultats/").content.decode()
    assert "1 page(s) non identifiée(s)" in ecran, "alerte fausse par les versos blancs"
    assert "Page blanche" in client.get(f"/lots/{lot.pk}/").content.decode()

    # La présélection saute le verso blanc entre la page 1 et la page 2.
    page2 = lot.sheets.get(status="no_match")
    assert services.suite_de_copie(page2, [a, b]) == b
    print("  4 versos blancs ignorés : seule la vraie page sans étiquette est signalée")


def jpeg_annonce(largeur, hauteur, progressif=False):
    """Un JPEG de quelques centaines d'octets dont l'en-tête annonce largeur × hauteur
    pixels : ce qu'un fichier piégé peut prétendre sans rien contenir."""
    drapeaux = [cv2.IMWRITE_JPEG_PROGRESSIVE, 1] if progressif else []
    octets = bytearray(cv2.imencode(".jpg", np.full((16, 16), 255, np.uint8), drapeaux)[1])
    i = 2
    while octets[i + 1] not in (0xC0, 0xC2):
        i += 2 + int.from_bytes(octets[i + 2:i + 4], "big")
    octets[i + 5:i + 9] = hauteur.to_bytes(2, "big") + largeur.to_bytes(2, "big")
    return bytes(octets)


def pdf_jpeg(flux, largeur_pt, hauteur_pt):
    """Le PDF d'un scanner : une page, une image JPEG pleine page, telle quelle."""
    doc = pymupdf.open()
    page = doc.new_page(width=largeur_pt, height=hauteur_pt)
    page.insert_image(page.rect, stream=flux)
    octets = doc.tobytes()
    doc.close()
    return octets


def verifier_haute_resolution(teacher):
    """Scanner réglé au-delà de 400 ppp (8 novembre 2026 : PDF à 1200 ppp, chaque page
    refusée « Image démesurée (9910 × 12694 pixels) ») : le JPEG se décode réduit, la
    limite porte sur l'image en mémoire ; un fichier piégé reste refusé sans être décodé.
    Le cas réel, page de garde à talon découpé, est dans test_e2e_talon."""
    blanc = np.full((40, 30), 255, np.uint8)
    couleur = cv2.imencode(".jpg", cv2.cvtColor(blanc, cv2.COLOR_GRAY2BGR))[1].tobytes()
    progressif = cv2.imencode(".jpg", blanc, [cv2.IMWRITE_JPEG_PROGRESSIVE, 1])[1].tobytes()
    assert services._entete_jpeg(cv2.imencode(".jpg", blanc)[1].tobytes()) == (30, 40, 1, True)
    assert services._entete_jpeg(couleur) == (30, 40, 3, True)
    assert services._entete_jpeg(progressif) == (30, 40, 1, False)
    for autre in (cv2.imencode(".png", blanc)[1].tobytes(), b"\xff\xd8\xff", b"", couleur[:3]):
        assert services._entete_jpeg(autre) is None, autre[:8]
    print("  en-tête JPEG lu sans décoder : taille, composantes, en un balayage ou non OK")

    _, quiz, (a, b) = creer_quiz("SC-PPP", teacher, nb_qcm=PAGE_1, id_mode="sticker",
                                 auto_enroll=True)
    disposition, pdf = preparer(quiz)

    def page_fine(candidat, ppp):
        """La copie scannée à ppp (les pages des tests sont rendues à 300 ppp)."""
        gris = cv2.cvtColor(copie(pdf, disposition, candidat, {0})[0], cv2.COLOR_BGR2GRAY)
        fine = cv2.resize(gris, None, fx=ppp / 300, fy=ppp / 300, interpolation=cv2.INTER_LINEAR)
        return fine.shape, cv2.imencode(".jpg", fine, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()

    # PDF à 600 ppp (35 millions de pixels, sous la limite) : décodé à la moitié, ramené à 200
    (hauteur, largeur), flux = page_fine(a, 600)
    pdf_600 = pdf_jpeg(flux, largeur * 72 / 600, hauteur * 72 / 600)
    with pymupdf.open(stream=pdf_600, filetype="pdf") as doc, \
            mock.patch.object(services.cv2, "imdecode", wraps=cv2.imdecode) as espion:
        lue = services._image_du_scanner(doc, doc[0])
    assert espion.call_args.args[1] & ~cv2.IMREAD_IGNORE_ORIENTATION == cv2.IMREAD_REDUCED_COLOR_2
    assert abs(lue.shape[1] - round(largeur / 3)) <= 2, (lue.shape, largeur)
    # image seule à 1200 ppp (139 millions) : décodée à la moitié, juste assez pour la limite
    (hauteur, largeur), flux_1200 = page_fine(b, 1200)
    assert largeur * hauteur > 3 * services.MAX_PIXELS_PAGE, (largeur, hauteur)
    with services._open_pages("fine.jpg", flux_1200) as pages:
        assert pages[0][1]().shape[:2] == (-(-hauteur // 2), -(-largeur // 2))
    lot = services.process_uploaded_files(quiz, [
        SimpleUploadedFile("scan600.pdf", pdf_600), SimpleUploadedFile("scan1200.jpg", flux_1200)],
        label="ppp")
    feuilles = list(lot.sheets.order_by("pk"))
    assert [f.student_id for f in feuilles] == [a.pk, b.pk], \
        [(f.status, f.error_message) for f in feuilles]
    rangs = lignes(quiz)
    assert rangs[a.pk]["score"] == rangs[b.pk]["score"] == PAGE_1, rangs
    print(f"  PDF à 600 ppp et image seule à 1200 ppp ({largeur} × {hauteur} pixels) : décodés "
          f"à la moitié, identifiés, {PAGE_1}/{PAGE_1} chacun OK")

    # Fichiers piégés : refusés sur leur seul en-tête, le message dit quoi régler.
    # Progressif au-delà de la limite (libjpeg garderait toute l'image même réduite) ;
    # séquentiel si grand que même le huitième dépasse la limite.
    a4_progressif = pdf_jpeg(jpeg_annonce(5400, 7640, progressif=True), 595, 842)
    carre = pdf_jpeg(jpeg_annonce(60000, 60000), 2268, 2268)          # 80 cm de côté
    with mock.patch.object(services.cv2, "imdecode", wraps=cv2.imdecode) as espion:
        for octets, ppp in ((a4_progressif, 653), (carre, 1905)):
            with services._open_pages("piege.pdf", octets, differer=True) as pages:
                try:
                    pages[0][1]()
                    raise AssertionError(f"image de {ppp} ppp acceptée")
                except ValueError as exc:
                    assert "démesurée" in str(exc) and f"{ppp} ppp" in str(exc) \
                        and "réglez le scanner sur 200 ou 300 ppp" in str(exc), str(exc)
        for octets in (jpeg_annonce(7000, 9900, progressif=True), jpeg_annonce(60000, 60000)):
            with services._open_pages("piege.jpg", octets) as pages:
                try:
                    pages[0][1]()
                    raise AssertionError("image piégée décodée")
                except ValueError as exc:
                    assert "démesurée" in str(exc) and "piege.jpg" in str(exc), str(exc)
    assert not espion.called, "une image démesurée a été décodée"
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile("progressif.pdf", a4_progressif)], label="piege")
    feuille = lot.sheets.get()
    assert lot.status == "done" and feuille.status == "error", (lot.status, feuille.status)
    assert "réglez le scanner" in feuille.error_message, feuille.error_message
    print("  JPEG piégés (progressif au-delà de la limite, 60000 × 60000) : refusés sur "
          "l'en-tête, sans décodage, le message dit quoi régler OK")


def main():
    logging.disable(logging.ERROR)     # le faux TIFF, refusé exprès, est journalisé
    Quiz.objects.filter(owner__username=PROF).delete()
    ClassGroup.objects.filter(owner__username=PROF).delete()
    User.objects.filter(username=PROF).delete()
    teacher = enseignant_complet(User.objects.create_user(PROF, password="x-pass-123"))
    print("TIFF multipage :")
    verifier_tiff(teacher)
    print("Noir et blanc :")
    verifier_noir_et_blanc(teacher)
    print("Versos blancs :")
    verifier_versos_blancs(teacher)
    print("Haute résolution :")
    verifier_haute_resolution(teacher)
    print("\n✅ SORTIES DU SCANNER À CHARGEUR : TEST RÉUSSI")


if __name__ == "__main__":
    main()
