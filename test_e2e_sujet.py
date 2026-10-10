"""Le SUJET (document des questions, mode « sujet séparé ») porte les choix.

Le sujet d'un concours n'imprimait que les intitulés : le candidat lisait
« Quelle est la capitale de la Tunisie ? » sans les réponses proposées, et
noircissait des cases A, B, C… au hasard sur la feuille de réponses.

On vérifie :
1. sujet de concours, en français et en arabe : chaque choix de QCM imprimé,
   et pas de cadre de réponse manuscrite (il est sur la feuille séparée) ;
2. un sujet déjà généré par l'ancienne version est refait à l'ouverture de
   la page du quiz (et par la commande lancée au démarrage du conteneur),
   sans toucher à la feuille de réponses.
"""
import os
import sys
import unicodedata

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import logging  # noqa: E402
from io import StringIO  # noqa: E402

import pymupdf  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402
from django.test import Client  # noqa: E402

from grader import layout as L  # noqa: E402
from grader import services, sheet_pdf  # noqa: E402
from grader.models import ClassGroup, Question, Quiz  # noqa: E402
from test_commun import enseignant_complet  # noqa: E402

QUESTIONS = {
    "fr": [("Quelle est la capitale de la Tunisie ?", ["Sfax", "Tunis", "Sousse"]),
           ("Combien de jours compte une semaine ?", ["Cinq", "Six", "Sept"])],
    # Choix sans lam-alef : la ligature se décompose dans un autre ordre à
    # l'extraction, ce qui compliquerait la comparaison sans rien vérifier.
    "ar": [("ما هي عاصمة تونس؟", ["صفاقس", "تونس", "سوسة"]),
           ("كم عدد أيام الأسبوع؟", ["خمسة", "ستة", "سبعة"])],
}


def _concours(teacher, langue):
    nom = f"SUJET-{langue}"
    Quiz.objects.filter(class_group__name=nom).delete()
    ClassGroup.objects.filter(name=nom).delete()
    groupe = ClassGroup.objects.create(name=nom, owner=teacher)
    quiz = Quiz.objects.create(title=f"concours-{langue}", class_group=groupe,
                               owner=teacher, sheet_mode="split", id_mode="sticker",
                               auto_enroll=True, language=langue,
                               entete="Ministère de la Justice\nInstitut supérieur",
                               duree="1 h 30", wrong_penalty=0.5)
    for i, (texte, choix) in enumerate(QUESTIONS[langue], start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1,
                                text=texte, choices=choix, num_choices=len(choix),
                                correct_choice=1)
    Question.objects.create(quiz=quiz, order=3, qtype="open", points=4,
                            text="Expliquez" if langue == "fr" else "اشرح",
                            open_height_mm=40)
    return quiz


def _texte(pdf):
    """Texte extrait, formes arabes ramenées aux lettres de base (NFKC)."""
    return unicodedata.normalize(
        "NFKC", "".join(p.get_text() for p in pymupdf.open(stream=pdf, filetype="pdf")))


def verifier_choix(teacher):
    for langue in ("fr", "ar"):
        quiz = _concours(teacher, langue)
        pdf = sheet_pdf.generate_subject_pdf(quiz)
        texte = _texte(pdf)
        for _, choix in QUESTIONS[langue]:
            for c in choix:
                # PyMuPDF rend chaque mot arabe dans l'ordre logique (seul
                # l'ordre des mots d'une ligne est inversé) : un choix d'un
                # mot se retrouve tel quel.
                assert c in texte, f"{langue} : choix « {c} » absent du sujet"
        # pas de cadre de réponse manuscrite sur les pages de questions (le
        # seul grand cadre est le cartouche de la page de garde)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        for page in list(doc)[1:]:
            grands = [d for d in page.get_drawings()
                      if d["rect"].height > 30 and d["rect"].width > 300]
            assert not grands, f"{langue} : cadre de réponse imprimé sur le sujet"
        # Page de garde du concours, comme les sujets de concours tunisiens :
        # cartouche (en-tête, durée), remarques et consignes, puis questions
        # à partir de la page 2, pages numérotées n/N.
        garde = unicodedata.normalize("NFKC", doc[0].get_text())
        mots = {"fr": ("Remarques", "Consignes", "Institut", "1 h 30", "0.5"),
                # en arabe, « 1 h 30 » (latin dans une ligne arabe) ressort
                # réordonné à l'extraction : on cherche le libellé « المدة »
                "ar": ("ملاحظات", "تعليمات", "Institut", "المدة", "0.5")}[langue]
        for mot in mots:
            assert mot in garde, f"{langue} : « {mot} » absent de la page de garde"
        assert "Tunis" not in garde and "تونس" not in garde.split(), \
            "les questions doivent commencer à la page 2"
        assert f"1/{len(doc)}" in garde.replace(" ", ""), "pagination n/N absente"
    print("  sujet concours page de garde (cartouche, remarques, consignes), "
          "choix sous chaque QCM, sans cadre manuscrit (fr, ar)")


def verifier_mise_a_jour(teacher):
    enseignant_complet(teacher)
    quiz = _concours(teacher, "fr")
    # Sujet de l'ancienne version : pas de numéro de version, pas de choix.
    disposition = L.build_layout(quiz)
    quiz.layout_json = disposition
    quiz.sheet_pdf.save("fiche.pdf", ContentFile(
        sheet_pdf.generate_sheet_pdf(quiz, disposition)), save=False)
    quiz.subject_pdf.save("ancien_sujet.pdf", ContentFile(b"%PDF-1.4 ancien"),
                          save=False)
    quiz.save()
    fiche, ancien = quiz.sheet_pdf.name, quiz.subject_pdf.name

    client = Client()
    client.force_login(teacher)
    page = client.get(f"/quiz/{quiz.pk}/").content.decode()
    assert "Sujet mis à jour" in page, "aucun message de mise à jour du sujet"
    quiz.refresh_from_db()
    assert quiz.subject_pdf.name != ancien, "le sujet n'a pas été refait"
    assert "Tunis" in _texte(quiz.subject_pdf.read())
    assert quiz.sheet_pdf.name == fiche, "la feuille de réponses ne devait pas bouger"
    assert quiz.layout_json["pages"] == disposition["pages"]
    assert "Sujet mis à jour" not in client.get(f"/quiz/{quiz.pk}/").content.decode()

    # Modifier l'en-tête dans les réglages refait le sujet aussitôt.
    avant = quiz.subject_pdf.name
    client.post(f"/quiz/{quiz.pk}/", {
        "update_settings": "1", "sheet_mode": "split", "id_mode": quiz.id_mode,
        "id_digits": quiz.id_digits, "wrong_penalty": "0.5",
        "grading_mode": quiz.grading_mode,
        "entete": "Académie régionale", "duree": "2 h"})
    quiz.refresh_from_db()
    assert quiz.entete == "Académie régionale" and quiz.duree == "2 h"
    assert quiz.subject_pdf.name != avant, "sujet non refait après l'en-tête"
    garde = _texte(quiz.subject_pdf.read())
    assert "Académie régionale" in garde and "2 h" in garde
    assert quiz.sheet_pdf.name == fiche, "la feuille de réponses ne devait pas bouger"

    # La commande du démarrage fait de même ; un quiz sans sujet séparé n'est
    # pas concerné.
    quiz2 = _concours(teacher, "ar")
    quiz2.layout_json = L.build_layout(quiz2)
    quiz2.sheet_pdf.save("fiche2.pdf", ContentFile(b"%PDF-1.4"), save=False)
    quiz2.subject_pdf.save("ancien2.pdf", ContentFile(b"%PDF-1.4"), save=False)
    quiz2.save()
    sortie = StringIO()
    call_command("moderniser_fiches", stdout=sortie)
    quiz2.refresh_from_db()
    assert quiz2.layout_json.get("sujet_version") == sheet_pdf.SUJET_VERSION, \
        sortie.getvalue()
    quiz2.sheet_mode = "grid"
    quiz2.layout_json = {k: v for k, v in quiz2.layout_json.items()
                         if k != "sujet_version"}
    quiz2.save()
    assert not services.actualiser_sujet(quiz2)
    print("  mise à jour    ancien sujet refait à l'ouverture du quiz et par la "
          "commande, feuille de réponses intacte")


def main():
    logging.disable(logging.WARNING)
    User.objects.filter(username="prof_sujet").delete()
    teacher = User.objects.create(username="prof_sujet")
    verifier_choix(teacher)
    verifier_mise_a_jour(teacher)
    print("\n✅ TEST DU SUJET AVEC SES CHOIX RÉUSSI")


if __name__ == "__main__":
    main()
