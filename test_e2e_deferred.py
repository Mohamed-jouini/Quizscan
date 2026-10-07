"""Test du concours « correction après scan » : les lots téléversés restent
en attente, puis « Lancer la correction » les corrige tous d'un coup."""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment

from grader.models import Question, Quiz, ScanBatch, SheetScan, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan
from test_e2e_web import jpg, stick_label

KEY = "ABCD"


def wait_done(quiz, timeout=120):
    t0 = time.time()
    while quiz.batches.exclude(status="done").exists():
        assert time.time() - t0 < timeout, "correction trop longue"
        time.sleep(0.3)


def main():
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_def").delete()
    User.objects.filter(username="prof_def").delete()
    teacher = User.objects.create_user("prof_def", password="x-pass-123")
    teacher = enseignant_complet(teacher)
    c = Client()
    c.force_login(teacher)

    r = c.post("/quiz/nouveau/?concours=1", {
        "title": "Concours après scan", "language": "fr", "sheet_mode": "grid",
        "id_mode": "sticker", "id_digits": 6, "grading_mode": "deferred",
        "wrong_penalty": 0})
    assert r.status_code == 302, r.context["form"].errors
    quiz = Quiz.objects.get(owner=teacher)
    assert quiz.auto_enroll and quiz.grading_mode == "deferred"
    for i, letter in enumerate(KEY, start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                num_choices=4, correct_choice=ord(letter) - ord("A"))
    cands = [Student.objects.create(class_group=quiz.class_group, last_name=n,
                                    first_name="X", student_number=num)
             for n, num in (("ALPHA", "1001"), ("BETA", "1002"), ("GAMMA", "1003"))]
    c.post(f"/quiz/{quiz.pk}/", {"generate_pdf": "1"})
    quiz.refresh_from_db()
    pdf = quiz.sheet_pdf.read()
    page = quiz.layout_json["pages"][0]

    def copy(student, answers):
        img = fill_sheet(render_page(pdf), quiz.layout_json, answers, name_text=("", ""))
        return jpg(simulate_scan(stick_label(img, page, student)))

    # deux lots téléversés : rien n'est corrigé
    r = c.post(f"/quiz/{quiz.pk}/upload/", {"label": "Salle 1", "files": [
        SimpleUploadedFile("a.jpg", copy(cands[0], {1: 0, 2: 1, 3: 2, 4: 3})),
        SimpleUploadedFile("b.jpg", copy(cands[1], {1: 0, 2: 1, 3: 2, 4: 0}))]})
    assert r.status_code == 302 and r.url.endswith(f"/quiz/{quiz.pk}/")
    c.post(f"/quiz/{quiz.pk}/upload/", {"label": "Salle 2", "files": [
        SimpleUploadedFile("c.jpg", copy(cands[2], {1: 1, 2: 1, 3: 2, 4: 0}))]})
    time.sleep(1.0)
    assert list(quiz.batches.values_list("status", flat=True)) == ["pending", "pending"]
    assert SheetScan.objects.filter(batch__quiz=quiz).count() == 0, "corrigé trop tôt !"
    html = c.get(f"/quiz/{quiz.pk}/").content.decode()
    assert 'name="launch_grading"' in html and "3 page(s)" in html
    assert "attendent encore la correction" in c.get(
        f"/quiz/{quiz.pk}/resultats/").content.decode()
    print("après scan : 2 lots / 3 pages en attente, aucune copie corrigée ✔")

    # lancer la correction
    r = c.post(f"/quiz/{quiz.pk}/", {"launch_grading": "1"})
    assert r.status_code == 302
    wait_done(quiz)
    r = c.get(f"/quiz/{quiz.pk}/resultats/")
    scores = {row["student"].last_name: row["score"] for row in r.context["rows"]}
    print("après « Lancer la correction » :", scores)
    assert scores == {"ALPHA": 4.0, "BETA": 3.0, "GAMMA": 2.0}, scores
    assert 'name="launch_grading"' not in c.get(f"/quiz/{quiz.pk}/").content.decode()

    # un lot arrivé ensuite attend à son tour ; relancer ne recorrige pas les autres
    c.post(f"/quiz/{quiz.pk}/upload/", {"label": "Retardataire", "files": [
        SimpleUploadedFile("d.jpg", copy(cands[0], {1: 0}))]})
    assert quiz.batches.filter(status="pending").count() == 1
    c.post(f"/quiz/{quiz.pk}/", {"launch_grading": "1"})
    wait_done(quiz)
    assert SheetScan.objects.filter(batch__quiz=quiz).count() == 4
    print("lot retardataire mis en attente puis corrigé seul ✔")

    # concours en correction immédiate : comportement inchangé
    quiz.grading_mode = "immediate"
    quiz.save()
    c.post(f"/quiz/{quiz.pk}/upload/", {"files": [
        SimpleUploadedFile("e.jpg", copy(cands[1], {1: 0}))]})
    assert quiz.batches.latest("pk").status in ("processing", "done")
    wait_done(quiz)
    print("correction immédiate : lot corrigé dès le téléversement ✔")

    Quiz.objects.filter(owner=teacher).delete()
    teacher.delete()
    print("\n✅ TEST CORRECTION APRÈS SCAN RÉUSSI")


if __name__ == "__main__":
    main()
