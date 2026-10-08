"""Test de bout en bout : génère la fiche, simule une copie remplie et
scannée (rotation + bruit), la traite et vérifie la notation."""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import random

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import numpy as np
import pymupdf
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from grader import layout as L
from grader import services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student

random.seed(7)
DPI = 300
PXMM = DPI / 25.4


def mm2px(v):
    return int(round(v * PXMM))


def setup_data():
    from django.contrib.auth.models import User
    teacher, _ = User.objects.get_or_create(username="prof_test")
    Quiz.objects.filter(class_group__name="TEST-3A").delete()
    ClassGroup.objects.filter(name="TEST-3A").delete()
    group = ClassGroup.objects.create(name="TEST-3A", owner=teacher)
    names = [("BEN SALAH", "Ahmed", "104523"), ("TRABELSI", "Mariem", "104524"),
             ("GHARBI", "Youssef", "104525"), ("JLASSI", "Fatma", "104612"),
             ("KHELIFI", "Omar", "104613")]
    for ln, fn, num in names:
        Student.objects.create(class_group=group, last_name=ln, first_name=fn,
                               student_number=num)
    quiz = Quiz.objects.create(title="Test contrôle", class_group=group,
                               num_choices=4, wrong_penalty=0.0,
                               sheet_mode="full", id_mode="grid", id_digits=6,
                               owner=teacher)
    key = "ABCDACBDAB"  # 10 QCM à 1 pt
    for i, letter in enumerate(key, start=1):
        # moitié avec textes de choix imprimés, moitié en simple rangée de cases
        choices = ([f"Proposition {c} de la question {i}" for c in "ABCD"]
                   if i <= 5 else [])
        Question.objects.create(
            quiz=quiz, order=i, qtype="qcm", points=1.0,
            text=f"Énoncé de la question numéro {i}, choisissez la bonne réponse",
            choices=choices, correct_choice=ord(letter) - ord("A"))
    Question.objects.create(quiz=quiz, order=11, qtype="open", points=4.0,
                            text="Expliquez la photosynthèse", open_height_mm=30)
    Question.objects.create(quiz=quiz, order=12, qtype="open", points=4.0,
                            open_height_mm=25)
    layout = L.build_layout(quiz)
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    quiz.layout_json = layout
    quiz.sheet_pdf.save("fiche_test.pdf", ContentFile(pdf), save=True)
    return group, quiz, layout, pdf


def render_page(pdf_bytes, page_idx=0):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    pix = doc[page_idx].get_pixmap(dpi=DPI, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def fill_sheet(img, layout, student_answers, name_text=("BEN SALAH", "AHMED"),
               page_index=0, student_number=""):
    page = layout["pages"][page_index]
    # grille du numéro d'inscription
    grid = page.get("id_grid")
    if grid and student_number:
        for row, digit in zip(grid["rows"], student_number):
            cx, cy = row[int(digit)]
            cv2.circle(img, (mm2px(cx), mm2px(cy)),
                       mm2px(grid["r"] * 0.9), (20, 20, 20), -1)
    # noircir les cases choisies
    for item in page["qcm"]:
        ans = student_answers.get(item["order"])
        if ans is None:
            continue
        cx, cy = item["bubbles"][ans]
        cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(L.BUBBLE_R * 0.9), (20, 20, 20), -1)
    # écrire le nom (simulé en texte imprimé pour le test)
    # (mode grille : pas de cases NOM/PRÉNOM, l'étiquette QR les remplace)
    nb = page["name_boxes"]
    for key, txt in zip(("last", "first"), name_text if nb else ()):
        x, y, w, h = nb[key]
        cv2.putText(img, txt, (mm2px(x + 4), mm2px(y + h - 3.5)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.4, (30, 30, 30), 3, cv2.LINE_AA)
    # gribouillis dans la zone manuscrite
    for item in page["open"]:
        x, y, w, h = item["rect"]
        pts = np.array([[mm2px(x + 10 + i * 3), mm2px(y + h / 2 + 4 * np.sin(i))]
                        for i in range(30)], dtype=np.int32)
        cv2.polylines(img, [pts], False, (40, 40, 40), 2)
    return img


def simulate_scan(img):
    """Rotation légère + translation + bruit, comme un vrai scanner."""
    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), 1.6, 0.985)
    M[0, 2] += 14
    M[1, 2] += -9
    img = cv2.warpAffine(img, M, (w, h), borderValue=(255, 255, 255))
    noise = np.random.default_rng(3).normal(0, 6, img.shape).astype(np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return img


def main():
    group, quiz, layout, pdf = setup_data()
    n_pages = len(layout["pages"])
    print(f"fiche de {n_pages} page(s)")
    # Ahmed répond : 8 justes, 1 faux (Q3), 1 vide (Q10)
    truth = {1: 0, 2: 1, 3: 3, 4: 3, 5: 0, 6: 2, 7: 1, 8: 3, 9: 0, 10: None}
    answers = {k: v for k, v in truth.items() if v is not None}

    uploads = []
    for p in range(n_pages):
        img = render_page(pdf, p)
        img = fill_sheet(img, layout, answers, page_index=p,
                         student_number="104523")
        img = simulate_scan(img)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        uploads.append(SimpleUploadedFile(f"scan_p{p + 1}.jpg", buf.tobytes(),
                                          content_type="image/jpeg"))
    batch = services.process_uploaded_files(quiz, uploads, label="test e2e")

    errors = []
    for sheet in batch.sheets.all():
        print(f"page {sheet.page_index + 1}: statut={sheet.status} "
              f"id_lu={sheet.id_read!r} étudiant={sheet.student} "
              f"(confiance {sheet.match_score:g})")
        assert sheet.status == "ok", sheet.error_message
        assert sheet.id_read == "104523", "grille du n° mal lue"
        assert sheet.match_score == 100.0
        assert sheet.student and sheet.student.last_name == "BEN SALAH", \
            "mauvaise identification"
        for a in sheet.answers.select_related("question"):
            if a.question.qtype != "qcm":
                continue
            expected = truth[a.question.order]
            if a.detected_choice != expected or a.is_multiple:
                errors.append((a.question.order, expected,
                               a.detected_choice, a.is_multiple))
    print("erreurs de lecture:", errors)
    assert not errors, errors

    rows = services.compute_results(quiz, batch)
    r = rows[0]
    print(f"note: {r['score']}/{r['max_score']}  répondues={r['answered']} "
          f"correctes={r['correct']} fausses={r['wrong']} vides={r['blank']} "
          f"manuscrites en attente={r['pending_open']}")
    assert r["score"] == 8.0, r
    assert r["correct"] == 8 and r["wrong"] == 1 and r["blank"] == 1, r
    assert r["answered"] == 9 and r["pending_open"] == 2, r
    print("\n✅ TEST DE BOUT EN BOUT RÉUSSI")


if __name__ == "__main__":
    main()
