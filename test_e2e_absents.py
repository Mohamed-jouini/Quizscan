"""Absents d'une épreuve de concours (demande de l'utilisateur, 25 novembre 2026) : la liste
des copies manquantes exportée en Excel, vidée des lignes de ceux qui n'ont pas composé, puis
réimportée ; les candidats qui n'y sont plus sont supprimés de l'épreuve, après confirmation.

Vérifie : le bouton à côté de l'export (concours, droit de créer) ; l'import montre qui sera
supprimé et ne supprime rien ; annuler ; la confirmation supprime les absents seulement ; un
candidat gardé par son n° (zéro de tête retiré par le tableur) ou par son nom (sans n°) ;
jamais un candidat dont une page est scannée, ici, dans une autre épreuve de la liste, ou
arrivée entre l'import et la confirmation ; fichier sans la colonne du n°, ou qui n'est pas un
tableau : refusé ; liste inchangée : rien à retirer ; liste vidée de toutes ses lignes ; pas
pour une classe ; pas sans le droit de créer.
"""
import html
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

from django.contrib.auth.models import Permission, User  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402
from django.test import Client  # noqa: E402
from django.test.utils import setup_test_environment  # noqa: E402
from django.urls import reverse  # noqa: E402
from openpyxl import Workbook, load_workbook  # noqa: E402

from grader import droits, services  # noqa: E402
from grader.models import ClassGroup, Question, Quiz, ScanBatch, SheetScan, Student  # noqa: E402
from test_commun import enseignant_complet  # noqa: E402

COMPTES = ("prof_absents", "correcteur_absents")
CANDIDATS = [("AMRI", "Sana", "804601"), ("BAKIR", "Omar", "804602"),
             ("CHERIF", "Lina", "804603"), ("DRISS", "Ali", "0804604"),
             ("ESSID", "Rim", "804605"), ("FERJANI", "Hedi", "")]


def xlsx(wb):
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


def reduire(export, garder, nombres=None):
    """Le classeur exporté, réduit aux lignes des candidats « garder » (colonne Nom), comme
    l'utilisateur le fait dans Excel ; nombres : n° réécrits en nombres (le tableur retire
    alors leurs zéros de tête)."""
    wb = load_workbook(io.BytesIO(export))
    ws = wb.active
    assert [c.value for c in ws[2]][:4] == ["Code d'anonymat", "N° d'inscription", "Prénom",
                                            "Nom"], [c.value for c in ws[2]]
    for rang in range(ws.max_row, 2, -1):
        nom = ws.cell(row=rang, column=4).value
        if nom not in garder:
            ws.delete_rows(rang)
        elif nom in (nombres or {}):
            ws.cell(row=rang, column=2).value = nombres[nom]
    return xlsx(wb)


def nettoyer():
    for nom in COMPTES:
        Quiz.objects.filter(owner__username=nom).delete()
        ClassGroup.objects.filter(owner__username=nom).delete()
        User.objects.filter(username=nom).delete()


def concours(proprietaire, nom, candidats):
    groupe = ClassGroup.objects.create(name=nom, owner=proprietaire)
    etudiants = {n: Student.objects.create(class_group=groupe, last_name=n, first_name=p,
                                           student_number=num) for n, p, num in candidats}
    quiz = Quiz.objects.create(title=f"Concours {nom}", class_group=groupe, owner=proprietaire,
                               sheet_mode="split", id_mode="sticker", language="fr",
                               auto_enroll=True)
    for n in range(1, 4):
        Question.objects.create(quiz=quiz, order=n, qtype="qcm", text=f"Question {n}",
                                choices=["a", "b", "c", "d"], correct_choice=0, points=1)
    return quiz, etudiants


def scanner(lot, etudiant):
    SheetScan.objects.create(batch=lot, page_index=0, student=etudiant, status="ok",
                             image="scans/absents.jpg", source_name=f"{etudiant.last_name}.jpg")


def manquants(quiz):
    return sorted(m["etudiant"].last_name for m in services.copies_manquantes(quiz)["lignes"])


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    nettoyer()
    prof = enseignant_complet(User.objects.create_user("prof_absents", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    quiz, etudiants = concours(prof, "ABSENTS", CANDIDATS)
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    assert quiz.layout_json.get("pages"), "PDF générés"
    lot = ScanBatch.objects.create(quiz=quiz, label="Scans")
    scanner(lot, etudiants["AMRI"])
    scanner(lot, etudiants["BAKIR"])
    # ESSID : une page scannée dans une autre épreuve de la même liste ; il a composé
    autre = Quiz.objects.create(title="Autre épreuve", class_group=quiz.class_group, owner=prof,
                                sheet_mode="split", id_mode="sticker", language="fr",
                                auto_enroll=True)
    scanner(ScanBatch.objects.create(quiz=autre, label="Autre"), etudiants["ESSID"])
    assert manquants(quiz) == ["CHERIF", "DRISS", "ESSID", "FERJANI"], manquants(quiz)
    url = reverse("quiz_absents", args=[quiz.pk])
    cle = f"absents_{quiz.pk}"

    # ---- le bouton, à côté de l'export
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert "Copies manquantes : 4 candidats" in page
    assert f'action="{url}"' in page and "Importer la liste sans les absents" in page
    assert page.index("Exporter la liste (Excel)") < page.index("Importer la liste sans les absents")
    export = c.get(reverse("quiz_copies_manquantes_xlsx", args=[quiz.pk])).content
    print("Absents d'un concours :")
    print("  bouton « Importer la liste sans les absents » à côté de l'export OK")

    # ---- fichiers refusés : sans la colonne du n°, ou pas un tableau ; rien en attente
    wb = Workbook()
    wb.active.append(["Nom", "Prénom"])
    wb.active.append(["DRISS", "Ali"])
    r = c.post(url, {"fichier": SimpleUploadedFile("liste.xlsx", xlsx(wb))}, follow=True)
    assert "Liste non reconnue" in r.content.decode() and cle not in c.session
    r = c.post(url, {"fichier": SimpleUploadedFile("liste.pdf", b"%PDF-1.4")}, follow=True)
    assert "Importez la liste exportée" in r.content.decode() and cle not in c.session
    r = c.post(url, {}, follow=True)
    assert "Choisissez le fichier de la liste" in r.content.decode()
    # ---- liste inchangée : rien à retirer
    r = c.post(url, {"fichier": SimpleUploadedFile("liste.xlsx", export)}, follow=True)
    assert "Aucun absent à retirer" in r.content.decode() and cle not in c.session
    assert c.get(url).status_code == 302, "rien en attente : retour aux résultats"
    print("  fichier sans colonne du n° ou pas un tableau : refusé ; liste inchangée : rien "
          "à retirer OK")

    # ---- liste sans CHERIF ni ESSID ; DRISS sans son zéro de tête, FERJANI sans n°
    fichier = reduire(export, {"DRISS", "FERJANI"}, nombres={"DRISS": 804604})
    r = c.post(url, {"fichier": SimpleUploadedFile("liste.xlsx", fichier)})
    assert r.status_code == 302 and r.url == url, r
    assert groupe_noms(quiz) == {n for n, _, _ in CANDIDATS}, "rien de supprimé à l'import"
    r = c.get(url)
    page = r.content.decode()
    assert [s.last_name for s in r.context["absents"]] == ["CHERIF"], r.context["absents"]
    assert [s.last_name for s in r.context["gardes"]] == ["ESSID"]
    assert r.context["restent"] == 2 and r.context["n_inconnues"] == 0
    assert "1 candidat(s) absent(s) à supprimer" in page and "804603" in page
    assert "Supprimer les absents (1)" in page and "ont composé" in page
    # annuler : rien de supprimé, plus rien en attente
    r = c.post(url, {"annuler": "1"})
    assert r.status_code == 302 and cle not in c.session
    assert groupe_noms(quiz) == {n for n, _, _ in CANDIDATS}
    # de nouveau, puis la confirmation : CHERIF seul est supprimé
    c.post(url, {"fichier": SimpleUploadedFile("liste.xlsx", fichier)})
    r = c.post(url, {"confirmer": "1"}, follow=True)
    assert "1 candidat(s) absent(s) supprimé(s) de « ABSENTS »" in r.content.decode()
    assert groupe_noms(quiz) == {"AMRI", "BAKIR", "DRISS", "ESSID", "FERJANI"}
    assert manquants(quiz) == ["DRISS", "ESSID", "FERJANI"], manquants(quiz)
    r = c.post(url, {"confirmer": "1"}, follow=True)
    assert "Importez d'abord la liste sans les absents" in html.unescape(r.content.decode())
    print("  import : CHERIF à supprimer, DRISS (n° sans zéro de tête) et FERJANI (par son "
          "nom) restent, ESSID gardé (page scannée ailleurs) ; annuler ; confirmer : CHERIF "
          "supprimé OK")

    # ---- liste vidée de toutes ses lignes ; une page de DRISS arrive avant la confirmation
    export = c.get(reverse("quiz_copies_manquantes_xlsx", args=[quiz.pk])).content
    c.post(url, {"fichier": SimpleUploadedFile("vide.xlsx", reduire(export, set()))})
    r = c.get(url)
    assert [s.last_name for s in r.context["absents"]] == ["DRISS", "FERJANI"]
    scanner(lot, etudiants["DRISS"])
    r = c.post(url, {"confirmer": "1"}, follow=True)
    page = r.content.decode()
    assert "1 candidat(s) absent(s) supprimé(s)" in page, page[:3000]
    assert "1 candidat(s) gardé(s) : une page de leur copie a été scannée" in page
    assert groupe_noms(quiz) == {"AMRI", "BAKIR", "DRISS", "ESSID"}
    print("  liste vidée : DRISS et FERJANI ; une page de DRISS arrivée avant la confirmation "
          "le garde, FERJANI supprimé OK")

    # ---- pas pour une classe : ni bouton, ni page
    classe = ClassGroup.objects.create(name="CLASSE ABSENTS", owner=prof)
    Student.objects.create(class_group=classe, last_name="GHARBI", first_name="Sami",
                           student_number="904601")
    qcm = Quiz.objects.create(title="QCM de classe", class_group=classe, owner=prof,
                              sheet_mode="split", id_mode="sticker", language="fr")
    Question.objects.create(quiz=qcm, order=1, qtype="qcm", text="Question", choices=["a", "b"],
                            correct_choice=0, points=1)
    c.post(reverse("quiz_detail", args=[qcm.pk]), {"generate_pdf": "1"})
    ScanBatch.objects.create(quiz=qcm, label="Scans")
    page = c.get(reverse("quiz_results", args=[qcm.pk])).content.decode()
    assert "Copies manquantes : 1 étudiant" in page and "sans les absents" not in page
    assert c.get(reverse("quiz_absents", args=[qcm.pk])).status_code == 404
    assert c.post(reverse("quiz_absents", args=[qcm.pk]), {"confirmer": "1"}).status_code == 404

    # ---- sans le droit de créer : pas de bouton, envoi refusé
    correcteur = User.objects.create_user("correcteur_absents", password="x-pass-123")
    application, code = droits.CORRIGER.split(".")
    correcteur.user_permissions.add(Permission.objects.get(
        content_type__app_label=application, codename=code))
    quiz_c, etudiants_c = concours(correcteur, "ABSENTS CORRECTEUR", CANDIDATS[:2])
    c.force_login(prof)
    quiz_c.owner = prof
    quiz_c.save(update_fields=["owner"])
    c.post(reverse("quiz_detail", args=[quiz_c.pk]), {"generate_pdf": "1"})
    quiz_c.owner = correcteur
    quiz_c.save(update_fields=["owner"])
    ScanBatch.objects.create(quiz=quiz_c, label="Scans")
    c.force_login(correcteur)
    page = c.get(reverse("quiz_results", args=[quiz_c.pk])).content.decode()
    assert "Copies manquantes : 2 candidats" in page and "sans les absents" not in page
    r = c.post(reverse("quiz_absents", args=[quiz_c.pk]),
               {"fichier": SimpleUploadedFile("liste.xlsx", reduire(export, set()))})
    assert r.status_code == 403
    assert Student.objects.filter(class_group=quiz_c.class_group).count() == 2
    print("  pas pour une classe (404) ; sans le droit de créer : pas de bouton, envoi "
          "refusé (403) OK")
    nettoyer()
    print("Tous les contrôles des absents sont passés.")


def groupe_noms(quiz):
    return set(quiz.class_group.students.values_list("last_name", flat=True))


if __name__ == "__main__":
    main()
