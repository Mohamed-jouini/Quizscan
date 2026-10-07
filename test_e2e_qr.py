"""Test de bout en bout du mode QR : fiches nominatives pré-imprimées,
identification par QR code sans que l'étudiant n'écrive rien."""
import os
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (data/test_db.sqlite3)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import cv2
import pymupdf
import numpy as np
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile

from grader import layout as L
from grader import services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student
from test_e2e import DPI, mm2px, simulate_scan


def setup_data():
    Quiz.objects.filter(class_group__name="TEST-QR").delete()
    ClassGroup.objects.filter(name="TEST-QR").delete()
    group = ClassGroup.objects.create(name="TEST-QR")
    for ln, fn, num in [("BEN AMOR", "Sami", "2201"), ("MEJRI", "Rania", "2202"),
                        ("SASSI", "Karim", "2203")]:
        Student.objects.create(class_group=group, last_name=ln, first_name=fn,
                               student_number=num)
    quiz = Quiz.objects.create(title="Test QR", class_group=group,
                               num_choices=4, sheet_mode="grid", id_mode="qr")
    for i, letter in enumerate("ABCDAB", start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                correct_choice=ord(letter) - ord("A"))
    layout = L.build_layout(quiz)
    students = list(group.students.all())
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout, students=students)
    quiz.layout_json = layout
    quiz.sheet_pdf.save("fiches_qr.pdf", ContentFile(pdf), save=True)
    return quiz, layout, pdf, students


def main():
    quiz, layout, pdf, students = setup_data()
    n_pages = len(layout["pages"])
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    assert len(doc) == n_pages * len(students), "nombre de fiches incorrect"
    print(f"{len(students)} fiches nominatives de {n_pages} page(s)")

    # copie de l'étudiante n°2 (MEJRI Rania) : 5 justes, 1 faux (Q4)
    target = students[1]
    truth = {1: 0, 2: 1, 3: 2, 4: 0, 5: 0, 6: 1}
    pix = doc[1 * n_pages + 0].get_pixmap(dpi=DPI, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    page = layout["pages"][0]
    for item in page["qcm"]:
        ans = truth.get(item["order"])
        if ans is None:
            continue
        cx, cy = item["bubbles"][ans]
        cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(L.BUBBLE_R * 0.9),
                   (20, 20, 20), -1)
    img = simulate_scan(img)

    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    up = SimpleUploadedFile("scan_qr.jpg", buf.tobytes(), content_type="image/jpeg")
    batch = services.process_uploaded_files(quiz, [up], label="test qr")

    sheet = batch.sheets.first()
    print("statut:", sheet.status, "| id lu:", sheet.id_read,
          "| étudiant:", sheet.student)
    assert sheet.status == "ok", sheet.error_message
    assert sheet.student == target, f"attendu {target}, obtenu {sheet.student}"
    assert sheet.match_score == 100.0

    errors = [(a.question.order, a.detected_choice)
              for a in sheet.answers.select_related("question")
              if a.detected_choice != truth[a.question.order]]
    assert not errors, errors
    r = services.compute_results(quiz, batch)[0]
    print(f"note: {r['score']}/{r['max_score']} correctes={r['correct']} fausses={r['wrong']}")
    assert r["score"] == 5.0 and r["correct"] == 5 and r["wrong"] == 1
    print("\n✅ TEST QR RÉUSSI")


if __name__ == "__main__":
    main()
