"""Tests de robustesse : une panne d'OCR, une fiche trop longue ou un
paramètre d'URL bricolé ne doivent jamais faire perdre une copie ni
renvoyer une erreur 500.

Ces tests n'ont pas besoin de Tesseract : l'OCR y est simulé.
"""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import pytesseract
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

import pymupdf

from grader import labels_pdf, layout as L
from grader import services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan

KEY = "ABCDAB"
# l'étudiant répond A B C D A D : 5 justes sur 6 (Q6 fausse)
REPONSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3}


def _quiz(nom, teacher, **kw):
    group = ClassGroup.objects.create(name=nom, owner=teacher)
    quiz = Quiz.objects.create(title=f"Robustesse {nom}", class_group=group,
                               owner=teacher, sheet_mode="grid", **kw)
    for i, letter in enumerate(KEY, start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                correct_choice=ord(letter) - ord("A"))
    return group, quiz


def test_panne_ocr(teacher):
    """Tesseract absent ou paquet de langue manquant : la copie est quand
    même lue et notée, elle reste simplement « à identifier »."""
    group, quiz = _quiz("ROB-OCR", teacher, id_mode="name")
    Student.objects.create(class_group=group, last_name="BEN SALAH",
                           first_name="Ahmed")
    cible = Student.objects.create(class_group=group, last_name="TRABELSI",
                                   first_name="Mariem")
    layout = L.build_layout(quiz)
    quiz.layout_json = layout
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    quiz.sheet_pdf.save("rob_ocr.pdf", ContentFile(pdf), save=True)
    img = simulate_scan(fill_sheet(render_page(pdf), layout, REPONSES))
    data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()

    vrai_ocr = pytesseract.image_to_string

    def rejoue(nom, faux_ocr):
        pytesseract.image_to_string = faux_ocr
        try:
            batch = services.process_uploaded_files(
                quiz, [SimpleUploadedFile(f"{nom}.jpg", data)], label=nom)
        finally:
            pytesseract.image_to_string = vrai_ocr
        sheet = batch.sheets.first()
        justes = sum(1 for a in sheet.answers.select_related("question")
                     if a.is_correct)
        # quoi qu'il arrive à l'OCR, les 6 cases sont lues et notées
        assert sheet.answers.count() == 6, sheet.answers.count()
        assert justes == 5, justes
        return sheet

    def absent(*a, **k):
        raise pytesseract.TesseractNotFoundError()

    sheet = rejoue("tesseract-absent", absent)
    assert sheet.status == "no_match" and sheet.student is None, sheet.status
    print("  Tesseract absent du serveur : copie lue (5/6), « à identifier » OK")

    def langue_absente(*a, **k):
        raise RuntimeError("Failed loading language 'ara'")

    sheet = rejoue("langue-absente", langue_absente)
    assert sheet.status == "no_match" and sheet.student is None, sheet.status
    print("  paquet de langue manquant : copie lue (5/6), « à identifier » OK")

    lu = iter(["TRABELSI", "Mariem"] * 4)
    sheet = rejoue("ocr-ok", lambda *a, **k: next(lu))
    assert sheet.status == "ok" and sheet.student_id == cible.pk, sheet.student
    assert sheet.ocr_name_raw == "TRABELSI Mariem", sheet.ocr_name_raw
    print("  OCR fonctionnel : identification par le nom manuscrit OK")


def test_fiche_longue(teacher):
    """Une fiche de plus de 12 pages doit se générer (limite portée à 62) ;
    au-delà, un message clair et non une erreur OpenCV brute."""
    group = ClassGroup.objects.create(name="ROB-PAGES", owner=teacher)
    quiz = Quiz.objects.create(title="Robustesse pages", class_group=group,
                               owner=teacher, sheet_mode="grid")
    for i in range(1, 41):
        Question.objects.create(quiz=quiz, order=i, qtype="open", points=1.0,
                                open_height_mm=120)
    layout = L.build_layout(quiz)
    assert len(layout["pages"]) > 12, len(layout["pages"])
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    assert pdf[:4] == b"%PDF"
    print(f"  fiche de {len(layout['pages'])} pages générée "
          f"(limite {L.MAX_SHEET_PAGES}) OK")

    for i in range(41, 81):
        Question.objects.create(quiz=quiz, order=i, qtype="open", points=1.0,
                                open_height_mm=120)
    try:
        L.build_layout(quiz)
    except L.TooManyPages as exc:
        assert "limite" in str(exc)
        print("  au-delà de la limite : message explicite à l'enseignant OK")
    else:
        raise AssertionError("TooManyPages attendue")


def test_etiquette_qr(teacher):
    """L'étiquette QR du candidat doit se lire après impression et scan.

    Régression : le décodeur d'OpenCV échouait sur certains QR pourtant
    parfaitement nets, et la copie partait en « non identifiée ». On vérifie
    ici plusieurs identifiants d'affilée, pas un seul — c'est la variété des
    motifs qui faisait apparaître la panne."""
    from test_e2e_web import stick_label

    group, quiz = _quiz("ROB-QR", teacher, id_mode="sticker")
    cands = [Student.objects.create(class_group=group, last_name=f"CAND{i}",
                                    first_name="X", student_number=f"20{i:03d}")
             for i in range(6)]
    layout = L.build_layout(quiz)
    quiz.layout_json = layout
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    quiz.sheet_pdf.save("rob_qr.pdf", ContentFile(pdf), save=True)
    page = layout["pages"][0]

    # Seul le QR est découpé et collé : l'emplacement réservé doit l'accueillir
    # avec de la tolérance, sinon il déborde sur les premières questions.
    _, _, zone_w, zone_h = page["qr"]["rect"]
    mini = L.STICKER_QR_MM + 2 * L.STICKER_QR_PAD
    assert zone_w >= mini and zone_h >= mini, (
        f"cadre réservé {zone_w:.1f}x{zone_h:.1f} mm trop petit pour un QR "
        f"découpé de {mini:.1f} mm")

    # Le QR est posé à la main : on le décale dans les quatre coins de la
    # tolérance du cadre, pas seulement au centre.
    marge = (zone_w - L.STICKER_QR_MM) / 2, (zone_h - L.STICKER_QR_MM) / 2
    decalages = [(0, 0), (-marge[0], -marge[1]), (marge[0], marge[1]),
                 (-marge[0], marge[1]), (marge[0], -marge[1]), (0, marge[1])]
    fichiers = []
    for i, (cand, (dx, dy)) in enumerate(zip(cands, decalages)):
        img = fill_sheet(render_page(pdf), layout, REPONSES, name_text=("", ""))
        img = simulate_scan(stick_label(img, page, cand, dx_mm=dx, dy_mm=dy))
        data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
        fichiers.append(SimpleUploadedFile(f"qr{i}.jpg", data))
    batch = services.process_uploaded_files(quiz, fichiers, label="etiquettes")

    lus = {sh.source_name: sh for sh in batch.sheets.all()}
    rates = [n for n, sh in lus.items() if sh.student is None]
    assert not rates, f"étiquettes non lues : {rates}"
    attendus = {c.pk for c in cands}
    assert {sh.student_id for sh in lus.values()} == attendus, "mauvais candidat"
    print(f"  {len(cands)} QR collés en tous points du cadre "
          f"({zone_w:.0f}x{zone_h:.0f} mm) : tous lus et affectés OK")

    # La planche doit permettre de savoir à qui remettre chaque QR : elle
    # porte le n° d'inscription et le nom à côté de chaque code.
    planche = labels_pdf.generate_labels_pdf(group, cands)
    doc = pymupdf.open(stream=planche, filetype="pdf")
    texte = "\n".join(p.get_text("text") for p in doc)
    nb_pages = doc.page_count
    doc.close()
    attendu_pages = -(-len(cands) // (L.STICKER_COLS * L.STICKER_ROWS))
    assert nb_pages == attendu_pages, (nb_pages, attendu_pages)
    for cand in cands:
        assert cand.student_number in texte, f"n° {cand.student_number} absent"
        assert cand.last_name.upper() in texte, f"nom {cand.last_name} absent"
    print(f"  planche : {len(cands)} vignettes « n° + nom + QR » sur "
          f"{nb_pages} page(s), {L.STICKER_COLS}x{L.STICKER_ROWS} par page OK")


def test_parametres_bricoles(teacher):
    """Un identifiant non numérique dans l'URL ou le formulaire doit donner
    une page normale ou une redirection, jamais une erreur 500."""
    group, quiz = _quiz("ROB-URL", teacher, id_mode="name")
    student = Student.objects.create(class_group=group, last_name="X",
                                     first_name="Y")
    c = Client()
    c.force_login(teacher)
    for url in (f"/quiz/{quiz.pk}/resultats/?batch=abc",
                f"/quiz/{quiz.pk}/resultats/?batch=9999999",
                f"/quiz/{quiz.pk}/resultats.xlsx?batch=abc",
                f"/quiz/{quiz.pk}/bulletins.pdf?student=abc",
                f"/classes/{group.pk}/?page=abc",
                f"/classes/{group.pk}/?page=-5",
                f"/classes/{group.pk}/?page=99999"):
        assert c.get(url).status_code == 200, url
    for champ, valeur in (("delete_student", "abc"),
                          ("delete_question", "abc"),
                          ("edit_question", "abc")):
        cible = (f"/classes/{group.pk}/" if champ == "delete_student"
                 else f"/quiz/{quiz.pk}/")
        r = c.post(cible, {champ: valeur})
        assert r.status_code in (302, 404), (champ, r.status_code)
    assert Student.objects.filter(pk=student.pk).exists(), \
        "un identifiant invalide ne doit rien supprimer"
    assert quiz.questions.count() == len(KEY)
    print("  identifiants d'URL et de formulaire invalides : aucune 500 OK")


def test_gros_lot(teacher):
    """« Nombre de copies illimité » (fiche technique) : un lot de 250 scans
    envoyés d'un coup doit être reçu en entier.

    Régression : Django refuse par défaut tout envoi de plus de 100 fichiers
    (DATA_UPLOAD_MAX_NUMBER_FILES) — le 101e scan faisait échouer tout le lot
    en erreur 400, alors que le formulaire promet « sans limite de nombre »."""
    from unittest import mock
    from django.conf import settings
    from django.test import Client
    from grader.models import ScanBatch
    group, quiz = _quiz("ROB-LOT", teacher, id_mode="sticker")
    quiz.layout_json = L.build_layout(quiz)
    quiz.save()
    client = Client()
    client.force_login(teacher)
    fichiers = [SimpleUploadedFile(f"scan{i}.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")
                for i in range(250)]
    # Seule la réception compte ici : la lecture des copies est remplacée.
    with mock.patch.object(services, "create_batch",
                           side_effect=lambda q, files, label="", **k:
                           ScanBatch.objects.create(quiz=q, label=label,
                                                    total_pages=len(files))), \
            mock.patch.object(services, "start_batch", create=True):
        r = client.post(f"/quiz/{quiz.pk}/upload/", {"label": "gros lot",
                                                     "files": fichiers})
    assert r.status_code == 302, f"lot de 250 scans refusé ({r.status_code})"
    lot = ScanBatch.objects.filter(quiz=quiz).latest("pk")
    assert lot.total_pages == 250, lot.total_pages
    # Les gros fichiers passent par le disque, pas par la mémoire.
    assert settings.FILE_UPLOAD_MAX_MEMORY_SIZE <= 10 * 1024 * 1024
    print("  250 scans envoyés d'un coup : tous reçus (plus de plafond à 100)")


def main():
    logging.disable(logging.WARNING)   # les pannes d'OCR simulées sont journalisées
    Quiz.objects.filter(owner__username="prof_rob").delete()
    ClassGroup.objects.filter(owner__username="prof_rob").delete()
    User.objects.filter(username="prof_rob").delete()
    teacher = User.objects.create_user("prof_rob", password="x-pass-123")
    teacher = enseignant_complet(teacher)

    print("Secours OCR :")
    test_panne_ocr(teacher)
    print("Fiches longues :")
    test_fiche_longue(teacher)
    print("Étiquettes QR :")
    test_etiquette_qr(teacher)
    print("Paramètres bricolés :")
    test_parametres_bricoles(teacher)
    print("Gros lot :")
    test_gros_lot(teacher)
    print("\n✅ TESTS DE ROBUSTESSE RÉUSSIS")


if __name__ == "__main__":
    main()
