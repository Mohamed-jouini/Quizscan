"""Copies à talon : le nom de l'étudiant est imprimé sur un talon, en haut de
la page, hors du cadre de lecture ; découpé avant la correction, il rend la
copie anonyme, et le QR (dans le cadre) l'identifie toujours au scan.

Vérifie : nom seulement sur le talon ; copie lue talon découpé (au trait,
un peu trop haut, un peu trop bas) ou non découpé ; pas de confusion avec
une copie normale ; concours et épreuve écrite.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import pymupdf
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from grader import layout as L, services
from grader.models import ClassGroup, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

REPONSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0}
ETUDIANTS = [("AMRI", "Sana", "104501"), ("BAKIR", "Omar", "104502"),
             ("CHERIF", "Lina", "104503")]
PT_PAR_MM = 72 / 25.4


def couper(img, a_mm):
    """La page telle qu'elle sort du massicot : le haut retiré jusqu'à a_mm."""
    return img[mm2px(a_mm):, :].copy()


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def _lire(quiz, nom_fichier, img):
    lot = services.process_uploaded_files(quiz, [SimpleUploadedFile(nom_fichier, jpg(img))])
    copie = lot.sheets.get()
    lus = {a.question.order: a.detected_choice for a in copie.answers.select_related("question")}
    return copie, lus


def main():
    logging.disable(logging.WARNING)
    Quiz.objects.filter(owner__username="prof_talon").delete()
    ClassGroup.objects.filter(owner__username="prof_talon").delete()
    User.objects.filter(username="prof_talon").delete()
    prof = enseignant_complet(User.objects.create_user("prof_talon", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    groupe = ClassGroup.objects.create(name="TALON 3e A", owner=prof)
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=nom, first_name=prenom,
                               student_number=num)

    # ---- réglage : la case « talon » impose les copies nominatives à QR
    quiz = Quiz.objects.create(title="Contrôle talon", class_group=groupe, owner=prof,
                               sheet_mode="grid", id_mode="grid")
    for i, lettre in enumerate("ABCDA", start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm",
                                correct_choice=ord(lettre) - 65)
    c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "update_settings": "1", "title": quiz.title, "sheet_mode": "grid",
        "id_mode": "grid", "id_digits": "6", "wrong_penalty": "0", "anonymat": "talon"})
    quiz.refresh_from_db()
    assert quiz.talon_nom and quiz.id_mode == "qr", (quiz.talon_nom, quiz.id_mode)
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    assert quiz.layout_json["talon"] and quiz.layout_json["cadre_haut"] == L.TALON_CADRE_HAUT
    pdf = open(quiz.sheet_pdf.path, "rb").read()
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert len(doc) == 3, len(doc)
    # le nom n'apparaît QUE sur le talon (au-dessus du trait de coupe)
    for k, (nom, _, num) in enumerate(ETUDIANTS):
        mots = doc[k].get_text("words")
        ys = [w[1] / PT_PAR_MM for w in mots if w[4] in (nom, num)]
        assert ys and all(y < L.TALON_COUPE for y in ys), (nom, ys)
    assert "COPIE ANONYME" in doc[0].get_text() and "découper" in doc[0].get_text()
    print("  copies à talon : nom et n° seulement au-dessus du trait de coupe OK")

    # ---- la copie de BAKIR, remplie, talon découpé : à qui ? quelles réponses ?
    bakir = groupe.students.get(last_name="BAKIR")
    page = fill_sheet(render_page(pdf, 1), quiz.layout_json, REPONSES, name_text=("", ""))
    for coupe in (30.0, 27.0, 34.0):
        copie, lus = _lire(quiz, f"coupe{coupe}.jpg", simulate_scan(couper(page, coupe)))
        assert copie.status == "ok" and copie.student_id == bakir.pk, (coupe, copie.status)
        assert lus == REPONSES, (coupe, lus)
    copie, lus = _lire(quiz, "entiere.jpg", simulate_scan(page))
    assert copie.student_id == bakir.pk and lus == REPONSES
    copie, lus = _lire(quiz, "envers.jpg",
                       cv2.rotate(simulate_scan(couper(page, 30.0)), cv2.ROTATE_180))
    assert copie.student_id == bakir.pk and lus == REPONSES
    print("  talon découpé (au trait, 3 mm trop haut, 4 mm trop bas), non découpé, "
          "à l'envers : BAKIR et ses réponses lus OK")

    # ---- une copie normale n'est pas prise pour une copie à talon
    normal = Quiz.objects.create(title="Contrôle normal", class_group=groupe, owner=prof,
                                 sheet_mode="grid", id_mode="qr")
    for i, lettre in enumerate("ABCDA", start=1):
        Question.objects.create(quiz=normal, order=i, qtype="qcm",
                                correct_choice=ord(lettre) - 65)
    c.post(reverse("quiz_detail", args=[normal.pk]), {"generate_pdf": "1"})
    normal.refresh_from_db()
    assert not normal.layout_json.get("talon")
    pdf_n = open(normal.sheet_pdf.path, "rb").read()
    page_n = fill_sheet(render_page(pdf_n, 2), normal.layout_json, REPONSES, name_text=("", ""))
    copie, lus = _lire(normal, "normale.jpg", simulate_scan(page_n))
    assert copie.student.last_name == "CHERIF" and lus == REPONSES, lus
    print("  copie normale toujours lue comme telle (pas de confusion de gabarit) OK")

    # ---- concours : copies nominatives à talon (et non fiches anonymes)
    r = c.post(reverse("quiz_create") + "?concours=1", {
        "title": "Concours talon", "type_epreuve": "qcm", "language": "fr",
        "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
        "anonymat": "talon", "grading_mode": "immediate", "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors
    concours = Quiz.objects.get(title="Concours talon")
    assert concours.talon_nom and concours.id_mode == "qr" and concours.auto_enroll
    Question.objects.create(quiz=concours, order=1, qtype="qcm", correct_choice=0)
    for nom, prenom, num in ETUDIANTS[:2]:
        Student.objects.create(class_group=concours.class_group, last_name=nom,
                               first_name=prenom, student_number=num)
    c.post(reverse("quiz_detail", args=[concours.pk]), {"generate_pdf": "1"})
    concours.refresh_from_db()
    doc = pymupdf.open(stream=open(concours.sheet_pdf.path, "rb").read(), filetype="pdf")
    assert len(doc) == 2 and "AMRI" in doc[0].get_text() and "BAKIR" in doc[1].get_text()
    print("  concours : une copie à talon par candidat OK")

    # ---- épreuve écrite à talon : la note lue, talon découpé
    r = c.post(reverse("quiz_create"), {
        "title": "Rédaction talon", "type_epreuve": "ecrit", "class_group": groupe.pk,
        "language": "fr", "note_sur": "20", "sheet_mode": "grid", "id_mode": "grid",
        "id_digits": "6", "anonymat": "talon", "wrong_penalty": "0"})
    ecrit = Quiz.objects.get(title="Rédaction talon")
    assert ecrit.talon_nom and ecrit.id_mode == "qr"
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"generer": "1"})
    ecrit.refresh_from_db()
    pdf_e = open(ecrit.sheet_pdf.path, "rb").read()
    grille = ecrit.layout_json["pages"][0]["note_grid"]
    pages_par_copie = 1 + ecrit.pages_redaction
    img = render_page(pdf_e, 2 * pages_par_copie)          # copie de CHERIF
    for row, d in zip(grille["rows"], [1, 3, 5, None]):     # 13,5
        if d is not None:
            cx, cy = row[d]
            cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(grille["r"] * 0.9),
                       (20, 20, 20), -1)
    services.process_uploaded_files(
        ecrit, [SimpleUploadedFile("ecrit.jpg", jpg(simulate_scan(couper(img, 30.0))))])
    rows = {r["student"].last_name: r["score"] for r in services.compute_results(ecrit)}
    assert rows == {"CHERIF": 13.5}, rows
    print("  écrit à talon : talon découpé, note 13,5 lue et affectée à CHERIF OK")
    print("\n✅ TEST DES COPIES À TALON RÉUSSI")


if __name__ == "__main__":
    main()
