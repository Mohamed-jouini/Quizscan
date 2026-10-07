"""Les statistiques d'une épreuve disent-elles la vérité ?

Trois questions, dont une qui vaut toutes les autres :

  1. L'écart-type décrit-il bien la dispersion ?
  2. La répartition montre-t-elle vers quelle case sont parties les fausses ?
  3. **Une bonne réponse erronée est-elle détectée ?** C'est le cas qui
     compte : avec le corrigé lu par scan, une case mal noircie compte toute
     une promotion comme fausse sans que rien ne le signale. L'indice de
     discrimination doit alors devenir négatif — les meilleures copies se
     trompant plus que les plus faibles — et la question être remontée.

On fabrique une promotion dont on connaît d'avance le classement, puis on
vérifie que les chiffres correspondent à ce qu'on a construit.
"""
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

from django.contrib.auth import get_user_model          # noqa: E402
from django.test import Client                          # noqa: E402

from grader import services                             # noqa: E402
from django.test.utils import CaptureQueriesContext           # noqa: E402
from django.db import connection                             # noqa: E402

from grader.models import (Answer, ClassGroup, Question, Quiz,  # noqa: E402
                           ScanBatch, SheetScan, Student)
from test_commun import enseignant_complet              # noqa: E402

N_COPIES = 12          # au-dessus du seuil de calcul de la discrimination
N_QUESTIONS = 6


def _promotion():
    """Une épreuve de 6 questions passée par 12 candidats, classement connu.

    Le candidat i répond juste aux i premières questions, ce qui fixe le
    classement. Les deux dernières questions imitent un corrigé faux, de
    deux façons différentes :

      - question 5 : TOUS cochent « B » alors que le corrigé dit « A ».
        L'indice de discrimination vaut 0 (aucun groupe ne réussit) :
        c'est le détecteur de convergence qui doit parler.
      - question 6 : seules les quatre copies les plus faibles tombent
        sur « A », la réponse enregistrée ; toutes les autres cochent
        « B », la vraie, comptée fausse. La question est donc réussie par
        le bas du classement : l indice devient négatif.
    """
    User = get_user_model()
    Quiz.objects.filter(owner__username="st_prof").delete()
    ClassGroup.objects.filter(name="ST classe").delete()
    User.objects.filter(username="st_prof").delete()

    prof = enseignant_complet(User.objects.create_user("st_prof", password="x"))
    groupe = ClassGroup.objects.create(name="ST classe", owner=prof)
    quiz = Quiz.objects.create(title="ST épreuve", class_group=groupe,
                               owner=prof)
    questions = [
        Question.objects.create(quiz=quiz, order=n, qtype="qcm", points=1,
                                num_choices=4, correct_choice=0)   # « A »
        for n in range(1, N_QUESTIONS + 1)
    ]
    lot = ScanBatch.objects.create(quiz=quiz, label="ST lot", status="done",
                                   total_pages=N_COPIES)

    for i in range(N_COPIES):
        etudiant = Student.objects.create(
            class_group=groupe, last_name=f"CANDIDAT{i:02d}",
            first_name="Test", student_number=f"9{i:05d}")
        copie = SheetScan.objects.create(batch=lot, source_name=f"st{i}.jpg",
                                         status="ok", student=etudiant)
        # Les i premières questions justes : le classement est 0 < 1 < … < 11.
        justes = min(i, N_QUESTIONS - 2)
        for n, question in enumerate(questions, start=1):
            if n == N_QUESTIONS - 1:
                case = 1              # convergence : « B » pour tout le monde
            elif n == N_QUESTIONS:
                # Discrimination négative : seules les copies les plus
                # FAIBLES tombent sur « A » (la réponse enregistrée, fausse)
                # — au hasard. Les autres cochent « B », la vraie, comptée
                # fausse. L indice devient négatif parce que la question est
                # réussie par le bas du classement et ratée par le haut.
                case = 0 if i < N_COPIES // 3 else 1
            elif n <= justes:
                case = 0              # « A », la bonne
            else:
                case = 2              # « C », une fausse
            Answer.objects.create(
                sheet=copie, question=question, detected_choice=case,
                points_awarded=1.0 if case == question.correct_choice else 0.0)
    return quiz, prof


def _gonfler(quiz, combien):
    """Verse des copies notées dans le lot existant.

    Dans le lot EXISTANT, et non dans un nouveau : la mesure qui suit
    compare le coût à volume différent, elle ne doit pas faire varier le
    nombre de lots affichés en même temps.
    """
    lot = quiz.batches.first()
    questions = list(quiz.questions.all())
    for i in range(combien):
        etudiant = Student.objects.create(
            class_group=quiz.class_group, last_name=f"VOLUME{i:03d}",
            first_name="Test", student_number=f"8{i:05d}")
        copie = SheetScan.objects.create(batch=lot, source_name=f"v{i}.jpg",
                                         status="ok", student=etudiant)
        for question in questions:
            # Même schéma que la promotion d origine : « B » sur les deux
            # dernières questions. Des copies toutes justes effaceraient le
            # corrigé douteux que les contrôles précédents viennent
            # d établir, et le test se mordrait la queue.
            case = 1 if question.order >= N_QUESTIONS - 1 else 0
            Answer.objects.create(
                sheet=copie, question=question, detected_choice=case,
                points_awarded=1.0 if case == question.correct_choice else 0.0)


def main():
    logging.disable(logging.ERROR)
    quiz, prof = _promotion()
    lignes = services.compute_results(quiz)
    stats = services.compute_stats(quiz, lignes)
    assert stats and stats["n"] == N_COPIES, f"{stats['n'] if stats else 0} copies"
    par_question = {s["question"].order: s for s in stats["questions"]}

    # ---------------------------------------------------------------- 1
    notes = sorted(r["score"] for r in lignes)
    moyenne = sum(notes) / len(notes)
    attendu = round((sum((x - moyenne) ** 2 for x in notes) / len(notes)) ** 0.5, 2)
    assert stats["stdev"] == attendu, (
        f"écart-type {stats['stdev']}, attendu {attendu}")
    assert stats["stdev"] > 0, "une promotion aux notes étalées a un écart-type"
    print(f"  écart-type     {stats['stdev']} sur des notes de "
          f"{notes[0]:g} à {notes[-1]:g} OK")

    # ---------------------------------------------------------------- 2
    q5 = par_question[N_QUESTIONS - 1]
    cases = {c["lettre"]: c for c in q5["repartition"]}
    assert cases["B"]["n"] == N_COPIES and cases["B"]["pct"] == 100, (
        f"les 12 copies ont coché B, la répartition en voit "
        f"{cases['B']['n']}")
    assert cases["A"]["bonne"] and not cases["B"]["bonne"], (
        "la case marquée « bonne » n'est pas celle du corrigé enregistré")
    assert sum(c["n"] for c in q5["repartition"]) == N_COPIES, (
        "la répartition ne totalise pas les copies")
    print("  répartition    les 12 fausses sont toutes parties sur « B » OK")

    # ---------------------------------------------------------------- 2bis
    # Convergence : l'indice vaut 0 ici, c'est l'autre détecteur qui parle.
    assert q5.get("corrige_suspect"), (
        "une question que personne ne réussit et où tous cochent la même "
        "autre case n'est pas signalée")
    assert "B" in q5["discrimination_aide"], (
        f"l'explication ne nomme pas la case majoritaire : "
        f"{q5['discrimination_aide']!r}")
    print("  convergence    « personne juste, tous sur B » signalé OK")

    # ---------------------------------------------------------------- 2ter
    q6 = par_question[N_QUESTIONS]
    assert q6.get("discrimination") is not None and q6["discrimination"] < 0, (
        f"les meilleures copies se trompent plus que les faibles, l'indice "
        f"devrait être négatif : {q6.get('discrimination')}")
    assert q6.get("corrige_suspect"), "indice négatif non signalé"
    assert N_QUESTIONS in stats["suspectes"] and \
        N_QUESTIONS - 1 in stats["suspectes"], (
        f"les deux questions douteuses devraient être remontées, "
        f"obtenu {stats['suspectes']}")
    print(f"  corrigé faux   question {N_QUESTIONS} à "
          f"{q6['discrimination']:+.2f}, les deux remontées en tête OK")

    # ---------------------------------------------------------------- 3
    q1 = par_question[1]
    assert q1["discrimination"] is not None, (
        "la discrimination n'est pas calculée alors qu'il y a 12 copies")
    assert q1["discrimination"] > 0, (
        f"une question que seuls les bons réussissent doit discriminer "
        f"positivement, obtenu {q1['discrimination']}")
    assert not q1.get("corrige_suspect"), "question 1 signalée à tort"
    print(f"  discrimination question 1 à {q1['discrimination']:+.2f} "
          f"({q1['discrimination_label']}) OK")

    # Sous le seuil, aucun chiffre plutôt qu'un chiffre trompeur.
    petit = services.compute_stats(quiz, lignes[:4])
    assert petit["questions"][0].get("discrimination") is None, (
        "la discrimination est calculée sur 4 copies : elle ne veut rien dire")
    print("  seuil          rien n'est affiché sous dix copies OK")

    # ---------------------------------------------------------------- 4
    # Jusqu'à l'écran : l'alerte et les chiffres doivent y être.
    client = Client()
    client.force_login(prof)
    page = client.get(f"/quiz/{quiz.pk}/resultats/")
    assert page.status_code == 200, f"page des résultats -> {page.status_code}"
    corps = page.content.decode()
    assert "Bonne réponse à vérifier" in corps, (
        "la page n'affiche pas l'alerte sur le corrigé douteux")
    assert "écart-type" in corps, "l'écart-type n'apparaît pas"
    assert "Où sont parties les réponses" in corps, (
        "la répartition des réponses n'apparaît pas")
    assert "Discrimination" in corps, "la colonne discrimination manque"
    print("  page           alerte, écart-type et répartition affichés OK")

    # Et l'export Excel, qui reprend le même dictionnaire.
    classeur = client.get(f"/quiz/{quiz.pk}/resultats.xlsx")
    assert classeur.status_code == 200, f"export -> {classeur.status_code}"
    assert len(classeur.content) > 4000, "classeur anormalement petit"
    print(f"  export Excel   {len(classeur.content) // 1024} Ko produits OK")

    # ---------------------------------------------------------------- 5
    # Le coût ne doit pas suivre le nombre de copies : c'est pour cela que
    # ces chiffres sont agrégés en base plutôt que calculés en Python.
    # Mesuré avant tout autre ajout, pour ne faire varier que le volume.
    client.get("/")                       # première passe : caches tièdes
    with CaptureQueriesContext(connection) as avant:
        client.get("/")
    _gonfler(quiz, 100)
    with CaptureQueriesContext(connection) as apres:
        client.get("/")
    assert len(apres) == len(avant), (
        f"{len(avant)} requêtes avec 12 copies, {len(apres)} avec 112 : "
        "le tableau de bord recharge les copies au lieu d'agréger")
    print(f"  coût           {len(apres)} requêtes, inchangé de 12 à "
          f"112 copies OK")

    # ---------------------------------------------------------------- 6
    # Le tableau de bord : les blocages doivent remonter.
    bord = client.get("/")
    assert bord.status_code == 200, f"tableau de bord -> {bord.status_code}"
    page = bord.content.decode()
    assert "Bonne réponse à vérifier" in page, (
        "le corrigé douteux ne remonte pas au tableau de bord")
    assert "Temps de correction épargné" in page, "le temps épargné manque"
    assert "Dernière épreuve corrigée" in page, "la dernière épreuve manque"

    # Un corrigé incomplet et un lot jamais lancé doivent être signalés.
    sans_corrige = Quiz.objects.create(
        title="ST sans corrigé", class_group=quiz.class_group, owner=prof)
    Question.objects.create(quiz=sans_corrige, order=1, qtype="qcm",
                            points=1, num_choices=4, correct_choice=None)
    ScanBatch.objects.create(quiz=quiz, label="ST lot en attente",
                             status="pending", total_pages=7)
    page = client.get("/").content.decode()
    assert "au corrigé incomplet" in page, (
        "une épreuve sans bonne réponse ne bloque rien à l'écran")
    assert "correction jamais lancée" in page, (
        "un lot en attente depuis des jours n'est pas signalé")
    assert "7 page" in page, "le nombre de pages en attente n'est pas repris"
    print("  tableau de bord blocages et alertes remontés OK")

    print("\n✅ TEST DES STATISTIQUES RÉUSSI")


if __name__ == "__main__":
    main()
