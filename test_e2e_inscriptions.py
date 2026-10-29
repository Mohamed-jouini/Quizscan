"""Registre des inscriptions, vérification des listes, ajout direct aux
concours et classes, et espace public (résultats par n°, communiqués).
"""
import io
import os
import sys

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
import numpy as np
import openpyxl
import pymupdf
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse
from django.utils.html import escape

from grader import inscriptions, layout as L, services, sheet_pdf
from grader.models import ClassGroup, Communique, Inscrit, Question, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

KEY = "ABCDAB"
REPONSES = {1: 0, 2: 1, 3: 2, 4: 3, 5: 0, 6: 3}       # 5 / 6


def _xlsx(lignes):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Nom", "Prénom", "N° d'inscription"])
    for l in lignes:
        ws.append(list(l))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _decode_png(data):
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    img = cv2.copyMakeBorder(img, 20, 20, 20, 20, cv2.BORDER_CONSTANT,
                             value=(255, 255, 255))
    texte, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
    return texte


def _messages(r):
    return " ".join(str(m) for m in r.context["messages"]) if r.context else ""


def test_registre(c, prof):
    r = c.post(reverse("inscriptions"), {"inscrire": "1", "nom": " ben  salem ",
                                         "prenom": "Samia", "numero": "104 523"},
               follow=True)
    i = Inscrit.objects.get(numero="104523")
    assert (i.nom, i.prenom, i.cree_par_id) == ("BEN SALEM", "Samia", prof.pk)
    assert "inscrit(e) sous le n° 104523" in r.content.decode()
    r = c.post(reverse("inscriptions"), {"inscrire": "1", "nom": "AUTRE",
                                         "prenom": "Nom", "numero": "104523"})
    assert r.status_code == 200 and "déjà attribué" in r.content.decode()
    assert Inscrit.objects.filter(numero="104523").count() == 1
    print("  inscription (n° nettoyé, NOM en capitales, n° unique) OK")

    r = c.get(reverse("inscrit_qr", args=[i.pk]))
    assert r["Content-Type"] == "image/png"
    assert _decode_png(r.content) == "QSI|104523|BEN SALEM|Samia", _decode_png(r.content)
    r = c.get(reverse("inscriptions_cartes") + f"?choix={i.pk}")
    assert r["Content-Type"] == "application/pdf"
    texte = pymupdf.open(stream=r.content, filetype="pdf")[0].get_text()
    assert "104523" in texte and "BEN SALEM" in texte and "Samia" in texte
    print("  QR de la carte = n°, NOM, prénom ; carte d'inscription PDF OK")

    r = c.post(reverse("inscriptions"), {"coller": "1", "text":
               "TRABELSI;Mariem;104524\nBEN SALEM;Samia;104523\n"
               "KHELIFI;Omar;104523\nSANS;Numero"}, follow=True)
    page = r.content.decode()
    assert Inscrit.objects.filter(numero="104524", nom="TRABELSI").exists()
    assert Inscrit.objects.get(numero="104523").nom == "BEN SALEM"   # pas écrasé
    assert "Bilan de l'import" in page and "N° déjà attribué" in page
    assert "Sans n° d'inscription" in page
    r = c.post(reverse("inscriptions"), {"importer": "1", "file": SimpleUploadedFile(
        "registre.xlsx", _xlsx([("ABDELLATIF", "Mohamed", 104525),
                                ("GHARBI", "Ines", 104526)]))}, follow=True)
    assert Inscrit.objects.filter(numero__in=["104525", "104526"]).count() == 2
    print("  inscription par liste et par Excel : conflits et n° manquants signalés OK")

    r = c.get(reverse("inscriptions") + "?q=salem")
    assert r.context["n_trouves"] == 1 and "104523" in r.content.decode()
    print("  recherche dans le registre OK")


def test_verification_import(c, prof):
    groupe = ClassGroup.objects.create(name="INSC-CONCOURS", owner=prof)
    lignes = [("ben salem", "samia", "104523"),        # conforme (casse)
              ("TRABELSY", "Mariem", "104524"),        # nom différent
              ("NOUVEAU", "Ali", "999999"),            # non inscrit
              ("SANSNUM", "Zied", "")]                 # sans n°
    r = c.post(reverse("class_detail", args=[groupe.pk]), {
        "import_xlsx": "1", "file": SimpleUploadedFile("cand.xlsx", _xlsx(lignes))},
        follow=True)
    page = r.content.decode()
    assert "Vérification par le registre des inscriptions" in page
    d = r.context["verification"]["decomptes"]
    assert d == {"conforme": 1, "nom_different": 1, "non_inscrit": 1,
                 "sans_numero": 1}, d
    trab = groupe.students.get(student_number="104524")
    assert trab.last_name == "TRABELSI" and trab.inscrit.numero == "104524"
    assert groupe.students.get(student_number="104523").inscrit is not None
    assert groupe.students.get(student_number="999999").inscrit is None
    assert not Inscrit.objects.filter(numero="999999").exists()
    print("  liste Excel vérifiée : 1 conforme, 1 nom différent (registre retenu), "
          "1 non inscrit, 1 sans n° OK")

    r = c.post(reverse("class_detail", args=[groupe.pk]), {
        "import_xlsx": "1", "inscrire_inconnus": "1",
        "file": SimpleUploadedFile("cand.xlsx", _xlsx(lignes))}, follow=True)
    assert Inscrit.objects.filter(numero="999999", nom="NOUVEAU").exists()
    assert groupe.students.get(student_number="999999").inscrit is not None
    assert r.context["verification"]["decomptes"]["non_inscrit"] == 0
    c.post(reverse("class_detail", args=[groupe.pk]), {"fermer_verification": "1"})
    assert "Vérification par le registre" not in c.get(
        reverse("class_detail", args=[groupe.pk])).content.decode()
    print("  option « inscrire au registre les inconnus » OK")


def test_ajout_direct(c, prof):
    classe = ClassGroup.objects.create(name="INSC-CLASSE", owner=prof)
    r = c.post(reverse("class_detail", args=[classe.pk]),
               {"ajouter_inscrits": "1", "numeros": "104523, 104526\n555"}, follow=True)
    assert set(classe.students.values_list("student_number", flat=True)) == {"104523", "104526"}
    s = classe.students.get(student_number="104526")
    assert (s.last_name, s.first_name) == ("GHARBI", "Ines") and s.inscrit_id
    assert "555" in _messages(r)
    # depuis le registre, cases cochées ; le doublon n'est pas recréé
    choix = list(Inscrit.objects.filter(numero__in=["104523", "104525"])
                 .values_list("pk", flat=True))
    r = c.post(reverse("inscriptions"), {"ajouter": "1", "groupe": classe.pk,
                                         "choix": choix}, follow=True)
    assert classe.students.count() == 3, classe.students.count()
    assert "1 étudiant(s) ajouté(s)" in _messages(r)
    print("  ajout direct d'inscrits (par n° et depuis le registre) OK")
    return classe


def _qsi(img, page, numero, nom, prenom):
    """Colle un QR de carte d'inscription dans l'emplacement de l'étiquette."""
    import qrcode
    x, y, w, h = page["qr"]["rect"]
    qr = qrcode.make(f"QSI|{numero}|{nom}|{prenom}").convert("RGB")
    arr = cv2.cvtColor(np.array(qr), cv2.COLOR_RGB2BGR)
    f = max(1, round(mm2px(L.STICKER_QR_MM) / arr.shape[0]))
    arr = cv2.resize(arr, None, fx=f, fy=f, interpolation=cv2.INTER_NEAREST)
    n = arr.shape[0]
    px, py = mm2px(x + (w - L.STICKER_QR_MM) / 2), mm2px(y + (h - L.STICKER_QR_MM) / 2)
    img[py:py + n, px:px + n] = arr
    return img


def test_scan_et_public(c, prof):
    groupe = ClassGroup.objects.create(name="Concours INSC", owner=prof)
    quiz = Quiz.objects.create(title="Concours d'entrée INSC", class_group=groupe,
                               owner=prof, sheet_mode="grid", id_mode="sticker",
                               auto_enroll=True)
    for k, lettre in enumerate(KEY, start=1):
        Question.objects.create(quiz=quiz, order=k, qtype="qcm", points=1.0,
                                correct_choice=ord(lettre) - 65)
    layout = L.build_layout(quiz)
    quiz.layout_json = layout
    pdf = sheet_pdf.generate_sheet_pdf(quiz, layout)
    quiz.sheet_pdf.save("insc.pdf", ContentFile(pdf), save=True)
    page = layout["pages"][0]
    img = fill_sheet(render_page(pdf), layout, REPONSES, name_text=("", ""))
    img = simulate_scan(_qsi(img, page, "104525", "ABDELLATIF", "Mohamed"))
    data = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()
    lot = services.process_uploaded_files(quiz, [SimpleUploadedFile("c.jpg", data)])
    copie = lot.sheets.get()
    assert copie.status == "ok", (copie.status, copie.id_read, copie.error_message)
    cand = copie.student
    assert (cand.student_number, cand.last_name, cand.first_name) == \
        ("104525", "ABDELLATIF", "Mohamed"), cand
    assert cand.inscrit.numero == "104525"
    print("  QR de carte collé sur la copie : candidat créé sous son identité du registre OK")

    anonyme = Client()
    cache.clear()
    r = anonyme.get(reverse("public_resultat") + "?numero=104525")
    assert r.status_code == 200 and "Aucun résultat publié" in r.content.decode()
    assert anonyme.get(reverse("public_epreuve", args=[quiz.pk])).status_code == 404

    c.post(reverse("quiz_publication", args=[quiz.pk]), {"resultats_publies": "1",
                                                         "admis_publies": "1"})
    quiz.refresh_from_db()
    assert quiz.resultats_publies and not quiz.admis_publies   # pas de note minimale
    r = anonyme.get(reverse("public_resultat") + "?numero=104 525")
    page_pub = r.content.decode()
    assert "ABDELLATIF Mohamed" in page_pub and "<b>5</b> / 6" in page_pub, page_pub[-1500:]
    assert anonyme.get(reverse("public_accueil")).content.decode().count(escape(quiz.title)) == 1
    print("  publication : note consultée par n° d'inscription, sans compte OK")

    quiz.note_admission = 4
    quiz.save(update_fields=["note_admission"])
    c.post(reverse("quiz_publication", args=[quiz.pk]), {"resultats_publies": "1",
                                                         "admis_publies": "1"})
    r = anonyme.get(reverse("public_epreuve", args=[quiz.pk]))
    assert r.status_code == 200 and "ABDELLATIF" in r.content.decode()
    assert "Admis" in anonyme.get(reverse("public_resultat") + "?numero=104525").content.decode()
    c.post(reverse("quiz_publication", args=[quiz.pk]), {})
    assert anonyme.get(reverse("public_epreuve", args=[quiz.pk])).status_code == 404
    assert "Aucun résultat publié" in anonyme.get(
        reverse("public_resultat") + "?numero=104525").content.decode()
    print("  liste des admis publiée puis retirée OK")

    cache.clear()
    for _ in range(30):
        anonyme.get(reverse("public_resultat") + "?numero=1")
    r = anonyme.get(reverse("public_resultat") + "?numero=104525")
    assert "Trop de recherches" in r.content.decode()
    cache.clear()
    print("  recherches par n° freinées (30 / 10 min par adresse) OK")


def test_communiques(c, prof, autre):
    c.post(reverse("communiques"), {"titre": "Convocation au concours",
                                    "texte": "Salle B12, 8 h.", "publie": "on"})
    c.post(reverse("communiques"), {"titre": "Brouillon secret", "texte": "x"})
    publie = Communique.objects.get(titre="Convocation au concours")
    brouillon = Communique.objects.get(titre="Brouillon secret")
    assert publie.publie and not brouillon.publie and publie.auteur_id == prof.pk
    anonyme = Client()
    accueil = anonyme.get(reverse("public_accueil")).content.decode()
    assert "Convocation au concours" in accueil and "Brouillon secret" not in accueil
    assert "Salle B12" in anonyme.get(
        reverse("public_communique", args=[publie.pk])).content.decode()
    assert anonyme.get(reverse("public_communique", args=[brouillon.pk])).status_code == 404
    c2 = Client()
    c2.force_login(autre)
    assert c2.get(reverse("communiques") + f"?modifier={publie.pk}").status_code == 403
    assert c2.post(reverse("communiques"), {"supprimer": publie.pk}).status_code == 403
    print("  communiqués : publié visible, brouillon caché, auteur seul modifie OK")

    i = Inscrit.objects.get(numero="104526")
    assert c2.post(reverse("inscriptions"), {"supprimer": i.pk}).status_code == 403
    c.post(reverse("inscriptions"), {"supprimer": i.pk})
    assert not Inscrit.objects.filter(numero="104526").exists()
    s = Student.objects.get(student_number="104526")
    assert s.inscrit is None and s.last_name == "GHARBI"
    print("  retrait d'une inscription : auteur seul, l'étudiant reste dans sa classe OK")


def test_fonctions():
    assert inscriptions.numero_propre(" 10 45 23 ") == "104523"
    assert inscriptions.lire_charge_qr("QSI|104523|BEN SALEM|Samia") == "104523"
    assert inscriptions.meme_nom("Bén Salem", "SAMIA", "BEN SALEM", "Samia")
    assert inscriptions.meme_nom("Samia", "Ben Salem", "BEN SALEM", "Samia")
    print("  n° nettoyé, QR relu, noms comparés sans casse ni accents OK")


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    noms = ["prof_insc", "prof_insc2"]
    Quiz.objects.filter(owner__username__in=noms).delete()
    ClassGroup.objects.filter(owner__username__in=noms).delete()
    User.objects.filter(username__in=noms).delete()
    Inscrit.objects.filter(numero__in=["104523", "104524", "104525", "104526",
                                       "999999"]).delete()
    Communique.objects.filter(titre__in=["Convocation au concours",
                                         "Brouillon secret"]).delete()
    prof = enseignant_complet(User.objects.create_user("prof_insc", password="x-pass-123"))
    autre = enseignant_complet(User.objects.create_user("prof_insc2", password="x-pass-123"))
    c = Client()
    c.force_login(prof)

    print("Fonctions :")
    test_fonctions()
    print("Registre :")
    test_registre(c, prof)
    print("Vérification d'une liste importée :")
    test_verification_import(c, prof)
    print("Ajout direct :")
    test_ajout_direct(c, prof)
    print("Scan et espace public :")
    test_scan_et_public(c, prof)
    print("Communiqués :")
    test_communiques(c, prof, autre)
    print("\n✅ TESTS INSCRIPTIONS ET ESPACE PUBLIC RÉUSSIS")


if __name__ == "__main__":
    main()
