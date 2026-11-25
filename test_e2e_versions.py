"""QCM en deux versions, A et B (demande de l'utilisateur, 23 novembre 2026).

La version B a ses questions, et les choix de chacune, dans un autre ordre. « VERSION A »
ou « VERSION B » est imprimé sur le sujet et sur la feuille de réponses, lu au scan (la
version a ses propres codes de page), et chaque copie est corrigée selon sa version :
  1. réglage : refusé avec une fiche de réponses seule (le sujet viendrait d'ailleurs) ;
  2. disposition : pages A puis B ; la version A est la fiche d'une seule version, au
     millimètre ; la version B est la même à chaque génération ; la réponse manuscrite
     garde sa place ;
  3. sujets : la question n° k de la version B et ses choix, dans l'ordre du tirage ;
  4. copies nominatives : A et B alternées, dans l'ordre des salles ;
  5. scan : une copie A et une copie B aux mêmes réponses ont la même note, les réponses
     ramenées à la version A ; un corrigé noirci sur une feuille B se lit juste ;
  6. copies manquantes : un candidat qui a rendu sa copie B n'y est pas ;
  7. questionnaire complet : la fiche A puis la fiche B, chacune téléchargeable seule, les
     choix de la version B imprimés et lus dans leur nouvel ordre.
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
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import layout as L, services
from grader.models import Answer, ClassGroup, Question, Quiz, SheetScan, Student
from test_commun import enseignant_complet
from test_e2e import mm2px, render_page, simulate_scan

QUESTIONS = [  # (n°, intitulé, choix, bonne réponse) ; le n° 4 est une réponse rédigée
    (1, "Capitale de la Tunisie ?", ["Tunis", "Sfax", "Sousse", "Bizerte"], 0),
    (2, "Combien font deux et deux ?", ["trois", "quatre", "cinq", "vingt-deux"], 1),
    (3, "Couleur du ciel par beau temps ?", ["vert", "rouge", "bleu", "jaune"], 2),
    (4, "Expliquez votre démarche.", None, None),
    (5, "Planète dite rouge ?", ["Vénus", "Jupiter", "Saturne", "Mars"], 3),
    (6, "Plus grand océan ?", ["Pacifique", "Atlantique", "Indien", "Arctique"], 0),
    (7, "Jours dans une semaine ?", ["cinq", "six", "sept", "huit"], 2),
]
CORRIGE = {n: bonne for n, _, choix, bonne in QUESTIONS if choix}
COPIE = {**CORRIGE, 6: 1}                    # 5 QCM justes sur 6 (le n° 6 faux)
ETUDIANTS = [("AMRI", "Sana", "104601"), ("BAKIR", "Omar", "104602"),
             ("CHERIF", "Lina", "104603"), ("DRISS", "Ali", "104604")]


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 88])[1].tobytes()


def textes(pdf):
    with pymupdf.open(stream=pdf, filetype="pdf") as doc:
        return [p.get_text() for p in doc]


def noircir(img, page, reponses):
    """Noircit, pour chaque question de la page, la case qui porte le choix voulu (choix de
    la version A) : sur une page B, la case k porte le choix perm[k]."""
    for item in page["qcm"]:
        choix = reponses.get(item["order"])
        if choix is None:
            continue
        k = (item.get("perm") or list(range(len(item["bubbles"])))).index(choix)
        cx, cy = item["bubbles"][k]
        cv2.circle(img, (mm2px(cx), mm2px(cy)), mm2px(L.BUBBLE_R * 0.85), (20, 20, 20), -1)
    return img


def creer_questions(quiz):
    for n, texte, choix, bonne in QUESTIONS:
        if choix:
            Question.objects.create(quiz=quiz, order=n, qtype="qcm", text=texte,
                                    choices=choix, correct_choice=bonne, points=1)
        else:
            Question.objects.create(quiz=quiz, order=n, qtype="open", text=texte, points=2,
                                    open_height_mm=25)


def sans_version(pages):
    return [{k: v for k, v in p.items() if k != "version"} for p in pages]


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_versions").delete()
    ClassGroup.objects.filter(owner__username="prof_versions").delete()
    User.objects.filter(username="prof_versions").delete()
    prof = enseignant_complet(User.objects.create_user("prof_versions", password="x-pass-123"))
    c = Client()
    c.force_login(prof)
    groupe = ClassGroup.objects.create(name="VERSIONS 4A", owner=prof)
    for nom, prenom, num in ETUDIANTS:
        Student.objects.create(class_group=groupe, last_name=nom, first_name=prenom,
                               student_number=num)
    quiz = Quiz.objects.create(title="QCM en deux versions", class_group=groupe, owner=prof,
                               sheet_mode="grid", id_mode="qr", language="fr")
    creer_questions(quiz)
    url = reverse("quiz_detail", args=[quiz.pk])
    print("QCM en deux versions :")

    # ---- 0) à la création d'un concours (25 novembre 2026) : le choix des deux versions
    creer = reverse("quiz_create") + "?concours=1"
    page = c.get(creer).content.decode()
    assert 'name="versions"' in page and "Versions du sujet" in page
    assert 'name="versions"' not in c.get(reverse("quiz_create")).content.decode(), \
        "le choix se fait sur la page du QCM, pour une classe"
    concours = {"type_epreuve": "qcm", "language": "fr", "id_mode": "sticker", "id_digits": "6",
                "anonymat": "", "grading_mode": "immediate", "wrong_penalty": "0", "annee": "1"}
    r = c.post(creer, dict(concours, title="Concours grille", sheet_mode="grid", versions="2"))
    assert r.status_code == 200 and "versions" in r.context["form"].errors
    assert not Quiz.objects.filter(owner=prof, title="Concours grille").exists()
    r = c.post(creer, dict(concours, title="Concours A et B", sheet_mode="split", versions="2"))
    assert r.status_code == 302, r.context["form"].errors
    assert Quiz.objects.get(owner=prof, title="Concours A et B").versions == 2
    c.post(creer, dict(concours, title="Concours sans le champ", sheet_mode="split"))
    assert Quiz.objects.get(owner=prof, title="Concours sans le champ").versions == 1
    c.post(creer, dict(concours, title="Écrit A et B", type_epreuve="ecrit", sheet_mode="split",
                       versions="2"))
    assert Quiz.objects.get(owner=prof, title="Écrit A et B").versions == 1, "écrit : une version"
    print("  création d'un concours : deux versions au choix (QCM avec sujet imprimé), refusées "
          "avec une fiche de réponses seule ; écrit et formulaire d'avant : une seule OK")

    # ---- 1) réglage : pas avec une fiche de réponses seule
    reglages = {"update_settings": "1", "title": quiz.title, "id_mode": "qr", "anonymat": "",
                "wrong_penalty": "0", "id_digits": "6"}
    r = c.post(url, dict(reglages, sheet_mode="grid", versions="2"), follow=True)
    quiz.refresh_from_db()
    assert quiz.versions == 1 and "Deux versions : choisissez" in r.content.decode()
    r = c.post(url, dict(reglages, sheet_mode="split", versions="2"))
    quiz.refresh_from_db()
    assert r.status_code == 302 and (quiz.versions, quiz.sheet_mode) == (2, "split")
    r = c.get(url)
    page = r.content.decode()
    assert 'name="versions"' in page and "Deux versions, A et B" in page
    # avant la génération : l'aperçu de la version B, en regard de la version A
    apercu = [(l["num"], l["question"].order) for l in r.context["version_b"]]
    assert r.context["version_b_apercu"] and "Questions (7) — version B" in page and "Aperçu" in page
    assert [num for num, _ in apercu] == [1, 2, 3, 4, 5, 6, 7], apercu
    print("  réglage : refusé avec une fiche de réponses seule, accepté avec un sujet séparé ; "
          "aperçu de la version B OK")

    # ---- 2) disposition : A puis B, A inchangée, B fixe, la réponse rédigée à sa place
    r = c.post(url, {"generate_pdf": "1"}, follow=True)
    assert "Deux versions, A et B" in r.content.decode(), r.content.decode()[-3000:]
    quiz.refresh_from_db()
    disposition = quiz.layout_json
    assert disposition["versions"] == ["A", "B"]
    pages_a = L.pages_de_version(disposition, "A")
    pages_b = L.pages_de_version(disposition, "B")
    assert len(pages_a) == len(pages_b) == 1 and len(disposition["pages"]) == 2
    assert pages_b[0]["page_index"] == 1, "la version B a ses propres codes de page"
    une_seule = Quiz.objects.get(pk=quiz.pk)
    une_seule.versions = 1
    assert sans_version(pages_a) == L.build_layout(une_seule)["pages"], \
        "la version A doit être la fiche d'une seule version"
    refaite = L.build_layout(quiz)
    assert refaite["pages"] == disposition["pages"] and refaite["version_b"] == disposition["version_b"]
    tirage = disposition["version_b"]
    assert [t["num"] for t in tirage] == [1, 2, 3, 5, 6, 7], tirage
    assert sorted(t["order"] for t in tirage) == [1, 2, 3, 5, 6, 7]
    assert any(t["num"] != t["order"] for t in tirage), "les questions doivent changer d'ordre"
    assert all(sorted(t["perm"]) == [0, 1, 2, 3] for t in tirage)
    assert any(t["perm"] != [0, 1, 2, 3] for t in tirage), "les choix doivent changer d'ordre"
    items_b = pages_b[0]["qcm"]
    assert [(i["num"], i["order"]) for i in items_b] == [(t["num"], t["order"]) for t in tirage]
    assert [i["bubbles"] for i in items_b] == [i["bubbles"] for i in pages_a[0]["qcm"]], \
        "mêmes cases, à la même place, sur les deux feuilles"
    assert [o["order"] for o in pages_b[0]["open"]] == [4], "la réponse rédigée garde sa place"
    print(f"  disposition : pages A puis B (code de page 1), A au millimètre, B fixe ; "
          f"ordre B {[t['order'] for t in tirage]} OK")

    # ---- 3) sujets : la version B dans l'ordre du tirage, choix compris
    par_ordre = {n: (texte, choix) for n, texte, choix, _ in QUESTIONS}
    # la page de l'épreuve : le tableau de la version B (demande du 23 novembre 2026), son
    # ordre, ses choix et sa bonne réponse dans cet ordre ; l'aperçu était ce tirage
    r = c.get(url)
    page = r.content.decode()
    assert not r.context["version_b_apercu"] and "Aperçu" not in page
    assert "Questions (7) — version A" in page and "Questions (7) — version B" in page
    # chaque tableau défile dans son cadre, côte à côte (demande du 23 novembre 2026)
    assert page.count("table-wrap--defile table-wrap--questions") == 2
    assert 'aria-label="Questions de la version A"' in page
    assert 'aria-label="Questions de la version B"' in page
    # documents : le sujet de chaque version, plus celui des deux ensemble ; les copies
    # nominatives restent dans un seul PDF, A et B alternées (demande du 23 novembre 2026)
    assert quiz.subject_pdf.url not in page and "Sujet (questions)" not in page
    assert "Sujet — version A" in page and "Sujet — version B" in page
    # une seule copie du correcteur, celle de la version A (25 novembre 2026, « fait un seul »)
    assert "Copie du correcteur — version" not in page and page.count("✅ Copie du correcteur") == 1
    assert f'href="{quiz.sheet_pdf.url}"' in page and "Copies (A et B alternées)" in page
    assert "Feuille de réponses</a>" not in page and "{#" not in page and "{% comment" not in page
    lignes = r.context["version_b"]
    assert [(l["num"], l["question"].order) for l in lignes] == apercu
    par_num = {t["num"]: t for t in tirage}
    for l in lignes:
        t = par_num.get(l["num"])
        if t is None:                               # la réponse rédigée, à sa place
            assert (l["num"], l["question"].order, l["bonne"]) == (4, 4, None), l
            continue
        assert l["question"].order == t["order"]
        assert l["choix"] == [par_ordre[t["order"]][1][k] for k in t["perm"]]
        assert l["bonne"] == "ABCD"[t["perm"].index(CORRIGE[t["order"]])], (l, t)
    print("  page de l'épreuve : version A à gauche, version B en regard (son ordre, ses "
          "choix, sa bonne réponse) OK")
    sujet_b = "".join(textes(c.get(reverse("quiz_version_pdf", args=[quiz.pk, "B", "sujet"])).content))
    assert "VERSION B" in sujet_b and "VERSION A" not in sujet_b
    position = 0
    for t in tirage:
        texte, choix = par_ordre[t["order"]]
        position = sujet_b.index(f"{t['num']}. {texte}", position)
        for k, origine in enumerate(t["perm"]):
            position = sujet_b.index(f"{'ABCD'[k]}. {choix[origine]}", position)
    assert "4. Expliquez votre démarche." in sujet_b
    sujet_a = "".join(textes(c.get(reverse("quiz_version_pdf", args=[quiz.pk, "A", "sujet"])).content))
    assert "VERSION A" in sujet_a and "1. Capitale de la Tunisie ?" in sujet_a
    assert sujet_a.index("A. Tunis") < sujet_a.index("B. Sfax") < sujet_a.index("D. Bizerte")
    complet = textes(quiz.subject_pdf.read())
    assert "VERSION A" in complet[0] and "VERSION B" in complet[-1], "sujet A puis sujet B"
    assert c.get(reverse("quiz_version_pdf", args=[quiz.pk, "C", "sujet"])).status_code == 404
    assert c.get(reverse("quiz_version_pdf", args=[quiz.pk, "A", "feuille"])).status_code == 404, \
        "copies nominatives : chacune a déjà sa version"
    print("  sujets : version B dans l'ordre du tirage (questions et choix), A inchangée, "
          "« VERSION A / B » imprimé OK")

    # ---- 4) copies nominatives : A, B, A, B dans l'ordre des salles
    pdf = quiz.sheet_pdf.read()
    feuilles = textes(pdf)
    ordre = list(services.etudiants_par_salle(quiz))
    assert len(feuilles) == len(ordre) == 4
    for k, (texte, s) in enumerate(zip(feuilles, ordre)):
        version = "AB"[k % 2]
        assert f"VERSION {version}" in texte and f"version {version}" in texte, (k, texte[:300])
        assert s.last_name in texte
    url_correcteur = reverse("quiz_copie_correcteur_pdf", args=[quiz.pk])
    correcteur = textes(c.get(url_correcteur).content)
    # la copie du correcteur, celle de la version A, sans « VERSION A » imprimé (25 novembre
    # 2026, barré par l'utilisateur) : le scan lit la version au code de page
    assert len(correcteur) == 1 and "CORRIGÉ" in correcteur[0] and "VERSION" not in correcteur[0]
    # une seule copie du correcteur sur la page, celle de la version A (25 novembre 2026,
    # « fait un seul ») ; le lien d'avant vers celle de la B donne encore la B
    page = c.get(url).content.decode()
    assert page.count(f'href="{url_correcteur}"') == 1 and f"{url_correcteur}?version=" not in page
    correcteur_b = c.get(url_correcteur + "?version=B").content
    assert [("VERSION B" in t, "VERSION A" in t) for t in textes(correcteur_b)] == [(True, False)]
    print("  copies nominatives : A, B, A, B dans l'ordre des salles ; une seule copie du "
          "correcteur, en version A OK")

    # ---- 5) scan : mêmes réponses en A et en B, même note, réponses ramenées à la version A
    envois = []
    for k, (s, reponses) in enumerate(zip(ordre[:3], (COPIE, COPIE, CORRIGE))):
        page_v = (pages_a if k % 2 == 0 else pages_b)[0]
        img = noircir(render_page(pdf, k), page_v, reponses)
        envois.append(SimpleUploadedFile(f"{s.last_name}.jpg", jpg(simulate_scan(img))))
    lot = services.process_uploaded_files(quiz, envois, label="Deux versions")
    copies = {f.student.last_name: f for f in lot.sheets.select_related("student")}
    assert len(copies) == 3 and all(f.status == "ok" for f in copies.values()), \
        [(f.status, f.error_message) for f in lot.sheets.all()]
    b = copies[ordre[1].last_name]
    assert b.page_index == 1, "la copie B est reconnue à son code de page"
    lues = dict(Answer.objects.filter(sheet=b, question__qtype="qcm")
                .values_list("question__order", "detected_choice"))
    assert lues == COPIE, (lues, COPIE)
    notes = {r["student"].last_name: r["score"] for r in services.compute_results(quiz)}
    assert notes == {ordre[0].last_name: 5, ordre[1].last_name: 5, ordre[2].last_name: 6}, notes
    assert b.overlay_image, "image de contrôle de la copie B"
    print("  scan : copies A et B aux mêmes réponses notées 5 / 6, réponses de la copie B "
          "ramenées à la version A ; la copie complète 6 / 6 OK")
    # relevé de notes et écran de vérification : la copie B le dit
    releve = c.get(reverse("quiz_bulletins_pdf", args=[quiz.pk]) + f"?student={ordre[1].pk}")
    assert "Copie en version B" in "".join(textes(releve.content))
    releve_a = c.get(reverse("quiz_bulletins_pdf", args=[quiz.pk]) + f"?student={ordre[0].pk}")
    assert "Copie en version" not in "".join(textes(releve_a.content))
    page = c.get(reverse("sheet_review", args=[b.pk])).content.decode()
    assert "Version B" in page and "Copie en version B" in page
    print("  relevé de notes et écran de vérification : « copie en version B » OK")

    # une copie du correcteur B déjà imprimée, noircie en suivant le sujet B : les bonnes
    # réponses de la A, par l'unique envoi du corrigé
    Question.objects.filter(quiz=quiz, qtype="qcm").update(correct_choice=None)
    feuille_b = noircir(render_page(correcteur_b, 0), pages_b[0], CORRIGE)
    c.post(reverse("quiz_answer_key", args=[quiz.pk]),
           {"files": [SimpleUploadedFile("corrige_b.jpg", jpg(simulate_scan(feuille_b)))]})
    lu = dict(quiz.questions.filter(qtype="qcm").values_list("order", "correct_choice"))
    assert lu == CORRIGE, (lu, CORRIGE)
    print("  copie du correcteur B noircie et scannée : les bonnes réponses de la version A OK")
    # un seul corrigé à l'écran (25 novembre 2026, « fait un seul ») : un encart, un envoi, une
    # liste de fiches, la page de chacune comptée dans sa version
    r = c.get(url)
    page = r.content.decode()
    assert "corrige_versions" not in r.context
    assert f"Corrigé complet — {len(CORRIGE)} question(s) QCM" in page
    assert "Corrigé complet — version" not in page and page.count("Lire le corrigé scanné") == 1
    assert [f.page_version for f in r.context["answer_key_sheets"]] == [1]
    # une question sans bonne réponse : son n° dans la version A, celle du tableau à remplir
    t = next(t for t in tirage if t["num"] != t["order"])
    Question.objects.filter(quiz=quiz, order=t["order"]).update(correct_choice=None)
    page = c.get(url).content.decode()
    assert f"Corrigé incomplet — 1 question(s) sur {len(CORRIGE)} sans bonne réponse" in page
    assert f"Questions n° {t['order']}." in page and "Corrigé incomplet — version" not in page
    Question.objects.filter(quiz=quiz, order=t["order"]).update(correct_choice=CORRIGE[t["order"]])
    # une copie du correcteur A dans le même envoi : lue, rangée avec l'autre
    feuille_a = noircir(render_page(c.get(url_correcteur).content, 0), pages_a[0], CORRIGE)
    r = c.post(reverse("quiz_answer_key", args=[quiz.pk]),
               {"files": [SimpleUploadedFile("corrige_a.jpg", jpg(simulate_scan(feuille_a)))]},
               follow=True)
    assert "Fiche déposée" not in r.content.decode()
    assert [f.page_version for f in r.context["answer_key_sheets"]] == [1, 1]
    print("  un seul corrigé : un envoi pour une copie du correcteur A ou B, une liste de "
          "fiches, questions manquantes aux n° de la version A OK")

    # ---- 6) copies manquantes : seul le 4e (pas scanné)
    manquantes = services.copies_manquantes(quiz)
    assert [(m["etudiant"].last_name, m["manquantes"]) for m in manquantes["lignes"]] == [
        (ordre[3].last_name, ["copie"])], manquantes["lignes"]
    print("  copies manquantes : la copie B rendue n'y est pas, le 4e (jamais scanné) y est OK")

    # ---- 7) questionnaire complet, fiche unique : A puis B, chacune seule ; choix lus
    complet_q = Quiz.objects.create(title="Questionnaire en deux versions", class_group=groupe,
                                    owner=prof, sheet_mode="full", id_mode="name",
                                    language="fr", versions=2)
    creer_questions(complet_q)
    c.post(reverse("quiz_detail", args=[complet_q.pk]), {"generate_pdf": "1"})
    complet_q.refresh_from_db()
    d = complet_q.layout_json
    pdf_complet = complet_q.sheet_pdf.read()
    fiche = textes(pdf_complet)
    n_a, n_b = len(L.pages_de_version(d, "A")), len(L.pages_de_version(d, "B"))
    assert len(fiche) == n_a + n_b
    assert all("VERSION A" in t for t in fiche[:n_a]) and all("VERSION B" in t for t in fiche[n_a:])
    page = c.get(reverse("quiz_detail", args=[complet_q.pk])).content.decode()
    assert complet_q.sheet_pdf.url not in page and "Télécharger la fiche" not in page, \
        "fiche unique en deux versions : la fiche de chaque version, pas le PDF des deux"
    assert "Fiche — version A" in page and "Fiche — version B" in page
    seule_b = textes(c.get(reverse("quiz_version_pdf", args=[complet_q.pk, "B", "feuille"])).content)
    assert len(seule_b) == n_b and all("VERSION B" in t for t in seule_b)
    assert c.get(reverse("quiz_version_pdf", args=[complet_q.pk, "B", "sujet"])).status_code == 404
    premiere = d["version_b"][0]
    texte, choix = par_ordre[premiere["order"]]
    texte_b = "".join(seule_b)
    assert texte_b.index(f"1. {texte}") < texte_b.index(choix[premiere["perm"][0]]) \
        < texte_b.index(choix[premiere["perm"][1]]), "les choix de la version B, dans leur ordre"
    page_b = L.pages_de_version(d, "B")[0]
    img = noircir(render_page(pdf_complet, n_a), page_b, CORRIGE)
    lot = services.process_uploaded_files(complet_q, [SimpleUploadedFile(
        "complet_b.jpg", jpg(simulate_scan(img)))], label="Complet B")
    feuille = lot.sheets.get()
    lues = dict(Answer.objects.filter(sheet=feuille, question__qtype="qcm")
                .values_list("question__order", "detected_choice"))
    assert feuille.page_index == page_b["page_index"]
    assert lues == {o: CORRIGE[o] for o in lues} and lues, (lues, CORRIGE)
    print(f"  questionnaire complet : fiche A ({n_a} p.) puis B ({n_b} p.), B seule, choix "
          "de la version B imprimés dans leur ordre et lus justes OK")

    # repasser à une seule version : la fiche est à régénérer
    r = c.post(url, dict(reglages, sheet_mode="split", versions="1"))
    assert c.get(url).context["layout_stale"], "la fiche en deux versions est à régénérer"

    Quiz.objects.filter(owner=prof).delete()
    ClassGroup.objects.filter(owner=prof).delete()
    prof.delete()
    print("\n✅ TEST DU QCM EN DEUX VERSIONS RÉUSSI")


if __name__ == "__main__":
    main()
