"""Identification automatique par le nom manuscrit (mode « nom »).

Tesseract lit mal l'écriture à la main : « DIWOSACCU SATLH » pour
« BEN SALEM Samia ». La lecture est donc tentée sur plusieurs variantes de
l'image et rapprochée de la liste FERMÉE de la classe ; les étudiants qui ont
déjà leur copie sont écartés, et une seconde passe en fin de lot profite de
ces éliminations. Ces tests simulent l'OCR (pas besoin de Tesseract).
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import pytesseract
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse

from grader import layout as L
from grader import omr, services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan

KEY = "ABCDAB"
REPONSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3}
ILLISIBLE = (["DIWOSACCU"] * 3, ["SATLH"] * 3)   # lecture réelle signalée


class FauxOCR:
    """Rejoue des lectures : pour chaque copie, les 3 variantes de la case
    NOM puis les 3 de la case PRÉNOM. Une fois épuisées : lecture illisible."""

    def __init__(self, *copies):
        self.appels = [t for noms, prenoms in copies for t in (*noms, *prenoms)]
        self.n = 0

    def __call__(self, *a, **k):
        if self.appels:
            return self.appels.pop(0)
        self.n += 1
        return ILLISIBLE[0 if (self.n - 1) % 6 < 3 else 1][0]


def absent(*a, **k):
    raise pytesseract.TesseractNotFoundError()


def _quiz(nom, teacher):
    group = ClassGroup.objects.create(name=nom, owner=teacher)
    etudiants = {
        "BEN SALEM": Student.objects.create(class_group=group, last_name="BEN SALEM",
                                            first_name="Samia"),
        "ABDELLATIF": Student.objects.create(class_group=group, last_name="ABDELLATIF",
                                             first_name="Mohamed"),
        "TRABELSI": Student.objects.create(class_group=group, last_name="TRABELSI",
                                           first_name="Mariem"),
    }
    quiz = Quiz.objects.create(title=f"Identification {nom}", class_group=group,
                               owner=teacher, sheet_mode="grid", id_mode="name")
    for i, letter in enumerate(KEY, start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                correct_choice=ord(letter) - ord("A"))
    layout = L.build_layout(quiz)
    quiz.layout_json = layout
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    quiz.sheet_pdf.save(f"ident_{nom}.pdf", ContentFile(pdf), save=True)
    img = simulate_scan(fill_sheet(render_page(pdf), layout, REPONSES))
    data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
    return quiz, etudiants, data


def _lot(quiz, data, noms, ocr, label):
    vrai = pytesseract.image_to_string
    pytesseract.image_to_string = ocr
    try:
        return services.process_uploaded_files(
            quiz, [SimpleUploadedFile(f"{n}.jpg", data) for n in noms], label=label)
    finally:
        pytesseract.image_to_string = vrai


def test_rapprochement():
    """Le rapprochement ignore la casse et les accents, et une lecture
    illisible seule ne désigne personne."""
    class S:
        def __init__(self, pk, nom, prenom):
            self.pk, self.last_name, self.first_name = pk, nom, prenom
    classe = [S(1, "BEN SALEM", "Samia"), S(2, "ABDELLATIF", "Mohamed"),
              S(3, "TRABELSI", "Mariem")]
    e, score, _, _ = omr.identifier([("BEN SALEM", "SAMIA")], classe)
    assert e is classe[0] and score == 100, score   # capitales : 73 % avant
    e, _, _, _ = omr.identifier([("Bén Salem", "samïa")], classe)
    assert e is classe[0]
    e, _, _, _ = omr.identifier([("DIWOSACCU", "SATLH")], classe)
    assert e is None
    e, _, _, _ = omr.identifier([("KHELIFI", "OMAR")], classe)
    assert e is None
    print("  casse et accents ignorés ; lecture illisible ou nom inconnu : personne OK")


def test_variantes(teacher):
    quiz, et, data = _quiz("VAR", teacher)
    lot = _lot(quiz, data, ["copie"], FauxOCR(
        (["DIWOSACCU", "8EN SALEN", "!!"], ["SATLH", "SAMlA", ""])), "variantes")
    sheet = lot.sheets.get()
    assert sheet.status == "ok" and sheet.student_id == et["BEN SALEM"].pk, \
        (sheet.status, sheet.student, sheet.ocr_name_raw, sheet.match_score)
    assert sum(a.is_correct for a in sheet.answers.select_related("question")) == 5
    print(f"  1re variante illisible, 2e lisible : BEN SALEM ({sheet.match_score:.0f} %) OK")


def test_elimination_et_doublon(teacher):
    quiz, et, data = _quiz("ELIM", teacher)
    # la copie illisible passe EN PREMIER : à ce moment personne n'a sa copie
    lot = _lot(quiz, data, ["illisible", "abdellatif", "trabelsi"], FauxOCR(
        ILLISIBLE,
        (["ABDELLATIF"] * 3, ["Mohamed"] * 3),
        (["TRABELSI"] * 3, ["Mariem"] * 3)), "elimination")
    copies = {s.source_name.split(".")[0]: s for s in lot.sheets.all()}
    assert copies["abdellatif"].student_id == et["ABDELLATIF"].pk
    assert copies["trabelsi"].student_id == et["TRABELSI"].pk
    ill = copies["illisible"]
    assert ill.status == "ok" and ill.student_id == et["BEN SALEM"].pk, \
        (ill.status, ill.student, ill.ocr_name_raw)
    c = Client()
    c.force_login(teacher)
    page = c.get(reverse("sheet_review", args=[ill.pk])).content.decode()
    assert "attribué par élimination" in page, "explication attendue"
    page = c.get(reverse("sheet_review", args=[copies["trabelsi"].pk])).content.decode()
    assert "Reconnu par le nom écrit" in page
    print("  copie illisible, les 2 autres reconnues : seconde passe -> BEN SALEM OK")

    # la même copie scannée une seconde fois n'écrase rien
    lot2 = _lot(quiz, data, ["rescan"], FauxOCR(
        (["BEN SALEM"] * 3, ["Samia"] * 3)), "doublon")
    double = lot2.sheets.get()
    assert double.status == "no_match" and double.student is None, double.student
    print("  copie scannée deux fois : reste « à identifier » OK")

    c = Client()
    c.force_login(teacher)
    vrai = pytesseract.image_to_string
    pytesseract.image_to_string = FauxOCR((["BEN SALEM"] * 3, ["Samia"] * 3))
    try:
        r = c.post(reverse("batch_detail", args=[lot2.pk]), {"reidentifier": "1"}, follow=True)
    finally:
        pytesseract.image_to_string = vrai
    assert r.status_code == 200
    assert "Aucune copie de plus" in r.content.decode(), "message attendu"
    double.refresh_from_db()
    assert double.student is None
    print("  bouton « Identifier automatiquement » sans effet sur un doublon OK")


def test_bouton_et_proposition(teacher):
    quiz, et, data = _quiz("BTN", teacher)
    lot = _lot(quiz, data, ["copie"], absent, "sans-tesseract")
    sheet = lot.sheets.get()
    assert sheet.status == "no_match" and sheet.student is None
    assert sum(a.is_correct for a in sheet.answers.select_related("question")) == 5
    print("  Tesseract absent : copie lue (5/6), « à identifier » OK")

    c = Client()
    c.force_login(teacher)
    page = c.get(reverse("batch_detail", args=[lot.pk])).content.decode()
    assert "Identifier automatiquement (1)" in page, "bouton attendu"
    # sans lecture et 3 étudiants libres : pas de proposition
    page = c.get(reverse("sheet_review", args=[sheet.pk])).content.decode()
    assert "Étudiant proposé" not in page and "Choisissez l'étudiant" in page
    # un nom lu (même partiel) : proposé et présélectionné
    sheet.ocr_name_raw = "SALEM"
    sheet.save(update_fields=["ocr_name_raw"])
    page = c.get(reverse("sheet_review", args=[sheet.pk])).content.decode()
    assert "Étudiant proposé" in page, "proposition attendue"
    assert f'value="{et["BEN SALEM"].pk}" selected' in page
    print("  écran de vérification : BEN SALEM proposé et présélectionné OK")

    # Tesseract toujours absent : la lecture déjà faite sert (et reste)
    vrai = pytesseract.image_to_string
    pytesseract.image_to_string = absent
    try:
        assert services.reidentifier_lot(lot) == 1
    finally:
        pytesseract.image_to_string = vrai
    sheet.refresh_from_db()
    assert sheet.student_id == et["BEN SALEM"].pk and sheet.ocr_name_raw == "SALEM"
    print("  OCR muet au 2e passage : la lecture enregistrée sert OK")
    sheet.student, sheet.status, sheet.ocr_name_raw = None, "no_match", ""
    sheet.save()

    pytesseract.image_to_string = FauxOCR((["8EN SALEM"] * 3, ["Samia"] * 3))
    try:
        r = c.post(reverse("batch_detail", args=[lot.pk]), {"reidentifier": "1"}, follow=True)
    finally:
        pytesseract.image_to_string = vrai
    assert "1 copie(s) identifiée(s) automatiquement" in r.content.decode()
    sheet.refresh_from_db()
    assert sheet.status == "ok" and sheet.student_id == et["BEN SALEM"].pk
    assert "Identifier automatiquement" not in c.get(reverse("batch_detail", args=[lot.pk])).content.decode()
    assert sheet.modifications.filter(action="identification").exists()
    print("  bouton « Identifier automatiquement » : copie affectée et journalisée OK")


def main():
    logging.disable(logging.WARNING)
    Quiz.objects.filter(owner__username="prof_ident").delete()
    ClassGroup.objects.filter(owner__username="prof_ident").delete()
    User.objects.filter(username="prof_ident").delete()
    teacher = enseignant_complet(
        User.objects.create_user("prof_ident", password="x-pass-123"))

    print("Rapprochement :")
    test_rapprochement()
    print("Variantes d'image :")
    test_variantes(teacher)
    print("Élimination et doublon :")
    test_elimination_et_doublon(teacher)
    print("Bouton et proposition :")
    test_bouton_et_proposition(teacher)
    print("\n✅ TESTS D'IDENTIFICATION RÉUSSIS")


if __name__ == "__main__":
    main()
