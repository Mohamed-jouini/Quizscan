"""Vitesse (demande du 5 novembre 2026) : ce qui a rendu la lecture 4 fois plus rapide et
les résultats d'un grand concours 2 fois plus rapides doit le rester, sans changer un
seul résultat.

  - une case se compte sur un petit carré, au pixel près comme sur toute la page ;
  - le code de page se lit sur la bande du haut, au pixel près comme sur la page ;
  - l'image JPEG d'un PDF de scanner se lit telle quelle ; toute autre page se dessine ;
  - les réponses d'une épreuve se lisent à partir de l'épreuve, sans tri par la base
    (le tri faisait parcourir à PostgreSQL les réponses de toutes les épreuves) ;
  - la mise à jour automatique attend la fin d'un lot en cours de lecture.
"""
import datetime
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

import cv2
import numpy as np
import pymupdf
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from grader import layout as L, omr, services
from grader.models import ClassGroup, Question, Quiz, ScanBatch, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, render_page, simulate_scan

REPONSES = {k: k % 4 for k in range(1, 13)}


def test_cases_et_bande():
    def ancien(binary, x, y, r):
        masque = np.zeros_like(binary)
        cv2.circle(masque, (x, y), r, 255, -1)
        return cv2.countNonZero(cv2.bitwise_and(binary, masque)), cv2.countNonZero(masque)

    rng = np.random.default_rng(11)
    binary = np.where(rng.random((900, 640)) < 0.4, 255, 0).astype(np.uint8)
    for x, y in [(0, 0), (639, 899), (-30, 50), (700, 20), (320, 450), (5, 898)] + [
            (int(rng.integers(-40, 680)), int(rng.integers(-40, 940))) for _ in range(200)]:
        for r in (3, 14, 23):
            assert omr._encre_disque(binary, x, y, r) == ancien(binary, x, y, r), (x, y, r)
    gris = rng.integers(0, 256, (2400, 1700), dtype=np.uint8)
    for haut in L.CADRES_HAUTS:
        src = np.array([[60, 70], [1640, 55], [1660, 2330], [40, 2350]], np.float32)
        dst = np.array([[0, 0], [omr.CANON_W, 0], [omr.CANON_W, omr.CANON_H],
                        [0, omr.CANON_H]], np.float32)
        H = cv2.getPerspectiveTransform(src, dst)
        page = cv2.warpPerspective(gris, H, (omr.CANON_W, omr.CANON_H), borderValue=255)
        bas = omr._bas_du_code(haut)
        bande = cv2.warpPerspective(gris, H, (omr.CANON_W, bas), borderValue=255)
        assert np.array_equal(bande, page[:bas])
        for case in L.code_cases(haut) + [L.case_variante(haut)]:
            assert omr._gris_moyen(bande, case) == omr._gris_moyen(page, case)
    print("  cases et code de page : mêmes nombres qu'en traitant la page entière OK")


def _pdf(images, forme="jpeg", texte=None, rotation=0):
    doc = pymupdf.open()
    for img in images:
        if forme == "jpeg":
            flux = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()
        else:
            flux = cv2.imencode(".png", img)[1].tobytes()
        page = doc.new_page(width=595, height=842)
        page.insert_image(page.rect, stream=flux)
        if texte:
            page.insert_text((40, 40), texte)
        if rotation:
            page.set_rotation(rotation)
    octets = doc.tobytes()
    doc.close()
    return octets


def test_pdf_du_scanner(prof):
    groupe = ClassGroup.objects.create(name="VITESSE", owner=prof)
    etudiants = [Student.objects.create(class_group=groupe, last_name=f"VITE{k}", first_name="A",
                                        student_number=str(880100 + k)) for k in range(3)]
    quiz = Quiz.objects.create(title="Vitesse", class_group=groupe, owner=prof,
                               sheet_mode="grid", id_mode="qr")
    for k in range(1, 13):
        Question.objects.create(quiz=quiz, order=k, qtype="qcm", correct_choice=k % 4)
    layout = L.build_layout(quiz)
    quiz.layout_json = layout
    quiz.save(update_fields=["layout_json"])
    pdf, _ = services.copies_nominatives_pdf(quiz, layout)
    n_pages = len(layout["pages"])
    copies = [simulate_scan(fill_sheet(render_page(pdf, k * n_pages), layout, REPONSES,
                                       name_text=("", ""))) for k in range(3)]

    doc = pymupdf.open(stream=_pdf(copies[:1]), filetype="pdf")
    directe = services._image_du_scanner(doc, doc[0])
    assert directe is not None and directe.shape[1] <= 1654 * 1.05, "JPEG non lu tel quel"
    for octets, quoi in ((_pdf(copies[:1], forme="png"), "image PNG"),
                         (_pdf(copies[:1], texte="Page 1"), "texte sur la page"),
                         (_pdf(copies[:1], rotation=90), "page tournée")):
        doc = pymupdf.open(stream=octets, filetype="pdf")
        assert services._image_du_scanner(doc, doc[0]) is None, f"{quoi} : à dessiner"
    print("  PDF du scanner : JPEG lu tel quel ; PNG, texte, rotation : page dessinée OK")

    lot = services.process_uploaded_files(quiz, [SimpleUploadedFile("paquet.pdf", _pdf(copies))])
    lues = {s.student_id: s for s in lot.sheets.all()}
    assert set(lues) == {e.pk for e in etudiants}, [s.status for s in lot.sheets.all()]
    for copie in lues.values():
        assert copie.status == "ok"
        lu = {a.question.order: a.detected_choice for a in copie.answers.select_related("question")}
        assert lu == REPONSES, lu
    print("  paquet PDF de 3 copies : chacune à son candidat, toutes les cases lues OK")

    # Plusieurs fils : exactement ce que lit un seul fil, dans le même ordre. Un paquet
    # mêlé : copies, page blanche, page sans repères, image illisible, copie en double.
    blanche = np.full_like(copies[0], 255)
    sans_reperes = np.full_like(copies[1], 255)       # de l'encre, mais ni cadre ni code
    h, w = sans_reperes.shape[:2]
    sans_reperes[h // 4:3 * h // 4, w // 4:3 * w // 4] = copies[1][h // 4:3 * h // 4,
                                                                  w // 4:3 * w // 4]
    paquet = [copies[2], blanche, copies[0], sans_reperes, copies[1], copies[2]]
    envois = lambda: [SimpleUploadedFile("melange.pdf", _pdf(paquet)),             # noqa: E731
                      SimpleUploadedFile("faux.jpg", b"pas une image"),
                      SimpleUploadedFile("seule.jpg", cv2.imencode(".jpg", copies[0])[1].tobytes())]
    lectures = {}
    for fils in ("1", "4"):
        os.environ["LECTURE_FILS"] = fils
        try:
            lot = services.process_uploaded_files(quiz, envois())
        finally:
            os.environ.pop("LECTURE_FILS", None)
        lectures[fils] = [
            (s.source_name, s.status, s.student_id, s.page_index, s.error_message, bool(s.image),
             sorted((a.question_id, a.detected_choice, a.is_multiple, tuple(a.fill_ratios or ()),
                     a.points_awarded) for a in s.answers.all()))
            for s in lot.sheets.order_by("pk")]
    assert lectures["1"] == lectures["4"], "4 fils ne lisent pas comme un seul"
    etats = [l[1] for l in lectures["4"]]
    assert etats[:6] == ["ok", "ignoree", "ok", "no_markers", "ok", "ok"], etats
    assert etats[6] == "error" and etats[7] == "ok", etats
    assert [l[0] for l in lectures["4"]][:2] == ["melange.pdf (page 1)", "melange.pdf (page 2)"]
    print("  4 fils de lecture : mêmes copies, mêmes cases, même ordre qu'un seul fil OK")
    return quiz


def test_requete_des_resultats(quiz):
    with CaptureQueriesContext(connection) as ctx:
        lignes = services.compute_results(quiz)
    assert len(lignes) == 3 and all(l["correct"] == 12 for l in lignes), lignes
    lecture = [q["sql"] for q in ctx.captured_queries
               if 'FROM "grader_answer"' in q["sql"] and '"detected_choice"' in q["sql"]]
    assert len(lecture) == 1, lecture
    sql = lecture[0]
    assert '"grader_scanbatch"."quiz_id" =' in sql and "ORDER BY" not in sql, sql
    ordres = [list(l["answers"]) for l in lignes]
    assert all(o == sorted(o) for o in ordres), "réponses hors de l'ordre des questions"
    print("  résultats : réponses lues à partir de l'épreuve, sans tri par la base OK")


def test_mise_a_jour(quiz):
    def compte():
        tampon = io.StringIO()
        call_command("lots_en_cours", stdout=tampon)
        return int(tampon.getvalue())

    avant = compte()
    en_cours = ScanBatch.objects.create(quiz=quiz, status="processing")
    assert compte() == avant + 1
    ScanBatch.objects.filter(pk=en_cours.pk).update(
        updated_at=timezone.now() - datetime.timedelta(seconds=services.STALLED_AFTER + 60))
    assert compte() == avant, "un lot arrêté ne doit pas retenir la mise à jour"
    ScanBatch.objects.create(quiz=quiz, status="done")
    assert compte() == avant
    with open("deploy/update.sh", encoding="utf-8") as fh:
        script = fh.read()
    assert script.index("lots_en_cours") < script.index("git reset --hard"), \
        "la mise à jour doit attendre avant de changer le code"
    assert "reportée" in script and "--force" in script
    with open("docker-entrypoint.sh", encoding="utf-8") as fh:
        assert "nproc" in fh.read(), "processus web selon les processeurs"
    print("  mise à jour : attend un lot qui avance, pas un lot arrêté ; "
          "processus web selon les processeurs OK")


def main():
    logging.disable(logging.WARNING)
    Quiz.objects.filter(owner__username="prof_vitesse").delete()
    ClassGroup.objects.filter(owner__username="prof_vitesse").delete()
    User.objects.filter(username="prof_vitesse").delete()
    prof = enseignant_complet(User.objects.create_user("prof_vitesse", password="x-pass-123"))
    print("Lecture des cases et du code de page :")
    test_cases_et_bande()
    print("PDF du scanner :")
    quiz = test_pdf_du_scanner(prof)
    print("Résultats :")
    test_requete_des_resultats(quiz)
    print("Mise à jour automatique :")
    test_mise_a_jour(quiz)
    Quiz.objects.filter(owner=prof).delete()
    ClassGroup.objects.filter(owner=prof).delete()
    prof.delete()
    print("\n✅ TEST DE VITESSE RÉUSSI")


if __name__ == "__main__":
    main()
