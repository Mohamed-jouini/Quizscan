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
import cv2, numpy as np
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from grader import layout as L, services, sheet_pdf, qr as qrmod
from grader.models import ClassGroup, Question, Quiz, Student
from test_e2e import DPI, mm2px, simulate_scan
from test_e2e_concours import render, fill_answers, upload

def main():
    """Scénario : étiquette QR d'un candidat collée sur sa copie.

    Encapsulé dans main() — et non exécuté à l'import — pour qu'importer ce
    module (outils partagés) ne rejoue pas tout le test."""
    # concours + 1 candidat connu (importé via Excel)
    Quiz.objects.filter(owner__username="prof_lbl").delete()
    User.objects.filter(username="prof_lbl").delete()
    t = User.objects.create(username="prof_lbl")
    g = ClassGroup.objects.create(name="Concours Etiquettes", owner=t)
    q = Quiz.objects.create(title="Concours Bac", class_group=g, owner=t,
                            num_choices=4, sheet_mode="grid", id_mode="qr",
                            auto_enroll=True, language="fr")
    for i, letter in enumerate("ABCDAB", start=1):
        Question.objects.create(quiz=q, order=i, qtype="qcm", points=1.0,
                                correct_choice=ord(letter) - ord("A"))
    cand = Student.objects.create(class_group=g, last_name="KACEM",
                                  first_name="Salma", student_number="104523")
    layout = L.build_layout(q)
    q.layout_json = layout
    pdf = sheet_pdf.generate_sheet_pdf(q, layout, serials=[1])
    q.sheet_pdf.save("f.pdf", ContentFile(pdf), save=True)

    # on colle l'ETIQUETTE du candidat (son QR) dans la zone réservée
    img = render(pdf, 0)
    zx, zy, zw, zh = layout["pages"][0]["qr"]["rect"]
    qpng = qrmod.student_qr_png(cand)
    qarr = cv2.imdecode(np.frombuffer(qpng, np.uint8), cv2.IMREAD_COLOR)
    # agrandissement par facteur entier (voir test_e2e_web.stick_label)
    facteur = max(1, round(mm2px(L.STICKER_QR_MM) / qarr.shape[0]))
    qarr = cv2.resize(qarr, None, fx=facteur, fy=facteur,
                      interpolation=cv2.INTER_NEAREST)
    size = qarr.shape[0]
    px = mm2px(zx + (zw - L.STICKER_QR_MM) / 2)
    py = mm2px(zy + (zh - L.STICKER_QR_MM) / 2)
    img[py:py + size, px:px + size] = qarr

    img = fill_answers(img, layout, {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3})  # 5/6
    img = simulate_scan(img)
    batch = upload(q, img, "etiquette")
    sheet = batch.sheets.first()
    print("statut:", sheet.status, "| id lu:", sheet.id_read, "| candidat:", sheet.student)
    assert sheet.status == "ok"
    assert sheet.student is not None and sheet.student.pk == cand.pk
    assert sheet.student.last_name == "KACEM"
    r = services.compute_results(q, batch)[0]
    print("note:", r["score"], "/", r["max_score"])
    assert r["score"] == 5.0
    print("\n✅ ÉTIQUETTE CANDIDAT RECONNUE AU SCAN")


if __name__ == "__main__":
    main()
