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
import time
import zipfile
from xml.sax.saxutils import escape

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (quizscan_test)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import openpyxl
import pymupdf
import pytesseract
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
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


MARKDOWN_AR = """# بنك أسئلة — ثقافة عامة (50 سؤالاً)

اختيار من متعدد · أربعة اختيارات لكل سؤال · نسخة التلميذ (بدون إجابات).

---

**1.** ما هي عاصمة تونس؟

- أ) صفاقس
- ب) تونس
- ت) سوسة
- ث) بنزرت

**2.** كم عدد قارات العالم؟

* أ) 5
* ب) 6
* ت) 7
* ث) 8

### 3. ما هو أطول نهر في العالم؟
- أ) الأمازون
- **ب) النيل** ✓
- ت) اليانغتسي
- ث) المسيسيبي
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


def pdf_arabe(html, taille=None):
    """PDF arabe à vrai texte, écrit par PyMuPDF dans la police de l'application."""
    polices = os.path.join(os.path.dirname(importers.__file__), "fonts")
    histoire = pymupdf.Story(
        html=f'<body dir="rtl">{html}</body>',
        user_css="@font-face { font-family: naskh; src: url(NotoNaskhArabic-Regular.ttf); }"
                 "body { font-family: naskh; direction: rtl; text-align: right; "
                 + (f"font-size: {taille}; " if taille else "") + "}",
        archive=pymupdf.Archive(polices))
    tampon = io.BytesIO()
    ecrivain = pymupdf.DocumentWriter(tampon)
    page = ecrivain.begin_page(pymupdf.paper_rect("a4"))
    histoire.place(pymupdf.paper_rect("a4") + (50, 50, -50, -50))
    histoire.draw(page)
    ecrivain.end_page()
    ecrivain.close()
    return tampon.getvalue()


def en_image(pdf, dpi=200):
    """Le même PDF sans texte, comme scanné : chaque page remise en simple image."""
    source = pymupdf.open(stream=pdf, filetype="pdf")
    scan = pymupdf.open()
    for page in source:
        pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        nouvelle = scan.new_page(width=page.rect.width, height=page.rect.height)
        nouvelle.insert_image(nouvelle.rect, pixmap=pix)
    scan.set_metadata({"producer": "Scanner"})
    return scan.tobytes(deflate=True)


# questionnaire arabe à puces, sans réponses, comme celui de l'utilisateur (6 novembre 2026)
QUESTIONS_OCR = [("ما هي عاصمة تونس؟", ["صفاقس", "تونس", "سوسة", "بنزرت"]),
                 ("كم عدد قارات العالم؟", ["5", "6", "7", "8"]),
                 ("ما هو أطول نهر في العالم؟", ["الأمازون", "النيل", "اليانغتسي", "المسيسيبي"])]
HTML_OCR = "<h2>بنك أسئلة — ثقافة عامة</h2>" + "".join(
    f"<p><b>{n}.</b> {q}</p><ul>"
    + "".join(f"<li>{lettre}) {c}</li>" for lettre, c in zip("أبتث", choix)) + "</ul>"
    for n, (q, choix) in enumerate(QUESTIONS_OCR, start=1))


def tesseract_arabe():
    """Tesseract et son arabe sont-ils là ? (l'image Docker : oui ; un poste : pas toujours)"""
    try:
        return "ara" in pytesseract.get_languages()
    except pytesseract.TesseractNotFoundError:
        return False


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
    setup_test_environment()      # r.context : les messages de la page
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

    # sans bonne réponse : importée sans corrigé (à renseigner ensuite) ; une réponse
    # écrite mais invalide reste refusée, sans bloquer les questions valides
    items, errors = importers.parse_questions_text(
        ["1. Sans réponse marquée", "A) oui", "B) non", "2. Ok ?", "A) x *", "B) y",
         "3. Réponse fausse ?", "A) a", "B) b", "Réponse : Z"])
    assert [q["correct"] for q in items] == [None, 0], items
    assert len(errors) == 1 and "Question 3" in errors[0] and "invalide" in errors[0], errors
    print("  sans réponse : importée sans corrigé ; réponse invalide refusée ✔")

    # Markdown, comme le questionnaire de l'utilisateur (6 novembre 2026) : titre, trait,
    # numéros en gras, puces « - » et « * », version élève sans réponses
    items, errors = importers.parse_questions_file("questionnaire.md", MARKDOWN_AR.encode("utf-8"))
    assert not errors, errors
    assert [len(q["choices"]) for q in items] == [4, 4, 4], items
    assert [q["correct"] for q in items] == [None, None, 1], items
    assert items[0]["text"] == "ما هي عاصمة تونس؟" and items[0]["choices"][1] == "تونس", items[0]
    assert items[1]["choices"] == ["5", "6", "7", "8"] and items[2]["choices"][1] == "النيل"
    print("  Markdown : numéros en gras, puces, trait, titre ; bonne réponse ✓ lue ✔")

    # PDF sans texte : imprimé avec « Microsoft Print to PDF » (lettres dessinées), ou scanné
    for producteur, attendu in (("Microsoft: Print To PDF", "Microsoft Print to PDF"),
                                ("Scanner", "document scanné")):
        dessin = pymupdf.open()
        page = dessin.new_page()
        page.draw_line((50, 50), (300, 60))
        dessin.set_metadata({"producer": producteur})
        try:
            importers.parse_questions_file("qcm.pdf", dessin.tobytes())
        except importers.ImportError_ as exc:
            assert "texte lisible" in str(exc) and attendu in str(exc), exc
            assert "Markdown" in str(exc), exc
        else:
            raise AssertionError("PDF sans texte accepté")
    print("  PDF sans texte : la cause dite (Print to PDF ou scan), le fichier à envoyer ✔")

    # lignes telles que l'OCR les rend (questionnaire de l'utilisateur, imprimé avec
    # « Microsoft Print to PDF ») : titre, lettre perdue, doublée ou collée au nombre,
    # choix illisible (sa place gardée), point du n° de question perdu
    lues = ["بنك أسئلة [] ثقافة عامة",
            "1. ما هي عاصمة تونس؟", ") صفاقس", "ب) تونس", "تث) سوسة", "ث) بنزرت",
            "2. كم عدد قارات العالم؟", "5)", "ب)6", "ث) ة8",
            "3 ما هو أطول نهر في العالم؟", "أ) الأمازون", "ب) النيل", "ت) اليانغتسي",
            "ث) المسيسيبي"]
    items, errors = importers.parse_questions_text(lues, ocr=True)
    assert not errors, errors
    assert [q["text"] for q in items] == [q for q, _ in QUESTIONS_OCR], items
    assert items[0]["choices"] == QUESTIONS_OCR[0][1], items[0]
    assert items[1]["choices"] == ["5", "6", importers.A_RELIRE, "8"], items[1]
    assert items[2]["choices"] == QUESTIONS_OCR[2][1], items[2]
    assert [q["correct"] for q in items] == [None, None, None], items
    print("  OCR : lettre perdue, doublée ou collée, choix illisible « (à relire) », "
          "n° sans point ✔")

    # PDF sans texte, en images (scan) : sans Tesseract ou sans son arabe, le message
    # d'avant (quoi envoyer à la place) ; avec, les questions lues par OCR
    scan = en_image(pdf_arabe(HTML_OCR, taille="13pt"))
    assert not pymupdf.open(stream=scan, filetype="pdf")[0].get_text().strip()
    vrai_moteur, vraies_langues = pytesseract.pytesseract.tesseract_cmd, pytesseract.get_languages
    for panne in ("Tesseract absent", "arabe absent"):
        if panne == "Tesseract absent":
            pytesseract.pytesseract.tesseract_cmd = "tesseract-introuvable"
        else:
            pytesseract.get_languages = lambda *a, **k: ["eng", "fra"]
        try:
            importers.parse_questions_file("scan.pdf", scan, langue="ar")
        except importers.ImportError_ as exc:
            assert "texte lisible" in str(exc) and "document scanné" in str(exc), exc
        else:
            raise AssertionError(f"{panne} : PDF sans texte accepté")
        finally:
            pytesseract.pytesseract.tesseract_cmd = vrai_moteur
            pytesseract.get_languages = vraies_langues
    print("  PDF sans texte, Tesseract ou son arabe absent : le fichier à envoyer dit ✔")
    if tesseract_arabe():
        debut = time.perf_counter()
        items, errors = importers.parse_questions_file("scan.pdf", scan, langue="ar")
        duree = time.perf_counter() - debut
        assert not errors, errors
        assert [len(q["choices"]) for q in items] == [4, 4, 4], items
        assert all(q["ocr"] and q["qtype"] == "qcm" and q["correct"] is None
                   for q in items), items
        assert "عاصمة تونس" in items[0]["text"], items[0]
        assert items[1]["choices"] == ["5", "6", "7", "8"], items[1]
        print(f"  PDF sans texte, lu par OCR : 3 questions à 4 choix, en {duree:.1f} s ✔")
    else:
        print("  PDF sans texte, lu par OCR : non essayé ici (Tesseract ou son arabe absent)")

    # PDF arabe à vrai texte (écrit ici par PyMuPDF) : relu en formes de présentation,
    # espaces insécables et parenthèse du choix retournée (« ب( ») — remis en ordre
    arabe = pdf_arabe("<p>1. ما هي عاصمة تونس؟</p><p>أ) صفاقس</p><p>ب) تونس *</p>"
                      "<p>ت) سوسة</p><p>2. كم عدد قارات العالم؟</p><p>أ) 5</p><p>ب) 6</p>"
                      "<p>ت) 7 *</p>")
    brut = pymupdf.open(stream=arabe, filetype="pdf")[0].get_text()
    assert "ﻭ" in brut or "ﺗ" in brut or "ﺗ" in brut, "le PDF ne rend plus de formes"
    items, errors = importers.parse_questions_file("arabe.pdf", arabe)
    assert not errors and [q["correct"] for q in items] == [1, 2], (items, errors)
    assert items[0]["text"] == "ما هي عاصمة تونس؟" and items[0]["choices"][1] == "تونس", items
    assert importers.texte_normalise("Question 1 (2 pts)") == "Question 1 (2 pts)"
    print("  PDF arabe : formes de présentation, espaces, parenthèse retournée remis en ordre ✔")

    # Excel : réponse vide = à renseigner ; réponse invalide = refusée
    items, errors = importers.parse_questions_rows(
        [["Question", "A", "B", "C", "Réponse"], ["Q vide", "x", "y", "z", ""],
         ["Q fausse", "x", "y", "z", "W"], ["Q bonne", "x", "y", "z", "C"]])
    assert [q["correct"] for q in items] == [None, 2], items
    assert len(errors) == 1 and "invalide" in errors[0], errors
    print("  Excel  : réponse vide à renseigner, réponse invalide refusée ✔")

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

    # le Markdown sans réponses, par la page de l'épreuve : importé, et ce qui manque dit
    q_md = Quiz.objects.create(title="Culture générale", class_group=group, owner=teacher,
                               language="ar")
    r = c.post(f"/quiz/{q_md.pk}/", {
        "import_questions": "1",
        "file": SimpleUploadedFile("questionnaire.md", MARKDOWN_AR.encode("utf-8"))},
        follow=True)
    notes = [(m.level_tag, str(m)) for m in r.context["messages"]]
    assert q_md.questions.count() == 3, notes
    assert list(q_md.questions.order_by("order").values_list("correct_choice", flat=True)) \
        == [None, None, 1]
    assert any(niveau == "warning" and "2 question(s) sans bonne réponse (n° 1, 2)" in texte
               for niveau, texte in notes), notes
    assert not any(niveau == "error" for niveau, _ in notes), notes
    assert q_md.questions_sans_corrige.count() == 2, "la correction n'attendrait pas le corrigé"
    print("Web : questionnaire Markdown sans réponses importé, questions à compléter dites ✔")

    # le PDF sans texte, par la page de l'épreuve : lu par OCR, à relire et à compléter
    if tesseract_arabe():
        q_ocr = Quiz.objects.create(title="Culture générale (scan)", class_group=group,
                                    owner=teacher, language="ar")
        r = c.post(f"/quiz/{q_ocr.pk}/", {
            "import_questions": "1", "file": SimpleUploadedFile("qcm1.pdf", scan)},
            follow=True)
        notes = [(m.level_tag, str(m)) for m in r.context["messages"]]
        assert q_ocr.questions.count() == 3, notes
        assert any(niveau == "warning" and "reconnaissance de caractères (OCR)" in texte
                   and "Relisez-les" in texte for niveau, texte in notes), notes
        assert any("3 question(s) sans bonne réponse" in texte for _, texte in notes), notes
        assert not any(niveau == "error" for niveau, _ in notes), notes
        print("Web : PDF sans texte importé par OCR, questions à relire et à compléter dites ✔")
    Quiz.objects.filter(owner=teacher).delete()
    teacher.delete()
    print("\n✅ TEST DES IMPORTS RÉUSSI")


if __name__ == "__main__":
    main()
