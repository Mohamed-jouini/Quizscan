"""Épreuve écrite (rédaction) et épreuve orale.

Écrit : sujet rédigé dans l'interface, sujet et copies à télécharger ; le
correcteur noircit la note dans le cadre de la page de garde ; au scan, la
note et l'identification (grille de n° ou QR) sont lues et la note est
affectée à l'étudiant. Oral : notes saisies à l'écran.
"""
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

import cv2
import pymupdf
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import services
from grader.models import ClassGroup, Inscrit, Quiz, Student
from test_commun import enseignant_complet
from test_e2e import fill_sheet, mm2px, render_page, simulate_scan

ETUDIANTS = [("AMRI", "Sana", "104501"), ("BAKIR", "Omar", "104502"),
             ("CHERIF", "Lina", "104503")]


def noircir_note(img, grille, chiffres):
    """chiffres : un chiffre (ou None, ou une liste = plusieurs cases) par ligne."""
    for row, d in zip(grille["rows"], chiffres):
        for k in ([] if d is None else d if isinstance(d, list) else [d]):
            cx, cy = row[k]
            cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(grille["r"] * 0.9),
                       (20, 20, 20), -1)
    return img


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 90])[1].tobytes()


def texte_pdf(data):
    return "".join(p.get_text() for p in pymupdf.open(stream=data, filetype="pdf"))


def test_ecrit(c, prof, groupe):
    r = c.post(reverse("quiz_create"), {
        "title": "Rédaction — le voyage", "type_epreuve": "ecrit",
        "class_group": groupe.pk, "language": "fr", "note_sur": "20",
        "sheet_mode": "grid", "id_mode": "grid", "id_digits": "6",
        "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors if r.context else r
    quiz = Quiz.objects.get(title="Rédaction — le voyage", owner=prof)
    assert quiz.type_epreuve == "ecrit"
    q = quiz.questions.get()
    assert (q.qtype, q.points) == ("open", 20.0)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "1. Le sujet" in page and 'class="etapes"' not in page
    print("  création : épreuve écrite, question « Note » sur 20 OK")

    r = c.post(reverse("quiz_detail", args=[quiz.pk]), {
        "reglages": "1", "title": quiz.title,
        "sujet_texte": "Racontez un voyage qui vous a marqué.\n\nDécrivez les lieux.",
        "entete": "Lycée pilote", "duree": "1 h 30", "note_sur": "20",
        "id_mode": "grid", "id_digits": "6", "pages_redaction": "2"})
    assert r.status_code == 302
    c.post(reverse("quiz_detail", args=[quiz.pk]), {"generer": "1"})
    quiz.refresh_from_db()
    assert quiz.sheet_pdf and quiz.subject_pdf
    sujet = texte_pdf(quiz.subject_pdf.read())
    assert "Racontez un voyage" in sujet and "note sur 20" in sujet and "1 h 30" in sujet
    copies = quiz.sheet_pdf.read()
    doc = pymupdf.open(stream=copies, filetype="pdf")
    # 2 pages lignées demandées, 3 imprimées : la copie remplit deux feuilles entières en
    # recto-verso (demande de l'utilisateur, 6 novembre 2026)
    assert quiz.layout_json["pages_redaction"] == 3 and len(doc) == 4, len(doc)
    assert "RÉSERVÉ AU CORRECTEUR" in doc[0].get_text()
    assert "Page non lue au scan" in doc[1].get_text() and "Ne pas scanner" not in doc[1].get_text()
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert "page de garde + 3 pages lignées, en A4 à 100 %, recto-verso sur le bord long" in page
    print("  sujet et copies générés (page de garde + 3 pages lignées : 2 demandées, "
          "feuilles entières) OK")

    layout = quiz.layout_json
    grille = layout["pages"][0]["note_grid"]
    # par demi-points (demande du 13 novembre 2026) : partie entière de 0 à 20 sur une
    # ligne, décimale « ,0 » ou « ,5 » ; 15,5 = 15 puis ,5
    assert grille["modele"] == "demi" and grille["kinds"] == ["entier", "demi"]
    assert [len(r) for r in grille["rows"]] == [21, 2], [len(r) for r in grille["rows"]]
    texte_garde = doc[0].get_text()
    assert "Partie entière" in texte_garde and "Décimale" in texte_garde \
        and "avec 20, noircissez ,0" in texte_garde and "Dizaines" not in texte_garde
    base = render_page(copies, 0)
    pages = []
    for (nom, prenom, num), chiffres in zip(ETUDIANTS, ([15, 1],
                                                       [8, 0],
                                                       [[12, 16], 0])):
        img = fill_sheet(base.copy(), layout, {}, student_number=num)
        pages.append((f"{nom}.jpg", jpg(simulate_scan(noircir_note(img, grille, chiffres)))))
    pages.append(("redaction.jpg", jpg(simulate_scan(render_page(copies, 1)))))
    lot = services.process_uploaded_files(
        quiz, [SimpleUploadedFile(n, d) for n, d in pages], label="Salle 1")
    copies_lues = {s.source_name: s for s in lot.sheets.all()}
    assert copies_lues["redaction.jpg"].status == "ignoree"
    for nom, _, num in ETUDIANTS:
        s = copies_lues[f"{nom}.jpg"]
        assert s.status == "ok" and s.student.student_number == num, (nom, s.status)
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    assert rows["AMRI"]["score"] == 15.5 and rows["AMRI"]["pct"] == 78, rows["AMRI"]
    assert rows["BAKIR"]["score"] == 8.0 and rows["BAKIR"]["pending_open"] == 0
    assert rows["CHERIF"]["pending_open"] == 1, "note ambiguë : à saisir"
    print("  scan : 15,5 et 8 lus (partie entière 0 à 20, décimale ,0 ou ,5) et affectés "
          "par n° ; note ambiguë à saisir ; page de rédaction ignorée OK")

    detail = c.get(reverse("batch_detail", args=[lot.pk])).content.decode()
    assert "Page de rédaction" in detail
    cherif = copies_lues["CHERIF.jpg"]
    # toute la ligne s'ouvre d'un clic : le lien de sa première cellule s'étend dessus
    cible = f'class="ligne-lien__cible ligne-lien__nom" href="{reverse("sheet_review", args=[cherif.pk])}"'
    assert cible in detail, "la ligne d'une copie ne s'ouvre pas"
    assert cible in c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert (f'class="ligne-lien__cible" href="{reverse("batch_detail", args=[lot.pk])}"'
            in c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()), "la ligne d'un lot ne s'ouvre pas"
    revue = c.get(reverse("sheet_review", args=[cherif.pk])).content.decode()
    assert "Note illisible : à saisir" in revue
    reponse = cherif.answers.get()
    c.post(reverse("sheet_review", args=[cherif.pk]), {f"points_{reponse.pk}": "11,5"})
    rows = {r["student"].last_name: r for r in services.compute_results(quiz)}
    assert rows["CHERIF"]["score"] == 11.5
    print("  écran de vérification : note illisible saisie à la main OK")

    # note minimale réglée sur la page de l'écrit (demande de l'utilisateur, 7 novembre 2026)
    page = c.get(reverse("quiz_detail", args=[quiz.pk])).content.decode()
    assert 'name="note_admission"' in page and "Note minimale de réussite" in page
    reglages = {"reglages": "1", "title": quiz.title, "sujet_texte": quiz.sujet_texte,
                "entete": quiz.entete, "duree": quiz.duree, "date_epreuve": "",
                "note_sur": "20", "id_mode": quiz.id_mode, "id_digits": "6",
                "pages_redaction": "2", "format_impression": quiz.format_impression}
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="25"))
    assert r.status_code == 200 and "Entre 0 et 20" in r.content.decode()
    quiz.refresh_from_db()
    assert quiz.note_admission is None
    sujet_avant = quiz.subject_pdf.name
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="10,0"))
    assert r.status_code == 302
    quiz.refresh_from_db()
    assert quiz.note_admission == 10.0, quiz.note_admission
    assert quiz.subject_pdf.name == sujet_avant, "le sujet n'imprime pas la note minimale"
    assert quiz.modifications.filter(action="note_admission").exists(), "non tracée au journal"
    c.post(reverse("quiz_detail", args=[quiz.pk]), reglages)          # formulaire d'avant
    quiz.refresh_from_db()
    assert quiz.note_admission == 10.0, "un envoi sans le champ garde la note minimale"
    print("  note minimale de réussite réglée sur la page de l'écrit (10 retenu, 25 refusé), "
          "tracée au journal OK")
    page = c.get(reverse("quiz_results", args=[quiz.pk])).content.decode()
    assert "<th>Bonnes réponses</th>" not in page and "<th>Note</th>" in page
    assert "78 %" in page and "Non réussi" in page and "Réussi" in page
    texte = texte_pdf(c.get(reverse("quiz_bulletins_pdf", args=[quiz.pk])).content)
    assert "NOTE" in texte and "15.5 / 20" in texte and "Épreuve écrite" in texte
    c.post(reverse("quiz_publication", args=[quiz.pk]), {"resultats_publies": "1"})
    cache.clear()
    public = Client().get(reverse("public_resultat") + "?numero=104501").content.decode()
    # espace public : le verdict seul, jamais la note (demande du 13 novembre 2026)
    assert "Rédaction" in public and '<p class="pub-verdict pub-verdict--admis">Réussi</p>' \
        in public, public[-1500:]
    for absent in ("15,5", "15.5", "78 %", "bonnes réponses"):
        assert absent not in public, absent
    cache.clear()
    print("  résultats et bulletin : note, pourcentage, réussi ; espace public : « Réussi » "
          "seul OK")
    return quiz


def test_ligne_vide_a_verifier(quiz):
    """Grille par demi-points (13 novembre 2026) : une case noircie par ligne, « ,0 »
    compris. Une ligne vide n'est pas lue 0 : la note est à vérifier (règle du 8 novembre
    2026, après une vraie fiche d'oral où 19 était lu 9). 20,5 dépasse le barème : à
    vérifier aussi."""
    from grader import omr
    grille = quiz.layout_json["pages"][0]["note_grid"]
    pdf = open(quiz.sheet_pdf.path, "rb").read()

    def en_clair(img, texte):                   # la note écrite dans sa case
        x, y, w, h = grille["note_box"]
        cv2.putText(img, texte, (mm2px(x + 4), mm2px(y + h - 3)),
                    cv2.FONT_HERSHEY_SIMPLEX, 2.4, (40, 40, 40), 5, cv2.LINE_AA)
        return img

    def lire(img):
        return omr.lire_note(cv2.resize(img, (int(210 * 8), int(297 * 8))), grille, 20)

    neuf = noircir_note(render_page(pdf, 0), grille, [9, 0])
    assert lire(neuf.copy())[0] == 9.0, "9 puis ,0 : 9"
    vide = noircir_note(render_page(pdf, 0), grille, [None, 0])
    note, lus = lire(vide.copy())
    assert note is None and lus == ["?", 0], (note, lus)          # partie entière vide
    note, lus = lire(en_clair(render_page(pdf, 0), "19"))         # écrite, rien de noirci
    assert note is None and lus == ["?", "?"], (note, lus)
    sans_decimale = noircir_note(render_page(pdf, 0), grille, [15, None])
    assert lire(sans_decimale)[0] is None, "décimale vide : à vérifier, pas 15"
    complete = en_clair(noircir_note(render_page(pdf, 0), grille, [19, 1]), "19,5")
    assert lire(complete)[0] == 19.5, "écrite et noircie : 19,5"
    assert lire(noircir_note(render_page(pdf, 0), grille, [20, 0]))[0] == 20.0
    trop = noircir_note(render_page(pdf, 0), grille, [20, 1])
    assert lire(trop)[0] is None, "20,5 : au-dessus du barème, à vérifier"
    assert lire(noircir_note(render_page(pdf, 0), grille, [14, [0, 1]]))[0] is None, \
        ",0 et ,5 noircis : à vérifier"
    # par le vrai chemin : feuille scannée (tournée, bruitée), recalée sur son cadre
    for image, attendu in ((neuf, 9.0), (vide, None), (complete, 19.5), (trop, None)):
        recalee, _ = omr.detect_and_warp(simulate_scan(image.copy()))
        assert omr.lire_note(recalee, grille, 20)[0] == attendu, attendu
    print("  grille par demi-points : 9, 19,5 et 20 lus ; partie entière ou décimale vide, "
          "deux décimales, 20,5 : note à vérifier OK")


def test_ancienne_grille(quiz):
    """Copies imprimées avec l'ancienne grille à chiffres (dizaines, unités, dixièmes,
    centièmes ; avant elle, trois lignes) : lues avec leur disposition d'alors. Lues avec la
    nouvelle, si l'épreuve a été régénérée depuis, leur ligne de la décimale tombe sous
    l'ancienne grille, sur du blanc : note à vérifier, jamais fausse (13 novembre 2026)."""
    import copy

    from grader import layout as L, omr, sheet_pdf
    nouvelle = quiz.layout_json["pages"][0]["note_grid"]
    page = {}
    L._add_note_grid(page, nouvelle["rect"][1], 20, nouvelle["order"])   # au même endroit
    ancienne = page["note_grid"]
    assert ancienne["kinds"] == ["dizaines", "unites", "dixiemes", "centiemes"]
    disposition = copy.deepcopy(quiz.layout_json)
    disposition["pages"][0]["note_grid"] = ancienne
    pdf = sheet_pdf.generate_sheet_pdf(quiz, disposition)        # copie imprimée d'avant
    x, y, w, h = ancienne["rect"]
    for chiffres, attendu in (([1, 3, 5, 0], 13.5), ([1, 9, 2, 5], 19.25), ([0, 8, 0, 0], 8.0)):
        img = noircir_note(render_page(pdf, 0), ancienne, chiffres)
        # la signature du correcteur, sous l'ancienne grille, sur la ligne des entiers
        cv2.line(img, (mm2px(x + 112), mm2px(y + h - 1)), (mm2px(x + w - 6), mm2px(y + h - 2)),
                 (30, 30, 30), mm2px(2.2))
        warped = cv2.resize(img, (int(210 * 8), int(297 * 8)))   # repère canonique
        assert omr.lire_note(warped, ancienne, 20)[0] == attendu, chiffres
        assert omr.lire_note(warped, nouvelle, 20)[0] is None, (chiffres, "lue fausse")
        recalee, _ = omr.detect_and_warp(simulate_scan(img.copy()))
        assert omr.lire_note(recalee, nouvelle, 20)[0] is None, (chiffres, "lue fausse")
    trois = {**ancienne, "rows": ancienne["rows"][:3], "kinds": ancienne["kinds"][:3],
             "decimales": 1}
    warped = cv2.resize(noircir_note(render_page(pdf, 0), trois, [1, 4, 5]),
                        (int(210 * 8), int(297 * 8)))
    assert omr.lire_note(warped, trois, 20)[0] == 14.5
    assert omr.lire_note(warped, ancienne, 20)[0] is None       # centièmes absents
    assert omr.lire_note(warped, nouvelle, 20)[0] is None
    print("  copies à l'ancienne grille (13,5 ; 19,25 ; 8 ; 14,5 sur trois lignes) lues avec "
          "leur disposition ; avec la nouvelle : à vérifier, jamais fausses OK")


def test_oral(c, groupe):
    """Oral : les notes s'importent d'un fichier Excel, et de lui seul (demande de
    l'utilisateur, 13 novembre 2026) : ni saisie à l'écran, ni fiche scannée."""
    import io

    from openpyxl import load_workbook
    r = c.post(reverse("quiz_create"), {
        "title": "Oral d'anglais", "type_epreuve": "oral", "class_group": groupe.pk,
        "language": "fr", "note_sur": "20", "sheet_mode": "grid", "id_mode": "grid",
        "id_digits": "6", "wrong_penalty": "0"})
    assert r.status_code == 302
    quiz = Quiz.objects.get(title="Oral d'anglais", class_group=groupe)
    url = reverse("quiz_detail", args=[quiz.pk])
    page = c.get(url).content.decode()
    assert "Notes de l'oral" in page and "Sana AMRI" in page and "pas encore notée" in page
    assert "Importer les notes" in page and reverse("quiz_notes_oral_xlsx", args=[quiz.pk]) in page
    assert "Classeur en arabe" in page and "n° de CIN" in page and "« العدد / 20 »" in page
    assert "Générer les fiches" not in page and "Lire les notes et les affecter" not in page
    e = {s.last_name: s for s in groupe.students.all()}
    assert f'name="note_{e["AMRI"].pk}"' not in page and "Enregistrer les notes" not in page
    # ni saisie fabriquée « à la main », ni fiches : rien n'est enregistré
    c.post(url, {"notes": "1", f"note_{e['AMRI'].pk}": "15,5"})
    c.post(url, {"generer": "1"})
    quiz.refresh_from_db()
    assert services.compute_results(quiz) == [] and not quiz.sheet_pdf
    r = c.post(reverse("quiz_upload", args=[quiz.pk]),
               {"label": "Jury 1", "files": SimpleUploadedFile("fiche.jpg", b"jpeg")}, follow=True)
    assert "plus de fiches scannées" in r.content.decode() and not quiz.batches.exists()
    formulaire = c.get(reverse("quiz_create")).content.decode()
    assert "Oral — notes importées d&#x27;un fichier Excel" in formulaire
    print("  oral : ni saisie à l'écran, ni fiches générées ou scannées ; import Excel "
          "seulement OK")

    # ---- note minimale réglée sur la page de l'oral (demande de l'utilisateur, 7 novembre)
    assert 'name="note_admission"' in page and "Note minimale de réussite" in page
    reglages = {"reglages": "1", "title": quiz.title, "note_sur": "20"}
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="21"))
    assert r.status_code == 200 and "Entre 0 et 20" in r.content.decode()
    r = c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission="12,5"))
    assert r.status_code == 302
    quiz.refresh_from_db()
    assert quiz.note_admission == 12.5, quiz.note_admission
    assert quiz.modifications.filter(action="note_admission").exists(), "non tracée au journal"
    c.post(reverse("quiz_detail", args=[quiz.pk]), reglages)          # formulaire d'avant
    quiz.refresh_from_db()
    assert quiz.note_admission == 12.5, "un envoi sans le champ garde la note minimale"
    c.post(reverse("quiz_detail", args=[quiz.pk]), dict(reglages, note_admission=""))
    quiz.refresh_from_db()
    assert quiz.note_admission is None, "vide : plus de note minimale"
    print("  oral : note minimale réglée sur sa page (12,5 retenu, 21 refusé, vide retiré) OK")

    # ---- modèle Excel : les candidats dans l'ordre des salles, la colonne de la note vide ;
    # en arabe, de droite à gauche, avec le n° de CIN du registre (13 novembre 2026)
    Inscrit.objects.filter(numero="ORAL-104501").delete()
    e["AMRI"].inscrit = Inscrit.objects.create(numero="ORAL-104501", nom="AMRI", prenom="Sana",
                                               cin="01166344", cree_par=groupe.owner)
    e["AMRI"].save()

    def modele():
        reponse = c.get(reverse("quiz_notes_oral_xlsx", args=[quiz.pk]))
        disposition = reponse["Content-Disposition"]
        assert disposition.startswith("attachment"), disposition
        classeur = load_workbook(io.BytesIO(reponse.content))
        feuille = classeur.active
        assert feuille.sheet_view.rightToLeft and feuille["A1"].value == quiz.title
        assert "أعداد الاختبار الشفاهي على 20" in feuille["A2"].value, feuille["A2"].value
        assert [x.value for x in feuille[3]] == [
            "رقم التسجيل", "الاسم", "اللقب", "رقم بطاقة التعريف الوطنية", "العدد / 20"]
        return classeur, {ligne[0].value: ligne for ligne in feuille.iter_rows(min_row=4)}

    def importer(classeur):
        tampon = io.BytesIO()
        classeur.save(tampon)
        envoi = c.post(url, {"importer_notes": "1",
                             "fichier": SimpleUploadedFile("notes.xlsx", tampon.getvalue())},
                       follow=True)
        return envoi.content.decode()

    def notes():
        return {r["student"].last_name: r["score"] for r in services.compute_results(quiz)}

    classeur, lignes = modele()
    assert set(lignes) == {num for _, _, num in ETUDIANTS}, set(lignes)
    assert all(ligne[4].value is None for ligne in lignes.values())
    # le CIN en texte, zéro de tête gardé ; vide sans fiche au registre
    assert lignes["104501"][3].value == "01166344", lignes["104501"][3].value
    assert lignes["104501"][3].number_format == "@" and lignes["104502"][3].value is None
    assert [lignes["104501"][k].value for k in (1, 2)] == ["Sana", "AMRI"]   # prénom d'abord
    lignes["104501"][4].value = "12,25"          # saisie avec une virgule
    lignes["104503"][4].value = 9                # BAKIR : vide (absent), passé
    page = importer(classeur)
    assert "Notes importées : 2 nouvelle(s), 0 modifiée(s), 0 inchangée(s)" in page, page[-2000:]
    assert notes() == {"AMRI": 12.25, "CHERIF": 9.0}, notes()
    page = c.get(url).content.decode()
    assert "importée" in page and "12,25" in page and "pas encore notée" in page
    assert quiz.modifications.filter(action="note", objet__contains="import Excel").count() == 2
    # le modèle reprend les notes ; réimporté : une note changée, une nouvelle
    classeur, lignes = modele()
    assert lignes["104501"][4].value == 12.25 and lignes["104502"][4].value is None
    lignes["104503"][4].value = 17
    lignes["104502"][4].value = 10.5
    page = importer(classeur)
    assert "Notes importées : 1 nouvelle(s), 1 modifiée(s), 1 inchangée(s)" in page, page[-2000:]
    assert notes() == {"AMRI": 12.25, "BAKIR": 10.5, "CHERIF": 17.0}, notes()
    print("  oral : modèle Excel téléchargé, rempli (12,25 ; 9 ; une case vide), importé ; "
          "réimporté : note remplacée, journal tenu OK")

    # un fichier faux n'importe rien, et le message nomme chaque ligne fausse
    avant = notes()
    lignes["104501"][4].value = 21
    lignes["104502"][4].value = "abs"
    page = importer(classeur)
    assert "Aucune note importée" in page and "« abs »" in page, page[-2000:]
    assert "n° 104501 : « 21 » n&#x27;est pas une note de 0 à 20" in page, page[-2000:]
    assert notes() == avant
    classeur, lignes = modele()
    classeur.active.append(["999999", "Inconnu", "X", None, 12])
    page = importer(classeur)
    assert "Aucune note importée" in page and "999999" in page and notes() == avant
    classeur, lignes = modele()
    classeur.active.append(["104501", "Sana", "AMRI", "01166344", 13])
    page = importer(classeur)
    assert "n° 104501 : deux fois dans le fichier" in page and notes() == avant
    feuille = load_workbook(io.BytesIO(c.get(reverse("quiz_notes_oral_xlsx",
                                                     args=[quiz.pk])).content)).active
    feuille.delete_rows(1, 3)                     # plus d'en-têtes
    page = importer(feuille.parent)
    assert "introuvables" in page and notes() == avant
    r = c.post(url, {"importer_notes": "1",
                     "fichier": SimpleUploadedFile("notes.csv", b"104501;12")}, follow=True)
    assert "Choisissez le fichier Excel (.xlsx)" in r.content.decode() and notes() == avant
    print("  oral : note hors barème ou illisible, n° inconnu, n° en double, en-têtes absents, "
          "autre format : rien d'importé, chaque ligne nommée OK")

    # l'ancien modèle en français s'importe toujours ; la colonne du CIN n'est jamais prise
    # pour celle du n°, même placée avant ; décimale après « , », « ٫ » ou « ، »
    from openpyxl import Workbook

    from grader import importers

    def classeur_de(*rangs):
        wb = Workbook()
        for rang in rangs:
            wb.active.append(rang)
        tampon = io.BytesIO()
        wb.save(tampon)
        return tampon.getvalue()

    assert importers.notes_du_classeur(classeur_de(
        ["N° d'inscription", "Prénom", "Nom", "Note / 20"], ["104501", "Sana", "AMRI", 12]),
        20) == [("104501", 12.0)]
    assert importers.notes_du_classeur(classeur_de(
        ["N° CIN", "N°", "Note"], ["01166344", "104501", "14,5"]), 20) == [("104501", 14.5)]
    assert importers.notes_du_classeur(classeur_de(
        ["رقم بطاقة التعريف الوطنية", "رقم التسجيل", "العدد / 20"],
        ["01166344", "104502", "١٢٫٥"], [None, "104503", "13،75"]), 20) == [
        ("104502", 12.5), ("104503", 13.75)]
    assert importers.notes_du_classeur(classeur_de(
        ["رقم بطاقة التعريف الوطنية", "رقم", "العدد"], ["01166344", "104502", 8]), 20) == [
        ("104502", 8.0)]
    print("  oral : ancien modèle en français, colonne du CIN jamais prise pour le n°, "
          "décimale après « , », « ٫ » ou « ، » OK")


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_ecrit").delete()
    ClassGroup.objects.filter(owner__username="prof_ecrit").delete()
    User.objects.filter(username="prof_ecrit").delete()
    prof = enseignant_complet(User.objects.create_user("prof_ecrit", password="x-pass-123"))
    groupe = ClassGroup.objects.create(name="ECRIT 3e A", owner=prof)
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=nom, first_name=prenom,
                               student_number=num)
    c = Client()
    c.force_login(prof)
    print("Épreuve écrite :")
    quiz = test_ecrit(c, prof, groupe)
    test_ancienne_grille(quiz)
    test_ligne_vide_a_verifier(quiz)
    print("Épreuve orale :")
    test_oral(c, groupe)
    print("\n✅ TESTS ÉCRIT ET ORAL RÉUSSIS")


if __name__ == "__main__":
    main()
