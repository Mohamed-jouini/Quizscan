"""Calage des copies par un cadre fin (plus de carrés noirs dans les coins).

La page est recalée sur les quatre coins du cadre ; une rangée de tirets en
haut donne le n° de page et le sens. Vérifie : page droite, à l'envers,
tournée d'un quart de tour, photographiée sur un fond sombre, pages
multiples, page sans cadre refusée, et copies d'avant (repères ArUco dans les
coins) toujours lues.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()

import cv2
import numpy as np
import pymupdf
from django.contrib.auth.models import User

from grader import layout as L, omr, sheet_pdf
from grader.models import ClassGroup, Question, Quiz
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

REPONSES = {1: 0, 2: 1, 3: 2, 4: 3}


def _quiz():
    u, _ = User.objects.get_or_create(username="essai_cadre")
    Quiz.objects.filter(owner=u).delete()
    ClassGroup.objects.filter(owner=u).delete()
    g = ClassGroup.objects.create(name="CADRE", owner=u)
    q = Quiz.objects.create(title="Cadre", class_group=g, owner=u, sheet_mode="grid",
                            id_mode="grid")
    for i in range(1, 5):
        Question.objects.create(quiz=q, order=i, qtype="qcm", correct_choice=0)
    return q


def _lit(img, layout, page_attendue=0):
    warped, page = omr.detect_and_warp(img)
    assert page == page_attendue, page
    lus = omr.read_bubbles(warped, layout["pages"][page])
    return {o: r["choice"] for o, r in lus.items()}


def main():
    quiz = _quiz()
    layout = L.build_layout(quiz)
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    # plus aucune image (les repères ArUco étaient des images) sur la fiche
    assert not doc[0].get_images(), "il reste des images (repères) sur la fiche"
    page = fill_sheet(render_page(pdf), layout, REPONSES, student_number="104523")
    scan = simulate_scan(page)
    assert _lit(scan, layout) == REPONSES
    print("  cadre fin, sans carrés dans les coins : copie lue OK")

    assert _lit(cv2.rotate(scan, cv2.ROTATE_180), layout) == REPONSES
    assert _lit(cv2.rotate(scan, cv2.ROTATE_90_CLOCKWISE), layout) == REPONSES
    assert _lit(cv2.rotate(scan, cv2.ROTATE_90_COUNTERCLOCKWISE), layout) == REPONSES
    print("  page à l'envers ou tournée d'un quart de tour : lue OK")

    # photo : la feuille posée sur une table sombre, en légère perspective
    h, w = scan.shape[:2]
    fond = np.full((int(h * 1.25), int(w * 1.25), 3), 45, np.uint8)
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[160, 120], [w + 260, 200], [w + 180, h + 330], [90, h + 260]])
    photo = cv2.warpPerspective(scan, cv2.getPerspectiveTransform(src, dst),
                                (fond.shape[1], fond.shape[0]), dst=fond,
                                borderMode=cv2.BORDER_TRANSPARENT)
    assert _lit(photo, layout) == REPONSES
    print("  photo sur fond sombre, en perspective : lue OK")

    # le n° de page est codé : chaque page se reconnaît
    for k in (1, 5, 37, 61):
        assert L.lire_code_page(L.code_page(k)) == k
    assert L.lire_code_page(L.code_page(3)[::-1]) is None   # à l'envers : invalide
    print("  code de page : 62 pages, sens détecté OK")

    # une page lignée ou blanche (sans cadre) n'est pas prise pour une copie
    blanche = np.full_like(scan, 255)
    cv2.line(blanche, (100, 500), (2300, 500), (0, 0, 0), 3)
    try:
        omr.detect_and_warp(blanche)
        raise AssertionError("page sans cadre acceptée")
    except omr.MarkerError:
        pass
    print("  page sans cadre : refusée OK")

    # copie imprimée AVANT : quatre repères ArUco dans les coins
    ancienne = page.copy()
    cv2.rectangle(ancienne, (0, 0), (ancienne.shape[1], ancienne.shape[0]),
                  (255, 255, 255), mm2px(L.CADRE_MARGE + 2) * 2)   # efface le cadre
    for (x, y, w_, h_) in L.code_cases():
        cv2.rectangle(ancienne, (mm2px(x) - 2, mm2px(y) - 2),
                      (mm2px(x + w_) + 2, mm2px(y + h_) + 2), (255, 255, 255), -1)
    for mid, (x, y) in L.marker_positions(0).items():
        arr = cv2.aruco.generateImageMarker(sheet_pdf.ARUCO_DICT, mid,
                                            mm2px(L.MARKER_SIZE))
        arr = cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        ancienne[mm2px(y):mm2px(y) + arr.shape[0], mm2px(x):mm2px(x) + arr.shape[1]] = arr
    assert _lit(simulate_scan(ancienne), layout) == REPONSES
    print("  copie d'avant (carrés ArUco) : toujours lue OK")

    # PDF déjà enregistré avec les carrés : refait avec le cadre, à l'identique
    from django.core.files.base import ContentFile
    from grader import services
    ancien_layout = {k: v for k, v in layout.items() if k != "calage"}
    quiz.layout_json = ancien_layout
    quiz.sheet_pdf.save("ancienne.pdf", ContentFile(b"%PDF-ancien"), save=True)
    assert services.moderniser_calage(quiz)
    quiz.refresh_from_db()
    assert quiz.layout_json["calage"] == "cadre"
    neuf = pymupdf.open(stream=open(quiz.sheet_pdf.path, "rb").read(), filetype="pdf")
    assert not neuf[0].get_images()
    assert not services.moderniser_calage(quiz), "refait deux fois"
    print("  PDF déjà généré avec les carrés : refait avec le cadre OK")
    print("\n✅ TEST DU CALAGE PAR CADRE RÉUSSI")


if __name__ == "__main__":
    main()
