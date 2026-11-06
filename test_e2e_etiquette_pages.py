"""Feuille à étiquette sur plusieurs pages : chaque page s'identifie seule au scan.

La planche donne donc une vignette par page à chaque candidat. Une étiquette oubliée
ne doit jamais passer inaperçue : la page 2 reste « à identifier », ses réponses ne
comptent pas, et une copie sans faute affichait 42/50, 0 « vide », « non admis ».
"""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import io
import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (quizscan_test)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import openpyxl
import pymupdf
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from grader import bulletin
from grader import layout as L
from grader import services, sheet_pdf
from grader.forms import QuizForm
from grader.models import ClassGroup, Question, Quiz, ScanBatch, SheetScan, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan
from test_e2e_web import stick_label

PAGE_1 = 63                 # questions sur la page 1 (trois colonnes depuis le 7 nov. 2026)
NB_QCM = PAGE_1 + 8         # 8 sur la page 2
PROF = "prof_etiq_pages"
INCOMPLETE = "à copie incomplète"


def corrige(order):
    return (order - 1) % 4


def creer_quiz(nom, teacher, nb_qcm=NB_QCM, **kw):
    groupe = ClassGroup.objects.create(name=nom, owner=teacher)
    quiz = Quiz.objects.create(title=f"Étiquette {nom}", class_group=groupe, owner=teacher,
                               num_choices=4, sheet_mode="grid", **kw)
    Question.objects.bulk_create(
        Question(quiz=quiz, order=o, qtype="qcm", points=1.0, correct_choice=corrige(o))
        for o in range(1, nb_qcm + 1))
    candidats = [Student.objects.create(class_group=groupe, last_name=f"CANDIDAT{i}",
                                        first_name="Test", student_number=f"30{i:03d}")
                 for i in range(2)]
    return groupe, quiz, candidats


def preparer(quiz, **kw_pdf):
    disposition = L.build_layout(quiz)
    quiz.layout_json = disposition
    pdf = sheet_pdf.generate_sheet_pdf(quiz, disposition, **kw_pdf)
    quiz.sheet_pdf.save(f"fiche_{quiz.pk}.pdf", ContentFile(pdf), save=True)
    return disposition, pdf


def copie(pdf, disposition, candidat, pages_etiquetees):
    """Pages scannées d'une copie sans faute ; étiquette sur les pages demandées."""
    parfaites = {o: corrige(o) for o in range(1, NB_QCM + 1)}
    images = []
    for p in range(len(disposition["pages"])):
        img = fill_sheet(render_page(pdf, p), disposition, parfaites, page_index=p)
        if p in pages_etiquetees:
            img = stick_label(img, disposition["pages"][p], candidat)
        images.append(simulate_scan(img))
    return images


def envoyer(quiz, images, label):
    """Ce que rend un chargeur de scanner : un seul PDF, les pages dans l'ordre."""
    doc = pymupdf.open()
    for img in images:
        jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
        h, w = img.shape[:2]
        page = doc.new_page(width=w * 72 / 300, height=h * 72 / 300)
        page.insert_image(page.rect, stream=jpg)
    lot = SimpleUploadedFile(f"{label}.pdf", doc.tobytes(), "application/pdf")
    doc.close()
    return services.process_uploaded_files(quiz, [lot], label=label)


def lignes(quiz):
    return {r["student"].pk: r for r in services.compute_results(quiz)}


def texte_pdf(octets):
    with pymupdf.open(stream=octets, filetype="pdf") as doc:
        return "\n".join(p.get_text("text") for p in doc)


def verifier_planche(teacher):
    client = Client()
    client.force_login(teacher)

    # Fiche de 2 pages : 2 vignettes par candidat, et l'enseignant est prévenu.
    groupe, quiz, candidats = creer_quiz("EP-PLANCHE", teacher, id_mode="sticker")
    assert services.etiquettes_par_candidat(groupe) == 1, "aucune fiche générée"
    disposition, pdf = preparer(quiz)
    assert L.pages_a_etiquette(disposition) == 2
    assert services.etiquettes_par_candidat(groupe) == 2
    planche = texte_pdf(client.get(f"/classes/{groupe.pk}/etiquettes.pdf").content)
    for c in candidats:
        assert planche.count(f"N° {c.student_number}") == 2, planche
    assert "le candidat en reçoit 2" in planche.replace("\n", " "), planche
    assert "2 par candidat" in client.get(f"/classes/{groupe.pk}/").content.decode()
    page = client.get(f"/quiz/{quiz.pk}/").content.decode()
    assert "Cette fiche compte 2 pages" in page, "pas de consigne sur la page de l'épreuve"

    # Le scan sait lire une étiquette sur chaque page : rien à perdre, rien à corriger.
    images = copie(pdf, disposition, candidats[0], {0, 1})
    lot = envoyer(quiz, images, "deux-etiquettes")
    feuilles = list(lot.sheets.order_by("pk"))
    assert [f.student_id for f in feuilles] == [candidats[0].pk] * 2, feuilles
    r = lignes(quiz)[candidats[0].pk]
    assert r["score"] == NB_QCM and r["questions_manquantes"] == 0, r
    assert INCOMPLETE not in client.get(f"/quiz/{quiz.pk}/resultats/").content.decode()
    print("  planche : 2 vignettes par candidat ; une étiquette sur chaque page : 50/50")

    # Fiche d'une page : rien ne change.
    groupe, quiz, _ = creer_quiz("EP-UNE-PAGE", teacher, nb_qcm=PAGE_1, id_mode="sticker")
    preparer(quiz)
    assert services.etiquettes_par_candidat(groupe) == 1
    assert "(PDF) : " not in client.get(f"/classes/{groupe.pk}/").content.decode()
    assert "Cette fiche compte" not in client.get(f"/quiz/{quiz.pk}/").content.decode()

    # Concours en « qr » : à étiquette aussi. QR imprimé sur chaque page : aucune vignette.
    groupe, quiz, _ = creer_quiz("EP-CONC-QR", teacher, id_mode="qr", auto_enroll=True)
    preparer(quiz)
    assert services.etiquettes_par_candidat(groupe) == 2
    groupe, quiz, candidats = creer_quiz("EP-NOMINATIF", teacher, id_mode="qr")
    preparer(quiz, students=candidats)
    assert services.etiquettes_par_candidat(groupe) == 1
    print("  fiche d'une page et QR nominatif : une vignette, aucune alerte")


def verifier_etiquette_oubliee(teacher):
    """Le premier candidat oublie l'étiquette de sa page 2, le second non."""
    client = Client()
    client.force_login(teacher)
    groupe, quiz, (oublie, complet) = creer_quiz("EP-OUBLI", teacher, id_mode="sticker",
                                                 auto_enroll=True, note_admission=45,
                                                 resultats_publies=True)
    disposition, pdf = preparer(quiz)
    lot = envoyer(quiz, copie(pdf, disposition, oublie, {0})
                  + copie(pdf, disposition, complet, {0, 1}), "oubli")
    feuilles = list(lot.sheets.order_by("pk"))
    assert [f.status for f in feuilles] == ["ok", "no_match", "ok", "ok"], feuilles
    page2 = feuilles[1]
    assert page2.student_id is None

    # La note reste partielle, mais elle ne se présente plus comme une note définitive.
    rangs = lignes(quiz)
    r = rangs[oublie.pk]
    assert r["score"] == PAGE_1 and r["questions_manquantes"] == 8 and r["blank"] == 0, r
    assert rangs[complet.pk]["score"] == NB_QCM
    assert rangs[complet.pk]["questions_manquantes"] == 0
    adm = services.admissions(quiz, list(rangs.values()))
    assert r["resultat"] == "en_attente", "copie sans faute refusée sur une note partielle"
    assert rangs[complet.pk]["resultat"] == "admis" and adm["n_attente"] == 1
    print(f"  étiquette oubliée : {PAGE_1}/{NB_QCM} en attente, 8 questions non lues, "
          "pas de refus")

    # Partout où la note se lit : écran, alerte qu'on ne peut pas fermer, bulletin, Excel.
    url = f"/quiz/{quiz.pk}/resultats/"
    ecran = client.get(url).content.decode()
    assert "Incomplète : 8 questions" in ecran, "pas de signe sur la ligne"
    assert f"1 candidat(s) {INCOMPLETE}" in ecran
    assert "leurs notes ne comptent pas encore" in ecran
    client.post(url, {"masquer_non_identifiees": "1"})
    ecran = client.get(url).content.decode()
    assert "leurs notes ne comptent" not in ecran and INCOMPLETE in ecran, \
        "fermer l'alerte des pages ne doit pas cacher les copies incomplètes"
    client.post(url, {"reafficher_non_identifiees": "1"})

    releve = texte_pdf(bulletin.generate_bulletins_pdf(quiz, [r], admission=adm))
    assert "page de la copie à identifier" in releve and "8 question(s) non lue(s)" in releve
    classeur = openpyxl.load_workbook(io.BytesIO(
        client.get(f"/quiz/{quiz.pk}/resultats.xlsx").content))["Résultats"]
    entetes = [c.value for c in classeur[2]]
    colonne = entetes.index("Questions non lues")
    valeurs = {ligne[entetes.index("N° inscription")].value: ligne[colonne].value
               for ligne in classeur.iter_rows(min_row=3)}
    assert valeurs == {oublie.student_number: 8, complet.student_number: 0}, valeurs

    # Espace public : pas de « non admis » sur une note partielle.
    cache.clear()           # le frein aux recherches publiques compte par adresse
    public = Client().get("/public/resultats/", {"numero": oublie.student_number}).content.decode()
    assert "Correction en cours" in public and "Non admis" not in public, public
    print("  signalé à l'écran (même alerte fermée), au bulletin, dans l'Excel et en public")

    # Sans note minimale : note provisoire. Avec une pénalité, la page manquante peut
    # aussi faire baisser la note : pas d'admission d'office.
    quiz.note_admission = None
    r = lignes(quiz)[oublie.pk]
    assert services.admissions(quiz, [r]) is None and r["resultat"] is None
    quiz.note_admission = 40
    r = lignes(quiz)[oublie.pk]
    services.admissions(quiz, [r])
    assert r["resultat"] == "admis", r["resultat"]
    quiz.wrong_penalty = 0.5
    r = lignes(quiz)[oublie.pk]
    services.admissions(quiz, [r])
    assert r["resultat"] == "en_attente", r["resultat"]
    quiz.note_admission, quiz.wrong_penalty = 45, 0.0

    # La page se rattache à la main, le candidat de la page précédente présélectionné.
    ecran = client.get(f"/copies/{page2.pk}/").content.decode()
    liste = ecran.split('id="id_student"')[1].split("</select>")[0]
    assert f'value="{oublie.pk}" selected' in liste, "candidat non présélectionné"
    assert liste.count(" selected") == 1 and "ordre du scan" in ecran
    assert client.post(f"/copies/{page2.pk}/", {"student": oublie.pk}).status_code == 302
    rangs = lignes(quiz)
    r = rangs[oublie.pk]
    assert r["score"] == NB_QCM and r["questions_manquantes"] == 0, r
    services.admissions(quiz, list(rangs.values()))
    assert r["resultat"] == "admis", r["resultat"]
    ecran = client.get(url).content.decode()
    assert INCOMPLETE not in ecran and "Incomplète :" not in ecran
    print("  page affectée : présélection d'après le scan, 50/50, admis, plus d'alerte")


def verifier_preselection(teacher):
    """L'ordre du scan ne fait que présélectionner, et seulement s'il est cohérent."""
    groupe, quiz, (a, b) = creer_quiz("EP-ORDRE", teacher, nb_qcm=1, id_mode="sticker")
    candidats = [a, b]

    def lot(*pages):
        """Pages (n° de page de la fiche, état, candidat), dans l'ordre du scan."""
        nouveau = ScanBatch.objects.create(quiz=quiz, label="ordre")
        return [SheetScan.objects.create(batch=nouveau, page_index=p, status=etat, student=qui)
                for p, etat, qui in pages]

    def propose(feuille):
        return services.suite_de_copie(feuille, candidats)

    assert propose(lot((0, "ok", a), (1, "no_match", None))[1]) == a
    # verso blanc entre les deux : sauté
    assert propose(lot((0, "ok", a), (0, "no_markers", None), (1, "no_match", None))[2]) == a
    # rien avant, page 1, page précédente sans candidat ou autre que celle attendue
    assert propose(lot((1, "no_match", None))[0]) is None
    assert propose(lot((0, "ok", a), (0, "no_match", None))[1]) is None
    assert propose(lot((0, "no_match", None), (1, "no_match", None))[1]) is None
    assert propose(lot((0, "ok", a), (1, "ok", a), (1, "no_match", None))[2]) is None
    # page reconnue, ou candidat qui a déjà cette page ailleurs
    assert propose(lot((0, "ok", a), (1, "ok", a))[1]) is None
    SheetScan.objects.filter(batch__quiz=quiz).delete()
    lot((1, "ok", a))
    assert propose(lot((0, "ok", a), (1, "no_match", None))[1]) is None
    SheetScan.objects.filter(batch__quiz=quiz).delete()
    # un nom lu sur la page décide, pas l'ordre du scan
    feuilles = lot((0, "ok", a), (1, "no_match", None))
    feuilles[1].ocr_name_raw = "TRABELSI"
    assert propose(feuilles[1]) is None
    print("  présélection : page précédente cohérente seulement, verso blanc sauté")


def verifier_formulaire():
    """Le concours propose le sujet séparé : le questionnaire complet demanderait une
    étiquette toutes les 5 QCM."""
    assert QuizForm(concours=True)["sheet_mode"].value() == "split"
    assert QuizForm()["sheet_mode"].value() == "full"
    print("  concours : « sujet séparé » proposé d'office, quiz de classe inchangé")


def main():
    logging.disable(logging.WARNING)
    Quiz.objects.filter(owner__username=PROF).delete()
    ClassGroup.objects.filter(owner__username=PROF).delete()
    User.objects.filter(username=PROF).delete()
    teacher = enseignant_complet(User.objects.create_user(PROF, password="x-pass-123"))

    print("Planche :")
    verifier_planche(teacher)
    print("Étiquette oubliée :")
    verifier_etiquette_oubliee(teacher)
    print("Ordre du scan :")
    verifier_preselection(teacher)
    print("Formulaire :")
    verifier_formulaire()
    print("\n✅ FEUILLE À ÉTIQUETTE SUR PLUSIEURS PAGES : TEST RÉUSSI")


if __name__ == "__main__":
    main()
