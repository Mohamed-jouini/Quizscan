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
from datetime import date  # noqa: E402
from io import StringIO  # noqa: E402

import pymupdf  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.core.files.base import ContentFile  # noqa: E402
from django.test import Client  # noqa: E402

from grader import layout as L  # noqa: E402
from grader import services, sheet_pdf  # noqa: E402
from grader.models import ClassGroup, Question, Quiz, Student  # noqa: E402
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


def verifier_entete(teacher):
    """Une ligne vide de l'en-tête reste un espace dans le cartouche : sur les sujets
    de concours, l'établissement est séparé des ministères."""
    quiz = _concours(teacher, "fr")

    def ecart(entete):
        quiz.entete = entete
        with pymupdf.open(stream=sheet_pdf.generate_subject_pdf(quiz), filetype="pdf") as doc:
            return doc[0].search_for("Institut")[0].y0 - doc[0].search_for("Ministère")[0].y0

    serre = ecart("Ministère de la Justice\nInstitut supérieur")
    aere = ecart("\nMinistère de la Justice\n\nInstitut supérieur\n\n")
    assert abs(aere - 2 * serre) < 0.5, f"ligne vide de l'en-tête : {serre} puis {aere}"
    print("  en-tête du sujet : une ligne vide y reste un espace")

    # Rien de saisi : l'en-tête de l'institut, sur tous les sujets. Saisi : lui seul.
    # (mots sans lam-alef, voir QUESTIONS : le PDF ne rend pas cette ligature telle quelle)
    for langue in ("fr", "ar"):
        quiz = _concours(teacher, langue)
        quiz.entete = ""
        for reglages in (dict(type_epreuve="qcm"), dict(type_epreuve="ecrit"),
                         dict(type_epreuve="qcm", auto_enroll=False, id_mode="name"),
                         dict(type_epreuve="qcm", auto_enroll=False, id_mode="sticker")):
            for champ, valeur in reglages.items():
                setattr(quiz, champ, valeur)
            texte = _texte(sheet_pdf.generate_subject_pdf(quiz))
            for mot in ("الجمهورية", "وزارة", "المعهد"):
                assert unicodedata.normalize("NFKC", mot) in texte, (langue, reglages, mot)
        quiz.entete = "Académie régionale"
        texte = _texte(sheet_pdf.generate_subject_pdf(quiz))
        assert "Académie" in texte and unicodedata.normalize("NFKC", "المعهد") not in texte, \
            f"{langue} : l'en-tête saisi doit remplacer celui de l'institut"
    print("  en-tête du sujet : celui de l'institut si rien n'est saisi (fr, ar)")


def verifier_tous_les_cas(teacher):
    """Le sujet ne parle que de ce que porte la copie remise au candidat, et son
    cartouche n'a pas de case vide où coller ou écrire quelque chose."""
    assert set(sheet_pdf.SUJET_TEXTES["fr"]) == set(sheet_pdf.SUJET_TEXTES["ar"])
    # Mots propres à une seule consigne d'identification, en français et en
    # arabe (sans lam-alef, voir QUESTIONS, ni voyelles : le PDF ne les garde pas).
    MARQUES = {"c_etiquette": ("emplacement réservé", "المكان"),
               "c_grille": ("dans la grille", "الشبكة"),
               "c_nom": ("cases prévues", "الخانات"),
               "c_nominative": ("sont bien les vôtres", "تخصك"),
               "c_talon": ("sur le talon", "القسيمة"),
               "c_place": ("signe distinctif", "مكانك")}
    CAS = [("c_etiquette", dict(id_mode="sticker", auto_enroll=True)),
           ("c_etiquette", dict(id_mode="qr", auto_enroll=True)),
           ("c_grille", dict(id_mode="grid", auto_enroll=True)),
           ("c_nom", dict(id_mode="name", auto_enroll=True)),
           ("c_nominative", dict(id_mode="qr")),
           ("c_talon", dict(id_mode="qr", auto_enroll=True, talon_nom=True)),
           ("c_place", dict(id_mode="qr", auto_enroll=True, place_anonyme=True))]
    for indice, langue in enumerate(("fr", "ar")):
        quiz = _concours(teacher, langue)
        for cle, reglages in CAS:
            base = dict(id_mode="name", auto_enroll=False, talon_nom=False,
                        place_anonyme=False, type_epreuve="qcm")
            for champ, valeur in {**base, **reglages}.items():
                setattr(quiz, champ, valeur)
            # hors concours, un sujet de QCM n'a pas de page de consignes
            for type_epreuve in ("qcm", "ecrit") if quiz.auto_enroll else ("ecrit",):
                quiz.type_epreuve = type_epreuve
                texte = " ".join(_texte(sheet_pdf.generate_subject_pdf(quiz)).split())
                for autre, marques in MARQUES.items():
                    marque = unicodedata.normalize("NFKC", marques[indice])
                    assert (marque in texte) == (autre == cle), \
                        (langue, cle, autre, type_epreuve)
                assert "Collez ici" not in texte and "الحاملة" not in texte, (langue, cle)
    print("  identification consigne selon la copie (étiquette, grille, nom, talon, "
          "n° de place), jamais de case d'étiquette (fr, ar)")

    # Le cartouche : le titre est au milieu de la case « épreuve », avec 2 ou 3 cases.
    centre = (L.CONTENT_X0 + L.CONTENT_X1) / 2
    largeur = L.CONTENT_X1 - L.CONTENT_X0
    quiz = _concours(teacher, "fr")

    def milieu_titre():
        page = pymupdf.open(stream=sheet_pdf.generate_subject_pdf(quiz), filetype="pdf")[0]
        cadre = [r for r in page.search_for(quiz.title) if r.y0 / MM < 60][0]
        return (cadre.x0 + cadre.x1) / 2 / MM

    MM = 72 / 25.4
    assert abs(milieu_titre() - (L.CONTENT_X0 + largeur * 0.75)) < 1, "institutions + épreuve"
    quiz.entete = ""
    assert abs(milieu_titre() - (L.CONTENT_X0 + largeur * 0.75)) < 1, \
        "concours sans en-tête saisi : celui de l'institut + épreuve"
    quiz.auto_enroll, quiz.id_mode = False, "name"
    texte = _texte(sheet_pdf.generate_subject_pdf(quiz))
    assert abs(milieu_titre() - centre) < 1 and "Prénom et nom" in texte, "classe : 3 cases"
    quiz.id_mode, quiz.talon_nom = "qr", True
    assert "Prénom et nom" not in _texte(sheet_pdf.generate_subject_pdf(quiz)), "copie anonyme"

    # Types de questions : pas de consigne de cases s'il n'y a rien à noircir.
    quiz = _concours(teacher, "fr")
    texte = " ".join(_texte(sheet_pdf.generate_subject_pdf(quiz)).split())
    assert "chaque question à choix multiples, noircissez" in texte and "cadres prévus" in texte
    assert "Questions à choix multiples et rédigées" in texte
    quiz.questions.filter(qtype="qcm").delete()
    texte = " ".join(_texte(sheet_pdf.generate_subject_pdf(quiz)).split())
    assert "noircissez" not in texte and "Questions rédigées" in texte and "retire" not in texte
    quiz = _concours(teacher, "fr")
    quiz.questions.filter(qtype="open").delete()
    texte = " ".join(_texte(sheet_pdf.generate_subject_pdf(quiz)).split())
    assert "Pour chaque question, noircissez" in texte and "cadres prévus" not in texte

    # Feuille de réponses en plusieurs pages : chacune est identifiée au scan. La
    # planche porte alors une étiquette par page, le sujet doit le dire.
    assert "étiquette QR sur chacune" not in _texte(sheet_pdf.generate_subject_pdf(quiz))
    quiz.layout_json = {"pages": [{}, {}]}
    assert "collez une étiquette QR sur chacune" in " ".join(
        _texte(sheet_pdf.generate_subject_pdf(quiz)).split())
    quiz.id_mode = "grid"
    assert "identifiez-vous sur chacune" in " ".join(
        _texte(sheet_pdf.generate_subject_pdf(quiz)).split())
    print("  cartouche sans case vide ; consignes selon les questions et les pages")


def verifier_ecrit(teacher):
    """Le sujet d'une épreuve écrite (jamais en « sujet séparé ») suit les mises à
    jour comme les autres : il portait aussi la case d'étiquette."""
    enseignant_complet(teacher)
    quiz = _concours(teacher, "fr")
    quiz.questions.all().delete()
    quiz.type_epreuve, quiz.sheet_mode, quiz.sujet_texte = "ecrit", "grid", "Racontez."
    services.question_note(quiz)
    quiz.layout_json = L.build_layout(quiz)
    quiz.subject_pdf.save("ancien_sujet.pdf", ContentFile(b"%PDF-1.4 ancien"), save=False)
    quiz.sheet_pdf.save("copies.pdf", ContentFile(b"%PDF-1.4"), save=False)
    quiz.save()
    assert services.actualiser_sujet(quiz), "sujet d'écrit non refait"
    quiz.refresh_from_db()
    assert "Racontez" in _texte(quiz.subject_pdf.read())
    assert not services.actualiser_sujet(quiz), "refait une seconde fois"
    print("  écrit          ancien sujet refait, une seule fois")


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


def verifier_date(teacher):
    """Date de l'épreuve : une case de plus dans le cartouche, seulement si elle est saisie, la
    durée y passe sous la date et aucune heure n'est imprimée ; le sujet suit sans régénérer."""
    enseignant_complet(teacher)
    client = Client()
    client.force_login(teacher)

    def lire(pdf):
        """Le cadre du cartouche, la date et la durée (« 1 h 30 ») trouvées sur la page."""
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            page = doc[0]
            cadre = next(d["rect"] for d in page.get_drawings()
                         if d["rect"].height > 80 and d["rect"].width > 300)
            return cadre, page.search_for("12/11/2026"), page.search_for("1 h 30")

    jour = date(2026, 11, 12)
    for langue in ("fr", "ar"):
        quiz = _concours(teacher, langue)
        libelle = {"fr": "Date", "ar": "التاريخ"}[langue]
        assert libelle not in _texte(sheet_pdf.generate_subject_pdf(quiz)), "case dessinée sans date"
        assert quiz.date_epreuve is None

        quiz.date_epreuve = jour
        pdf = sheet_pdf.generate_subject_pdf(quiz)
        texte = _texte(pdf)
        assert libelle in texte, f"{langue} : libellé de la date absent"
        cadre, dates, durees = lire(pdf)
        assert len(dates) == 1 and len(durees) == 1, f"{langue} : date ou durée absente, ou en double"
        # à gauche en arabe, à droite en français, comme le sens de lecture le veut
        milieu = (cadre.x0 + cadre.x1) / 2
        assert (dates[0].x0 < milieu) == (langue == "ar"), (langue, dates[0], milieu)
        # la durée sous la date, dans la même colonne et dans le cadre
        assert dates[0].y1 <= (cadre.y0 + cadre.y1) / 2 <= durees[0].y0, (dates[0], durees[0])
        assert abs((dates[0].x0 + dates[0].x1) - (durees[0].x0 + durees[0].x1)) < 1
        assert cadre.contains(dates[0]) and cadre.contains(durees[0])
        # sans heure : son libellé a disparu (les chiffres aussi : la date n'en porte pas)
        assert "Heure" not in texte and unicodedata.normalize("NFKC", "الساعة") not in texte

        # une date sans durée : seule dans sa case, à mi-hauteur
        quiz.duree = ""
        cadre, dates, durees = lire(sheet_pdf.generate_subject_pdf(quiz))
        assert not durees and abs((dates[0].y0 + dates[0].y1) / 2 - (cadre.y0 + cadre.y1) / 2) < 8, \
            (dates[0], cadre)

        # avec le nom du candidat, quatre cases : tout tient encore dans le cadre
        classe = ClassGroup.objects.create(name=f"SUJET date {langue}", owner=teacher)
        devoir = Quiz.objects.create(title="Devoir", class_group=classe, owner=teacher,
                                     sheet_mode="split", id_mode="name", language=langue,
                                     date_epreuve=jour, duree="1 h 30")
        cadre, dates, durees = lire(sheet_pdf.generate_subject_pdf(devoir))
        assert dates and durees and cadre.contains(dates[0]) and cadre.contains(durees[0])
        devoir.delete()
        classe.delete()

    # Réglages d'un QCM : une date seule, le sujet refait aussitôt
    quiz = _concours(teacher, "fr")
    assert client.post(f"/quiz/{quiz.pk}/", {"generate_pdf": "1"}).status_code == 302
    reglages = {"update_settings": "1", "sheet_mode": "split", "id_mode": quiz.id_mode,
                "id_digits": quiz.id_digits, "wrong_penalty": "0.5",
                "grading_mode": quiz.grading_mode, "entete": quiz.entete, "duree": quiz.duree}
    page = client.get(f"/quiz/{quiz.pk}/").content.decode()
    assert 'name="date_epreuve"' in page and 'type="date"' in page
    assert 'max="9999-12-31"' in page, "sans max, l'année déborde au clavier"
    client.post(f"/quiz/{quiz.pk}/", {**reglages, "date_epreuve": "2026-11-12"})
    quiz.refresh_from_db()
    assert quiz.date_epreuve == jour
    assert "12/11/2026" in _texte(quiz.subject_pdf.read()), "sujet non refait après la date"
    page = client.get(f"/quiz/{quiz.pk}/").content.decode()
    assert 'value="2026-11-12"' in page, "le champ ne rend pas la date saisie"
    # une valeur fausse ne touche à rien et le dit ; vide : plus de date, la durée revient à sa place
    r = client.post(f"/quiz/{quiz.pk}/", {**reglages, "date_epreuve": "n'importe quoi"}, follow=True)
    quiz.refresh_from_db()
    assert quiz.date_epreuve == jour and "Certaines valeurs" in r.content.decode()
    client.post(f"/quiz/{quiz.pk}/", {**reglages, "date_epreuve": ""})
    quiz.refresh_from_db()
    texte = _texte(quiz.subject_pdf.read())
    assert quiz.date_epreuve is None and "12/11/2026" not in texte and "Durée : 1 h 30" in texte

    # Écrit : le sujet existant suit dès l'enregistrement (avant : seulement à la régénération)
    classe = ClassGroup.objects.create(name="SUJET date écrit", owner=teacher)
    Student.objects.create(class_group=classe, last_name="AMRI", first_name="Sana",
                           student_number="104501")
    ecrit = Quiz.objects.create(title="Écrit daté", class_group=classe, owner=teacher,
                                type_epreuve="ecrit", id_mode="qr", talon_nom=True, language="fr")
    client.post(f"/quiz/{ecrit.pk}/", {"generer": "1"})
    ecrit.refresh_from_db()
    assert "Date" not in _texte(ecrit.subject_pdf.read())
    copies = ecrit.sheet_pdf.name
    page = client.get(f"/quiz/{ecrit.pk}/").content.decode()
    assert 'name="date_epreuve"' in page and 'type="date"' in page
    assert 'max="9999-12-31"' in page, "sans max, l'année déborde au clavier"
    client.post(f"/quiz/{ecrit.pk}/", {
        "reglages": "1", "title": ecrit.title, "sujet_texte": "Racontez.", "note_sur": "20",
        "id_mode": "sticker", "id_digits": "6", "anonymat": "talon", "pages_redaction": "1",
        "duree": "1 h 30", "date_epreuve": "2026-11-12"})
    ecrit.refresh_from_db()
    sujet = ecrit.subject_pdf.read()
    cadre, dates, durees = lire(sujet)
    assert dates and durees and dates[0].y1 <= durees[0].y0, "date et durée dans la même colonne"
    assert "Racontez." in _texte(sujet)
    assert ecrit.sheet_pdf.name == copies, "les copies ne devaient pas bouger"
    ecrit.delete()
    classe.delete()

    # À la création aussi
    assert 'name="date_epreuve"' in client.get("/quiz/nouveau/?concours=1").content.decode()
    print("  date           case du cartouche si saisie (fr, ar), durée dessous, sans heure, sujet refait")


def verifier_renommage(teacher):
    """Renommer un concours depuis ses réglages : le titre, sa liste de
    candidats, et les PDF qui l'impriment suivent ; le geste est tracé."""
    from grader.models import Modification
    enseignant_complet(teacher)
    quiz = _concours(teacher, "fr")
    quiz.class_group.name = "Concours — concours-fr"
    quiz.class_group.save()
    client = Client()
    client.force_login(teacher)
    assert client.post(f"/quiz/{quiz.pk}/", {"generate_pdf": "1"}).status_code == 302
    quiz.refresh_from_db()
    fiche, sujet = quiz.sheet_pdf.name, quiz.subject_pdf.name
    page = client.get(f"/quiz/{quiz.pk}/").content.decode()
    assert 'name="title"' in page and "Titre du concours" in page, \
        "champ « Titre du concours » absent des réglages"

    r = client.post(f"/quiz/{quiz.pk}/", {
        "update_settings": "1", "title": "  Concours national   2026 ",
        "sheet_mode": quiz.sheet_mode, "id_mode": quiz.id_mode,
        "id_digits": quiz.id_digits, "wrong_penalty": "0.5",
        "grading_mode": quiz.grading_mode,
        "entete": quiz.entete, "duree": quiz.duree})
    assert r.status_code == 302
    quiz.refresh_from_db()
    assert quiz.title == "Concours national 2026", quiz.title
    # « concours » déjà dans le titre : la liste prend le titre tel quel
    assert quiz.class_group.name == "Concours national 2026", quiz.class_group.name
    assert quiz.sheet_pdf.name != fiche and quiz.subject_pdf.name != sujet, \
        "les PDF n'ont pas été refaits"
    for doc in (quiz.sheet_pdf, quiz.subject_pdf):
        assert "Concours national 2026" in _texte(doc.read()), f"{doc.name} : ancien titre"
    assert "Régénérez la fiche" not in client.get(f"/quiz/{quiz.pk}/").content.decode(), \
        "fiche signalée à régénérer alors qu'elle vient de l'être"
    trace = Modification.objects.get(quiz=quiz, action="titre")
    assert (trace.avant, trace.apres) == ("concours-fr", "Concours national 2026")

    # Une classe ordinaire ne se renomme pas avec ses épreuves.
    classe = ClassGroup.objects.create(name="SUJET classe", owner=teacher)
    devoir = Quiz.objects.create(title="Devoir 1", class_group=classe, owner=teacher)
    client.post(f"/quiz/{devoir.pk}/", {"update_settings": "1", "title": "Devoir 2",
                                        "sheet_mode": "full", "id_mode": "name",
                                        "id_digits": "6", "wrong_penalty": "0"})
    devoir.refresh_from_db()
    classe.refresh_from_db()
    assert devoir.title == "Devoir 2" and classe.name == "SUJET classe"
    devoir.delete()
    classe.delete()
    print("  renommage      titre, liste de candidats, sujet et feuille suivent ; "
          "journalisé")


def main():
    logging.disable(logging.WARNING)
    User.objects.filter(username="prof_sujet").delete()
    teacher = User.objects.create(username="prof_sujet")
    verifier_choix(teacher)
    verifier_entete(teacher)
    verifier_tous_les_cas(teacher)
    verifier_ecrit(teacher)
    verifier_mise_a_jour(teacher)
    verifier_date(teacher)
    verifier_renommage(teacher)
    print("\n✅ TEST DU SUJET AVEC SES CHOIX RÉUSSI")


if __name__ == "__main__":
    main()
