"""Copies anonymes à n° de place (solution 2, autre choix que le talon) :
la copie porte un n° de place et le QR, jamais le nom ; la liste de placement
et d'émargement dit au surveillant qui s'assoit où ; au scan, le QR attribue
la copie au bon étudiant. Les places restent stables si la liste change.
"""
import os
import re
import sys
from unittest import mock

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

from grader import bulletin, services
from grader.fonts import shape
from grader.models import ClassGroup, Inscrit, Question, Quiz, Student
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

    # ---- tableau des candidats : la colonne Prénom avant la colonne Nom (demande de
    # l'utilisateur, 7 novembre 2026, « change dans tout le logiciel »)
    page = c.get(reverse("class_detail", args=[groupe.pk])).content.decode()
    assert "<th>N°</th><th>Prénom</th><th>Nom</th>" in page
    assert "<td>Sana</td><td>AMRI</td>" in page, "le prénom d'abord dans chaque ligne"
    print("  tableau des candidats : Prénom, puis Nom OK")

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
    # le n° de place, seul, au centre de la bande grise
    bande = quiz.layout_json["pages"][0]["qr"]["band"]
    r = pymupdf.open(stream=pdf, filetype="pdf")[0].search_for("PLACE N° 1")[0]
    assert abs((r.x0 + r.x1) / 2 * 25.4 / 72 - (bande[0] + bande[2] / 2)) < 1.5, r
    for k, t in enumerate(pages, start=1):
        assert f"PLACE N° {k}" in t and "COPIE ANONYME" not in t
        assert not any(n in t for n, _, num in ETUDIANTS for n in (n, num)), \
            f"un nom ou un n° figure sur la copie {k}"
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert reverse("quiz_emargement_pdf", args=[quiz.pk]) in page
    print("  copies : « PLACE N° 1..3 », aucun nom ni n° d'inscription OK")

    # ---- liste de placement et d'émargement
    r = c.get(reverse("quiz_emargement_pdf", args=[quiz.pk]))
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf"
    liste = textes(r.content)[0]
    # « Liste des candidats » (8 novembre 2026), des étudiants pour une classe
    assert "Liste des étudiants et des places" in liste and "Signature" in liste
    for nom, prenom, num in ETUDIANTS:
        assert f"{prenom} {nom}" in liste and num in liste
    print("  liste d'émargement : place, n°, nom, case de signature OK")
    # le prénom d'abord, comme sur la liste officielle et le talon (demande de
    # l'utilisateur, 7 novembre 2026), ici pour un nom écrit en arabe
    dessines = []
    with mock.patch.object(bulletin, "shape",
                           side_effect=lambda t: dessines.append(t) or shape(t)):
        bulletin.generate_liste_emargement(quiz, [(1, Student(
            last_name="الجبالي", first_name="محمد أمين", student_number="1782"))])
    assert "محمد أمين الجبالي" in dessines and "الجبالي محمد أمين" not in dessines, dessines
    print("  liste d'émargement : nom arabe, prénom d'abord (« محمد أمين الجبالي ») OK")

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
    assert "Ali ABID" in liste and "CHERIF" not in liste
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
    # le recto de chaque feuille reprend l'en-tête de la page de garde (n° de place et QR),
    # le dos non (7 novembre 2026) ; jamais le nom ni le n° d'inscription
    ecrit.refresh_from_db()
    pages = textes(open(ecrit.sheet_pdf.path, "rb").read())
    par_copie = 1 + ecrit.layout_json["pages_redaction"]
    assert ecrit.pages_redaction >= 1 and len(pages) % par_copie == 0, len(pages)
    for numero, suite in enumerate(pages[1:par_copie], start=2):
        assert ("PLACE N° 1" in suite) == (numero % 2 == 1), (numero, suite)
        assert not any(nom in suite or num in suite for nom, _, num in ETUDIANTS), suite
    page = c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    assert "Liste des étudiants et des places par salle" in page
    assert c.get(reverse("quiz_emargement_pdf", args=[ecrit.pk])).status_code == 200
    print("  concours et écrit : copies à n° de place et liste d'émargement OK")

    # liste d'émargement aussi sans copies anonymes (8 novembre 2026) : sans colonne place
    autre = Quiz.objects.create(title="Sans places", class_group=groupe, owner=prof)
    r = c.get(reverse("quiz_emargement_pdf", args=[autre.pk]))
    assert r.status_code == 200 and r["Content-Type"] == "application/pdf", r.status_code
    liste = "\n".join(textes(r.content))
    assert "Liste des étudiants" in liste and "Place" not in liste and "Ali ABID" in liste
    assert "N° CIN" in liste and "—" in liste, "sans fiche au registre : pas de CIN"
    print("  liste d'émargement aussi en présentation standard, sans colonne place OK")

    # ---- par salle (demande de l'utilisateur, 8 novembre 2026) : copies dans l'ordre des
    # salles du registre (2 avant 15), les étudiants sans salle à la fin ; une liste par salle
    assert services.cle_salle("قاعة عدد 2") < services.cle_salle("قاعة عدد 15") \
        < services.cle_salle("")
    classe = ClassGroup.objects.create(name="PLACES salles", owner=prof)
    for num, nom, prenom, salle in (("104601", "ZAIER", "Amel", "15"),
                                    ("104602", "YAHIA", "Badis", "2"),
                                    ("104603", "XUEREB", "Cyrine", ""),
                                    ("104604", "WALI", "Dora", "2")):
        Inscrit.objects.filter(numero=num).delete()
        fiche = Inscrit.objects.create(numero=num, nom=nom, prenom=prenom, salle=salle,
                                       cin=f"0{num}9", cree_par=prof)
        Student.objects.create(class_group=classe, last_name=nom, first_name=prenom,
                               student_number=num, inscrit=fiche)
    for anonymat in ("talon", "place"):
        q = Quiz.objects.create(title=f"Salles {anonymat}", class_group=classe, owner=prof,
                                sheet_mode="grid", id_mode="qr", talon_nom=anonymat == "talon",
                                place_anonyme=anonymat == "place")
        Question.objects.create(quiz=q, order=1, qtype="qcm", correct_choice=0)
        c.post(reverse("quiz_detail", args=[q.pk]), {"generate_pdf": "1"})
        q.refresh_from_db()
        pages = textes(open(q.sheet_pdf.path, "rb").read())
        assert len(pages) == 4, len(pages)
        if anonymat == "talon":                 # salle 2 : n° 104602 puis 104604
            vus = [next(n for n in ("YAHIA", "WALI", "ZAIER", "XUEREB") if n in p)
                   for p in pages]
            assert vus == ["YAHIA", "WALI", "ZAIER", "XUEREB"], vus
        else:                                   # places par n° : ZAIER 1, YAHIA 2, …
            vus = [re.search(r"PLACE N° (\d+)", p).group(1) for p in pages]
            assert vus == ["2", "4", "1", "3"], vus
        r = c.get(reverse("quiz_emargement_pdf", args=[q.pk]))
        feuilles = textes(r.content)
        assert len(feuilles) == 3, len(feuilles)                    # une page par salle
        assert "Salle : 2" in feuilles[0] and "YAHIA" in feuilles[0] and "WALI" in feuilles[0]
        assert "Salle : 15" in feuilles[1] and "ZAIER" in feuilles[1]
        assert "XUEREB" in feuilles[2] and "Salle : ………" in feuilles[2], feuilles[2]
        assert all("Signature" in f and "Présents" in f for f in feuilles)
        assert ("Place" in feuilles[0]) == (anonymat == "place")
        # n° de CIN du registre en face de chaque nom (demande du 8 novembre 2026)
        assert all("N° CIN" in f for f in feuilles)
        assert "01046029" in feuilles[0] and "01046049" in feuilles[0], feuilles[0]
        assert "01046039" in feuilles[2], feuilles[2]
    # l'écrit aussi (« fait aussi pour écrire », 8 novembre 2026) : copies à talon (A4) et à
    # n° de place (A3, livret plié) dans l'ordre des salles ; liste d'émargement par salle
    for anonymat, fmt in (("talon", "a4"), ("place", "a3")):
        ecrit_s = Quiz.objects.create(title=f"Ecrit salles {anonymat}", class_group=classe,
                                      owner=prof, type_epreuve="ecrit", id_mode="qr",
                                      talon_nom=anonymat == "talon",
                                      place_anonyme=anonymat == "place", note_sur=20,
                                      pages_redaction=1, format_impression=fmt)
        c.post(reverse("quiz_detail", args=[ecrit_s.pk]), {"generer": "1"})
        ecrit_s.refresh_from_db()
        pages = textes(open(ecrit_s.sheet_pdf.path, "rb").read())
        gardes = pages[::2]         # A4 : garde et page au dos ; A3 : recto et verso
        assert len(gardes) == 4, len(pages)
        if anonymat == "talon":
            vus = [next(n for n in ("YAHIA", "WALI", "ZAIER", "XUEREB") if n in p)
                   for p in gardes]
            assert vus == ["YAHIA", "WALI", "ZAIER", "XUEREB"], vus
        else:
            vus = [re.search(r"PLACE N° (\d+)", p).group(1) for p in gardes]
            assert vus == ["2", "4", "1", "3"], vus
        page = c.get(reverse("quiz_detail", args=[ecrit_s.pk])).content.decode()
        assert "Liste des étudiants" in page and "par salle" in page, "lien de la liste"
        feuilles = textes(c.get(reverse("quiz_emargement_pdf", args=[ecrit_s.pk])).content)
        assert len(feuilles) == 3 and "Salle : 2" in feuilles[0] and "WALI" in feuilles[0], \
            feuilles
    print("  écrit aussi : copies à talon (A4) et à n° de place (A3) dans l'ordre des "
          "salles, liste d'émargement par salle OK")
    print("  par salle : copies (talon, n° de place) dans l'ordre des salles, puis du n° ou "
          "de la place ; une liste d'émargement par salle, sans salle à la fin OK")
    print("\n✅ TEST DES COPIES ANONYMES À N° DE PLACE RÉUSSI")


if __name__ == "__main__":
    main()
