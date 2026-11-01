"""Copies anonymes à n° de place (solution 2, autre choix que le talon) :
la copie porte un n° de place et le QR, jamais le nom ; la liste de placement
et d'émargement dit au surveillant qui s'assoit où ; au scan, le QR attribue
la copie au bon étudiant. Les places restent stables si la liste change.
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
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import services
from grader.models import ClassGroup, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan

REPONSES = {1: 0, 2: 1, 3: 2, 4: 3}
# volontairement pas dans l'ordre des n° : les places suivent le n° d'inscription
ETUDIANTS = [("CHERIF", "Lina", "104503"), ("AMRI", "Sana", "104501"),
             ("BAKIR", "Omar", "104502")]


def textes(pdf):
    return [p.get_text() for p in pymupdf.open(stream=pdf, filetype="pdf")]


def reglages(c, quiz, anonymat):
    c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "update_settings": "1", "title": quiz.title, "sheet_mode": "grid",
        "id_mode": "grid", "id_digits": "6", "wrong_penalty": "0", "anonymat": anonymat})
    quiz.refresh_from_db()


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_places").delete()
    ClassGroup.objects.filter(owner__username="prof_places").delete()
    User.objects.filter(username="prof_places").delete()
    prof = enseignant_complet(User.objects.create_user("prof_places", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    groupe = ClassGroup.objects.create(name="PLACES 3e A", owner=prof)
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=nom, first_name=prenom,
                               student_number=num)
    e = {s.last_name: s for s in groupe.students.all()}

    # ---- le choix : nom imprimé / talon / n° de place (exclusifs)
    quiz = Quiz.objects.create(title="Contrôle places", class_group=groupe, owner=prof,
                               sheet_mode="grid", id_mode="grid")
    for i, lettre in enumerate("ABCD", start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm",
                                correct_choice=ord(lettre) - 65)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert 'name="anonymat" value="place"' in page
    reglages(c, quiz, "talon")
    assert quiz.talon_nom and not quiz.place_anonyme
    reglages(c, quiz, "place")
    assert quiz.place_anonyme and not quiz.talon_nom and quiz.id_mode == "qr"
    print("  présentation des copies : nom / talon / n° de place, au choix "
          "(exclusifs) OK")

    # ---- génération : une copie anonyme par étudiant, places par n°
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    places = quiz.layout_json["places"]
    assert places == {str(e["AMRI"].pk): 1, str(e["BAKIR"].pk): 2,
                      str(e["CHERIF"].pk): 3}, places
    pdf = open(quiz.sheet_pdf.path, "rb").read()
    pages = textes(pdf)
    assert len(pages) == 3
    for k, t in enumerate(pages, start=1):
        assert f"PLACE N° {k}" in t and "COPIE ANONYME" in t
        assert not any(n in t for n, _, num in ETUDIANTS for n in (n, num)), \
            f"un nom ou un n° figure sur la copie {k}"
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert reverse("quiz_emargement_pdf", args=[quiz.pk]) in page
    print("  copies : « PLACE N° 1..3 », aucun nom ni n° d'inscription OK")

    # ---- liste de placement et d'émargement
    r = c.get(reverse("quiz_emargement_pdf", args=[quiz.pk]))
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    liste = textes(r.content)[0]
    assert "Liste de placement et d'émargement" in liste and "Signature" in liste
    for nom, prenom, num in ETUDIANTS:
        assert f"{nom} {prenom}" in liste and num in liste
    print("  liste d'émargement : place, n°, nom, case de signature OK")

    # ---- la copie de la place 2, scannée : BAKIR et ses réponses
    img = simulate_scan(fill_sheet(render_page(pdf, 1), quiz.layout_json, REPONSES,
                                   name_text=("", "")))
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile("place2.jpg", cv2.imencode(".jpg", img)[1].tobytes())])
    copie = lot.sheets.get()
    lus = {a.question.order: a.detected_choice for a in copie.answers.select_related("question")}
    assert copie.student_id == e["BAKIR"].pk and lus == REPONSES, (copie.student, lus)
    print("  scan de la copie « place 2 » : attribuée à BAKIR, réponses lues OK")

    # ---- un étudiant ajouté après l'impression : les places ne bougent pas
    nouveau = Student.objects.create(class_group=groupe, last_name="ABID", first_name="Ali",
                                     student_number="104500")   # n° plus petit
    assert c.get(reverse("quiz_detail", args=[quiz.pk])).context["layout_stale"]
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    places = quiz.layout_json["places"]
    assert places[str(e["AMRI"].pk)] == 1 and places[str(e["CHERIF"].pk)] == 3
    assert places[str(nouveau.pk)] == 4, places
    e["CHERIF"].delete()                                  # retiré : pas de renumérotation
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    assert sorted(quiz.layout_json["places"].values()) == [1, 2, 4]
    liste = textes(c.get(reverse("quiz_emargement_pdf", args=[quiz.pk])).content)[0]
    assert "ABID Ali" in liste and "CHERIF" not in liste
    print("  étudiant ajouté : place 4, les autres gardent la leur ; retiré : "
          "aucune renumérotation OK")

    # ---- concours et écrit : mêmes copies anonymes
    r = c.post(reverse("quiz_create") + "?concours=1", {
        "title": "Concours places", "type_epreuve": "qcm", "language": "fr",
        "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
        "anonymat": "place", "grading_mode": "immediate", "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors
    concours = Quiz.objects.get(title="Concours places")
    assert concours.place_anonyme and concours.id_mode == "qr"
    Question.objects.create(quiz=concours, order=1, qtype="qcm", correct_choice=0)
    for nom, prenom, num in ETUDIANTS[:2]:
        Student.objects.create(class_group=concours.class_group, last_name=nom,
                               first_name=prenom, student_number=num)
    c.post(reverse("quiz_detail", args=[concours.pk]), {"generate_pdf": "1"})
    concours.refresh_from_db()
    pages = textes(open(concours.sheet_pdf.path, "rb").read())
    assert len(pages) == 2 and "PLACE N° 1" in pages[0] and "AMRI" not in pages[0]
    r = c.post(reverse("quiz_create"), {
        "title": "Rédaction places", "type_epreuve": "ecrit", "class_group": groupe.pk,
        "language": "fr", "note_sur": "20", "sheet_mode": "grid", "id_mode": "grid",
        "id_digits": "6", "anonymat": "place", "wrong_penalty": "0"})
    ecrit = Quiz.objects.get(title="Rédaction places")
    assert ecrit.place_anonyme
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"generer": "1"})
    page = c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    assert "Liste de placement et d'émargement" in page
    assert c.get(reverse("quiz_emargement_pdf", args=[ecrit.pk])).status_code == 200
    print("  concours et écrit : copies à n° de place et liste d'émargement OK")

    # liste demandée sans copies anonymes : renvoi vers l'épreuve
    autre = Quiz.objects.create(title="Sans places", class_group=groupe, owner=prof)
    r = c.get(reverse("quiz_emargement_pdf", args=[autre.pk]))
    assert r.status_code == 302
    print("\n✅ TEST DES COPIES ANONYMES À N° DE PLACE RÉUSSI")


if __name__ == "__main__":
    main()
