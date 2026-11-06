"""Copies à talon : le nom de l'étudiant est imprimé sur un talon, en haut de
la page, hors du cadre de lecture ; découpé avant la correction, il rend la
copie anonyme, et le QR (dans le cadre) l'identifie toujours au scan.

Vérifie : nom seulement sur le talon ; copie lue talon découpé (au trait,
un peu trop haut, un peu trop bas) ou non découpé ; pas de confusion avec
une copie normale ; concours et épreuve écrite.
"""
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
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from grader import layout as L, services, sheet_pdf
from grader.models import ClassGroup, Inscrit, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

REPONSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0}
ETUDIANTS = [("AMRI", "Sana", "104501"), ("BAKIR", "Omar", "104502"),
             ("CHERIF", "Lina", "104503")]
PT_PAR_MM = 72 / 25.4


def couper(img, a_mm):
    """La page telle qu'elle sort du massicot : le haut retiré jusqu'à a_mm."""
    return img[mm2px(a_mm):, :].copy()


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def _lire(quiz, nom_fichier, img):
    lot = services.process_uploaded_files(quiz, [SimpleUploadedFile(nom_fichier, jpg(img))])
    copie = lot.sheets.get()
    lus = {a.question.order: a.detected_choice for a in copie.answers.select_related("question")}
    return copie, lus


def main():
    logging.disable(logging.WARNING)
    Quiz.objects.filter(owner__username="prof_talon").delete()
    ClassGroup.objects.filter(owner__username="prof_talon").delete()
    User.objects.filter(username="prof_talon").delete()
    prof = enseignant_complet(User.objects.create_user("prof_talon", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    groupe = ClassGroup.objects.create(name="TALON 3e A", owner=prof)
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=nom, first_name=prenom,
                               student_number=num)

    # ---- réglage : la case « talon » impose les copies nominatives à QR
    quiz = Quiz.objects.create(title="Contrôle talon", class_group=groupe, owner=prof,
                               sheet_mode="grid", id_mode="grid")
    for i, lettre in enumerate("ABCDA", start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm",
                                correct_choice=ord(lettre) - 65)
    c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "update_settings": "1", "title": quiz.title, "sheet_mode": "grid",
        "id_mode": "grid", "id_digits": "6", "wrong_penalty": "0", "anonymat": "talon"})
    quiz.refresh_from_db()
    assert quiz.talon_nom and quiz.id_mode == "qr", (quiz.talon_nom, quiz.id_mode)
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    assert quiz.layout_json["talon"] and quiz.layout_json["cadre_haut"] == L.TALON_CADRE_HAUT
    pdf = open(quiz.sheet_pdf.path, "rb").read()
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert len(doc) == 3, len(doc)
    # le nom n'apparaît QUE sur le talon (au-dessus du trait de coupe)
    for k, (nom, _, num) in enumerate(ETUDIANTS):
        mots = doc[k].get_text("words")
        ys = [w[1] / PT_PAR_MM for w in mots if w[4] in (nom, num)]
        assert ys and all(y < L.TALON_COUPE for y in ys), (nom, ys)
    assert "découper" in doc[0].get_text() and "COPIE ANONYME" not in doc[0].get_text()
    # le titre au centre : sur le talon, et seul dans la bande grise
    bande = quiz.layout_json["pages"][0]["qr"]["band"]
    sur_talon = doc[0].search_for(f"{quiz.title} — {quiz.class_group.name}")
    dans_bande = [r for r in doc[0].search_for(quiz.title)
                  if bande[1] <= r.y0 / PT_PAR_MM <= bande[1] + bande[3]]
    assert len(sur_talon) == 1 and len(dans_bande) == 1, (sur_talon, dans_bande)
    for r, milieu in ((sur_talon[0], L.PAGE_W / 2), (dans_bande[0], bande[0] + bande[2] / 2)):
        assert abs((r.x0 + r.x1) / 2 / PT_PAR_MM - milieu) < 1.5, (r, milieu)
    print("  copies à talon : nom et n° seulement au-dessus du trait de coupe OK")

    # ---- sous le n° : la CIN et la salle de la fiche du registre (5 novembre 2026)
    Inscrit.objects.filter(numero="TAL104501").delete()
    amri = groupe.students.get(last_name="AMRI")
    amri.inscrit = Inscrit.objects.create(numero="TAL104501", nom="AMRI", prenom="Sana",
                                          cin="08870001", salle="B12")
    amri.save(update_fields=["inscrit"])
    disposition = quiz.layout_json
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generate_pdf": "1"})
    quiz.refresh_from_db()
    assert quiz.layout_json == disposition, "la zone lue a bougé"
    doc = pymupdf.open(stream=open(quiz.sheet_pdf.path, "rb").read(), filetype="pdf")
    for texte in ("CIN 08870001", "Salle B12", "Page 1 / 1"):
        r = doc[0].search_for(texte)
        assert len(r) == 1 and r[0].y1 / PT_PAR_MM < L.TALON_COUPE - 2, (texte, r)
        assert r[0].x0 / PT_PAR_MM > L.PAGE_W / 2, (texte, "pas du côté du n°")
    cote_n = doc[0].search_for("N° 104501")[0]
    assert doc[0].search_for("CIN 08870001")[0].y0 > cote_n.y1, "la CIN n'est pas sous le n°"
    assert "CIN" not in doc[1].get_text() and "Salle" not in doc[1].get_text()
    page_seule = doc[1].search_for("Page 1 / 1")[0]
    assert abs(page_seule.y1 / PT_PAR_MM - 18.0) < 1.5, "sans fiche, le talon a bougé"
    assert sheet_pdf._ligne_salle("Salle 4", sheet_pdf.LABELS["fr"]) == "Salle 4"
    assert sheet_pdf._ligne_salle("قاعة عدد 37", sheet_pdf.LABELS["ar"]) == "قاعة عدد 37"
    assert sheet_pdf._ligne_salle("37", sheet_pdf.LABELS["ar"]) == "القاعة 37"
    # une requête pour toutes les fiches, pas une par copie
    def requetes(n):
        etudiants = list(groupe.students.all())[:n]
        with CaptureQueriesContext(connection) as ctx:
            sheet_pdf.generate_sheet_pdf(quiz, quiz.layout_json, students=etudiants)
        return len(ctx)
    requetes(3)     # le premier appel charge aussi la classe du quiz, une fois
    une, trois = requetes(1), requetes(3)
    assert une == trois, (une, trois)
    print("  talon : CIN et salle sous le n°, au-dessus du trait ; rien sans fiche OK")

    # ---- la copie de BAKIR, remplie, talon découpé : à qui ? quelles réponses ?
    bakir = groupe.students.get(last_name="BAKIR")
    page = fill_sheet(render_page(pdf, 1), quiz.layout_json, REPONSES, name_text=("", ""))
    for coupe in (30.0, 27.0, 34.0):
        copie, lus = _lire(quiz, f"coupe{coupe}.jpg", simulate_scan(couper(page, coupe)))
        assert copie.status == "ok" and copie.student_id == bakir.pk, (coupe, copie.status)
        assert lus == REPONSES, (coupe, lus)
    copie, lus = _lire(quiz, "entiere.jpg", simulate_scan(page))
    assert copie.student_id == bakir.pk and lus == REPONSES
    copie, lus = _lire(quiz, "envers.jpg",
                       cv2.rotate(simulate_scan(couper(page, 30.0)), cv2.ROTATE_180))
    assert copie.student_id == bakir.pk and lus == REPONSES
    print("  talon découpé (au trait, 3 mm trop haut, 4 mm trop bas), non découpé, "
          "à l'envers : BAKIR et ses réponses lus OK")

    # ---- une copie normale n'est pas prise pour une copie à talon
    normal = Quiz.objects.create(title="Contrôle normal", class_group=groupe, owner=prof,
                                 sheet_mode="grid", id_mode="qr")
    for i, lettre in enumerate("ABCDA", start=1):
        Question.objects.create(quiz=normal, order=i, qtype="qcm",
                                correct_choice=ord(lettre) - 65)
    c.post(reverse("quiz_detail", args=[normal.pk]), {"generate_pdf": "1"})
    normal.refresh_from_db()
    assert not normal.layout_json.get("talon")
    pdf_n = open(normal.sheet_pdf.path, "rb").read()
    page_n = fill_sheet(render_page(pdf_n, 2), normal.layout_json, REPONSES, name_text=("", ""))
    copie, lus = _lire(normal, "normale.jpg", simulate_scan(page_n))
    assert copie.student.last_name == "CHERIF" and lus == REPONSES, lus
    print("  copie normale toujours lue comme telle (pas de confusion de gabarit) OK")

    # ---- concours : copies nominatives à talon (et non fiches anonymes)
    r = c.post(reverse("quiz_create") + "?concours=1", {
        "title": "Concours talon", "type_epreuve": "qcm", "language": "fr",
        "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
        "anonymat": "talon", "grading_mode": "immediate", "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors
    concours = Quiz.objects.get(title="Concours talon")
    assert concours.talon_nom and concours.id_mode == "qr" and concours.auto_enroll
    Question.objects.create(quiz=concours, order=1, qtype="qcm", correct_choice=0)
    for nom, prenom, num in ETUDIANTS[:2]:
        Student.objects.create(class_group=concours.class_group, last_name=nom,
                               first_name=prenom, student_number=num)
    c.post(reverse("quiz_detail", args=[concours.pk]), {"generate_pdf": "1"})
    concours.refresh_from_db()
    doc = pymupdf.open(stream=open(concours.sheet_pdf.path, "rb").read(), filetype="pdf")
    assert len(doc) == 2 and "AMRI" in doc[0].get_text() and "BAKIR" in doc[1].get_text()
    print("  concours : une copie à talon par candidat OK")

    # ---- épreuve écrite à talon : la note lue, talon découpé
    r = c.post(reverse("quiz_create"), {
        "title": "Rédaction talon", "type_epreuve": "ecrit", "class_group": groupe.pk,
        "language": "fr", "note_sur": "20", "sheet_mode": "grid", "id_mode": "grid",
        "id_digits": "6", "anonymat": "talon", "wrong_penalty": "0"})
    ecrit = Quiz.objects.get(title="Rédaction talon")
    assert ecrit.talon_nom and ecrit.id_mode == "qr"
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"generer": "1"})
    ecrit.refresh_from_db()
    pdf_e = open(ecrit.sheet_pdf.path, "rb").read()
    grille = ecrit.layout_json["pages"][0]["note_grid"]
    pages_par_copie = 1 + ecrit.pages_redaction
    img = render_page(pdf_e, 2 * pages_par_copie)          # copie de CHERIF
    for row, d in zip(grille["rows"], [1, 3, 5, None]):     # 13,5
        if d is not None:
            cx, cy = row[d]
            cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(grille["r"] * 0.9),
                       (20, 20, 20), -1)
    services.process_uploaded_files(
        ecrit, [SimpleUploadedFile("ecrit.jpg", jpg(simulate_scan(couper(img, 30.0))))])
    rows = {r["student"].last_name: r["score"] for r in services.compute_results(ecrit)}
    assert rows == {"CHERIF": 13.5}, rows
    print("  écrit à talon : talon découpé, note 13,5 lue et affectée à CHERIF OK")

    # pages de rédaction (demande de l'utilisateur, 6 novembre 2026) : chacune reprend
    # l'en-tête de la page de garde, talon avec le nom et le n°, bande du titre et QR de
    # l'étudiant ; toujours sans cadre ni code de page : au scan, elles restent ignorées
    doc_e = pymupdf.open(stream=pdf_e, filetype="pdf")
    amri = ecrit.class_group.students.get(last_name="AMRI")
    assert ecrit.pages_redaction >= 1 and f"Page 1 / {pages_par_copie}" in doc_e[0].get_text(), \
        "le talon de la page de garde doit compter toutes les pages de la copie"
    for k in range(1, pages_par_copie):
        texte = doc_e[k].get_text()
        for attendu in ("AMRI Sana", "N° 104501", f"Page {k + 1} / {pages_par_copie}",
                        "Talon à découper", "Ne pas scanner"):
            assert attendu in texte, (k, attendu, texte)
        lu, _, _ = cv2.QRCodeDetector().detectAndDecode(render_page(pdf_e, k))
        assert lu == f"QS|{ecrit.pk}|{amri.pk}", (k, lu)
    lot = services.process_uploaded_files(ecrit, [
        SimpleUploadedFile("suite_decoupee.jpg",
                           jpg(simulate_scan(couper(render_page(pdf_e, 1), 30.0)))),
        SimpleUploadedFile("suite_entiere.jpg", jpg(simulate_scan(render_page(pdf_e, 1))))])
    statuts = [(s.status, s.error_message) for s in lot.sheets.order_by("pk")]
    assert [s for s, _ in statuts] == ["ignoree", "ignoree"], statuts
    rows = {r["student"].last_name: r["score"] for r in services.compute_results(ecrit)}
    assert rows == {"CHERIF": 13.5}, rows
    print("  écrit à talon : talon, bande et QR sur chaque page de rédaction, "
          "ignorées au scan OK")

    # copie nominative sans talon : le nom, le n° et le QR sur chaque page de rédaction
    nominatif = Quiz.objects.create(title="Rédaction nominative", class_group=groupe,
                                    owner=prof, type_epreuve="ecrit", language="fr",
                                    id_mode="qr", note_sur=20, pages_redaction=2)
    c.post(reverse("quiz_detail", args=[nominatif.pk]), {"generer": "1"})
    nominatif.refresh_from_db()
    pdf_n = open(nominatif.sheet_pdf.path, "rb").read()
    doc_n = pymupdf.open(stream=pdf_n, filetype="pdf")
    assert len(doc_n) == 3 * len(ETUDIANTS), len(doc_n)
    for k in (1, 2):
        texte = doc_n[k].get_text()
        assert "AMRI Sana" in texte and "N° 104501" in texte and f"Page {k + 1} / 3" in texte \
            and "Ne pas scanner" in texte, (k, texte)
        lu, _, _ = cv2.QRCodeDetector().detectAndDecode(render_page(pdf_n, k))
        assert lu == f"QS|{nominatif.pk}|{amri.pk}", (k, lu)
    print("  écrit nominatif : nom, n° et QR sur chaque page de rédaction OK")

    # ---- nombre de copies : il suit la liste, le champ n'apparaît que s'il agit
    def page(q):
        return c.get(reverse("quiz_detail", args=[q.pk])).content.decode()

    def nb_pages(q):
        q.refresh_from_db()
        return len(pymupdf.open(stream=open(q.sheet_pdf.path, "rb").read(), filetype="pdf"))

    assert 'name="qr_count"' not in page(concours) and "2 copies</b>" in page(concours)
    c.post(reverse("quiz_detail", args=[concours.pk]), {"generate_pdf": "1", "qr_count": "100"})
    assert nb_pages(concours) == 2, "QCM à talon : le nombre demandé ne compte pas"

    r = c.post(reverse("quiz_create") + "?concours=1", {
        "title": "Écrit concours talon", "type_epreuve": "ecrit", "language": "fr",
        "note_sur": "20", "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
        "anonymat": "talon", "grading_mode": "immediate", "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors
    ecrit_c = Quiz.objects.get(title="Écrit concours talon")
    assert ecrit_c.talon_nom and ecrit_c.auto_enroll
    ecrit_c.format_impression = "a3"
    ecrit_c.save()
    for nom, prenom, num in ETUDIANTS[:2]:
        Student.objects.create(class_group=ecrit_c.class_group, last_name=nom,
                               first_name=prenom, student_number=num)
    texte = page(ecrit_c)
    assert 'name="nb"' not in texte and "2 copies</b>" in texte, "le champ est revenu"
    r = c.post(reverse("quiz_detail", args=[ecrit_c.pk]), {"generer": "1", "nb": "100"}, follow=True)
    texte = r.content.decode()
    assert nb_pages(ecrit_c) == 4, "A3 : 2 copies, recto et verso"
    assert ecrit_c.layout_json["nb_copies"] == 2 and ecrit_c.layout_json["nb_pages"] == 4
    assert "2 copies, soit 4 pages dans le PDF" in texte
    assert "colle la sienne" not in texte, "talon : le QR est imprimé, aucune étiquette à coller"

    # candidat ajouté (admis de l'étape précédente) : il n'a pas de copie, la page le dit
    assert "régénérez les documents" not in texte
    Student.objects.create(class_group=ecrit_c.class_group, last_name=ETUDIANTS[2][0],
                           first_name=ETUDIANTS[2][1], student_number=ETUDIANTS[2][2])
    texte = page(ecrit_c)
    assert "régénérez les documents" in texte and "3 copies</b>" in texte
    c.post(reverse("quiz_detail", args=[ecrit_c.pk]), {"generer": "1"})
    assert nb_pages(ecrit_c) == 6 and "régénérez les documents" not in page(ecrit_c)

    # ancien concours en « qr », sans talon : là, le nombre demandé est respecté
    anonymes = Quiz.objects.create(title="Fiches anonymes", class_group=ecrit_c.class_group,
                                   owner=prof, auto_enroll=True, id_mode="qr",
                                   type_epreuve="ecrit")
    assert 'name="nb"' in page(anonymes)
    r = c.post(reverse("quiz_detail", args=[anonymes.pk]), {"generer": "1", "nb": "3"}, follow=True)
    assert nb_pages(anonymes) == 3 * (1 + anonymes.pages_redaction)
    assert anonymes.layout_json["nb_copies"] == 3 and "colle la sienne" in r.content.decode()
    qcm_anonymes = Quiz.objects.create(title="QCM anonymes", class_group=ecrit_c.class_group,
                                       owner=prof, auto_enroll=True, id_mode="qr")
    Question.objects.create(quiz=qcm_anonymes, order=1, qtype="qcm", correct_choice=0)
    assert 'name="qr_count"' in page(qcm_anonymes)
    c.post(reverse("quiz_detail", args=[qcm_anonymes.pk]), {"generate_pdf": "1", "qr_count": "3"})
    assert nb_pages(qcm_anonymes) == 3

    # étiquette à coller : un seul exemplaire, à imprimer autant de fois qu'il le faut
    etiquette = Quiz.objects.create(title="Écrit étiquette", class_group=ecrit_c.class_group,
                                    owner=prof, auto_enroll=True, id_mode="sticker",
                                    type_epreuve="ecrit")
    assert 'name="nb"' not in page(etiquette) and "3 en tout" in page(etiquette)
    r = c.post(reverse("quiz_detail", args=[etiquette.pk]), {"generer": "1", "nb": "100"}, follow=True)
    assert nb_pages(etiquette) == 1 + etiquette.pages_redaction
    assert etiquette.layout_json["nb_copies"] == 1 and "colle la sienne" in r.content.decode()
    print("  nombre de copies : suit la liste, champ seulement s'il agit, ajout signalé OK")
    print("\n✅ TEST DES COPIES À TALON RÉUSSI")


if __name__ == "__main__":
    main()
