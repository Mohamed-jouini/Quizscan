"""Une QCM = un point ; le résultat se lit en bonnes réponses, en note sur 20
(plus en pourcentage depuis le 13 novembre 2026), et admis / non admis — sur la
page des résultats, l'espace public, les bulletins, la liste des admis et
l'export Excel.
"""
import io
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (quizscan_test)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import importlib

import pymupdf
from django.apps import apps
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from openpyxl import load_workbook

from grader import services
from grader.models import (Answer, ClassGroup, Question, Quiz, ScanBatch, SheetScan,
                           Student)
from test_commun import enseignant_complet

# nom : bonnes réponses sur 6
CANDIDATS = {"AMRI": 5, "BAKIR": 3, "CHERIF": 2}


def _epreuve(prof, avec_redaction=False):
    groupe = ClassGroup.objects.create(name=f"UNPT{avec_redaction:d}", owner=prof)
    quiz = Quiz.objects.create(title=f"Un point {avec_redaction:d}", class_group=groupe,
                               owner=prof, sheet_mode="grid", id_mode="grid",
                               note_admission=3)
    qcm = [Question.objects.create(quiz=quiz, order=n, qtype="qcm", points=3,
                                   num_choices=4, correct_choice=0)
           for n in range(1, 7)]
    if avec_redaction:
        Question.objects.create(quiz=quiz, order=7, qtype="open", points=4)
    lot = ScanBatch.objects.create(quiz=quiz, status="done", total_pages=3)
    for i, (nom, justes) in enumerate(CANDIDATS.items()):
        s = Student.objects.create(class_group=groupe, last_name=nom, first_name="X",
                                   student_number=f"88{avec_redaction:d}{i}")
        copie = SheetScan.objects.create(batch=lot, status="ok", student=s,
                                         source_name=f"{nom}.jpg")
        for n, q in enumerate(qcm, start=1):
            a = Answer(sheet=copie, question=q, detected_choice=0 if n <= justes else 1)
            a.points_awarded = services._auto_points(q, a)
            a.save()
    return quiz


def main():
    Quiz.objects.filter(owner__username="prof_unpt").delete()
    ClassGroup.objects.filter(owner__username="prof_unpt").delete()
    User.objects.filter(username="prof_unpt").delete()
    prof = enseignant_complet(User.objects.create_user("prof_unpt", password="x-pass-123"))
    c = Client()
    c.force_login(prof)

    quiz = _epreuve(prof)
    assert set(quiz.questions.values_list("points", flat=True)) == {1.0}, \
        "une QCM créée avec 3 points doit valoir 1 point"
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    a = rows["AMRI"]
    assert (a["correct"], a["n_qcm"], a["score"], a["note20"]) == (5, 6, 5.0, 16.67), a
    assert rows["BAKIR"]["note20"] == 10.0 and rows["CHERIF"]["note20"] == 6.67
    assert not quiz.note_distincte
    print("  une QCM vaut 1 point : 5 bonnes réponses = 5 / 6 = 16,67 / 20 OK")

    # la note sur 20, nulle part le pourcentage (demande du 13 novembre 2026)
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    for attendu in ("<th>Bonnes réponses</th>", "<th>Note sur 20</th>", "5 / 6",
                    "<b>16,67 / 20</b>", "<b>10 / 20</b>", "Réussi", "Non réussi",
                    "au moins 3 bonnes réponses sur 6, soit 10 / 20",
                    "réussi ou non, sans sa note",
                    "Répartition des notes sur 20", ">8–12<", "meilleure note <b>16,67</b>",
                    "(sur 20)"):
        assert attendu in page, f"« {attendu} » absent de la page des résultats"
    for absent in ("<th>Pourcentage</th>", "83 %", "<th>Note</th>", "<th>Points</th>",
                   "80–100 %"):
        assert absent not in page, f"« {absent} » encore sur la page des résultats"
    print("  page des résultats : bonnes réponses, note sur 20 (seuil compris), "
          "réussi / non réussi, sans pourcentage OK")

    xl = load_workbook(io.BytesIO(c.get(reverse("quiz_results_xlsx", args=[quiz.pk])).content))
    ws = xl["Résultats"]
    entetes = [x.value for x in ws[2]]
    assert "Pourcentage" not in entetes and "Note" not in entetes, entetes
    ligne = {r[entetes.index("Nom")].value: r for r in ws.iter_rows(min_row=3)}["AMRI"]
    assert ligne[entetes.index("Note sur 20")].value == 16.67
    assert ligne[entetes.index("Note sur 20")].number_format == "0.00"
    assert ligne[entetes.index("Points")].value == 5
    assert ligne[entetes.index("Correctes")].value == 5
    assert ligne[entetes.index("Résultat")].value == "Admis"
    admis = [x.value for x in xl["Admis"][2]]
    assert "Note sur 20" in admis and "Pourcentage" not in admis, admis
    print("  Excel : colonnes « Correctes », « Note sur 20 », « Résultat » OK")

    pdf = c.get(reverse("quiz_bulletins_pdf", args=[quiz.pk]) + "?student="
                + str(a["student"].pk)).content
    texte = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
    assert "BONNES RÉPONSES" in texte and "5 / 6" in texte and "NOTE SUR 20" in texte \
        and "16,67 / 20" in texte and "%" not in texte, texte
    texte = pymupdf.open(stream=c.get(reverse("quiz_admis_pdf", args=[quiz.pk])).content,
                         filetype="pdf")[0].get_text()
    assert "Bonnes rép." in texte and "5 / 6" in texte and "Note / 20" in texte \
        and "16,67" in texte and "3 / 6 (10 / 20)" in texte and "83 %" not in texte, texte
    assert "CHERIF" not in texte
    print("  bulletin et liste des réussis en PDF : bonnes réponses et note sur 20 OK")

    c.post(reverse("quiz_publication", args=[quiz.pk]), {"resultats_publies": "1",
                                                         "admis_publies": "1"})
    # espace public : le résultat seul, « Réussi » ou « Non réussi » (« Admis » ou « Non
    # admis » en concours) : ni note, ni bonnes réponses, ni rang (13 novembre 2026)
    cache.clear()
    anonyme = Client()
    page = anonyme.get(reverse("public_resultat") + "?numero=8800").content.decode()
    assert '<p class="pub-verdict pub-verdict--admis">Réussi</p>' in page, page[-1500:]
    refus = anonyme.get(reverse("public_resultat") + "?numero=8802").content.decode()
    assert '<p class="pub-verdict pub-verdict--refuse">Non réussi</p>' in refus, refus[-1500:]
    liste = anonyme.get(reverse("public_epreuve", args=[quiz.pk])).content.decode()
    assert "<th>Résultat</th>" in liste and "<th>Rang</th>" not in liste
    assert "8800" in liste and "8801" in liste and "8802" not in liste, "réussis seulement"
    for texte in (page, refus, liste):
        for absent in ("bonnes réponses", "16,67", "6,67", "/ 20", "83 %", "<progress",
                       "<b>5</b>", "Note sur 20", "Pourcentage", "ordre de mérite"):
            assert absent not in texte, absent
    # la liste suit le n° d'inscription, pas le mérite (l'ordre dirait le rang) : AMRI, la
    # meilleure, sous un n° plus grand que celui de BAKIR passe après lui
    amri = Student.objects.get(class_group=quiz.class_group, last_name="AMRI")
    amri.student_number = "8809"
    amri.save(update_fields=["student_number"])
    cache.clear()
    liste = anonyme.get(reverse("public_epreuve", args=[quiz.pk])).content.decode()
    assert liste.index("8801") < liste.index("8809"), "liste par ordre de mérite"
    amri.student_number = "8800"
    amri.save(update_fields=["student_number"])
    cache.clear()
    print("  espace public : « Réussi » ou « Non réussi » seulement ; liste des réussis par "
          "n° d'inscription, sans rang ni note OK")

    # avec une réponse manuscrite, les points ne se confondent plus avec les bonnes
    # réponses : colonne « Points » en plus de la note sur 20
    quiz2 = _epreuve(prof, avec_redaction=True)
    assert quiz2.questions.get(order=7).points == 4.0
    assert quiz2.note_distincte
    page = c.get(reverse("quiz_results", args=[quiz2.pk])).content.decode()
    assert "<th>Points</th>" in page and "/ 10" in page and "<th>Note sur 20</th>" in page
    print("  réponse manuscrite : barème libre, colonne « Points » en plus OK")

    # reprise des épreuves existantes (migration 0028)
    q = quiz.questions.get(order=1)
    Question.objects.filter(pk=q.pk).update(points=3)
    Answer.objects.filter(question=q, detected_choice=0).update(points_awarded=3)
    migration = importlib.import_module("grader.migrations.0028_qcm_un_point")
    migration.un_point(apps, None)
    q.refresh_from_db()
    assert q.points == 1.0
    assert set(Answer.objects.filter(question=q, detected_choice=0)
               .values_list("points_awarded", flat=True)) == {1.0}
    print("  migration : QCM existantes ramenées à 1 point, notes recalculées OK")
    print("\n✅ TEST « UNE QCM = UN POINT » RÉUSSI")


if __name__ == "__main__":
    main()
