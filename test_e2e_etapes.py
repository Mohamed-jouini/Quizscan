"""Concours en plusieurs étapes : les admis au QCM passent à l'écrit, les
admis à l'écrit passent à l'oral.
"""
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import importlib
import logging

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "quizscan.settings")
os.environ["QUIZSCAN_TEST"] = "1"
django.setup()
from django.core.management import call_command  # noqa: E402
call_command("migrate", verbosity=0)

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import setup_test_environment
from django.urls import reverse

from grader import services
from grader.models import (Answer, ClassGroup, Question, Quiz, ScanBatch, SheetScan,
                           Student)
from test_commun import enseignant_complet

# nom : bonnes réponses au QCM (sur 10)
CANDIDATS = {"AMRI": 9, "BAKIR": 8, "CHERIF": 7, "DRISS": 5, "ESSID": 4, "FERCHICHI": 2}


def _noter(quiz, notes):
    """Copies déjà corrigées : {étudiant: note} sur la question « Note »
    (écrit / oral) ou {étudiant: nb de bonnes réponses} (QCM)."""
    lot = ScanBatch.objects.create(quiz=quiz, status="done", total_pages=len(notes))
    for s, note in notes.items():
        copie = SheetScan.objects.create(batch=lot, status="ok", student=s,
                                         source_name=f"{s.last_name}.jpg")
        if quiz.est_qcm:
            for n, q in enumerate(quiz.questions.order_by("order"), start=1):
                a = Answer(sheet=copie, question=q, detected_choice=0 if n <= note else 1)
                a.points_awarded = services._auto_points(q, a)
                a.save()
        else:
            Answer.objects.create(sheet=copie, question=services.question_note(quiz),
                                  points_awarded=note, manually_set=True)


def main():
    logging.disable(logging.WARNING)
    setup_test_environment()
    Quiz.objects.filter(owner__username="prof_etapes").delete()
    ClassGroup.objects.filter(owner__username="prof_etapes").delete()
    User.objects.filter(username="prof_etapes").delete()
    prof = enseignant_complet(User.objects.create_user("prof_etapes", password="x-pass-123"))
    c = Client()
    c.force_login(prof)

    # ---- étape 1 : QCM du concours, note minimale 6 / 10 -> 3 admis
    groupe = ClassGroup.objects.create(name="Concours — Étapes QCM", owner=prof)
    qcm = Quiz.objects.create(title="Étapes QCM", class_group=groupe, owner=prof,
                              auto_enroll=True, id_mode="sticker", note_admission=6)
    for i in range(1, 11):
        Question.objects.create(quiz=qcm, order=i, qtype="qcm", correct_choice=0)
    cands = {nom: Student.objects.create(class_group=groupe, last_name=nom,
                                         first_name="X", student_number=f"55{i:02d}")
             for i, nom in enumerate(CANDIDATS)}
    _noter(qcm, {cands[n]: v for n, v in CANDIDATS.items()})
    assert [s.last_name for s in services.admis_de(qcm)] == ["AMRI", "BAKIR", "CHERIF"]
    page = c.get(reverse("quiz_results", args=[qcm.pk])).content.decode()
    assert "Créer l'écrit des 3 admis" in page and "Créer l'oral" not in page
    print("  QCM : 3 admis ; « Créer l'écrit des 3 admis » seul sur la page des résultats OK")

    # ---- étape 2 : l'écrit, créé depuis ce bouton (formulaire prérempli)
    url = reverse("quiz_create") + f"?concours=1&precedente={qcm.pk}&type=ecrit"
    form = c.get(url).context["form"]
    assert form.initial["epreuve_precedente"] == qcm.pk
    assert form.initial["type_epreuve"] == "ecrit"
    r = c.post(url, {"title": "Étapes écrit", "type_epreuve": "ecrit",
                     "epreuve_precedente": qcm.pk, "language": "fr", "note_sur": "20",
                     "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
                     "grading_mode": "immediate", "wrong_penalty": "0"}, follow=True)
    ecrit = Quiz.objects.get(title="Étapes écrit")
    assert ecrit.epreuve_precedente_id == qcm.pk and ecrit.class_group_id != groupe.pk
    inscrits = sorted(ecrit.class_group.students.values_list("last_name", flat=True))
    assert inscrits == ["AMRI", "BAKIR", "CHERIF"], inscrits
    assert ecrit.class_group.students.get(last_name="AMRI").student_number == "5500"
    assert "3 admis de « Étapes QCM » ajoutés" in r.content.decode()
    page = c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    # liste à jour : pas d'encart ni de bouton, l'en-tête rappelle l'origine
    assert "candidats : admis" in page and "du QCM</a>" in page and "Mettre à jour la liste" not in page
    print("  écrit : liste des candidats = les 3 admis du QCM OK")

    # ---- étape 3 : l'oral (épreuve de classe), admis de l'écrit (≥ 10 / 20)
    e = {s.last_name: s for s in ecrit.class_group.students.all()}
    ecrit.note_admission = 10
    ecrit.save(update_fields=["note_admission"])
    _noter(ecrit, {e["AMRI"]: 14.5, e["BAKIR"]: 8.0, e["CHERIF"]: 12.25})
    page = c.get(reverse("quiz_results", args=[ecrit.pk])).content.decode()
    assert "Créer l'oral des 2 admis" in page and "Créer l'écrit" not in page
    r = c.post(reverse("quiz_create") + f"?precedente={ecrit.pk}&type=oral",
               {"title": "Étapes oral", "type_epreuve": "oral",
                "epreuve_precedente": ecrit.pk, "language": "fr", "note_sur": "20",
                "sheet_mode": "grid", "id_mode": "grid", "id_digits": "6",
                "wrong_penalty": "0"})
    assert r.status_code == 302, r.context["form"].errors
    oral = Quiz.objects.get(title="Étapes oral")
    inscrits = sorted(oral.class_group.students.values_list("last_name", flat=True))
    assert inscrits == ["AMRI", "CHERIF"], inscrits
    page = c.get(reverse("quiz_detail", args=[oral.pk])).content.decode()
    assert "AMRI X" in page and "BAKIR" not in page.split("Notes de l'oral")[1]
    print("  oral : liste des candidats = les 2 admis de l'écrit OK")

    # ---- la note minimale du QCM baisse : un nouvel admis à faire passer
    qcm.note_admission = 5
    qcm.save(update_fields=["note_admission"])
    page = c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    assert "1 nouvel admis" in page, "nouvel admis non signalé"
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"passer_admis": "1"})
    assert ecrit.class_group.students.filter(last_name="DRISS").exists()
    c.post(reverse("quiz_detail", args=[ecrit.pk]), {"passer_admis": "1"})
    assert ecrit.class_group.students.count() == 4, "doublon"
    page = c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    assert "Mettre à jour la liste" not in page and "nouvel admis" not in page
    print("  note minimale baissée : nouvel admis signalé, ajouté, sans doublon OK")

    # ---- résultats de tout le concours, depuis n'importe quelle étape
    oral.note_admission = 10
    oral.save(update_fields=["note_admission"])
    o = {s.last_name: s for s in oral.class_group.students.all()}
    _noter(oral, {o["AMRI"]: 15, o["CHERIF"]: 9})
    # dernière étape : rien à créer après l'oral
    page = c.get(reverse("quiz_results", args=[oral.pk])).content.decode()
    assert "Créer l'" not in page and "Étape suivante" not in page
    attendu = {"AMRI": "Admis au concours", "CHERIF": "Non admis : Oral",
               "BAKIR": "Non admis : Écrit", "DRISS": "Non noté : Écrit",
               "ESSID": "Non admis : QCM", "FERCHICHI": "Non admis : QCM"}
    for depart in (qcm, ecrit, oral):
        r = c.get(reverse("quiz_parcours", args=[depart.pk]))
        assert r.status_code == 200
        p = r.context["parcours"]
        assert [q.pk for q in p["epreuves"]] == [qcm.pk, ecrit.pk, oral.pk]
        finals = {x["etudiant"].last_name: x["final"] for x in p["candidats"]}
        assert finals == attendu, finals
        assert p["n_admis_final"] == 1
    ordre = [x["etudiant"].last_name for x in p["candidats"]]
    assert ordre == ["AMRI", "CHERIF", "BAKIR", "DRISS", "ESSID", "FERCHICHI"], ordre
    page = r.content.decode()
    assert "Admis au concours" in page and "non convoqué" in page
    assert [x["etat"] for x in p["candidats"][-1]["cases"]] == ["note", "non_convoque", "non_convoque"]
    # la recherche filtre, l'export Excel reprend tout
    r = c.get(reverse("quiz_parcours", args=[ecrit.pk]) + "?q=driss")
    assert [x["etudiant"].last_name for x in r.context["candidats_page"]] == ["DRISS"]
    page = r.content.decode()
    assert 'action="#liste-candidats"' in page and "table-wrap--defile" in page
    assert "1 résultat pour « driss »" in page
    assert r.context["candidats_page"].paginator.per_page == 1000
    r = c.get(reverse("quiz_parcours", args=[ecrit.pk]) + "?format=xlsx")
    assert r["Content-Type"].startswith("application/vnd.openxmlformats")
    import io
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(r.content)).active
    assert ws.cell(row=2, column=ws.max_column).value == "Résultat final"
    assert ws.cell(row=3, column=1).value == "AMRI"
    assert ws.cell(row=3, column=ws.max_column).value == "Admis au concours"
    assert ws.max_row == 2 + len(attendu)
    # export PDF : tous les candidats, malgré une recherche, chacun avec son résultat
    import pymupdf
    page = c.get(reverse("quiz_parcours", args=[ecrit.pk])).content.decode()
    assert 'href="?format=pdf"' in page, "bouton « Exporter en PDF » absent"
    r = c.get(reverse("quiz_parcours", args=[ecrit.pk]) + "?q=driss&format=pdf")
    assert r["Content-Type"] == "application/pdf"
    assert f'filename="concours_{qcm.pk}.pdf"' in r["Content-Disposition"]

    def lire_pdf(octets):
        with pymupdf.open(stream=octets, filetype="pdf") as doc:
            return [(p.get_text(), p.get_text("words"), p.rect) for p in doc]

    pages = lire_pdf(r.content)
    texte = "".join(p[0] for p in pages)
    assert len(pages) == 1 and "Résultats du concours" in texte and qcm.title in texte
    assert "1 admis au concours sur 6 candidats" in texte, texte[:400]
    for nom, final in attendu.items():
        assert nom in texte and final in texte, (nom, final)
    assert texte.index("AMRI") < texte.index("CHERIF") < texte.index("FERCHICHI"), "même ordre que la page"
    assert "14,50 / 20" in texte and "9 / 10" in texte and "non convoqué" in texte
    # beaucoup de candidats : pages numérotées, intitulés répétés, rien hors de la page
    from grader import bulletin as bulletin_mod
    from grader.models import Student as Etudiant
    long_nom = Etudiant(last_name="ABDELLATIF BEN MOHAMED EL KEFI ZOUARI", first_name="Mohamed Salah Amine",
                        student_number="5599")
    grand = dict(p, candidats=p["candidats"] * 30
                 + [dict(p["candidats"][0], etudiant=long_nom, final="Admis : QCM — étape suivante à venir")])
    pages = lire_pdf(bulletin_mod.generate_parcours_pdf(grand))
    assert len(pages) >= 8, len(pages)
    assert f"page 1 / {len(pages)}" in pages[0][0] and f"page {len(pages)} / {len(pages)}" in pages[-1][0]
    assert "(suite)" in pages[1][0] and "Résultat final" in pages[1][0]
    assert "ZOUARI" in pages[-1][0] and "suivante à venir" in pages[-1][0]
    for _, mots, rect in pages:
        assert all(m[0] >= 0 and m[2] <= rect.width and m[3] <= rect.height for m in mots)
        noms = [m for m in mots if m[4] == "ZOUARI"]
        assert not noms or noms[0][2] < (14 + 54) * 72 / 25.4, "le nom déborde sur la colonne suivante"
    assert lire_pdf(bulletin_mod.generate_parcours_pdf(dict(p, candidats=[])))[0][0].count("Aucun candidat") == 1
    # retour vers la liste des résultats ; l'épreuve reste à un clic (titre)
    page = c.get(reverse("quiz_results", args=[ecrit.pk])).content.decode()
    assert f'href="{reverse("resultats_list")}">← Retour aux résultats' in page
    assert f'href="{reverse("quiz_detail", args=[ecrit.pk])}" title="Ouvrir l' in page
    # chaque page d'une étape : le nom du concours, et la barre de ses étapes, une fois
    for vue, etape in (("quiz_results", q) for q in (qcm, ecrit, oral)):
        page = c.get(reverse(vue, args=[etape.pk])).content.decode()
        assert page.count("Résultats du concours") == 1, (vue, etape.title)
        assert page.count('aria-current="step"') == 1 and "Cette page ne montre" not in page
        for autre in {qcm, ecrit, oral} - {etape}:
            assert f'href="{reverse(vue, args=[autre.pk])}"' in page, (vue, etape.title, autre.title)
    for etape in (qcm, ecrit, oral):
        page = c.get(reverse("quiz_detail", args=[etape.pk])).content.decode()
        assert f"<h1>{qcm.title}</h1>" in page, etape.title
        assert page.count("Résultats du concours") == 1, etape.title
        assert reverse("quiz_parcours", args=[etape.pk]) in page
        for autre in {qcm, ecrit, oral} - {etape}:
            assert f'href="{reverse("quiz_detail", args=[autre.pk])}"' in page, (etape.title, autre.title)
    # le titre complet d'une étape reste modifiable sur sa page
    assert f'value="{ecrit.title}"' in c.get(reverse("quiz_detail", args=[ecrit.pk])).content.decode()
    # une étape au titre à elle : il reste écrit en clair à côté du nom du concours
    assert services.fil_etapes(ecrit)["intitule"] == "Étapes écrit"
    # une épreuve seule : ni barre ni « Résultats du concours », son titre tel quel
    seul = Quiz.objects.create(title="Hors concours", class_group=groupe, owner=prof)
    assert services.fil_etapes(seul) is None
    for vue in ("quiz_results", "quiz_detail"):
        page = c.get(reverse(vue, args=[seul.pk])).content.decode()
        assert "Résultats du concours" not in page and "Hors concours" in page and "fil-etapes" not in page, vue
    print("  résultats du concours : parcours de chaque candidat, résultat final, Excel OK")

    # ---- listes Concours et Résultats : un seul bloc par concours, ses étapes dedans
    for nom in ("concours_list", "resultats_list"):
        r = c.get(reverse(nom))
        blocs = [g for g in r.context["concours"]
                 if {qcm.pk, ecrit.pk, oral.pk} & {e["quiz"].pk for e in g["etapes"]}]
        assert len(blocs) == 1, (nom, len(blocs))
        assert blocs[0]["titre"] == "Étapes QCM" and blocs[0]["multi"]
        assert [e["quiz"].pk for e in blocs[0]["etapes"]] == [qcm.pk, ecrit.pk, oral.pk]
        # un titre qui dit autre chose que celui du concours reste affiché
        assert [e["intitule"] for e in blocs[0]["etapes"]] == ["", "Étapes écrit", "Étapes oral"]
        page = r.content.decode()
        assert "3 étapes" in page and "Résultats du concours" in page, nom
        assert page.count(f'title="{qcm.title}"') == 1, nom
    page = c.get(reverse("resultats_list")).content.decode()
    assert f'href="/quiz/{oral.pk}/resultats/"' in page and "Copies notées" in page
    page = c.get(reverse("concours_list")).content.decode()
    assert f'href="/classes/{ecrit.class_group_id}/"' in page and "Candidats &amp; QR" in page
    # une boucle de liens (Administration) ne fait disparaître aucune épreuve
    boucle = [Quiz(pk=1, title="a", epreuve_precedente_id=2), Quiz(pk=2, title="b", epreuve_precedente_id=1),
              Quiz(pk=3, title="seule")]
    groupes = services.groupes_epreuves(boucle)
    assert sorted(e["quiz"].pk for g in groupes for e in g["etapes"]) == [1, 2, 3]
    assert groupes[0]["titre"] == "seule" and not groupes[0]["multi"]
    # le titre proposé part de celui du concours, pas de l'étape précédente
    form = c.get(reverse("quiz_create") + f"?precedente={ecrit.pk}&type=oral").context["form"]
    assert form.initial["title"] == "Étapes QCM — oral", form.initial["title"]
    # le tableau de bord aussi : un seul bloc, chaque étape mène à ses résultats
    page = c.get("/").content.decode()
    assert "3 étapes" in page and page.count(f'title="{qcm.title}"') == 1
    for q in (qcm, ecrit, oral):
        assert f'href="/quiz/{q.pk}/resultats/"' in page, q.title
    assert f"<b>{oral.title}" not in page, "le titre complet d'une étape ne remonte pas"
    print("  listes : un bloc par concours, ses étapes dedans, titre « concours — oral » OK")

    # ---- chaque épreuve est un bouton : tout le bloc mène à l'épreuve ou à ses résultats
    concours_seul = Quiz.objects.create(title="Concours seul", class_group=groupe, owner=prof,
                                        auto_enroll=True)
    page = c.get(reverse("concours_list")).content.decode()
    assert (f'<a class="carte-bouton__lien" href="/quiz/{concours_seul.pk}/">Concours seul</a>'
            in page)
    assert f'href="/classes/{groupe.pk}/"' in page, "« Candidats & QR » a disparu du bouton"
    resultats = c.get(reverse("resultats_list")).content.decode()
    for q in (concours_seul, seul):
        assert (f'<a class="carte-bouton__lien" href="/quiz/{q.pk}/resultats/">{q.title}</a>'
                in resultats), q.title
    assert "Voir les résultats" not in resultats, "un bouton dans le bouton"
    # une étape aussi, et un seul lien par bloc : c'est lui qui s'étend sur tout le bloc
    assert f'carte-bouton__lien" title="{qcm.title}" href="/quiz/{qcm.pk}/resultats/"' in resultats
    for texte in (page, resultats):
        blocs = re.findall(r'class="[^"]*\bcarte-bouton\b[^"]*"', texte)
        assert blocs and len(blocs) == texte.count("carte-bouton__lien")
    concours_seul.delete()
    print("  listes : chaque épreuve est un bouton, tout le bloc cliquable OK")

    # ---- page Classes : les mêmes boutons, pour les classes et leurs quiz
    derniere = ClassGroup.objects.create(name="Boutons Z", owner=prof)
    premiere = ClassGroup.objects.create(name="Boutons A", owner=prof)
    Student.objects.create(class_group=premiere, last_name="UN", first_name="U", student_number="901")
    quiz_classe = Quiz.objects.create(title="Quiz boutons", class_group=premiere, owner=prof)
    page = c.get(reverse("class_list")).content.decode()
    assert f'<a class="carte-bouton__lien" href="/classes/{premiere.pk}/">Boutons A</a>' in page
    assert f'<a class="carte-bouton__lien" href="/quiz/{quiz_classe.pk}/">Quiz boutons</a>' in page
    assert "Étudiants &amp; QR" in page and "<table" not in page
    assert page.index(">Boutons A<") < page.index(">Boutons Z<"), "classes par ordre alphabétique"
    blocs = re.findall(r'class="[^"]*\bcarte-bouton\b[^"]*"', page)
    assert blocs and len(blocs) == page.count("carte-bouton__lien")
    # un concours garde son libellé, une classe n'en a pas d'autre ; créer une classe marche toujours
    assert "Candidats &amp; QR" in c.get(reverse("concours_list")).content.decode()
    r = c.post(reverse("class_list"), {"name": "Boutons créée"})
    assert r.status_code == 302 and ClassGroup.objects.filter(name="Boutons créée", owner=prof).exists()
    quiz_classe.delete()
    ClassGroup.objects.filter(name__startswith="Boutons ", owner=prof).delete()
    print("  page Classes : classes et quiz en boutons, création d'une classe OK")

    # ---- espace public : un bloc par concours, ses étapes dedans, rien de non publié
    cache.clear()
    anonyme = Client()

    def mes_resultats(numero):
        return anonyme.get(reverse("public_resultat"), {"numero": numero}).content.decode()

    Quiz.objects.filter(pk__in=[qcm.pk, ecrit.pk]).update(resultats_publies=True)
    page = mes_resultats("5500")
    assert page.count('class="groupe__titre"') == 1 and "2 étapes" in page
    assert ">Oral<" not in page and "<b>15</b>" not in page, "l'oral n'est pas publié"
    Quiz.objects.filter(pk=oral.pk).update(resultats_publies=True)
    page = mes_resultats("5500")
    assert page.count('class="groupe__titre"') == 1 and "3 étapes" in page
    assert page.index(">QCM<") < page.index(">Écrit<") < page.index(">Oral<")
    for note in ("<b>9</b> / 10 bonnes réponses", "<b>14,50</b> / 20", "<b>15</b> / 20"):
        assert note in page, note
    # éliminé à l'écrit : ses deux étapes seulement, et le verdict de chacune
    page = mes_resultats("5501")
    assert "2 étapes" in page and ">Oral<" not in page
    assert page.count(">Admis<") == 1 and page.count(">Non admis<") == 1
    # accueil : une seule ligne pour le concours, un bouton par étape
    page = anonyme.get(reverse("public_accueil")).content.decode()
    assert page.count("<b>Étapes QCM</b>") == 1 and "<b>Étapes oral</b>" not in page
    for q in (qcm, ecrit, oral):
        assert f'href="{reverse("public_epreuve", args=[q.pk])}"' in page, q.title
    Quiz.objects.filter(pk__in=[qcm.pk, ecrit.pk, oral.pk]).update(resultats_publies=False)
    assert "Aucun résultat publié" in mes_resultats("5500")
    cache.clear()
    print("  espace public : un bloc par concours, étapes publiées seules, une ligne à l'accueil OK")

    # une épreuve d'un collègue n'est pas proposée comme épreuve précédente
    autre = enseignant_complet(User.objects.get_or_create(username="prof_etapes2")[0])
    c2 = Client()
    c2.force_login(autre)
    form = c2.get(reverse("quiz_create")).context["form"]
    assert qcm not in form.fields["epreuve_precedente"].queryset
    assert c2.get(reverse("quiz_parcours", args=[ecrit.pk])).status_code == 404
    print("  épreuve d'un collègue : jamais proposée OK")

    # ---- QCM -> écrit -> oral : un QCM ne suit aucune épreuve, chaque type a la sienne
    for titre, type_epreuve, precedente in (("QCM relié", "qcm", qcm),
                                            ("Écrit après écrit", "ecrit", ecrit),
                                            ("Écrit après oral", "ecrit", oral),
                                            ("Oral après QCM", "oral", qcm)):
        r = c.post(reverse("quiz_create") + "?concours=1",
                   {"title": titre, "type_epreuve": type_epreuve,
                    "epreuve_precedente": precedente.pk, "language": "fr", "note_sur": "20",
                    "sheet_mode": "grid", "id_mode": "sticker", "id_digits": "6",
                    "grading_mode": "immediate", "wrong_penalty": "0"})
        assert r.status_code == 200, (titre, r.status_code)
        assert "epreuve_precedente" in r.context["form"].errors, titre
        assert not Quiz.objects.filter(title=titre).exists(), titre
    # le type de chaque épreuve est dans la page : pages.js filtre la liste
    page = c.get(reverse("quiz_create")).content.decode()
    assert f'value="{qcm.pk}" data-type="qcm"' in page
    assert f'value="{ecrit.pk}" data-type="ecrit"' in page
    assert 'id="row_epreuve_precedente"' in page

    # la migration retire le lien des QCM déjà reliés, candidats gardés, et rien d'autre
    relie = Quiz.objects.create(title="QCM déjà relié", class_group=groupe, owner=prof,
                                epreuve_precedente=qcm)
    migration = importlib.import_module("grader.migrations.0038_qcm_sans_epreuve_precedente")
    avant = groupe.students.count()
    with connection.cursor() as curseur:
        curseur.execute(migration.Migration.operations[0].sql)
    relie.refresh_from_db()
    ecrit.refresh_from_db()
    oral.refresh_from_db()
    assert relie.epreuve_precedente_id is None and groupe.students.count() == avant
    assert ecrit.epreuve_precedente_id == qcm.pk and oral.epreuve_precedente_id == ecrit.pk
    assert [q.pk for q in services.chaine_epreuves(relie)] == [relie.pk]
    print("  étapes : un QCM ne suit aucune épreuve, écrit après QCM, oral après écrit OK")
    print("\n✅ TEST DES CONCOURS EN PLUSIEURS ÉTAPES RÉUSSI")


if __name__ == "__main__":
    main()
