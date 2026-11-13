"""Écrit d'un concours en matières (demande de l'utilisateur, 13 novembre 2026) : trois
épreuves, قانون مدني، قانون جزائي، الحريات الأساسية, chacune notée sur 20 avec son
coefficient ; la note de l'écrit est leur moyenne pondérée, et la note minimale porte sur
elle.

Vérifie : les trois matières créées avec l'écrit d'un concours (pas avec un écrit de
classe) ; noms, coefficients, sujets, durées et dates réglés sur la page ; un sujet, des
copies et une liste par salle pour chaque matière, la page de garde à son nom et à son code
de page ; un seul envoi de scans pour toutes, chaque copie notée dans sa matière ; moyenne
pondérée, « en attente » tant qu'une matière manque ; admis à la note minimale ; l'oral créé
avec les admis ; ajouter et retirer une matière.
"""
import io
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
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import layout as L, services
from grader.models import ClassGroup, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import mm2px, render_page, simulate_scan

ETUDIANTS = [("AMRI", "Sana", "704501"), ("BAKIR", "Omar", "704502"),
             ("CHERIF", "Lina", "704503")]
NOMS = ["Droit civil", "Droit pénal", "Libertés fondamentales"]
CONCOURS = {"type_epreuve": "ecrit", "annee": "1", "language": "fr", "note_sur": "20",
            "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
            "anonymat": "talon", "grading_mode": "immediate", "wrong_penalty": "0"}


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def noircir(img, grille, note):
    """La note noircie par le correcteur : partie entière, puis ,0 ou ,5."""
    entier, demi = int(note), int(round((note - int(note)) * 2))
    for row, d in zip(grille["rows"], [entier, demi]):
        cx, cy = row[d]
        cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(grille["r"] * 0.9), (20, 20, 20), -1)
    return img


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_matieres").delete()
    ClassGroup.objects.filter(owner__username="prof_matieres").delete()
    User.objects.filter(username="prof_matieres").delete()
    prof = enseignant_complet(User.objects.create_user("prof_matieres", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    print("Écrit d'un concours en matières :")

    # ---- l'écrit d'un concours naît avec ses trois matières, notées sur 20, coefficient 1
    r = c.post(reverse("quiz_create") + "?concours=1", dict(CONCOURS, title="Concours M"))
    assert r.status_code == 302, r.context["form"].errors
    quiz = Quiz.objects.get(title="Concours M")
    matieres = quiz.matieres
    assert [m.text for m in matieres] == list(services.MATIERES_CONCOURS), matieres
    assert all(m.points == 20 and m.coefficient == 1 for m in matieres)
    assert quiz.max_score == 20
    classe = ClassGroup.objects.create(name="MATIERES 3e A", owner=prof)
    r = c.post(reverse("quiz_create"), dict(CONCOURS, title="Écrit de classe",
                                            class_group=classe.pk))
    assert r.status_code == 302, r.context["form"].errors
    assert Quiz.objects.get(title="Écrit de classe").matieres == []
    print("  création : l'écrit d'un concours a trois matières (قانون مدني، قانون جزائي، "
          "الحريات الأساسية), un écrit de classe une seule note OK")

    # ---- noms, coefficients, sujets, durées et dates, réglés sur la page
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "Les matières" in page and 'name="sujet_texte"' not in page
    assert all(f'name="matiere_{m.pk}_coefficient"' in page for m in matieres)
    reglages = {"reglages": "1", "title": quiz.title, "note_sur": "20", "id_mode": "sticker",
                "id_digits": "6", "anonymat": "talon", "pages_redaction": "1",
                "format_impression": "a4"}
    for m, nom, coefficient in zip(matieres, NOMS, ("2", "1", "1,5")):
        reglages.update({f"matiere_{m.pk}_nom": nom, f"matiere_{m.pk}_coefficient": coefficient,
                         f"matiere_{m.pk}_sujet": f"Sujet de {nom}.\n\nDeuxième paragraphe.",
                         f"matiere_{m.pk}_duree": "3 h", f"matiere_{m.pk}_date": "2026-12-0"
                         + str(NOMS.index(nom) + 1)})
    faux = dict(reglages, **{f"matiere_{matieres[0].pk}_coefficient": "0"})
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), faux, follow=True)
    assert "le coefficient va de 0,1 à 99" in r.content.decode()
    assert [m.coefficient for m in quiz.matieres] == [1, 1, 1], "rien d'enregistré"
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), reglages)
    assert r.status_code == 302, r.content.decode()[-1500:]
    matieres = quiz.matieres
    assert [(m.text, m.coefficient, m.duree) for m in matieres] == [
        ("Droit civil", 2, "3 h"), ("Droit pénal", 1, "3 h"), ("Libertés fondamentales", 1.5, "3 h")]
    assert str(matieres[2].date_epreuve) == "2026-12-03" and "Deuxième" in matieres[1].sujet
    print("  réglages : noms, coefficients (2 ; 1 ; 1,5), sujets, durées et dates ; "
          "coefficient 0 refusé, rien d'enregistré OK")

    # ---- documents : pour chaque matière, son sujet, ses copies (page de garde à son nom,
    # code de page propre) et sa liste par salle
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=quiz.class_group, last_name=nom, first_name=prenom,
                               student_number=num)
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    layout = quiz.layout_json
    assert [p["page_index"] for p in layout["pages"]] == [0, 1, 2]
    assert [p["matiere"] for p in layout["pages"]] == NOMS
    assert [p["note_grid"]["order"] for p in layout["pages"]] == [m.order for m in matieres]
    docs = layout["documents_matieres"]
    assert [d["nom"] for d in docs] == NOMS and quiz.sheet_pdf.name == docs[0]["copies"]
    par_copie = 1 + layout["pages_redaction"] + layout.get("pages_blanches", 0)
    pdfs = []
    for k, d in enumerate(docs):
        pdf = default_storage.open(d["copies"]).read()
        pdfs.append(pdf)
        doc = pymupdf.open(stream=pdf, filetype="pdf")
        assert len(doc) == 3 * par_copie == d["nb_pages"], (len(doc), par_copie)
        garde = doc[0].get_text()
        assert NOMS[k] in garde and "Sana AMRI" in garde and "Page 1 / 2" in garde, garde
        assert not any(autre in garde for autre in NOMS if autre != NOMS[k])
        sujet = pymupdf.open(stream=default_storage.open(d["sujet"]).read(), filetype="pdf")
        texte_sujet = "".join(p.get_text() for p in sujet)
        assert f"Sujet de {NOMS[k]}" in texte_sujet and NOMS[k] in texte_sujet, texte_sujet[:300]
        assert "3 h" in texte_sujet
        liste = c.get(reverse("quiz_emargement_pdf", args=[quiz.pk]) + f"?matiere={d['order']}")
        texte_liste = pymupdf.open(stream=liste.content, filetype="pdf")[0].get_text()
        assert NOMS[k] in texte_liste and "Signature entrée" in texte_liste
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert all(default_storage.url(d["copies"]) in page for d in docs)
    print(f"  documents : sujet, copies ({par_copie} pages par copie) et liste par salle de "
          "chaque matière, la page de garde à son nom OK")

    # ---- un seul envoi pour toutes les matières : chaque copie notée dans la sienne
    notes = {"AMRI": (14, 10, 12), "BAKIR": (8, 9, 7.5), "CHERIF": (15, None, None)}
    images = []
    for rang, (nom, _, _) in enumerate(ETUDIANTS):
        for k, note in enumerate(notes[nom]):
            if note is None:
                continue                    # CHERIF n'a composé qu'en droit civil
            img = render_page(pdfs[k], rang * par_copie)
            img = noircir(img, layout["pages"][k]["note_grid"], note)
            img = img[mm2px(30.0):, :].copy()           # talon découpé au massicot
            images.append(SimpleUploadedFile(f"{nom}_{k}.jpg", jpg(simulate_scan(img))))
    lot = services.process_uploaded_files(quiz, images, label="Trois matières")
    assert lot.sheets.filter(student__isnull=False).count() == 7, \
        [(s.status, s.error_message) for s in lot.sheets.all()]
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    assert rows["AMRI"]["notes_matieres"] == [14, 10, 12], rows["AMRI"]["notes_matieres"]
    # moyenne pondérée : (2×14 + 10 + 1,5×12) / 4,5 = 56 / 4,5 = 12,44
    assert rows["AMRI"]["score"] == 12.44 and rows["AMRI"]["max_score"] == 20, rows["AMRI"]
    assert rows["BAKIR"]["score"] == round((16 + 9 + 1.5 * 7.5) / 4.5, 2), rows["BAKIR"]
    assert rows["CHERIF"]["notes_matieres"] == [15, None, None]
    assert rows["CHERIF"]["questions_manquantes"] == 2 and rows["CHERIF"]["score"] == 6.67
    print("  un seul envoi : 7 copies notées chacune dans sa matière ; moyenne pondérée "
          "(AMRI 12,44 / 20), matière manquante comptée 0 OK")

    # ---- note minimale sur la moyenne : admis, non admis, en attente
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="10"))
    assert r.status_code == 302
    quiz.refresh_from_db()
    rows = services.compute_results(quiz)
    services.admissions(quiz, rows)
    verdicts = {r["student"].last_name: r["resultat"] for r in rows}
    assert verdicts == {"AMRI": "admis", "BAKIR": "refuse", "CHERIF": "en_attente"}, verdicts
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert all(nom in page for nom in NOMS) and "Moyenne" in page and "12,44 / 20" in page
    assert "Incomplète : 2 matières" in page
    # relevé de notes et export : la note de chaque matière, et son coefficient
    amri = quiz.class_group.students.get(last_name="AMRI")
    releve = c.get(reverse("quiz_bulletins_pdf", args=[quiz.pk]) + f"?student={amri.pk}")
    texte = pymupdf.open(stream=releve.content, filetype="pdf")[0].get_text()
    assert all(nom in texte for nom in NOMS) and "14 / 20" in texte and "12.44 / 20" in texte,         texte
    from openpyxl import load_workbook
    classeur = load_workbook(io.BytesIO(c.get(reverse("quiz_results_xlsx", args=[quiz.pk])).content))
    feuille = classeur["Résultats"]
    entetes = [x.value for x in feuille[2]]
    assert entetes[-3:] == ["Droit civil (coef. 2)", "Droit pénal (coef. 1)",
                            "Libertés fondamentales (coef. 1.5)"], entetes
    ligne_amri = next(ligne for ligne in feuille.iter_rows(min_row=3, values_only=True)
                      if ligne[1] == "AMRI")
    assert list(ligne_amri[-3:]) == [14, 10, 12] and ligne_amri[3] == 12.44, ligne_amri
    print("  note minimale 10 sur la moyenne : AMRI admis, BAKIR non admis, CHERIF en "
          "attente ; une colonne par matière dans les résultats OK")

    # ---- l'oral, étape suivante : les admis de l'écrit
    suite = reverse("quiz_create") + f"?concours=1&precedente={quiz.pk}&type=oral"
    r = c.post(suite, dict(CONCOURS, title="Concours M — oral", type_epreuve="oral",
                           epreuve_precedente=quiz.pk))
    assert r.status_code == 302, r.context["form"].errors
    oral = Quiz.objects.get(title="Concours M — oral")
    assert list(oral.class_group.students.values_list("last_name", flat=True)) == ["AMRI"]
    print("  oral : créé avec le seul admis de l'écrit (AMRI) OK")

    # ---- l'oral en 1re année : trois notes, إ م ت، ن إج، ق ت ; leur moyenne pondérée
    assert [m.text for m in oral.matieres] == list(services.MATIERES_ORAL["1"]), oral.matieres
    url_oral = reverse("quiz_detail", args=[oral.pk])
    page = c.get(url_oral).content.decode()
    assert all(f'name="matiere_{m.pk}_coefficient"' in page for m in oral.matieres)
    reglages_oral = {"reglages": "1", "title": oral.title, "note_sur": "20",
                     "note_admission": "10"}
    for m, coefficient in zip(oral.matieres, ("2", "1", "1")):
        reglages_oral.update({f"matiere_{m.pk}_nom": m.text,
                              f"matiere_{m.pk}_coefficient": coefficient})
    assert c.post(url_oral, reglages_oral).status_code == 302
    assert [m.coefficient for m in oral.matieres] == [2, 1, 1]
    from openpyxl import load_workbook as charger
    classeur = charger(io.BytesIO(c.get(reverse("quiz_notes_oral_xlsx", args=[oral.pk])).content))
    feuille = classeur.active
    assert [x.value for x in feuille[3]][4:] == ["إ م ت / 20", "ن إج / 20", "ق ت / 20"], \
        [x.value for x in feuille[3]]
    ligne = next(feuille.iter_rows(min_row=4))
    assert ligne[0].value == "704501"
    ligne[4].value, ligne[5].value = 15, "12,5"         # ق ت laissée vide : en attente
    tampon = io.BytesIO()
    classeur.save(tampon)
    r = c.post(url_oral, {"importer_notes": "1",
                          "fichier": SimpleUploadedFile("notes.xlsx", tampon.getvalue())},
               follow=True)
    assert "Notes importées : 2 nouvelle(s), 0 modifiée(s), 0 inchangée(s)" in r.content.decode()
    ligne_oral = services.compute_results(oral)[0]
    assert ligne_oral["notes_matieres"] == [15, 12.5, None]
    assert ligne_oral["questions_manquantes"] == 1 and ligne_oral["score"] == 10.62, ligne_oral
    assert "notes incomplètes" in r.content.decode()
    ligne[6].value = 9
    tampon = io.BytesIO()
    classeur.save(tampon)
    r = c.post(url_oral, {"importer_notes": "1",
                          "fichier": SimpleUploadedFile("notes.xlsx", tampon.getvalue())},
               follow=True)
    assert "Notes importées : 1 nouvelle(s), 0 modifiée(s), 2 inchangée(s)" in r.content.decode()
    ligne_oral = services.compute_results(oral)[0]
    # (2×15 + 12,5 + 9) / 4 = 12,88
    assert ligne_oral["notes_matieres"] == [15, 12.5, 9] and ligne_oral["score"] == 12.88
    assert len(oral.batches.get().sheets.all()) == 1, "une copie d'import par candidat"
    oral.refresh_from_db()                      # note minimale 10, réglée plus haut
    services.admissions(oral, [ligne_oral])
    assert ligne_oral["resultat"] == "admis"
    feuille.cell(row=3, column=6).value = "autre"           # une colonne de note perdue
    tampon = io.BytesIO()
    classeur.save(tampon)
    r = c.post(url_oral, {"importer_notes": "1",
                          "fichier": SimpleUploadedFile("notes.xlsx", tampon.getvalue())},
               follow=True)
    assert "introuvables" in r.content.decode()
    print("  oral de 1re année : trois notes (إ م ت، ن إج، ق ت), coefficients, une colonne "
          "par note dans le modèle Excel ; moyenne pondérée 12,88, en attente tant qu'une "
          "note manque OK")

    # ---- un concours de 2e année : son oral a deux notes, إ م ت et ن إج
    r = c.post(reverse("quiz_create") + "?concours=1",
               dict(CONCOURS, title="Concours 2A", annee="2"))
    assert r.status_code == 302
    ecrit2 = Quiz.objects.get(title="Concours 2A")
    ecrit2.note_admission = 10
    ecrit2.save(update_fields=["note_admission"])
    r = c.post(reverse("quiz_create") + f"?concours=1&precedente={ecrit2.pk}&type=oral",
               dict(CONCOURS, title="Concours 2A — oral", type_epreuve="oral", annee="2",
                    epreuve_precedente=ecrit2.pk))
    assert r.status_code == 302, r.context["form"].errors
    oral2 = Quiz.objects.get(title="Concours 2A — oral")
    assert [m.text for m in oral2.matieres] == ["إ م ت", "ن إج"], oral2.matieres
    print("  oral de 2e année : deux notes (إ م ت، ن إج) OK")

    # ---- une matière de plus, puis retirée ; une matière déjà notée reste
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"ajouter_matiere": "1"})
    assert len(quiz.matieres) == 4 and quiz.matieres[-1].points == 20
    assert "régénérez les documents" in c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"retirer_matiere": quiz.matieres[-1].pk})
    assert len(quiz.matieres) == 3
    r = c.post(reverse("quiz_detail", args=[quiz.pk]),
               {"retirer_matiere": quiz.matieres[0].pk}, follow=True)
    assert "a déjà des copies lues" in r.content.decode() and len(quiz.matieres) == 3
    print("  matière ajoutée (documents à régénérer) puis retirée ; une matière notée "
          "reste OK")

    print("\n✅ TEST DE L'ÉCRIT EN MATIÈRES RÉUSSI")


if __name__ == "__main__":
    main()
