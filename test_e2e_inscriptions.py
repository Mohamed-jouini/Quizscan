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


def test_identite(c, prof, autre):
    import datetime
    url = reverse("inscriptions")
    page = c.get(url).content.decode()
    for champ in ("id_cin", "id_date_naissance", "id_email", "id_etablissement"):
        assert f'id="{champ}"' in page, champ
    r = c.post(url, {"inscrire": "1", "nom": "Khelifi", "prenom": "Omar",
                     "numero": "207001", "cin": " 0987 6543 ",
                     "date_naissance": "2006-03-15", "email": "omar.k@exemple.tn",
                     "etablissement": "  Lycée  pilote de Sfax "}, follow=True)
    i = Inscrit.objects.get(numero="207001")
    assert (i.cin, i.date_naissance, i.email, i.etablissement) == (
        "09876543", datetime.date(2006, 3, 15), "omar.k@exemple.tn",
        "Lycée pilote de Sfax"), (i.cin, i.date_naissance, i.email, i.etablissement)
    print("  inscription complète : CIN nettoyée, date, e-mail, établissement OK")

    # contrôles : CIN déjà prise, CIN invalide, naissance future, e-mail faux
    for donnees, erreur in (
            ({"cin": "09876543"}, "déjà celle de KHELIFI Omar"),
            ({"cin": "12-34"}, "N° de CIN invalide"),
            ({"date_naissance": "2099-01-01"}, "invraisemblable"),
            ({"email": "pas-un-mail"}, "adresse de courriel valide")):
        r = c.post(url, dict(_fiche("TEST", "X", "207002", "22220002"), **donnees))
        assert r.status_code == 200 and erreur in r.content.decode(), (donnees, erreur)
    assert not Inscrit.objects.filter(numero="207002").exists()
    print("  contrôles : CIN en double ou invalide, date future, e-mail faux refusés OK")

    # modifier : le registre fait foi, les listes reliées suivent
    groupe = ClassGroup.objects.create(name="Identité — classe", owner=prof)
    c.post(reverse("class_detail", args=[groupe.pk]),
           {"ajouter_inscrits": "1", "numeros": "207001"})
    page = c.get(url + f"?modifier={i.pk}").content.decode()
    assert "Modifier l'inscription de KHELIFI Omar" in page and 'value="09876543"' in page
    c.post(url, {"inscrire": "1", "modifier": i.pk, "nom": "KHELIFI", "prenom": "Omar Ali",
                 "numero": "207001", "cin": "09876543", "date_naissance": "2006-03-15",
                 "email": "omar@exemple.tn", "etablissement": "Lycée pilote de Sfax"})
    i.refresh_from_db()
    assert i.prenom == "Omar Ali" and i.email == "omar@exemple.tn"
    assert groupe.students.get().first_name == "Omar Ali"
    c2 = Client()
    c2.force_login(autre)
    assert c2.get(url + f"?modifier={i.pk}").status_code == 403
    print("  modifier : fiche préremplie, liste de classe mise à jour, "
          "réservé à l'auteur OK")

    # fiche inscrite avant le 30 octobre : signalée, à compléter pour la modifier
    t = Inscrit.objects.create(numero="207003", nom="TOUMI", prenom="Rim", cree_par=prof)
    page = c.get(url + "?q=207003").content.decode()
    assert "fiche à compléter" in page
    r = c.post(url, {"inscrire": "1", "modifier": t.pk, "nom": "TOUMI", "prenom": "Rim",
                     "numero": "207003"})
    assert r.content.decode().count("Champ obligatoire.") == 4
    c.post(url, _fiche("TOUMI", "Rim", "207003", "11223344", modifier=t.pk,
                       date_naissance="2005-12-01", etablissement="Lycée Bourguiba"))
    t.refresh_from_db()
    assert t.fiche_complete and t.etablissement == "Lycée Bourguiba"
    assert "fiche à compléter" not in c.get(url + "?q=207003").content.decode()
    print("  ancienne fiche incomplète : signalée, complétée par « Modifier » OK")

    # recherche par CIN ou établissement ; carte PDF avec CIN et naissance
    assert c.get(url + "?q=11223344").context["n_trouves"] == 1
    assert c.get(url + "?q=Bourguiba").context["n_trouves"] == 1
    carte = c.get(reverse("inscriptions_cartes") + f"?choix={t.pk}").content
    texte = pymupdf.open(stream=carte, filetype="pdf")[0].get_text()
    assert "CIN 11223344" in texte and "01/12/2005" in texte and "Lycée Bourguiba" in texte
    qr = _decode_png(c.get(reverse("inscrit_qr", args=[t.pk])).content)
    assert "11223344" not in qr, "la CIN ne doit pas être dans le QR"
    print("  recherche par CIN / établissement ; carte avec CIN et date ; "
          "CIN absente du QR OK")


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


def _fiche(nom, prenom, numero, cin, **autres):
    """Envoi complet du formulaire d'inscription (tous les champs obligatoires)."""
    return dict({"inscrire": "1", "nom": nom, "prenom": prenom, "numero": numero,
                 "cin": cin, "date_naissance": "2005-06-01",
                 "email": f"{prenom.lower()}.{cin}@exemple.tn",
                 "etablissement": "Lycée pilote"}, **autres)


def test_registre(c, prof):
    url = reverse("inscriptions")
    # nom, prénom et n° seuls ne suffisent plus : tout est obligatoire
    r = c.post(url, {"inscrire": "1", "nom": "BEN SALEM", "prenom": "Samia",
                     "numero": "104523"})
    assert r.status_code == 200 and r.content.decode().count("Champ obligatoire.") == 4
    assert not Inscrit.objects.filter(numero="104523").exists()
    page = c.get(url).content.decode()
    assert "Tous les champs sont obligatoires" in page
    print("  inscription incomplète refusée : CIN, naissance, e-mail, établissement "
          "obligatoires OK")

    r = c.post(url, _fiche(" ben  salem ", "Samia", "104 523", "11110001"), follow=True)
    i = Inscrit.objects.get(numero="104523")
    assert (i.nom, i.prenom, i.cree_par_id) == ("BEN SALEM", "Samia", prof.pk)
    assert i.fiche_complete
    assert "inscrit(e) sous le n° 104523" in r.content.decode()
    r = c.post(url, _fiche("AUTRE", "Nom", "104523", "11110009"))
    assert r.status_code == 200 and "déjà attribué" in r.content.decode()
    assert Inscrit.objects.filter(numero="104523").count() == 1
    print("  inscription complète (n° nettoyé, NOM en capitales, n° unique) OK")

    r = c.get(reverse("inscrit_qr", args=[i.pk]))
    assert r["Content-Type"] == "image/png"
    assert _decode_png(r.content) == "QSI|104523|BEN SALEM|Samia", _decode_png(r.content)
    r = c.get(reverse("inscriptions_cartes") + f"?choix={i.pk}")
    assert r["Content-Type"] == "application/pdf"
    texte = pymupdf.open(stream=r.content, filetype="pdf")[0].get_text()
    assert "104523" in texte and "BEN SALEM" in texte and "Samia" in texte
    print("  QR de la carte = n° d'inscription, NOM, prénom (sans CIN) ; carte PDF OK")

    for nom, prenom, numero, cin in (("TRABELSI", "Mariem", "104524", "11110002"),
                                     ("ABDELLATIF", "Mohamed", "104525", "11110003"),
                                     ("GHARBI", "Ines", "104526", "11110004")):
        c.post(url, _fiche(nom, prenom, numero, cin))
    assert Inscrit.objects.filter(numero__in=["104524", "104525", "104526"]).count() == 3

    # plus d'inscription par liste : ni carte à l'écran, ni envoi accepté
    page = c.get(url).content.decode()
    assert "Inscrire depuis une liste" not in page and 'name="coller"' not in page
    c.post(url, {"coller": "1", "text": "NOUVEAU;Ali;555001"})
    c.post(url, {"importer": "1", "file": SimpleUploadedFile(
        "registre.xlsx", _xlsx([("NOUVEAU", "Ali", 555002)]))})
    assert not Inscrit.objects.filter(numero__in=["555001", "555002"]).exists()
    print("  « Inscrire depuis une liste » supprimé (envoi direct refusé) OK")

    r = c.get(url + "?q=salem")
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

    page = c.get(reverse("class_detail", args=[groupe.pk])).content.decode()
    assert "inscrire_inconnus" not in page, "plus d'inscription au registre par liste"
    c.post(reverse("class_detail", args=[groupe.pk]), {"fermer_verification": "1"})
    assert "Vérification par le registre" not in c.get(
        reverse("class_detail", args=[groupe.pk])).content.decode()
    print("  plus d'inscription au registre depuis une liste de classe OK")


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


def test_import_base(c, prof, classe):
    concours = ClassGroup.objects.create(name="Concours — base", owner=prof)
    url = reverse("class_detail", args=[concours.pk])
    page = c.get(url).content.decode()
    assert "Importer depuis la base de données" in page and "Registre des inscriptions" in page
    assert f'value="groupe:{classe.pk}"' in page
    # registre, filtré : un seul
    r = c.post(url, {"importer_base": "1", "source": "registre", "filtre": "salem"},
               follow=True)
    assert list(concours.students.values_list("student_number", flat=True)) == ["104523"]
    s = concours.students.get()
    assert s.inscrit.numero == "104523" and s.last_name == "BEN SALEM"
    # registre entier : les autres, sans doublon
    n_registre = Inscrit.objects.count()
    r = c.post(url, {"importer_base": "1", "source": "registre", "filtre": ""}, follow=True)
    assert concours.students.count() == n_registre
    assert "1 déjà présent(s)" in _messages(r), _messages(r)
    # une autre classe, dont un étudiant sans n° (dédoublonné par le nom)
    Student.objects.create(class_group=classe, last_name="SANSNUMERO", first_name="Ali")
    autre = ClassGroup.objects.create(name="Vide — base", owner=prof)
    url2 = reverse("class_detail", args=[autre.pk])
    c.post(url2, {"importer_base": "1", "source": f"groupe:{classe.pk}"})
    c.post(url2, {"importer_base": "1", "source": f"groupe:{classe.pk}"})
    assert autre.students.count() == classe.students.count(), autre.students.count()
    assert autre.students.get(student_number="104526").inscrit is not None
    # liste d'un collègue : refusée
    c2 = Client()
    c2.force_login(enseignant_complet(User.objects.get_or_create(username="prof_insc2")[0]))
    c2.post(reverse("class_detail", args=[ClassGroup.objects.create(
        name="Collègue — base", owner=User.objects.get(username="prof_insc2")).pk]),
        {"importer_base": "1", "source": f"groupe:{classe.pk}"})
    assert not Student.objects.filter(class_group__name="Collègue — base").exists()
    print("  import direct : registre (filtré puis entier, sans doublon) et autre "
          "classe ; liste d'un collègue refusée OK")


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
    restes = Student.objects.filter(student_number="104526")
    assert restes.exists() and all(s.inscrit is None and s.last_name == "GHARBI"
                                   for s in restes)
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
                                       "999999", "207001", "207002", "207003",
                                       "207004"]).delete()
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
    classe = test_ajout_direct(c, prof)
    print("Import depuis la base de données :")
    test_import_base(c, prof, classe)
    print("Scan et espace public :")
    test_scan_et_public(c, prof)
    print("Communiqués :")
    test_communiques(c, prof, autre)
    print("Identité : CIN, date de naissance, établissement, e-mail :")
    test_identite(c, prof, autre)
    print("\n✅ TESTS INSCRIPTIONS ET ESPACE PUBLIC RÉUSSIS")


if __name__ == "__main__":
    main()
