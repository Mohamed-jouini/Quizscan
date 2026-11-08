"""Épreuve écrite (rédaction) et épreuve orale.

Écrit : sujet rédigé dans l'interface, sujet et copies à télécharger ; le
correcteur noircit la note dans le cadre de la page de garde ; au scan, la
note et l'identification (grille de n° ou QR) sont lues et la note est
affectée à l'étudiant. Oral : notes saisies à l'écran.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (quizscan_test)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import pymupdf
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import services
from grader.models import ClassGroup, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

ETUDIANTS = [("AMRI", "Sana", "104501"), ("BAKIR", "Omar", "104502"),
             ("CHERIF", "Lina", "104503")]


def noircir_note(img, grille, chiffres):
    """chiffres : un chiffre (ou None, ou une liste = plusieurs cases) par ligne."""
    for row, d in zip(grille["rows"], chiffres):
        for k in ([] if d is None else d if isinstance(d, list) else [d]):
            cx, cy = row[k]
            cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(grille["r"] * 0.9),
                       (20, 20, 20), -1)
    return img


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def texte_pdf(data):
    return "".join(p.get_text() for p in pymupdf.open(stream=data, filetype="pdf"))


def test_ecrit(c, prof, groupe):
    r = c.post(reverse("quiz_create"), {
        "title": "Rédaction — le voyage", "type_epreuve": "ecrit",
        "class_group": groupe.pk, "language": "fr", "note_sur": "20",
        "sheet_mode": "grid", "id_mode": "grid", "id_digits": "6",
        "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors if r.context else r
    quiz = Quiz.objects.get(title="Rédaction — le voyage", owner=prof)
    assert quiz.type_epreuve == "ecrit"
    q = quiz.questions.get()
    assert (q.qtype, q.points) == ("open", 20.0)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "1. Le sujet" in page and 'class="etapes"' not in page
    print("  création : épreuve écrite, question « Note » sur 20 OK")

    r = c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "reglages": "1", "title": quiz.title,
        "sujet_texte": "Racontez un voyage qui vous a marqué.\n\nDécrivez les lieux.",
        "entete": "Lycée pilote", "duree": "1 h 30", "note_sur": "20",
        "id_mode": "grid", "id_digits": "6", "pages_redaction": "2"})
    assert r.status_code == 302
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    assert quiz.sheet_pdf and quiz.subject_pdf
    sujet = texte_pdf(quiz.subject_pdf.read())
    assert "Racontez un voyage" in sujet and "note sur 20" in sujet and "1 h 30" in sujet
    copies = quiz.sheet_pdf.read()
    doc = pymupdf.open(stream=copies, filetype="pdf")
    # 2 pages lignées demandées, 3 imprimées : la copie remplit deux feuilles entières en
    # recto-verso (demande de l'utilisateur, 6 novembre 2026)
    assert quiz.layout_json["pages_redaction"] == 3 and len(doc) == 4, len(doc)
    assert "RÉSERVÉ AU CORRECTEUR" in doc[0].get_text()
    assert "Page non lue au scan" in doc[1].get_text() and "Ne pas scanner" not in doc[1].get_text()
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "page de garde + 3 pages lignées, en A4 à 100 %, recto-verso sur le bord long" in page
    print("  sujet et copies générés (page de garde + 3 pages lignées : 2 demandées, "
          "feuilles entières) OK")

    layout = quiz.layout_json
    grille = layout["pages"][0]["note_grid"]
    # quatre champs : 15,75 = 1 · 5 · 7 · 5
    assert grille["kinds"] == ["dizaines", "unites", "dixiemes", "centiemes"]
    base = render_page(copies, 0)
    pages = []
    for (nom, prenom, num), chiffres in zip(ETUDIANTS, ([1, 5, 7, 5],
                                                       [0, 8, 0, 0],
                                                       [1, [2, 6], 0, 0])):
        img = fill_sheet(base.copy(), layout, {}, student_number=num)
        pages.append((f"{nom}.jpg", jpg(simulate_scan(noircir_note(img, grille, chiffres)))))
    pages.append(("redaction.jpg", jpg(simulate_scan(render_page(copies, 1)))))
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile(n, d) for n, d in pages], label="Salle 1")
    copies_lues = {s.source_name: s for s in lot.sheets.all()}
    assert copies_lues["redaction.jpg"].status == "ignoree"
    for nom, _, num in ETUDIANTS:
        s = copies_lues[f"{nom}.jpg"]
        assert s.status == "ok" and s.student.student_number == num, (nom, s.status)
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    assert rows["AMRI"]["score"] == 15.75 and rows["AMRI"]["pct"] == 79, rows["AMRI"]
    assert rows["BAKIR"]["score"] == 8.0 and rows["BAKIR"]["pending_open"] == 0
    assert rows["CHERIF"]["pending_open"] == 1, "note ambiguë : à saisir"
    print("  scan : 15,75 et 8 lus et affectés par n° ; note ambiguë à saisir ; "
          "page de rédaction ignorée OK")

    detail = c.get(reverse("batch_detail", args=[lot.pk])).content.decode()
    assert "Page de rédaction" in detail
    cherif = copies_lues["CHERIF.jpg"]
    # toute la ligne s'ouvre d'un clic : le lien de sa première cellule s'étend dessus
    cible = f'class="ligne-lien__cible ligne-lien__nom" href="{reverse("sheet_review", args=[cherif.pk])}"'
    assert cible in detail, "la ligne d'une copie ne s'ouvre pas"
    assert cible in c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert (f'class="ligne-lien__cible" href="{reverse("batch_detail", args=[lot.pk])}"'
            in c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()), "la ligne d'un lot ne s'ouvre pas"
    revue = c.get(reverse("sheet_review", args=[cherif.pk])).content.decode()
    assert "Note illisible : à saisir" in revue
    reponse = cherif.answers.get()
    c.post(reverse("sheet_review", args=[cherif.pk]), {f"points_{reponse.pk}": "11,5"})
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    assert rows["CHERIF"]["score"] == 11.5
    print("  écran de vérification : note illisible saisie à la main OK")

    # note minimale réglée sur la page de l'écrit (demande de l'utilisateur, 7 novembre 2026)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert 'name="note_admission"' in page and "Note minimale de réussite" in page
    reglages = {"reglages": "1", "title": quiz.title, "sujet_texte": quiz.sujet_texte,
                "entete": quiz.entete, "duree": quiz.duree, "date_epreuve": "",
                "note_sur": "20", "id_mode": quiz.id_mode, "id_digits": "6",
                "pages_redaction": "2", "format_impression": quiz.format_impression}
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="25"))
    assert r.status_code == 200 and "Entre 0 et 20" in r.content.decode()
    quiz.refresh_from_db()
    assert quiz.note_admission is None
    sujet_avant = quiz.subject_pdf.name
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="10,0"))
    assert r.status_code == 302
    quiz.refresh_from_db()
    assert quiz.note_admission == 10.0, quiz.note_admission
    assert quiz.subject_pdf.name == sujet_avant, "le sujet n'imprime pas la note minimale"
    assert quiz.modifications.filter(action="note_admission").exists(), "non tracée au journal"
    c.post(reverse("quiz_detail", args=[quiz.pk]), reglages)          # formulaire d'avant
    quiz.refresh_from_db()
    assert quiz.note_admission == 10.0, "un envoi sans le champ garde la note minimale"
    print("  note minimale de réussite réglée sur la page de l'écrit (10 retenu, 25 refusé), "
          "tracée au journal OK")
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert "<th>Bonnes réponses</th>" not in page and "<th>Note</th>" in page
    assert "79 %" in page and "Non réussi" in page and "Réussi" in page
    texte = texte_pdf(c.get(reverse("quiz_bulletins_pdf", args=[quiz.pk])).content)
    assert "NOTE" in texte and "15.75 / 20" in texte and "Épreuve écrite" in texte
    c.post(reverse("quiz_publication", args=[quiz.pk]), {"resultats_publies": "1"})
    cache.clear()
    public = Client().get(reverse("public_resultat") + "?numero=104501").content.decode()
    assert "Rédaction" in public and ("15,75" in public or "15.75" in public) \
        and "79 %" in public
    assert "bonnes réponses" not in public
    cache.clear()
    print("  résultats, bulletin et espace public : note, pourcentage, réussi OK")
    return quiz


def test_ligne_vide_a_verifier(quiz):
    """Chaque ligne de la note se noircit, 0 compris ; une ligne vide n'est pas lue 0, la
    note est à vérifier (demande de l'utilisateur, 8 novembre 2026, après une vraie fiche
    d'oral : « 1 » écrit aux dizaines, rond vide, 19 lu 9)."""
    from grader import omr
    grille = quiz.layout_json["pages"][0]["note_grid"]
    pdf = open(quiz.sheet_pdf.path, "rb").read()

    def ecrire(img, ligne, chiffre="1"):
        x, y, w, h = grille["write_boxes"][ligne]
        cv2.putText(img, chiffre, (mm2px(x + 1.6), mm2px(y + h - 0.9)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.6, (40, 40, 40), 4, cv2.LINE_AA)
        return img

    def lire(img):
        return omr.lire_note(cv2.resize(img, (int(210 * 8), int(297 * 8))), grille, 20)

    neuf = noircir_note(render_page(pdf, 0), grille, [0, 9, 0, 0])
    assert lire(neuf.copy())[0] == 9.0, "0 · 9 · 0 · 0 : 9"
    vide = noircir_note(render_page(pdf, 0), grille, [None, 9, 0, 0])
    note, lus = lire(vide.copy())
    assert note is None and lus == ["?", 9, 0, 0], (note, lus)     # dizaines vides
    note, lus = lire(ecrire(vide.copy(), 0))                       # « 1 » écrit, rond vide
    assert note is None and lus[0] == "?", (note, lus)
    complete = ecrire(noircir_note(render_page(pdf, 0), grille, [1, 9, 0, 0]), 0)
    assert lire(complete)[0] == 19.0, "écrit et noirci : 19"
    sans_centiemes = noircir_note(render_page(pdf, 0), grille, [1, 5, 7, None])
    assert lire(sans_centiemes)[0] is None, "centièmes vides : à vérifier, pas 15,7"
    # par le vrai chemin : feuille scannée (tournée, bruitée), recalée sur son cadre
    for image, attendu in ((neuf, 9.0), (vide, None), (complete, 19.0)):
        recalee, _ = omr.detect_and_warp(simulate_scan(image.copy()))
        assert omr.lire_note(recalee, grille, 20)[0] == attendu, attendu
    print("  ligne de la note sans case noircie (0 compris) : note à vérifier, et non "
          "9 pour 19 OK")


def test_ancienne_grille(quiz):
    """Copies imprimées avant les centièmes (grille à 3 lignes, decimales 1) : lues avec
    leur disposition d'alors ; avec la nouvelle, leur ligne des centièmes, absente, est
    vide : note à vérifier, jamais fausse (8 novembre 2026)."""
    from grader import omr
    nouvelle = quiz.layout_json["pages"][0]["note_grid"]
    ancienne = {**nouvelle, "rows": nouvelle["rows"][:3],
                "kinds": nouvelle["kinds"][:3], "decimales": 1}
    img = noircir_note(render_page(open(quiz.sheet_pdf.path, "rb").read(), 0), ancienne, [1, 4, 5])
    warped = cv2.resize(img, (int(210 * 8), int(297 * 8)))   # repère canonique
    assert omr.lire_note(warped, ancienne, 20)[0] == 14.5
    assert omr.lire_note(warped, nouvelle, 20)[0] is None    # centièmes absents : à vérifier
    img = noircir_note(render_page(open(quiz.sheet_pdf.path, "rb").read(), 0), nouvelle, [1, 5, 7, 5])
    warped = cv2.resize(img, (int(210 * 8), int(297 * 8)))
    assert omr.lire_note(warped, nouvelle, 20)[0] == 15.75
    print("  grille à 4 champs (15,75) ; copies à 3 lignes lues avec leur disposition "
          "(14,5), à vérifier avec la nouvelle OK")


def test_oral(c, groupe):
    """Oral : les notes ne viennent QUE des fiches scannées (plus de saisie
    à la main dans le tableau, demande de l'utilisateur, 30 octobre 2026)."""
    from grader.models import ScanBatch, SheetScan
    r = c.post(reverse("quiz_create"), {
        "title": "Oral d'anglais", "type_epreuve": "oral", "class_group": groupe.pk,
        "language": "fr", "note_sur": "20", "sheet_mode": "grid", "id_mode": "grid",
        "id_digits": "6", "wrong_penalty": "0"})
    assert r.status_code == 302
    quiz = Quiz.objects.get(title="Oral d'anglais", class_group=groupe)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "Notes de l'oral" in page and "Sana AMRI" in page
    assert "pas encore scannée" in page and 'class="etapes"' not in page
    e = {s.last_name: s for s in groupe.students.all()}
    assert f'name="note_{e["AMRI"].pk}"' not in page
    assert "Enregistrer les notes" not in page
    # un envoi fabriqué « à la main » n'enregistre rien
    c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "notes": "1", f"note_{e['AMRI'].pk}": "15,5"})
    assert services.compute_results(quiz) == []
    print("  oral : plus de saisie à la main (champ absent, envoi fabriqué ignoré) OK")

    # ---- note minimale réglée sur la page de l'oral (demande de l'utilisateur, 7 novembre)
    assert 'name="note_admission"' in page and "Note minimale de réussite" in page
    reglages = {"reglages": "1", "title": quiz.title, "note_sur": "20"}
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="21"))
    assert r.status_code == 200 and "Entre 0 et 20" in r.content.decode()
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="12,5"))
    assert r.status_code == 302
    quiz.refresh_from_db()
    assert quiz.note_admission == 12.5, quiz.note_admission
    assert quiz.modifications.filter(action="note_admission").exists(), "non tracée au journal"
    c.post(reverse("quiz_detail", args=[quiz.pk]), reglages)          # formulaire d'avant
    quiz.refresh_from_db()
    assert quiz.note_admission == 12.5, "un envoi sans le champ garde la note minimale"
    c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission=""))
    quiz.refresh_from_db()
    assert quiz.note_admission is None, "vide : plus de note minimale"
    print("  oral : note minimale réglée sur sa page (12,5 retenu, 21 refusé, vide retiré) OK")

    # ---- fiches d'évaluation : imprimées, notées par l'examinateur, scannées
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    fiches = open(quiz.sheet_pdf.path, "rb").read()
    doc = pymupdf.open(stream=fiches, filetype="pdf")
    assert len(doc) == 3, len(doc)                      # une fiche par étudiant
    assert "AMRI" in doc[0].get_text() and "CHERIF" in doc[2].get_text()
    assert "RÉSERVÉ AU CORRECTEUR" in doc[0].get_text()
    grille = quiz.layout_json["pages"][0]["note_grid"]
    scans = []
    # AMRI : 12,25 ; CHERIF : deux cases noircies sur les unités (illisible)
    for nom, page_k, chiffres in (("AMRI", 0, [1, 2, 2, 5]), ("CHERIF", 2, [1, [7, 3], 0, 0])):
        img = noircir_note(render_page(fiches, page_k), grille, chiffres)
        scans.append(SimpleUploadedFile(f"oral_{nom}.jpg", jpg(simulate_scan(img))))
    lot = services.process_uploaded_files(quiz, scans, label="Jury 1")
    assert all(s.status == "ok" for s in lot.sheets.all()), \
        [(s.source_name, s.status, s.error_message) for s in lot.sheets.all()]
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    assert rows["AMRI"]["score"] == 12.25 and rows["CHERIF"]["pending_open"] == 1, rows
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "12,25" in page and "Voir la fiche" in page
    assert "Note illisible : vérifier" in page and "pas encore scannée" in page
    print("  oral scanné : 12,25 lue et affectée par QR ; note illisible à "
          "vérifier ; BAKIR pas encore scannée OK")

    # la note illisible se reprend sur la fiche scannée (image sous les yeux)
    cherif = SheetScan.objects.get(batch=lot, student=e["CHERIF"])
    reponse = cherif.answers.get()
    # résultats : la pastille « note à saisir » mène droit à la fiche
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert f'href="{reverse("sheet_review", args=[cherif.pk])}" title="Ouvrir la copie pour saisir la note"' in page
    # sur la fiche, le champ de la note est dans le bloc « Note », hors du
    # tableau défilant où l'image de la grille le repoussait hors de l'écran
    revue = c.get(reverse("sheet_review", args=[cherif.pk])).content.decode()
    bloc = revue.split('class="note-lue note-lue--a-saisir"')[1].split("</form>")[0]
    assert f'name="points_{reponse.pk}"' in bloc and "Note sur 20" in bloc
    assert "table-wrap" not in bloc.split(f'name="points_{reponse.pk}"')[0]
    # retour aux résultats (demande du 9 novembre 2026) : en tête de page, et après
    # l'enregistrement de la note
    resultats = reverse("quiz_results", args=[quiz.pk])
    assert f'href="{resultats}">← Retour aux résultats</a>' in revue
    assert 'name="retour_resultats"' in bloc and "Enregistrer et retour aux résultats" in bloc
    r = c.post(reverse("sheet_review", args=[cherif.pk]),
               {f"points_{reponse.pk}": "17", "retour_resultats": "1"})
    assert r.status_code == 302 and r["Location"] == resultats, r.get("Location")
    rows = {r["student"].last_name: r["score"] for r in services.compute_results(quiz)}
    assert rows == {"AMRI": 12.25, "CHERIF": 17.0}, rows
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert ">fiche</a>" in page
    print("  note illisible vérifiée sur la fiche scannée : 17 OK")

    # anciennes notes saisies à la main (avant ce changement) : signalées
    ancien = ScanBatch.objects.create(quiz=quiz, label=services.LOT_ORAL, status="done")
    vieille = SheetScan.objects.create(batch=ancien, student=e["BAKIR"], status="ok",
                                       source_name="oral", student_confirmed=True)
    from grader.models import Answer
    Answer.objects.create(sheet=vieille, question=services.question_note(quiz),
                          points_awarded=9.0, manually_set=True)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "saisies à la main" in page and "Supprimer ces notes saisies" in page
    assert reverse("batch_delete", args=[ancien.pk]) in page
    print("  anciennes notes saisies à la main : signalées, avec lien de suppression OK")


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_ecrit").delete()
    ClassGroup.objects.filter(owner__username="prof_ecrit").delete()
    User.objects.filter(username="prof_ecrit").delete()
    prof = enseignant_complet(User.objects.create_user("prof_ecrit", password="x-pass-123"))
    groupe = ClassGroup.objects.create(name="ECRIT 3e A", owner=prof)
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=nom, first_name=prenom,
                               student_number=num)
    c = Client()
    c.force_login(prof)
    print("Épreuve écrite :")
    quiz = test_ecrit(c, prof, groupe)
    test_ancienne_grille(quiz)
    test_ligne_vide_a_verifier(quiz)
    print("Épreuve orale :")
    test_oral(c, groupe)
    print("\n✅ TESTS ÉCRIT ET ORAL RÉUSSIS")


if __name__ == "__main__":
    main()
