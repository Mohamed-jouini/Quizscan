"""Mode grille : l'emplacement de l'étiquette QR remplace les cases NOM/PRÉNOM.

On vérifie :
1. la disposition — plus de cases nom/prénom, un cadre QR en haut à droite
   qui ne touche ni la grille de n°, ni les questions ;
2. la compatibilité des fiches déjà imprimées — grille et questions gardent
   exactement leur place : une fiche imprimée AVANT ce changement (avec les
   cases NOM/PRÉNOM) se lit toujours avec la disposition régénérée ;
3. la lecture — une copie avec étiquette QR collée (même de travers) est
   identifiée par le QR, une copie sans étiquette par la grille de n°.
"""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import logging  # noqa: E402
from unittest import mock  # noqa: E402

import cv2  # noqa: E402
import pymupdf  # noqa: E402
from django.contrib.auth.models import User  # noqa: E402
from django.core.files.uploadedfile import SimpleUploadedFile  # noqa: E402

from grader import layout as L  # noqa: E402
from grader import services, sheet_pdf  # noqa: E402
from grader.models import ClassGroup, Question, Quiz, Student  # noqa: E402
from test_e2e import fill_sheet, render_page, simulate_scan  # noqa: E402
from test_e2e_web import stick_label  # noqa: E402

REPONSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 1, 7: 2, 8: 3}


def _quiz(teacher, nom, **reglages):
    Quiz.objects.filter(class_group__name=nom).delete()
    ClassGroup.objects.filter(name=nom).delete()
    groupe = ClassGroup.objects.create(name=nom, owner=teacher)
    quiz = Quiz.objects.create(title=f"Grille QR {nom}", class_group=groupe,
                               owner=teacher, num_choices=4,
                               **{"sheet_mode": "grid", "id_mode": "grid",
                                  **reglages})
    for i, bonne in REPONSES.items():
        Question.objects.create(quiz=quiz, order=i, qtype="qcm", points=1.0,
                                text=f"Question {i}", choices=[],
                                correct_choice=bonne)
    return groupe, quiz


def _ancienne_disposition(quiz):
    """La disposition d'avant ce changement : cases NOM/PRÉNOM, pas de cadre."""
    origine = L._name_boxes
    with mock.patch.object(L, "_name_boxes", lambda id_mode="name": origine()), \
            mock.patch.object(L, "_add_qr_corner", lambda page, y: y):
        return L.build_layout(quiz)


def _chevauche(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _rects_occupes(page):
    """Rectangles de tout ce qui est imprimé à lire : cases, grille, zones."""
    rects = []
    grille = page.get("id_grid")
    if grille:
        r = grille["r"]
        rects += [[x - r, y - r, 2 * r, 2 * r]
                  for ligne in grille["rows"] for x, y in ligne]
        rects += grille["write_boxes"]
        rects.append([L.CONTENT_X0, grille["label_y"] - 3.0, 1.0, 3.5])
    for item in page["qcm"]:
        rects += [[x - L.BUBBLE_R, y - L.BUBBLE_R, 2 * L.BUBBLE_R, 2 * L.BUBBLE_R]
                  for x, y in item["bubbles"]]
    rects += [o["rect"] for o in page["open"]]
    return rects


def verifier_disposition(teacher):
    for mode in ("grid", "full"):
        for chiffres in (4, 6, 8, 10):
            _, quiz = _quiz(teacher, f"GQR-{mode}-{chiffres}", sheet_mode=mode,
                            id_digits=chiffres)
            neuve = L.build_layout(quiz)
            ancienne = _ancienne_disposition(quiz)
            for page, vieille in zip(neuve["pages"], ancienne["pages"]):
                assert not page["name_boxes"], "cases NOM/PRÉNOM encore prévues"
                cadre = page["qr"]["rect"]
                assert page["qr"]["sticker"]
                x, y, w, h = cadre
                assert x + w <= L.CONTENT_X1 + 1e-6 and x >= L.CONTENT_X0
                assert y >= L.NAME_BOXES_Y - 1e-6, "cadre sur l'en-tête"
                assert w >= L.STICKER_QR_MM + 2 * L.STICKER_QR_PAD
                assert h >= L.STICKER_QR_MM + 2 * L.STICKER_QR_PAD
                for rect in _rects_occupes(page):
                    assert not _chevauche(cadre, rect), (
                        f"{mode}/{chiffres} : le cadre QR {cadre} touche {rect}")
                # énoncés imprimés (mode complet) : sous le cadre et son libellé
                for t in page.get("texts", []):
                    assert t["y"] - 4.0 > y + h + 4.0, (
                        f"{mode}/{chiffres} : texte « {t['text'][:30]} » sous le cadre")
                # compatibilité : tout ce qui se lit est à la même place
                assert page["id_grid"] == vieille["id_grid"], "grille déplacée"
                assert page["qcm"] == vieille["qcm"], "questions déplacées"
                assert page["open"] == vieille["open"], "zones ouvertes déplacées"
                assert len(neuve["pages"]) == len(ancienne["pages"])
                assert services._has_name_zone(vieille, quiz)
                assert not services._has_name_zone(page, quiz)
    print("  disposition    cadre QR libre, grille et questions à leur place "
          "(modes grille et complet, 4 à 10 chiffres)")


def verifier_impression(teacher):
    for langue, attendu, absent in (("fr", "CODE QR", ("NOM", "PRÉNOM")),
                                    ("ar", "QR", ("اللقب",))):
        _, quiz = _quiz(teacher, f"GQR-PDF-{langue}", language=langue)
        layout = L.build_layout(quiz)
        pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
        texte = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
        mots = texte.split()
        for mot in absent:
            assert mot not in mots, f"« {mot} » encore imprimé ({langue})"
        assert attendu in texte, f"emplacement QR non signalé ({langue})"

    # Partout où un code QR identifie la copie, il est seul : plus de cases
    # NOM/PRÉNOM (étiquette, QR nominatif). Elles ne restent qu'en mode
    # « nom manuscrit », où elles sont le seul moyen d'identifier la copie.
    for mode, cases in (("sticker", False), ("qr", False), ("name", True)):
        groupe, quiz = _quiz(teacher, f"GQR-MODE-{mode}", id_mode=mode)
        eleve = Student.objects.create(class_group=groupe, last_name="DUPONT",
                                       first_name="Amel", student_number="1")
        layout = L.build_layout(quiz)
        assert bool(layout["pages"][0]["name_boxes"]) == cases, mode
        pdf = sheet_pdf.generate_sheet_pdf(
            quiz, layout, students=[eleve] if mode == "qr" else None)
        mots = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text().split()
        assert ("PRÉNOM" in mots) == cases, f"{mode} : NOM/PRÉNOM {mots[:12]}"
        # la consigne « IDENTIFICATION » est celle du coin du mode grille
        assert "IDENTIFICATION" not in mots, f"{mode} : consigne de la grille"
    print("  impression     plus de NOM / PRÉNOM, emplacement « CODE QR » (fr, ar) ; "
          "QR seul en modes étiquette et QR nominatif, cases en mode nom")


def verifier_lecture(teacher):
    groupe, quiz = _quiz(teacher, "GQR-LECTURE", id_digits=6)
    eleves = [Student.objects.create(class_group=groupe, last_name=f"ELEVE{i}",
                                     first_name="X", student_number=f"30{i:04d}")
              for i in range(5)]
    layout = L.build_layout(quiz)
    quiz.layout_json = layout
    quiz.save()
    page = layout["pages"][0]
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)

    # Fiche imprimée avant le changement, scannée après régénération.
    ancien_pdf = sheet_pdf.generate_sheet_pdf(quiz, _ancienne_disposition(quiz))

    marge = ((page["qr"]["rect"][2] - L.STICKER_QR_MM) / 2,
             (page["qr"]["rect"][3] - L.STICKER_QR_MM) / 2)
    copies = []
    # étiquette collée au centre et dans deux coins de la tolérance
    for eleve, (dx, dy) in zip(eleves[:3], [(0, 0), (-marge[0], -marge[1]),
                                            (marge[0], marge[1])]):
        img = fill_sheet(render_page(pdf), layout, REPONSES)
        copies.append((f"qr-{eleve.pk}.jpg",
                       stick_label(img, page, eleve, dx_mm=dx, dy_mm=dy),
                       eleve, "QR"))
    # pas d'étiquette : n° noirci dans la grille
    img = fill_sheet(render_page(pdf), layout, REPONSES,
                     student_number=eleves[3].student_number)
    copies.append(("grille.jpg", img, eleves[3], "grille"))
    # ancienne fiche (cases NOM/PRÉNOM) : la grille se lit toujours
    img = fill_sheet(render_page(ancien_pdf), _ancienne_disposition(quiz),
                     REPONSES, name_text=("", ""),
                     student_number=eleves[4].student_number)
    copies.append(("ancienne.jpg", img, eleves[4], "ancienne fiche"))

    fichiers = [SimpleUploadedFile(nom, cv2.imencode(
        ".jpg", simulate_scan(img), [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes())
        for nom, img, _, _ in copies]
    lot = services.process_uploaded_files(quiz, fichiers, label="grille qr")
    feuilles = {f.source_name: f for f in lot.sheets.all()}
    for nom, _, eleve, comment in copies:
        f = feuilles[nom]
        assert f.student_id == eleve.pk, (
            f"{nom} ({comment}) : {f.status} {f.id_read!r} -> {f.student}")
        if comment == "QR":
            assert f.id_read.startswith("QR"), f.id_read
        else:
            assert f.id_read == eleve.student_number, f.id_read
        assert not f.ocr_name_raw
        bonnes = sum(1 for a in f.answers.all()
                     if a.detected_choice == REPONSES[a.question.order])
        assert bonnes == len(REPONSES), f"{nom} : {bonnes} réponses lues"
    print("  lecture        3 étiquettes QR (centrée et de travers), 1 grille, "
          "1 ancienne fiche — toutes identifiées, réponses lues")


def _fiche_ancienne(quiz):
    """Fiche générée avant le cadre QR : PDF et disposition avec NOM/PRÉNOM."""
    from django.core.files.base import ContentFile
    disposition = _ancienne_disposition(quiz)
    disposition["signature"] = L.layout_signature(quiz)
    quiz.layout_json = disposition
    quiz.sheet_pdf.save(f"ancienne_{quiz.pk}.pdf", ContentFile(
        sheet_pdf.generate_sheet_pdf(quiz, disposition)), save=False)
    quiz.save()
    return disposition


def verifier_mise_a_jour(teacher):
    """Une fiche déjà générée ne changeait qu'au clic sur « Régénérer » :
    l'enseignant téléchargeait encore NOM/PRÉNOM. Elle se met maintenant à
    jour seule à l'ouverture de la page du quiz."""
    from django.test import Client
    from test_commun import enseignant_complet
    enseignant_complet(teacher)
    _, quiz = _quiz(teacher, "GQR-MAJ")
    ancienne = _fiche_ancienne(quiz)
    ancien_pdf = quiz.sheet_pdf.name
    assert services.fiche_grille_perimee(quiz)

    client = Client()
    client.force_login(teacher)
    page = client.get(f"/quiz/{quiz.pk}/").content.decode()
    assert "Fiche de réponses mise à jour" in page, "aucun message de mise à jour"
    quiz.refresh_from_db()
    assert quiz.sheet_pdf.name != ancien_pdf, "le PDF n'a pas été remplacé"
    texte = pymupdf.open(stream=quiz.sheet_pdf.read(), filetype="pdf")[0].get_text()
    assert "PRÉNOM" not in texte.split() and "CODE QR" in texte, texte[:300]
    for page_neuve, avant in zip(quiz.layout_json["pages"], ancienne["pages"]):
        assert not page_neuve["name_boxes"] and page_neuve["qr"]["sticker"]
        for cle in ("id_grid", "qcm", "open"):
            assert page_neuve[cle] == avant[cle], f"{cle} a bougé"
    assert quiz.layout_json["signature"] == L.layout_signature(quiz)
    # Une seule fois : la page suivante ne refait rien.
    assert "Fiche de réponses mise à jour" not in client.get(
        f"/quiz/{quiz.pk}/").content.decode()

    # Fiche modifiée depuis son impression (question ajoutée) : on ne touche
    # à rien, c'est l'alerte « fiche à régénérer » qui prend le relais.
    _, quiz2 = _quiz(teacher, "GQR-MAJ2")
    _fiche_ancienne(quiz2)
    Question.objects.create(quiz=quiz2, order=99, qtype="qcm", points=1.0,
                            correct_choice=0)
    nom = quiz2.sheet_pdf.name
    assert not services.moderniser_fiche_grille(quiz2)
    quiz2.refresh_from_db()
    assert quiz2.sheet_pdf.name == nom and quiz2.layout_json["pages"][0]["name_boxes"]

    # La commande lancée au démarrage du conteneur fait de même pour toutes —
    # y compris une fiche des premières versions, sans empreinte : c'était
    # le cas de « Contrôle n°1 », que la première version de cette mise à
    # jour laissait de côté.
    from io import StringIO
    from django.core.management import call_command
    _, quiz3 = _quiz(teacher, "GQR-MAJ3")
    _fiche_ancienne(quiz3)
    sans_empreinte = dict(quiz3.layout_json)
    del sans_empreinte["signature"]
    quiz3.layout_json = sans_empreinte
    quiz3.save()
    # … et une feuille du mode étiquette, qui portait aussi NOM/PRÉNOM.
    _, quiz4 = _quiz(teacher, "GQR-MAJ4", id_mode="sticker")
    ancienne4 = _fiche_ancienne(quiz4)
    sortie = StringIO()
    call_command("moderniser_fiches", stdout=sortie)
    quiz3.refresh_from_db()
    quiz4.refresh_from_db()
    assert not quiz3.layout_json["pages"][0]["name_boxes"], sortie.getvalue()
    page4 = quiz4.layout_json["pages"][0]
    assert not page4["name_boxes"], sortie.getvalue()
    for cle in ("qr", "qcm", "open"):
        assert page4[cle] == ancienne4["pages"][0][cle], f"étiquette : {cle} a bougé"
    print("  mise à jour    ancienne fiche remplacée à l'ouverture du quiz et par "
          "la commande ; fiche modifiée laissée intacte")


def verifier_trois_colonnes(teacher):
    """Feuille de réponses sur trois colonnes (demande de l'utilisateur, 7 novembre 2026) :
    plus de questions par page. Deux colonnes, aux places d'avant, quand trois ne tiennent
    pas (5 choix) ou quand des copies sont déjà scannées ; alerte quand une régénération
    déplace les cases d'une fiche déjà générée."""
    from django.test import Client
    from grader.models import ScanBatch
    from test_commun import enseignant_complet
    enseignant_complet(teacher)
    client = Client()
    client.force_login(teacher)

    def colonnes_vues(page):
        return len({item["bubbles"][0][0] for item in page["qcm"]})

    for langue in ("fr", "ar"):
        _, quiz = _quiz(teacher, f"GQR-3COL-{langue}", language=langue)
        Question.objects.bulk_create(
            Question(quiz=quiz, order=o, qtype="qcm", points=1.0, correct_choice=0)
            for o in range(9, 91))                                  # 90 questions
        trois = L.build_layout(quiz)
        with mock.patch.object(L, "_colonnes_grille", lambda quiz, bloc: 2):
            deux = L.build_layout(quiz)
        assert trois["colonnes"] == 3 and colonnes_vues(trois["pages"][0]) == 3
        n3, n2 = len(trois["pages"][0]["qcm"]), len(deux["pages"][0]["qcm"])
        assert n3 >= n2 * 3 // 2, (langue, n3, n2)
        assert sum(len(p["qcm"]) for p in trois["pages"]) == 90
        for page in trois["pages"]:
            for item in page["qcm"]:
                for x, _ in item["bubbles"]:
                    assert L.CONTENT_X0 <= x - L.BUBBLE_R and x + L.BUBBLE_R <= L.CONTENT_X1
        xs = sorted({x for item in trois["pages"][0]["qcm"] for x, _ in item["bubbles"]})
        ecarts = [b - a for a, b in zip(xs, xs[1:]) if b - a > L.BUBBLE_STEP + 0.01]
        assert len(ecarts) == 2 and min(ecarts) >= 20, ecarts    # place du n° de question
    print(f"  trois colonnes  {n3} questions en page 1 au lieu de {n2}, cases dans la page, "
          "place des numéros (fr, ar) OK")

    # 5 choix : trois colonnes ne tiennent pas, deux, aux places d'avant
    _, quiz5 = _quiz(teacher, "GQR-5CHOIX")
    quiz5.num_choices = 5
    quiz5.save(update_fields=["num_choices"])
    disposition = L.build_layout(quiz5)
    largeur = L.CONTENT_X1 - L.CONTENT_X0
    assert disposition["colonnes"] == 2
    assert {item["bubbles"][0][0] for item in disposition["pages"][0]["qcm"]} == {
        L.CONTENT_X0 + 14.0, round(L.CONTENT_X0 + largeur / 2 + 4.0 + 14.0, 2)}

    # Fiche générée en deux colonnes avant ce changement, copies déjà scannées : elle garde
    # ses deux colonnes ; sans copie scannée, la régénération passe à trois et le signale.
    for scannee in (True, False):
        _, quiz2 = _quiz(teacher, f"GQR-AVANT-{scannee}")
        with mock.patch.object(L, "_colonnes_grille", lambda quiz, bloc: 2):
            client.post(f"/quiz/{quiz2.pk}/", {"generate_pdf": "1"})
        quiz2.refresh_from_db()
        ancienne = dict(quiz2.layout_json)
        del ancienne["colonnes"]                     # les fiches d'avant ne le notaient pas
        quiz2.layout_json = ancienne
        quiz2.save(update_fields=["layout_json"])
        if scannee:
            ScanBatch.objects.create(quiz=quiz2, label="copies d'avant")
            refaite = L.build_layout(quiz2)
            assert refaite["colonnes"] == 2 and not services.zones_lues_changees(
                ancienne, refaite), "copies scannées : les cases ne doivent pas bouger"
        else:
            page = client.post(f"/quiz/{quiz2.pk}/", {"generate_pdf": "1"},
                               follow=True).content.decode()
            quiz2.refresh_from_db()
            assert quiz2.layout_json["colonnes"] == 3
            assert "Les cases de la feuille ont changé de place" in page
            page = client.post(f"/quiz/{quiz2.pk}/", {"generate_pdf": "1"},
                               follow=True).content.decode()
            assert "Les cases de la feuille ont changé de place" not in page
    print("  trois colonnes  5 choix : deux, aux places d'avant ; copies déjà scannées : "
          "fiche gardée ; régénération qui déplace les cases signalée OK")


def main():
    logging.disable(logging.WARNING)
    User.objects.filter(username="prof_gqr").delete()
    teacher = User.objects.create(username="prof_gqr")
    verifier_disposition(teacher)
    verifier_impression(teacher)
    verifier_lecture(teacher)
    verifier_mise_a_jour(teacher)
    verifier_trois_colonnes(teacher)
    print("\n✅ TEST GRILLE + ÉTIQUETTE QR RÉUSSI")


if __name__ == "__main__":
    main()
