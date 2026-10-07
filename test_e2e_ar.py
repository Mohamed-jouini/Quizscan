"""Test de bout en bout en ARABE : fiche RTL, nom écrit en arabe,
OCR arabe et rapprochement avec la liste de classe."""
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

import arabic_reshaper
import cv2
import numpy as np
from bidi.algorithm import get_display
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image, ImageDraw, ImageFont, features

from grader import layout as L
from grader import services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student
from test_e2e import DPI, mm2px, render_page, simulate_scan

FONT = "grader/fonts/NotoNaskhArabic-Regular.ttf"


def setup_data():
    Quiz.objects.filter(class_group__name="TEST-AR").delete()
    ClassGroup.objects.filter(name="TEST-AR").delete()
    group = ClassGroup.objects.create(name="TEST-AR")
    names = [("بن صالح", "أحمد"), ("الطرابلسي", "مريم"), ("الغربي", "يوسف"),
             ("الجلاصي", "فاطمة"), ("الخليفي", "عمر")]
    for ln, fn in names:
        Student.objects.create(class_group=group, last_name=ln, first_name=fn)
    quiz = Quiz.objects.create(title="فرض مراقبة عدد 1", class_group=group,
                               language="ar", num_choices=4, sheet_mode="full")
    for i, letter in enumerate("ABCDACBDAB", start=1):
        choices = ([f"الاقتراح رقم {k + 1} للسؤال {i}" for k in range(4)]
                   if i <= 5 else [])
        Question.objects.create(
            quiz=quiz, order=i, qtype="qcm", points=1.0,
            text=f"نص السؤال رقم {i}، اختر الإجابة الصحيحة من بين الاقتراحات التالية",
            choices=choices, correct_choice=ord(letter) - ord("A"))
    Question.objects.create(quiz=quiz, order=11, qtype="open", points=4.0,
                            text="اشرح عملية التمثيل الضوئي", open_height_mm=30)
    layout = L.build_layout(quiz)
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    quiz.layout_json = layout
    quiz.sheet_pdf.save("fiche_ar.pdf", ContentFile(pdf), save=True)
    return group, quiz, layout, pdf


def draw_arabic_centered(img_bgr, text, box_mm, size_px):
    """Écrit du texte arabe (ligaturé, RTL) centré verticalement dans une case."""
    x, y, w, h = box_mm
    pil = Image.fromarray(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    font = ImageFont.truetype(FONT, size_px)
    # PIL avec libraqm applique lui-même les ligatures et le sens RTL ;
    # sans libraqm (certaines versions de Pillow), on ligature et on inverse
    # le texte nous-mêmes — et il ne faut PAS passer direction="rtl", que
    # Pillow refuse alors avec une erreur.
    kwargs = {}
    if features.check("raqm"):
        kwargs["direction"] = "rtl"
    else:
        text = get_display(arabic_reshaper.reshape(text))
    cx = mm2px(x + w / 2)
    cy = mm2px(y + h / 2)
    d.text((cx, cy), text, font=font, fill=(25, 25, 25), anchor="mm", **kwargs)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def fill_sheet(img, layout, student_answers, last="بن صالح", first="أحمد",
               page_index=0):
    page = layout["pages"][page_index]
    for item in page["qcm"]:
        ans = student_answers.get(item["order"])
        if ans is None:
            continue
        cx, cy = item["bubbles"][ans]
        cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(L.BUBBLE_R * 0.9), (20, 20, 20), -1)
    nb = page["name_boxes"]
    # en arabe : اللقب dans la case de droite ("first"), الاسم dans celle de gauche
    for key, txt in (("first", last), ("last", first)):
        img = draw_arabic_centered(img, txt, nb[key], mm2px(5.5))
    return img


def main():
    group, quiz, layout, pdf = setup_data()
    n_pages = len(layout["pages"])
    print(f"fiche de {n_pages} page(s)")
    # أحمد : 9 réponses, 7 justes, 2 fausses (Q2, Q6), 1 vide (Q9)
    truth = {1: 0, 2: 3, 3: 2, 4: 3, 5: 0, 6: 0, 7: 1, 8: 3, 9: None, 10: 1}
    answers = {k: v for k, v in truth.items() if v is not None}

    uploads = []
    for p in range(n_pages):
        img = render_page(pdf, p)
        img = fill_sheet(img, layout, answers, page_index=p)
        img = simulate_scan(img)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
        uploads.append(SimpleUploadedFile(f"scan_ar_p{p + 1}.jpg", buf.tobytes(),
                                          content_type="image/jpeg"))
    batch = services.process_uploaded_files(quiz, uploads, label="test arabe")

    errors = []
    for sheet in batch.sheets.all():
        print(f"page {sheet.page_index + 1}: statut={sheet.status} "
              f"OCR={sheet.ocr_name_raw!r} score={round(sheet.match_score, 1)} "
              f"étudiant={sheet.student}")
        assert sheet.status == "ok", sheet.error_message
        assert sheet.student and sheet.student.last_name == "بن صالح", \
            "mauvaise identification"
        for a in sheet.answers.select_related("question"):
            if a.question.qtype != "qcm":
                continue
            if a.detected_choice != truth[a.question.order] or a.is_multiple:
                errors.append((a.question.order, truth[a.question.order],
                               a.detected_choice, a.is_multiple))
    print("erreurs de lecture:", errors)
    assert not errors, errors

    r = services.compute_results(quiz, batch)[0]
    print(f"note: {r['score']}/{r['max_score']}  répondues={r['answered']} "
          f"correctes={r['correct']} fausses={r['wrong']} vides={r['blank']}")
    assert r["score"] == 7.0 and r["correct"] == 7 and r["wrong"] == 2 and r["blank"] == 1, r
    print("\n✅ TEST ARABE RÉUSSI")


if __name__ == "__main__":
    main()
