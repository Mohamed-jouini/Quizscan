"""Test des formulaires de création : concours (sans classe, sans case
« mode concours », sans nombre de choix global), quiz de classe, et nombre de
choix réglé question par question — jusqu'à la lecture d'une copie scannée."""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


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

from grader import services
from grader.models import ClassGroup, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan
from test_e2e_web import jpg


def main():
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_forms").delete()
    User.objects.filter(username="prof_forms").delete()
    teacher = User.objects.create_user("prof_forms", password="x-pass-123")
    teacher = enseignant_complet(teacher)
    c = Client()
    c.force_login(teacher)

    # ---- nouveau concours : seuls les champs utiles sont proposés
    r = c.get("/quiz/nouveau/?concours=1")
    fields = set(r.context["form"].fields)
    assert fields == {"title", "language", "sheet_mode", "id_mode", "id_digits",
                      "grading_mode", "wrong_penalty"}, fields
    modes = [m for m, _ in r.context["form"].fields["id_mode"].choices]
    assert modes == ["grid", "sticker"], modes
    html = r.content.decode()
    for absent in ("Mode concours : créer", "Nombre de choix par question QCM",
                   "concours : créé automatiquement"):
        assert absent not in html, absent
    r = c.post("/quiz/nouveau/?concours=1", {
        "title": "Concours d'entrée 2026", "language": "ar", "sheet_mode": "split",
        "id_mode": "sticker", "id_digits": 6, "grading_mode": "immediate",
        "wrong_penalty": 0})
    assert r.status_code == 302, r.context["form"].errors
    conc = Quiz.objects.get(owner=teacher, title="Concours d'entrée 2026")
    assert conc.auto_enroll and conc.id_mode == "sticker"
    assert conc.class_group.name == "Concours d'entrée 2026"
    print("concours : créé sans classe ni case « mode concours », liste de "
          "candidats dédiée ✔")

    # ---- nouveau quiz de classe : la classe est obligatoire, pas de concours
    r = c.get("/quiz/nouveau/")
    assert "auto_enroll" not in r.context["form"].fields
    assert "num_choices" not in r.context["form"].fields
    assert "grading_mode" not in r.context["form"].fields
    r = c.post("/quiz/nouveau/", {"title": "Sans classe", "language": "fr",
                                  "sheet_mode": "grid", "id_mode": "name",
                                  "id_digits": 6, "wrong_penalty": 0})
    assert r.status_code == 200 and "class_group" in r.context["form"].errors
    group = ClassGroup.objects.create(name="FORMS-1A", owner=teacher)
    Student.objects.create(class_group=group, last_name="BEN SALAH",
                           first_name="Ahmed", student_number="104523")
    r = c.post("/quiz/nouveau/", {"title": "Contrôle", "class_group": group.pk,
                                  "language": "fr", "sheet_mode": "grid",
                                  "id_mode": "grid", "id_digits": 6,
                                  "wrong_penalty": 0})
    assert r.status_code == 302
    quiz = Quiz.objects.get(owner=teacher, title="Contrôle")
    assert not quiz.auto_enroll
    print("quiz de classe : classe obligatoire, jamais en mode concours ✔")

    # ---- nombre de choix par question
    url = f"/quiz/{quiz.pk}/"
    c.post(url, {"bulk_add": "1", "answer_key": "ABC", "num_choices": 3, "points": 1})
    r = c.post(url, {"bulk_add": "1", "answer_key": "D", "num_choices": 3, "points": 1})
    assert "answer_key" in r.context["bulk_form"].errors, "D refusé avec 3 choix"
    c.post(url, {"add_question": "1", "qtype": "qcm", "text": "Q à 5 cases",
                 "choices_text": "", "num_choices": 5, "correct_letter": "4",
                 "points": 2, "open_height_mm": 25})
    c.post(url, {"add_question": "1", "qtype": "qcm", "text": "Vrai ou faux ?",
                 "choices_text": "Vrai\nFaux", "num_choices": 5,
                 "correct_letter": "1", "points": 1, "open_height_mm": 25})
    # Bonne réponse FACULTATIVE : la question est acceptée et le quiz est
    # signalé « corrigé incomplet » (le corrigé viendra à la main ou par scan).
    r = c.post(url, {"add_question": "1", "qtype": "qcm", "text": "Sans réponse",
                     "choices_text": "a\nb", "correct_letter": "", "points": "1,5",
                     "open_height_mm": 25})
    assert r.status_code == 302, "une question sans bonne réponse doit être acceptée"
    q_sans = quiz.questions.order_by("-order").first()
    assert q_sans.correct_choice is None and q_sans.points == 1.5, q_sans.points
    assert not quiz.corrige_complet
    assert q_sans.order in quiz.questions_sans_corrige.values_list("order", flat=True)
    q_sans.delete()
    assert quiz.corrige_complet

    # barème avec une virgule (« 1,5 ») sur une question manuscrite
    c.post(url, {"add_question": "1", "qtype": "open", "text": "Expliquez",
                 "points": "1,5", "open_height_mm": 25})
    q_open = quiz.questions.order_by("-order").first()
    assert q_open.qtype == "open" and q_open.points == 1.5, q_open.points
    q_open.delete()
    print("barème « 1,5 » accepté, bonne réponse facultative ✔")
    counts = [q.num_bubbles for q in quiz.questions.order_by("order")]
    assert counts == [3, 3, 3, 5, 2], counts
    print("nombre de choix par question :", counts, "✔")

    # fiche et lecture d'une copie : chaque question a son nombre de cases
    c.post(url, {"generate_pdf": "1"})
    quiz.refresh_from_db()
    page = quiz.layout_json["pages"][0]
    assert [len(i["bubbles"]) for i in page["qcm"]] == counts
    img = render_page(quiz.sheet_pdf.read())
    img = fill_sheet(img, quiz.layout_json, {1: 0, 2: 1, 3: 2, 4: 4, 5: 0},
                     name_text=("", ""), student_number="104523")
    batch = services.process_uploaded_files(
        quiz, [SimpleUploadedFile("copie.jpg", jpg(simulate_scan(img)))])
    sheet = batch.sheets.get()
    row = services.compute_results(quiz, batch)[0]
    print("copie :", sheet.status, sheet.student, "note", row["score"], "/",
          row["max_score"])
    assert sheet.student and row["score"] == 5.0 and row["wrong"] == 1
    r = c.get(f"/copies/{sheet.pk}/")
    assert r.status_code == 200

    # réglages : plus de nombre de choix global
    r = c.get(url)
    assert 'name="num_choices" value' not in r.content.decode().split("Type de fiche")[1] \
        .split("Enregistrer les réglages")[0]

    Quiz.objects.filter(owner=teacher).delete()
    teacher.delete()
    print("\n✅ TEST DES FORMULAIRES RÉUSSI")


if __name__ == "__main__":
    main()
