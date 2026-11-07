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
    print("\n✅ SORTIES DU SCANNER À CHARGEUR : TEST RÉUSSI")


if __name__ == "__main__":
    main()
