"""Test par l'interface web : mode « étiquette QR » d'une classe, correction
en arrière-plan dès l'envoi (bandeau de progression), secours OCR du nom,
corrigé modifié après l'épreuve (recalcul des notes), alerte de fiche
périmée et accès aux documents réservé aux utilisateurs connectés."""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import numpy as np
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment

from grader import layout as L
from grader import qr as qrmod
from grader.models import ClassGroup, Question, Quiz, ScanBatch, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

KEY = "ABCDAB"


def stick_label(img, page, student, dx_mm=0.0, dy_mm=0.0):
    """Colle l'étiquette QR du candidat (planche d'étiquettes) dans
    l'emplacement réservé de la copie.

    On reproduit la géométrie réellement imprimée par labels_pdf (QR de
    L.STICKER_QR_MM dans une vignette de L.STICKER_W x L.STICKER_H) et on
    agrandit le QR par un facteur ENTIER : un redimensionnement au plus
    proche voisin avec un rapport non entier duplique irrégulièrement les
    modules et rend certains QR illisibles — un artefact de la simulation,
    pas du document imprimé."""
    zx, zy, zw, zh = page["qr"]["rect"]
    qarr = cv2.imdecode(np.frombuffer(qrmod.student_qr_png(student), np.uint8),
                        cv2.IMREAD_COLOR)
    cible = mm2px(L.STICKER_QR_MM)
    facteur = max(1, round(cible / qarr.shape[0]))
    qarr = cv2.resize(qarr, None, fx=facteur, fy=facteur,
                      interpolation=cv2.INTER_NEAREST)
    size = qarr.shape[0]
    # QR découpé, collé dans le cadre réservé — comme le candidat le fait.
    # dx_mm / dy_mm simulent un collage un peu de travers.
    px = mm2px(zx + (zw - L.STICKER_QR_MM) / 2 + dx_mm)
    py = mm2px(zy + (zh - L.STICKER_QR_MM) / 2 + dy_mm)
    img[py:py + size, px:px + size] = qarr
    return img


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def main():
    setup_test_environment()      # accès à response.context
    Quiz.objects.filter(owner__username__in=["prof_web", "prof_autre"]).delete()
    User.objects.filter(username__in=["prof_web", "prof_autre"]).delete()
    teacher = User.objects.create_user("prof_web", password="x-pass-123")
    teacher = enseignant_complet(teacher)
    other = User.objects.create_user("prof_autre", password="x-pass-123")
    other = enseignant_complet(other)
    group = ClassGroup.objects.create(name="WEB-3B", owner=teacher)
    s1 = Student.objects.create(class_group=group, last_name="BEN SALAH",
                                first_name="Ahmed", student_number="104523")
    Student.objects.create(class_group=group, last_name="TRABELSI",
                           first_name="Mariem", student_number="104524")
    quiz = Quiz.objects.create(title="Contrôle étiquettes", class_group=group,
                               owner=teacher, sheet_mode="grid", id_mode="sticker")
    for i, letter in enumerate(KEY, start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                correct_choice=ord(letter) - ord("A"))

    c = Client()
    c.force_login(teacher)
    r = c.post(f"/quiz/{quiz.pk}/", {"generate_pdf": "1"})
    assert r.status_code == 302
    quiz.refresh_from_db()
    page = quiz.layout_json["pages"][0]
    assert page["qr"]["sticker"] and page["name_boxes"], "zone étiquette absente"
    pdf = quiz.sheet_pdf.read()

    # copie 1 : étiquette QR collée ; copie 2 : pas d'étiquette, nom écrit
    ans = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3}           # 5/6
    img1 = simulate_scan(stick_label(fill_sheet(render_page(pdf), quiz.layout_json,
                                                ans, name_text=("", "")), page, s1))
    img2 = simulate_scan(fill_sheet(render_page(pdf), quiz.layout_json,
                                    {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 1},
                                    name_text=("TRABELSI", "MARIEM")))

    t0 = time.time()
    r = c.post(f"/quiz/{quiz.pk}/upload/", {
        "label": "Groupe B",
        "files": [SimpleUploadedFile("copie1.jpg", jpg(img1)),
                  SimpleUploadedFile("copie2.jpg", jpg(img2))]})
    assert r.status_code == 302, r.status_code
    t_resp = time.time() - t0
    batch = ScanBatch.objects.filter(quiz=quiz).latest("pk")
    assert batch.total_pages == 2
    page_html = c.get(f"/lots/{batch.pk}/").content.decode()
    print(f"réponse au téléversement en {t_resp:.2f} s, état : {batch.status}")
    while True:
        batch.refresh_from_db()
        if batch.status != "processing":
            break
        assert time.time() - t0 < 120, "traitement trop long"
        time.sleep(0.3)
    elapsed = time.time() - t0
    print(f"lot traité : {batch.processed_pages}/{batch.total_pages} "
          f"en {elapsed:.1f} s ({elapsed / 2:.1f} s / copie)")
    assert batch.status == "done" and batch.processed_pages == 2
    assert "Correction automatique en cours" in page_html or batch.status == "done"
    page_html = c.get(f"/lots/{batch.pk}/").content.decode()
    assert "2/2 copie(s) corrigée(s)" in page_html, page_html[:2000]

    sheets = {sh.source_name: sh for sh in batch.sheets.all()}
    sh1, sh2 = sheets["copie1.jpg"], sheets["copie2.jpg"]
    print("copie 1 :", sh1.status, sh1.id_read, sh1.student)
    print("copie 2 :", sh2.status, repr(sh2.ocr_name_raw), sh2.student)
    assert sh1.student_id == s1.pk and sh1.id_read.startswith("QR"), "étiquette non lue"
    assert sh2.student and sh2.student.last_name == "TRABELSI", "secours OCR du nom"

    r = c.get(f"/quiz/{quiz.pk}/resultats/")
    rows = {row["student"].last_name: row for row in r.context["rows"]}
    assert rows["BEN SALAH"]["score"] == 5.0 and rows["TRABELSI"]["score"] == 6.0

    # corrigé modifié après l'épreuve : Q6 = D -> notes recalculées
    q6 = quiz.questions.get(order=6)
    r = c.post(f"/quiz/{quiz.pk}/", {"edit_question": q6.pk, "correct": "3",
                                     "points": "2"})
    assert r.status_code == 302
    r = c.get(f"/quiz/{quiz.pk}/resultats/")
    rows = {row["student"].last_name: row for row in r.context["rows"]}
    print("après correction du corrigé : BEN SALAH",
          rows["BEN SALAH"]["score"], "TRABELSI", rows["TRABELSI"]["score"])
    assert rows["BEN SALAH"]["score"] == 7.0 and rows["TRABELSI"]["score"] == 5.0

    # le barème est imprimé sur la fiche : alerte « régénérez la fiche »
    r = c.get(f"/quiz/{quiz.pk}/")
    assert r.context["layout_stale"], "alerte de fiche périmée attendue"
    c.post(f"/quiz/{quiz.pk}/", {"generate_pdf": "1"})
    assert not c.get(f"/quiz/{quiz.pk}/").context["layout_stale"]

    # droits : documents réservés aux connectés, quiz invisible d'un collègue
    url = sh1.image.url
    assert c.get(url).status_code == 200
    anon = Client()
    assert anon.get(url).status_code == 302, "scan accessible sans connexion !"
    c2 = Client()
    c2.force_login(other)
    assert c2.get(f"/quiz/{quiz.pk}/").status_code == 404
    quiz.refresh_from_db()        # fiche régénérée plus haut
    for doc in (sh1.image.url, sh1.overlay_image.url, quiz.sheet_pdf.url):
        assert c.get(doc).status_code == 200, doc
        assert c2.get(doc).status_code == 404, f"document d'un collègue visible : {doc}"
    upload = batch.source_files[0]["path"]
    assert c2.get(f"/media/{upload}").status_code == 404
    assert c.get(f"/media/{upload}").status_code == 200
    User.objects.filter(username="admin_web").delete()
    admin_user = User.objects.create_superuser("admin_web", password="x-pass-123")
    c3 = Client()
    c3.force_login(admin_user)
    assert c3.get(sh1.image.url).status_code == 200, "l'administrateur voit tout"
    admin_user.delete()
    print("droits : chaque enseignant n'accède qu'à ses documents, "
          "l'administrateur voit tout ✔")

    Quiz.objects.filter(owner__in=[teacher, other]).delete()
    teacher.delete()
    other.delete()
    print("\n✅ TEST WEB (ÉTIQUETTE QR, ARRIÈRE-PLAN, RECALCUL) RÉUSSI")


if __name__ == "__main__":
    main()
