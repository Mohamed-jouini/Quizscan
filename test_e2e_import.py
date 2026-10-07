"""Test des imports (fiche technique : « Import Excel / PDF / Word ») :
questionnaires et listes de candidats depuis Excel, Word, PDF et texte,
puis banque de questions (reprise des questions d'un autre quiz)."""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import io
import zipfile
from xml.sax.saxutils import escape

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import openpyxl
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from grader import importers
from grader.models import ClassGroup, Question, Quiz
from test_commun import enseignant_complet

QUESTIONNAIRE = """Contrôle de géographie — 2e année
Consigne : une seule bonne réponse par question.

1. Quelle est la capitale de la Tunisie ? (2 pts)
A) Sfax
B) Tunis *
C) Sousse
D) Bizerte
2) Combien de gouvernorats compte la Tunisie ?
a. 20
b. 24
c. 26
Réponse : b
Q3 - Le plus long fleuve de Tunisie est
A. la Medjerda
B. l'oued Zeroud
Bonne réponse : A
Barème : 1,5
4. Décrivez le climat du Sahel tunisien. [manuscrite] (4 pts)
"""


def make_docx(paragraphs, table=None):
    """Petit .docx valide (paragraphes, et un tableau facultatif)."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>"
                   for p in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{escape(c)}</w:t></w:r></w:p></w:tc>"
                               for c in row) + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    doc = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/content-types"><Default Extension="xml" '
                   'ContentType="application/xml"/><Override PartName="/word/document.xml" '
                   'ContentType="application/vnd.openxmlformats-officedocument.'
                   'wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def make_pdf(lines):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    y = 800
    for line in lines:
        c.drawString(50, y, line)
        y -= 16
    c.save()
    return buf.getvalue()


def make_xlsx(rows):
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def check_questionnaire(items, errors, source):
    assert not errors, f"{source} : erreurs {errors}"
    assert len(items) == 4, f"{source} : {len(items)} questions"
    q1, q2, q3, q4 = items
    assert q1["qtype"] == "qcm" and q1["correct"] == 1 and q1["points"] == 2.0, q1
    assert q1["text"] == "Quelle est la capitale de la Tunisie ?", q1["text"]
    assert q1["choices"] == ["Sfax", "Tunis", "Sousse", "Bizerte"], q1["choices"]
    assert q2["correct"] == 1 and len(q2["choices"]) == 3, q2
    assert q3["correct"] == 0 and q3["points"] == 1.5, q3
    assert q4["qtype"] == "open" and q4["points"] == 4.0, q4
    print(f"  {source:6s} : 4 questions (3 QCM + 1 manuscrite) ✔")


def main():
    lines = QUESTIONNAIRE.splitlines()

    print("Questionnaires :")
    check_questionnaire(*importers.parse_questions_file(
        "sujet.txt", QUESTIONNAIRE.encode("utf-8")), "texte")
    check_questionnaire(*importers.parse_questions_file(
        "sujet.docx", make_docx(lines)), "Word")
    check_questionnaire(*importers.parse_questions_file(
        "sujet.pdf", make_pdf([l.replace("×", "x") for l in lines])), "PDF")
    with open("modele_questions.xlsx", "rb") as fh:
        items, errors = importers.parse_questions_file("modele_questions.xlsx", fh.read())
    assert not errors, errors
    assert [q["qtype"] for q in items] == ["qcm", "qcm", "qcm", "open", "qcm"], items
    assert items[0]["correct"] == 1 and items[1]["choices"] == ["54", "56", "64"]
    assert items[3]["open_height_mm"] == 35 and items[4]["correct"] == 1
    print("  Excel  : modele_questions.xlsx (4 QCM dont 1 en arabe + 1 manuscrite) ✔")

    # questionnaire en arabe (lettres أ ب ت ث)
    ar = "1. ما هي عاصمة تونس؟\nأ) صفاقس\nب) تونس *\nت) سوسة\n"
    items, errors = importers.parse_questions_text(ar.splitlines())
    assert not errors and items[0]["correct"] == 1 and len(items[0]["choices"]) == 3
    print("  arabe  : choix أ ب ت, bonne réponse ب ✔")

    # erreurs signalées, sans bloquer les questions valides
    items, errors = importers.parse_questions_text(
        ["1. Sans réponse marquée", "A) oui", "B) non", "2. Ok ?", "A) x *", "B) y"])
    assert len(items) == 1 and len(errors) == 1 and "Question 1" in errors[0]
    print("  erreurs: question sans bonne réponse signalée ✔")

    print("Candidats :")
    expected = [("BEN SALAH", "Ahmed", "104523"), ("TRABELSI", "Mariem", "104524")]
    got = importers.parse_candidates_file("liste.xlsx", make_xlsx(
        [["N° d'inscription", "Nom", "Prénom"], [104523, "BEN SALAH", "Ahmed"],
         [104524, "TRABELSI", "Mariem"]]))
    assert got == expected, got
    got = importers.parse_candidates_file("liste.docx", make_docx(
        ["Liste des candidats"], table=[["Nom", "Prénom", "Matricule"],
                                        ["BEN SALAH", "Ahmed", "104523"],
                                        ["TRABELSI", "Mariem", "104524"]]))
    assert got == expected, got
    got = importers.parse_candidates_file("liste.pdf", make_pdf(
        ["Nom Prénom N°", "BEN SALAH Ahmed 104523", "TRABELSI Mariem 104524"]))
    assert got == expected, got
    got = importers.parse_candidates_file(
        "liste.csv", "Nom;Prénom;Numéro\nBEN SALAH;Ahmed;104523\nTRABELSI;Mariem;104524\n"
        .encode("utf-8"))
    assert got == expected, got
    print("  Excel, Word (tableau), PDF, CSV ✔")

    # ---- par l'interface web : import + banque de questions
    # Quiz protège ClassGroup : sans retirer les quiz d'abord, la suppression
    # du compte échoue sur la clé protégée si un passage précédent s'est
    # interrompu avant la fin.
    Quiz.objects.filter(owner__username="prof_import").delete()
    ClassGroup.objects.filter(owner__username="prof_import").delete()
    User.objects.filter(username="prof_import").delete()
    teacher = User.objects.create_user("prof_import", password="x-pass-123")
    teacher = enseignant_complet(teacher)
    group = ClassGroup.objects.create(name="IMPORT-2A", owner=teacher)
    q_src = Quiz.objects.create(title="Géographie T1", class_group=group, owner=teacher)
    q_new = Quiz.objects.create(title="Géographie T2", class_group=group, owner=teacher)
    c = Client()
    c.force_login(teacher)
    r = c.post(f"/quiz/{q_src.pk}/", {
        "import_questions": "1",
        "file": SimpleUploadedFile("sujet.docx", make_docx(lines))})
    assert r.status_code == 302
    assert q_src.questions.count() == 4, q_src.questions.count()
    r = c.post(f"/classes/{group.pk}/", {
        "import_xlsx": "1",
        "file": SimpleUploadedFile("liste.pdf", make_pdf(
            ["BEN SALAH Ahmed 104523", "TRABELSI Mariem 104524"]))})
    assert group.students.count() == 2, list(group.students.values_list("last_name"))
    r = c.post(f"/quiz/{q_new.pk}/", {"copy_questions": "1",
                                      "source": q_src.pk, "orders": "1-2, 4"})
    assert r.status_code == 302, r.content[:500]
    copied = list(q_new.questions.order_by("order"))
    assert [q.order for q in copied] == [1, 2, 3]
    assert copied[0].choices == ["Sfax", "Tunis", "Sousse", "Bizerte"]
    assert copied[0].correct_choice == 1 and copied[2].qtype == "open"
    print("Web : import Word du questionnaire, import PDF des candidats, "
          "banque de questions (1-2, 4) ✔")
    Quiz.objects.filter(owner=teacher).delete()
    teacher.delete()
    print("\n✅ TEST DES IMPORTS RÉUSSI")


if __name__ == "__main__":
    main()
