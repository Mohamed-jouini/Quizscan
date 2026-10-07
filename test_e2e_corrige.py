"""Test du corrigé facultatif puis scanné.

Scénario complet :
  1. on crée un quiz SANS renseigner les bonnes réponses ;
  2. le téléversement des copies les met en attente, rien n'est noté ;
  3. « Lancer la correction » est refusé, avec un message explicite ;
  4. on imprime la fiche, on noircit les bonnes réponses, on la scanne :
     le corrigé se renseigne et la fiche reste conservée avec l'épreuve ;
  5. la correction part alors et les notes sont justes ;
  6. une case laissée vide sur le corrigé laisse la question « à définir »,
     qu'on complète à la main.
"""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
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
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from grader import services
from grader.models import (AnswerKeySheet, ClassGroup, Question, Quiz, ScanBatch,
                           Student)
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan

CORRIGE = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 1}      # A B C D A B
COPIE = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3}        # 5 justes sur 6


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


def main():
    logging.disable(logging.WARNING)
    Quiz.objects.filter(owner__username="prof_cor").delete()
    ClassGroup.objects.filter(owner__username="prof_cor").delete()
    User.objects.filter(username="prof_cor").delete()
    teacher = User.objects.create_user("prof_cor", password="x-pass-123")
    teacher = enseignant_complet(teacher)
    c = Client()
    c.force_login(teacher)

    group = ClassGroup.objects.create(name="COR-3A", owner=teacher)
    eleve = Student.objects.create(class_group=group, last_name="BEN SALAH",
                                   first_name="Ahmed", student_number="104523")
    quiz = Quiz.objects.create(title="Contrôle sans corrigé", class_group=group,
                               owner=teacher, sheet_mode="grid", id_mode="grid",
                               id_digits=6)

    # ---- 1) questions créées SANS bonne réponse, par le formulaire web ----
    for i in range(1, 7):
        r = c.post(f"/quiz/{quiz.pk}/", {
            "add_question": "1", "qtype": "qcm", "text": f"Question {i}",
            "choices_text": "", "num_choices": 4, "correct_letter": "",
            "points": "1", "open_height_mm": 25})
        assert r.status_code == 302, "la bonne réponse doit être facultative"
    assert quiz.questions.count() == 6, quiz.questions.count()
    assert not quiz.corrige_complet
    assert list(quiz.questions_sans_corrige.values_list("order", flat=True)) == \
        [1, 2, 3, 4, 5, 6]
    print("  6 questions créées sans bonne réponse (formulaire accepté) OK")

    c.post(f"/quiz/{quiz.pk}/", {"generate_pdf": "1"})
    quiz.refresh_from_db()
    pdf = quiz.sheet_pdf.read()
    layout = quiz.layout_json

    # ---- 2) les copies arrivent mais ne sont pas notées -------------------
    copie = jpg(simulate_scan(fill_sheet(render_page(pdf), layout, COPIE,
                                         student_number=eleve.student_number)))
    r = c.post(f"/quiz/{quiz.pk}/upload/",
               {"label": "Salle A", "files": [SimpleUploadedFile("c1.jpg", copie)]})
    assert r.status_code == 302
    lot = ScanBatch.objects.filter(quiz=quiz).latest("pk")
    assert lot.status == "pending", lot.status
    assert lot.sheets.count() == 0, "aucune copie ne doit être lue"
    print("  copies reçues et mises en attente, rien n'est noté OK")

    # ---- 3) « Lancer la correction » est refusé ---------------------------
    r = c.post(f"/quiz/{quiz.pk}/", {"launch_grading": "1"}, follow=True)
    corps = r.content.decode()
    assert "sans bonne réponse" in corps, corps[:400]
    lot.refresh_from_db()
    assert lot.status == "pending", "le lot ne doit pas avoir démarré"
    print("  « Lancer la correction » refusé, message explicite OK")

    # ---- 4) on scanne la fiche remplie avec les bonnes réponses ----------
    feuille = jpg(simulate_scan(fill_sheet(render_page(pdf), layout, CORRIGE)))
    r = c.post(f"/quiz/{quiz.pk}/corrige/",
               {"files": [SimpleUploadedFile("corrige.jpg", feuille)]}, follow=True)
    assert r.status_code == 200
    quiz.refresh_from_db()
    lu = dict(quiz.questions.values_list("order", "correct_choice"))
    assert lu == CORRIGE, lu
    assert quiz.corrige_complet
    page = AnswerKeySheet.objects.filter(quiz=quiz).latest("pk")
    assert page.status == "ok" and page.nb_lues == 6, (page.status, page.nb_lues)
    assert page.image and page.overlay_image, "la fiche doit être conservée"
    print(f"  corrigé scanné : 6 bonnes réponses lues, fiche conservée "
          f"({page.source_name}) OK")

    # ---- 5) la correction part et les notes sont justes ------------------
    r = c.post(f"/quiz/{quiz.pk}/", {"launch_grading": "1"})
    assert r.status_code == 302
    import time
    t0 = time.time()
    while quiz.batches.exclude(status="done").exists():
        assert time.time() - t0 < 120, "correction trop longue"
        time.sleep(0.3)
    ligne = services.compute_results(quiz)[0]
    assert ligne["score"] == 5.0 and ligne["correct"] == 5 and ligne["wrong"] == 1, ligne
    print(f"  correction lancée : note {ligne['score']}/{ligne['max_score']} "
          f"({ligne['correct']} justes, {ligne['wrong']} fausse) OK")

    # ---- 6) corrigé partiel : la question illisible reste à définir -------
    quiz2 = Quiz.objects.create(title="Corrigé partiel", class_group=group,
                                owner=teacher, sheet_mode="grid", id_mode="grid")
    for i in range(1, 5):
        Question.objects.create(quiz=quiz2, order=i, qtype="qcm", points=1.0,
                                num_choices=4)
    c.post(f"/quiz/{quiz2.pk}/", {"generate_pdf": "1"})
    quiz2.refresh_from_db()
    pdf2, layout2 = quiz2.sheet_pdf.read(), quiz2.layout_json
    partiel = {1: 0, 2: 1, 4: 3}          # la question 3 reste vide
    feuille2 = jpg(simulate_scan(fill_sheet(render_page(pdf2), layout2, partiel)))
    c.post(f"/quiz/{quiz2.pk}/corrige/",
           {"files": [SimpleUploadedFile("partiel.jpg", feuille2)]}, follow=True)
    quiz2.refresh_from_db()
    reste = list(quiz2.questions_sans_corrige.values_list("order", flat=True))
    assert reste == [3], reste
    page2 = AnswerKeySheet.objects.filter(quiz=quiz2).latest("pk")
    assert page2.status == "partial" and page2.unreadable == [3], \
        (page2.status, page2.unreadable)
    print("  case laissée vide : question 3 signalée « à définir » OK")

    # on la complète à la main, le quiz redevient corrigeable
    q3 = quiz2.questions.get(order=3)
    r = c.post(f"/quiz/{quiz2.pk}/", {"edit_question": q3.pk, "correct": "2",
                                      "points": "1"})
    quiz2.refresh_from_db()
    assert quiz2.corrige_complet and quiz2.questions.get(order=3).correct_choice == 2
    print("  complétée à la main : corrigé complet OK")

    print("\n✅ TEST DU CORRIGÉ SCANNÉ RÉUSSI")


if __name__ == "__main__":
    main()
