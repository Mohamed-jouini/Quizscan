"""La liste officielle de l'institut, en Excel, intitulés en arabe (fichier remis
par l'utilisateur le 5 novembre 2026, ses noms et CIN effacés) : elle remplit les
candidats d'un concours et le registre des inscriptions. Tout est inventé ici.
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
os.environ["QUIZSCAN_TEST"] = "1"   # base de test séparée (quizscan_test)
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

import openpyxl
import pymupdf
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext, setup_test_environment
from django.urls import reverse

from grader import importers, inscriptions
from grader.models import ClassGroup, Inscrit, Quiz, Student
from test_commun import enseignant_complet

# Tels qu'écrits dans le fichier de l'utilisateur, faute de frappe et espace compris.
INTITULES = ["عدد الترسيم", "الاسم ة اللقب", "عدد ب ت و ", "مكانها", "تاريخ الإصدار",
             "تاريخ الولادة", "المركز", "القاعة"]
PREFIXE = "88"


def date(annee, mois, jour):
    return datetime.datetime(annee, mois, jour)


def classeur(lignes, intitules=INTITULES, titre=None):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.sheet_view.rightToLeft = True
    if titre:
        ws.append([titre])
    ws.append(list(intitules) + [None] * 6)
    for ligne in lignes:
        ws.append(list(ligne))
    ws.cell(ws.max_row + 4, 8).value = " "      # cellule égarée, comme H8 du fichier
    tampon = io.BytesIO()
    wb.save(tampon)
    return tampon.getvalue()


def envoi(lignes, nom="liste.xlsx", **autres):
    return SimpleUploadedFile(nom, classeur(lignes, **autres))


TROIS = [
    (880761, "سامي التجريبي", 8812345, "تونس", date(2024, 12, 17), date(2003, 1, 3),
     "معهد ابن ابي الضياف", "قاعة عدد 37"),
    (883654, "ليلى بن تجربة", "08854321", "تونس", date(2013, 3, 25), date(1991, 5, 8),
     "معهد خير الدين باردو", "قاعة عدد 39"),
    (884817, "محمد علي الاختبار", "88223344", "صفاقس", date(2024, 8, 16), date(2003, 4, 12),
     "نموذجية خزندار ", "قاعة عدد 15"),
]


def test_lecture():
    fiches = importers.parse_fiches_file("l.xlsx", classeur(TROIS))
    assert len(fiches) == 3, fiches
    f = fiches[0]
    assert (f["numero"], f["nom"], f["prenom"]) == ("880761", "التجريبي", "سامي"), f
    assert (f["cin_lieu"], f["centre"], f["salle"]) == ("تونس", "معهد ابن ابي الضياف", "قاعة عدد 37")
    assert importers.lire_date(f["cin_date"]) == datetime.date(2024, 12, 17)
    assert importers.lire_date(f["naissance"]) == datetime.date(2003, 1, 3)
    assert fiches[2]["centre"] == "نموذجية خزندار"
    print("  huit colonnes reconnues : le n° reste le n°, la CIN reste la CIN OK")

    # le nom : dernier mot, avec ses particules ; toujours un mot pour le prénom
    for texte, attendu in (("سامي التجريبي", ("التجريبي", "سامي")),
                           ("ليلى بن تجربة", ("بن تجربة", "ليلى")),
                           ("محمد علي بن سالم", ("بن سالم", "محمد علي")),
                           ("عبد الله الجلاصي", ("الجلاصي", "عبد الله")),
                           ("عبد الله بن عبد الرحمان", ("بن عبد الرحمان", "عبد الله")),
                           ("أحمد بن محمد الجويني", ("الجويني", "أحمد بن محمد")),
                           ("سامي بو علي", ("بو علي", "سامي")),
                           ("بن سالم", ("سالم", "بن")),
                           ("الجويني", ("الجويني", ""))):
        assert importers.decouper_nom_complet(texte, True)[:2] == attendu, texte
    for texte, attendu in (("بن سالم محمد علي", ("بن سالم", "محمد علي")),
                           ("الجويني سامي", ("الجويني", "سامي"))):
        assert importers.decouper_nom_complet(texte, False)[:2] == attendu, texte
    print("  nom et prénom découpés, particules comprises (بن, بو, عبد) OK")

    # variantes d'intitulés : l'ordre, l'orthographe, un titre au-dessus
    variante = importers.parse_fiches_file("l.xlsx", classeur(
        [("ع", "القاعة 3", "الجويني سامي", 500, "12345678")],
        intitules=["مكانها", "القاعة", "اللقب و الاسم", "رقم الترسيم", "ع.ب.ت.و"],
        titre="قائمة المترشحين"))
    assert len(variante) == 1, variante
    v = variante[0]
    assert (v["numero"], v["nom"], v["prenom"], v["cin"], v["salle"]) == (
        "500", "الجويني", "سامي", "12345678", "القاعة 3"), v
    print("  intitulés dans un autre ordre, autre orthographe, titre au-dessus OK")

    # le défaut d'origine : des intitulés, mais ni nom ni n° reconnus. On ne devine plus.
    try:
        importers.parse_fiches_file("l.xlsx", classeur(
            [(1, "x", date(2000, 1, 1))], intitules=["ع", "ب", "تاريخ الولادة"]))
    except importers.ImportError_ as exc:
        assert "Colonnes non reconnues" in str(exc), exc
    else:
        raise AssertionError("colonnes devinées au lieu d'être refusées")
    print("  ni nom ni n° reconnus : refusé, plus de colonnes devinées OK")

    for texte, attendu in (("17/12/2024", (2024, 12, 17)), ("2024-12-17 00:00:00", (2024, 12, 17)),
                           ("17-12-2024", (2024, 12, 17)), ("45643", (2024, 12, 17)),
                           ("١٧/١٢/٢٠٢٤", (2024, 12, 17))):
        assert importers.lire_date(texte) == datetime.date(*attendu), texte
    assert importers.lire_date("") is None and importers.lire_date("13/13/2000") is None
    assert inscriptions.cin_propre(" 9876543 ") == "09876543"
    assert inscriptions.cin_propre("٠٩٨٧٦٥٤٣") == "09876543"
    assert inscriptions.cin_propre("ab123456") == "AB123456"
    assert inscriptions.numero_propre("٧٦١") == "761"
    assert inscriptions.meme_nom("علي بن سالم", "محمد", "بن سالم", "محمد علي"), "découpage de travers"
    print("  dates d'Excel ou saisies, zéro de tête de la CIN, chiffres arabes OK")


def test_import_candidats(c, prof):
    groupe = ClassGroup.objects.create(name="Concours — liste", owner=prof)
    Quiz.objects.create(title="Concours liste", class_group=groupe, owner=prof, auto_enroll=True)
    url = reverse("class_detail", args=[groupe.pk])
    r = c.post(url, {"import_xlsx": "1", "file": envoi(TROIS)}, follow=True)
    assert groupe.students.count() == 3, _messages(r)
    assert "3 fiche(s) ajoutée(s) au registre" in _messages(r), _messages(r)

    s = groupe.students.get(student_number="880761")
    assert (s.last_name, s.first_name) == ("التجريبي", "سامي"), s
    i = s.inscrit
    assert i is not None and i.cree_par_id == prof.pk
    assert i.cin == "08812345", i.cin        # Excel avait effacé le zéro
    assert (i.cin_lieu, i.cin_date, i.date_naissance, i.centre, i.salle) == (
        "تونس", datetime.date(2024, 12, 17), datetime.date(2003, 1, 3),
        "معهد ابن ابي الضياف", "قاعة عدد 37")
    assert i.fiche_complete, "ni e-mail ni établissement dans le fichier : la fiche est complète"
    print("  candidats créés, fiches au registre, CIN avec son zéro, dates, centre, salle OK")

    d = r.context["verification"]["decomptes"]
    assert d["ajoute"] == 3 and d["conforme"] == 0 and d["non_inscrit"] == 0, d
    page = r.content.decode()
    for texte in ("08812345", "17/12/2024", "03/01/2003", "قاعة عدد 37", "معهد ابن ابي الضياف",
                  "ajoutée(s) au registre"):
        assert texte in page, texte
    assert f"?modifier={i.pk}#fiche-inscrit" in page, "la ligne d'un candidat n'ouvre pas sa fiche"
    assert "<details" in page and " open" not in page.split("<details")[1].split(">")[0], \
        "formulaires d'import ouverts alors que la liste est remplie"
    print("  page Candidats : colonnes du fichier, ligne qui ouvre la fiche, import replié OK")

    # sans changement : ni doublon de candidat, ni de fiche, tout est conforme
    n_fiches = Inscrit.objects.filter(numero__startswith=PREFIXE).count()
    r = c.post(url, {"import_xlsx": "1", "file": envoi(TROIS)}, follow=True)
    assert groupe.students.count() == 3 and Inscrit.objects.filter(
        numero__startswith=PREFIXE).count() == n_fiches
    d = r.context["verification"]["decomptes"]
    assert d["conforme"] == 3 and d["ajoute"] == 0, d
    assert "0 créé(s), 3 mis à jour" in _messages(r), _messages(r)
    print("  même liste renvoyée : rien de créé deux fois, tout conforme OK")

    # une nouvelle session : la salle change, l'identité du registre reste
    session = [(880761, "سامي التجريبي", 8812345, "تونس", date(2024, 12, 17), date(2003, 1, 3),
                "معهد آخر", "قاعة عدد 1")]
    r = c.post(url, {"import_xlsx": "1", "file": envoi(session)}, follow=True)
    i.refresh_from_db()
    assert (i.centre, i.salle) == ("معهد آخر", "قاعة عدد 1"), (i.centre, i.salle)
    autre_nom = [(880761, "كريم الغريب", "88999999", "صفاقس", date(2020, 1, 1), date(1999, 9, 9),
                  "معهد آخر", "قاعة عدد 1")]
    r = c.post(url, {"import_xlsx": "1", "file": envoi(autre_nom)}, follow=True)
    i.refresh_from_db()
    s.refresh_from_db()
    assert (i.nom, i.cin, i.cin_lieu, i.date_naissance) == (
        "التجريبي", "08812345", "تونس", datetime.date(2003, 1, 3)), "le registre fait foi"
    assert s.last_name == "التجريبي"
    verif = r.context["verification"]
    assert verif["decomptes"]["nom_different"] == 1, verif
    constats = " ".join(verif["anomalies"][0]["remarques"])
    assert "Autre CIN au registre" in constats and "Autre date de naissance" in constats, constats
    print("  nouvelle session : centre et salle remplacés ; nom, CIN et naissance du registre gardés OK")

    # dans un autre concours : la même fiche, pas une deuxième
    second = ClassGroup.objects.create(name="Concours — seconde", owner=prof)
    r = c.post(reverse("class_detail", args=[second.pk]),
               {"import_xlsx": "1", "file": envoi(TROIS[:1])}, follow=True)
    assert second.students.get().inscrit_id == i.pk
    assert Inscrit.objects.filter(numero="880761").count() == 1
    print("  autre concours : même fiche au registre OK")
    return groupe


def test_lignes_difficiles(c, prof):
    groupe = ClassGroup.objects.create(name="Concours — difficile", owner=prof)
    url = reverse("class_detail", args=[groupe.pk])
    lignes = [
        (885001, "منير الصادق", "88121212", "تونس", "13/13/2000", date(2001, 1, 1), "مركز", "قاعة 1"),
        (885002, "منير الصادق", "88131313", "تونس", date(2020, 1, 1), date(2001, 1, 1), "مركز", "قاعة 1"),
        (885003, "هند الشاهد", "88121212", "تونس", date(2020, 1, 1), date(2002, 2, 2), "مركز", "قاعة 2"),
        (885004, "نادية الرابع", "12-34", "تونس", date(2020, 1, 1), "n'importe quoi", "مركز", "قاعة 2"),
        (885001, "اسم آخر", "88141414", "تونس", date(2020, 1, 1), date(2001, 1, 1), "مركز", "قاعة 9"),
        (885005, None, None, "تونس", date(2024, 12, 17), date(2003, 1, 3), "مركز", "قاعة 3"),
        (None, "سلوى بلا عدد", "88151515", "تونس", date(2020, 1, 1), date(2001, 1, 1), "مركز", "قاعة 4"),
    ]
    r = c.post(url, {"import_xlsx": "1", "file": envoi(lignes)}, follow=True)
    verif = r.context["verification"]
    d = verif["decomptes"]
    assert d["ajoute"] == 5 and d["doublon"] == 1 and d["sans_numero"] == 1, d
    anomalies = {}
    for a in verif["anomalies"]:      # la première ligne d'un n°, pas son double
        anomalies.setdefault(a["numero"] or a["nom"], a)
    assert anomalies["885001"]["etat"] == "ajoute"

    # deux personnes de même nom, de n° différents : deux candidats (l'ancien
    # get_or_create par nom gardait le premier et perdait le second sans rien dire)
    homonymes = groupe.students.filter(last_name="الصادق", first_name="منير")
    assert homonymes.count() == 2, list(homonymes.values_list("student_number", flat=True))
    print("  homonymes aux n° différents : deux candidats OK")

    fiche = Inscrit.objects.get(numero="885001")
    assert fiche.cin_date is None and fiche.date_naissance == datetime.date(2001, 1, 1)
    assert "Date de délivrance illisible" in " ".join(anomalies["885001"]["remarques"])
    assert not fiche.fiche_complete and d["a_completer"] >= 1
    assert fiche.salle == "قاعة 1", "la ligne en double a écrasé la première"
    print("  date illisible : écartée et signalée, la ligne reste OK")

    assert Inscrit.objects.get(numero="885003").cin == ""
    assert "déjà celle de" in " ".join(anomalies["885003"]["remarques"])
    assert "CIN invalide" in " ".join(anomalies["885004"]["remarques"])
    assert "Date de naissance illisible" in " ".join(anomalies["885004"]["remarques"])
    print("  CIN déjà prise ou invalide : écartée et signalée OK")

    # ligne sans nom ni CIN, comme le fichier remis : le n° et le reste sont gardés
    vide = Inscrit.objects.get(numero="885005")
    assert (vide.nom, vide.prenom, vide.cin, vide.salle) == ("", "", "", "قاعة 3")
    assert not vide.fiche_complete
    assert "Nom manquant" in " ".join(anomalies["885005"]["remarques"])
    assert groupe.students.get(student_number="885005").full_name == ""
    assert r.status_code == 200
    # ... que le nom arrive ensuite : la fiche et les listes reliées le prennent
    c.post(url, {"import_xlsx": "1", "file": envoi(
        [(885005, "وفاء الموجودة", "88161616", "تونس", date(2024, 12, 17), date(2003, 1, 3),
          "مركز", "قاعة 3")])})
    vide.refresh_from_db()
    assert (vide.nom, vide.prenom, vide.cin) == ("الموجودة", "وفاء", "88161616") and vide.fiche_complete
    assert groupe.students.get(student_number="885005").last_name == "الموجودة"
    print("  ligne sans nom : gardée, signalée, complétée par la liste suivante OK")

    # sans n° : candidat par son nom, pas de fiche
    assert groupe.students.filter(student_number="", first_name="سلوى بلا").exists()
    assert not Inscrit.objects.filter(cin="88151515").exists()
    print("  ligne sans n° : candidat, mais pas de fiche au registre OK")

    r = c.get(reverse("class_labels_pdf", args=[groupe.pk]))
    assert r.status_code == 200 and r.content[:4] == b"%PDF"
    cartes = c.get(reverse("inscriptions_cartes") + f"?choix={vide.pk}")
    assert cartes.status_code == 200
    assert "88161616" not in inscriptions.charge_qr(vide), "la CIN ne doit pas être dans le QR"
    print("  planche d'étiquettes et carte : se tirent, sans CIN dans le QR OK")


def test_registre(c, prof, autre):
    url = reverse("inscriptions")
    # une liste simple n'inscrit personne, comme avant
    simple = io.BytesIO()
    wb = openpyxl.Workbook()
    wb.active.append(["Nom", "Prénom", "N° d'inscription"])
    wb.active.append(["SIMPLE", "Liste", 886000])
    wb.save(simple)
    r = c.post(url, {"importer_liste": "1", "file": SimpleUploadedFile(
        "simple.xlsx", simple.getvalue())}, follow=True)
    assert "n'inscrit personne" in _messages(r), _messages(r)
    assert not Inscrit.objects.filter(numero="886000").exists()
    print("  liste simple (nom, prénom, n°) : personne n'est inscrit OK")

    classe = ClassGroup.objects.create(name="Classe — registre", owner=prof)
    mien = Student.objects.create(class_group=classe, last_name="الأول", first_name="نبيل",
                                  student_number="886001")
    classe_autre = ClassGroup.objects.create(name="Classe — collègue", owner=autre)
    sien = Student.objects.create(class_group=classe_autre, last_name="الأول", first_name="نبيل",
                                  student_number="886001")
    lignes = [(886001, "نبيل الأول", "88001600", "تونس", date(2020, 1, 1), date(2001, 1, 1),
               "مركز", "قاعة 1"),
              (886002, "نجلاء الثانية", "88001601", "سوسة", date(2021, 2, 2), date(2002, 2, 2),
               "مركز", "قاعة 2")]
    n_candidats = Student.objects.count()
    r = c.post(url, {"importer_liste": "1", "file": envoi(lignes)}, follow=True)
    assert "2 fiche(s) ajoutée(s) au registre" in _messages(r), _messages(r)
    assert Student.objects.count() == n_candidats, "le registre seul ne crée aucun candidat"
    fiche = Inscrit.objects.get(numero="886001")
    assert fiche.cree_par_id == prof.pk and fiche.cin_lieu == "تونس"
    mien.refresh_from_db()
    sien.refresh_from_db()
    assert mien.inscrit_id == fiche.pk, "son propre candidat n'est pas relié à la fiche"
    assert sien.inscrit_id is None, "le candidat d'un collègue a été relié"
    page = r.content.decode()
    for texte in ("Vérification par le registre", "88001600", "سوسة", "قاعة 2", "02/02/2021"):
        assert texte in page, texte
    assert "Importer la liste Excel" in page and reverse("modele_candidats") in page
    print("  import au registre : fiches créées, aucun candidat, seuls ses candidats reliés OK")

    # recherche par salle ; le rapport se ferme
    assert c.get(url, {"q": "قاعة 2"}).context["n_trouves"] >= 1
    c.post(url, {"fermer_verification": "1"})
    assert "Vérification par le registre" not in c.get(url).content.decode()

    # un collègue n'y change rien : la fiche reste celle de son auteur
    c2 = Client()
    c2.force_login(autre)
    r = c2.post(url, {"importer_liste": "1", "file": envoi(
        [(886001, "نبيل الأول", "88001600", "تونس", date(2020, 1, 1), date(2001, 1, 1),
          "مركز الزميل", "قاعة 99")])}, follow=True)
    fiche.refresh_from_db()
    assert (fiche.centre, fiche.salle) == ("مركز", "قاعة 1"), "fiche d'un collègue modifiée"
    assert "inscrite par un collègue" in r.content.decode()
    print("  fiche d'un collègue : pas modifiée, et il le voit dans le rapport OK")

    # formulaire : les colonnes du fichier, ni e-mail ni établissement (retirés le
    # 7 novembre 2026)
    page = c.get(url).content.decode()
    for champ in ("id_numero", "id_nom", "id_prenom", "id_cin", "id_cin_lieu", "id_cin_date",
                  "id_date_naissance", "id_centre", "id_salle"):
        assert f'id="{champ}"' in page, champ
    assert 'id="id_email"' not in page and 'id="id_etablissement"' not in page
    assert page.index('id="id_numero"') < page.index('id="id_nom"') < page.index('id="id_cin_lieu"')
    assert 'max="9999-12-31"' in page, "année à six chiffres possible au clavier"
    base = {"inscrire": "1", "numero": "886003", "nom": "Mabrouk", "prenom": "Ali",
            "cin": "88001603", "cin_lieu": " Le  Kef ", "cin_date": "2022-03-04",
            "date_naissance": "2001-01-01"}
    r = c.post(url, dict(base, cin_date="2000-01-01"))
    assert r.status_code == 200 and "ne peut pas précéder la naissance" in r.content.decode()
    r = c.post(url, dict(base, cin_lieu=""))
    assert r.status_code == 200 and "Champ obligatoire." in r.content.decode()
    r = c.post(url, base)
    assert r.status_code == 302, r.content.decode()[-800:]
    saisie = Inscrit.objects.get(numero="886003")
    assert saisie.fiche_complete and saisie.cin_lieu == "Le Kef" and saisie.email == ""
    assert not saisie.centre and not saisie.salle
    print("  formulaire : colonnes du fichier, délivrance après naissance, facultatifs vides OK")


def test_modele(c):
    r = c.get(reverse("modele_candidats"))
    assert r.status_code == 200 and "attachment" in r["Content-Disposition"]
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    assert wb.active.sheet_view.rightToLeft
    fiches = importers.parse_fiches_file("modele_candidats.xlsx", r.content)
    assert len(fiches) == 2, fiches
    f = fiches[0]
    assert f["numero"] == "1001" and f["cin"] == "01234567" and f["nom"] == "بن تجربة", f
    assert importers.lire_date(f["cin_date"]) == datetime.date(2020, 3, 2)
    assert Client().get(reverse("modele_candidats")).status_code == 302, "modèle ouvert sans compte"
    with open("modele_candidats.xlsx", "rb") as fh:
        assert len(importers.parse_fiches_file("modele_candidats.xlsx", fh.read())) == 2, \
            "le modèle du dépôt n'a plus les colonnes de l'import"
    print("  modèle téléchargé : se relit, zéro de la CIN gardé, réservé aux comptes OK")


def test_volume(c, prof):
    """Une liste de concours compte des milliers de lignes : le nombre de requêtes
    ne doit pas la suivre (l'ancien import en faisait au moins deux par ligne)."""
    def requetes(n, debut, groupe):
        lignes = [(debut + k, f"اسم{k} لقب{k}", f"7{debut + k:07d}", "تونس", date(2020, 1, 1),
                   date(2001, 1, 1), "مركز", f"قاعة {k % 40}") for k in range(n)]
        url = reverse("class_detail", args=[groupe.pk])
        with CaptureQueriesContext(connection) as ctx:
            r = c.post(url, {"import_xlsx": "1", "file": envoi(lignes)})
        assert r.status_code == 302
        assert groupe.students.count() == n and Inscrit.objects.filter(
            numero__gte=str(debut), numero__lt=str(debut + n)).count() == n
        return len(ctx)

    petit = requetes(50, 8870000, ClassGroup.objects.create(name="Volume A", owner=prof))
    grand = requetes(5000, 8880000, ClassGroup.objects.create(name="Volume B", owner=prof))
    assert grand <= petit + 30, (petit, grand)
    print(f"  coût : {petit} requêtes pour 50 lignes, {grand} pour 5 000 OK")


def _messages(r):
    return " ".join(str(m) for m in r.context["messages"]) if r.context else ""


def nettoyer():
    noms = ["prof_liste", "prof_liste2"]
    Quiz.objects.filter(owner__username__in=noms).delete()
    ClassGroup.objects.filter(owner__username__in=noms).delete()
    User.objects.filter(username__in=noms).delete()
    Inscrit.objects.filter(numero__startswith=PREFIXE).delete()
    Inscrit.objects.filter(cin__startswith=PREFIXE).delete()


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    nettoyer()
    prof = enseignant_complet(User.objects.create_user("prof_liste", password="x-pass-123"))
    autre = enseignant_complet(User.objects.create_user("prof_liste2", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    print("Lecture du fichier :")
    test_lecture()
    print("Import dans les candidats d'un concours :")
    test_import_candidats(c, prof)
    print("Lignes difficiles :")
    test_lignes_difficiles(c, prof)
    print("Page Inscriptions :")
    test_registre(c, prof, autre)
    print("Modèle Excel :")
    test_modele(c)
    print("Volume :")
    test_volume(c, prof)
    nettoyer()
    print("\n✅ TEST DE LA LISTE OFFICIELLE RÉUSSI")


if __name__ == "__main__":
    main()
