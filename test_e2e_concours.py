"""Test du mode concours : aucun étudiant saisi à l'avance — les candidats
sont créés automatiquement au scan (QR de l'étiquette, ou grille de n°),
puis les noms sont complétés par import de la liste des candidats."""
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
import numpy as np
import pymupdf
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from grader import layout as L
from grader import services, sheet_pdf
from grader.models import ClassGroup, Question, Quiz, Student
from test_e2e import DPI, mm2px, simulate_scan

KEY = "ABCDAB"


def make_quiz(name, **kwargs):
    Quiz.objects.filter(class_group__name=name).delete()
    ClassGroup.objects.filter(name=name).delete()
    teacher, _ = User.objects.get_or_create(username="prof_conc")
    group = ClassGroup.objects.create(name=name, owner=teacher)
    quiz = Quiz.objects.create(title=f"Concours {name}", class_group=group,
                               num_choices=4, sheet_mode="grid",
                               auto_enroll=True, owner=teacher, **kwargs)
    for i, letter in enumerate(KEY, start=1):
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                correct_choice=ord(letter) - ord("A"))
    layout = L.build_layout(quiz)
    return group, quiz, layout, teacher


def render(pdf, idx=0):
    pix = pymupdf.open(stream=pdf, filetype="pdf")[idx].get_pixmap(
        dpi=DPI, colorspace=pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def stick_label(img, layout, quizpk, serial, page_idx=0):
    """Simule l'étiquette autocollante (n° + QR) apposée dans l'emplacement
    réservé du concours — c'est ce QR que le scan lit pour identifier."""
    import qrcode
    x, y, w, h = layout["pages"][page_idx]["qr"]["rect"]
    qr = qrcode.make(f"QSA|{quizpk}|{serial}").convert("RGB")
    arr = cv2.cvtColor(np.array(qr), cv2.COLOR_RGB2BGR)
    # Agrandissement par facteur ENTIER : au plus proche voisin avec un
    # rapport non entier, les modules sont dupliques irregulierement et
    # certains QR deviennent illisibles — artefact de la simulation, pas du
    # document imprime (voir test_e2e_web.stick_label).
    facteur = max(1, round(mm2px(L.STICKER_QR_MM) / arr.shape[0]))
    arr = cv2.resize(arr, None, fx=facteur, fy=facteur,
                     interpolation=cv2.INTER_NEAREST)
    size = arr.shape[0]
    px = mm2px(x + (w - L.STICKER_QR_MM) / 2)
    py = mm2px(y + (h - L.STICKER_QR_MM) / 2)
    img[py:py + size, px:px + size] = arr
    return img


def fill_answers(img, layout, answers):
    for item in layout["pages"][0]["qcm"]:
        ans = answers.get(item["order"])
        if ans is not None:
            cx, cy = item["bubbles"][ans]
            cv2.circle(img, (mm2px(cx), mm2px(cy)),
                       mm2px(L.BUBBLE_R * 0.9), (20, 20, 20), -1)
    return img


def upload(quiz, img, label):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])
    up = SimpleUploadedFile("scan.jpg", buf.tobytes(), content_type="image/jpeg")
    return services.process_uploaded_files(quiz, [up], label=label)


def main():
    """Scénario complet du mode concours.

    Encapsulé dans main() — et non exécuté à l'import — pour que
    test_e2e_labels.py puisse réutiliser les fonctions utilitaires
    (render, fill_answers, upload) sans rejouer tout ce test."""
    # ---------- variante 1 : fiches anonymes à QR code ----------
    group, quiz, layout, teacher = make_quiz("TEST-CONC-QR", id_mode="qr")
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout, serials=[1, 2, 3])
    quiz.layout_json = layout
    quiz.sheet_pdf.save("fiches_conc.pdf", ContentFile(pdf), save=True)
    assert group.students.count() == 0

    img = render(pdf, 1)                       # fiche anonyme n° 0002
    # Pas de nom manuscrit ni de QR imprimé : la fiche concours est identifiée par
    # l'étiquette autocollante (n° + QR) apposée dans l'emplacement réservé ; le
    # nom officiel est complété ensuite par import du fichier des candidats.
    img = stick_label(img, layout, quiz.pk, 2, page_idx=0)
    img = fill_answers(img, layout, {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3})  # 5 justes
    img = simulate_scan(img)
    batch = upload(quiz, img, "concours QR")
    sheet = batch.sheets.first()
    print("QR anonyme :", sheet.status, "|", sheet.id_read, "|", sheet.student,
          "| OCR:", sheet.ocr_name_raw)
    assert sheet.status == "ok" and sheet.student is not None
    assert sheet.student.student_number == "0002"
    r = services.compute_results(quiz, batch)[0]
    assert r["score"] == 5.0 and r["correct"] == 5 and r["wrong"] == 1, r
    print("candidat créé automatiquement, note", r["score"], "/", r["max_score"])

    # Image de contrôle : pas de cadres bleus « NOM / PRÉNOM ». La fiche de
    # concours n'imprime pas ces cases et rien n'y est lu ; l'image de
    # contrôle les dessinait quand même, et l'enseignant se demandait à quoi
    # servaient ces deux champs vides.
    controle = cv2.imdecode(np.frombuffer(sheet.overlay_image.read(), np.uint8),
                            cv2.IMREAD_COLOR)
    # (coordonnées à l'échelle de l'image redressée, pas du scan simulé)
    px = lambda v: int(round(v * L.PX_PER_MM))  # noqa: E731
    for x, y, w, h in layout["pages"][0]["name_boxes"].values():
        zone = controle[px(y) - 4:px(y + h) + 4, px(x) - 4:px(x + w) + 4]
        bleu = ((zone[:, :, 0] > 150) & (zone[:, :, 1] < 130)
                & (zone[:, :, 2] < 60)).sum()
        assert bleu < 50, f"cadre NOM/PRÉNOM dessiné sur l'image de contrôle ({bleu} px)"
    print("image de contrôle sans cadre NOM/PRÉNOM (non imprimé sur un concours)")

    # Copies scannées AVANT la correction : leur image de contrôle enregistrée
    # porte encore les cadres. Ouvrir la copie les efface, une fois, sans
    # toucher au reste de l'image.
    from grader import omr
    redressee = cv2.imdecode(np.frombuffer(sheet.warped_image.read(), np.uint8),
                             cv2.IMREAD_COLOR)
    ancienne = omr.draw_overlay(redressee, {"qcm": [], "name_boxes": L._name_boxes()},
                                {}, {}, zone_nom=True)
    temoin = (px(30), px(120), px(60), px(130))      # marque hors des cadres
    cv2.rectangle(ancienne, temoin[:2], temoin[2:], (0, 0, 230), -1)
    services._save_jpg(sheet.overlay_image, ancienne, "overlay.jpg", quality=70)
    sheet.save(update_fields=["overlay_image"])

    def bleus(image):
        total = 0
        for x, y, w, h in L._name_boxes().values():
            z = image[px(y) - 4:px(y + h) + 4, px(x) - 4:px(x + w) + 4]
            total += int(((z[:, :, 0] > 150) & (z[:, :, 1] < 130) & (z[:, :, 2] < 60)).sum())
        return total

    assert bleus(ancienne) > 1000, "simulation de l'ancienne image ratée"
    lecteur = Client()
    lecteur.force_login(teacher)
    assert lecteur.get(f"/copies/{sheet.pk}/").status_code == 200
    sheet.refresh_from_db()
    nette = cv2.imdecode(np.frombuffer(sheet.overlay_image.read(), np.uint8),
                         cv2.IMREAD_COLOR)
    assert bleus(nette) < 50, f"cadres NOM/PRÉNOM toujours là ({bleus(nette)} px)"
    zone = nette[temoin[1] + 3:temoin[3] - 3, temoin[0] + 3:temoin[2] - 3]
    assert zone[:, :, 2].mean() > 180 and zone[:, :, 0].mean() < 60, \
        "le nettoyage a touché au reste de l'image"
    assert not services.nettoyer_controle(sheet), "nettoyage refait une seconde fois"
    print("anciennes images de contrôle : cadres effacés à l'ouverture, reste intact")

    # import ultérieur des noms officiels par numéro
    teacher.set_password("x")
    teacher.save()
    c = Client()
    c.login(username="prof_conc", password="x")
    resp = c.post(f"/classes/{group.pk}/",
                  {"import": "1", "text": "KACEM OFFICIEL;Salma;0002"})
    assert resp.status_code == 302
    sheet.refresh_from_db()
    assert sheet.student.last_name == "KACEM OFFICIEL" or \
        group.students.get(student_number="0002").last_name == "KACEM OFFICIEL"
    print("nom mis à jour par numéro via l'import")

    # ---------- variante 2 : grille de n° d'inscription ----------
    group2, quiz2, layout2, _ = make_quiz("TEST-CONC-GRID", id_mode="grid",
                                          id_digits=4)
    pdf2 = sheet_pdf.generate_sheet_pdf(quiz2, layout2)
    quiz2.layout_json = layout2
    quiz2.sheet_pdf.save("fiche_conc_grid.pdf", ContentFile(pdf2), save=True)

    # Mode grille : plus de cases NOM/PRÉNOM, l'emplacement de l'étiquette
    # QR les remplace — le candidat n'écrit pas son nom, il noircit son n°.
    page2 = layout2["pages"][0]
    assert not page2["name_boxes"] and page2["qr"]["sticker"], page2.get("qr")
    img2 = render(pdf2, 0)
    grid = layout2["pages"][0]["id_grid"]
    for row, digit in zip(grid["rows"], "7315"):
        cx, cy = row[int(digit)]
        cv2.circle(img2, (mm2px(cx), mm2px(cy)), mm2px(grid["r"] * 0.9),
                   (20, 20, 20), -1)
    img2 = fill_answers(img2, layout2, {1: 0, 2: 1, 3: 1, 4: 3, 5: 0, 6: 1})  # 5 justes
    img2 = simulate_scan(img2)
    batch2 = upload(quiz2, img2, "concours grille")
    sheet2 = batch2.sheets.first()
    print("Grille :", sheet2.status, "|", sheet2.id_read, "|", sheet2.student,
          "| OCR:", sheet2.ocr_name_raw)
    assert sheet2.status == "ok" and sheet2.student is not None
    assert sheet2.student.student_number == "7315"
    # sans nom manuscrit à lire, le candidat est créé sous son numéro ; son
    # nom vient ensuite de l'import de la liste (comme pour la variante QR)
    assert sheet2.student.last_name == "CANDIDAT 7315", sheet2.student
    assert not sheet2.ocr_name_raw
    r2 = services.compute_results(quiz2, batch2)[0]
    assert r2["score"] == 5.0, r2
    print("candidat créé depuis la grille, note", r2["score"], "/", r2["max_score"])

    print("\n✅ TEST MODE CONCOURS RÉUSSI")


if __name__ == "__main__":
    main()
