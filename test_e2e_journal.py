"""Le journal des modifications trace-t-il vraiment tout ce qui change une note ?

Pour une épreuve contestée, il faut pouvoir répondre : qui a modifié cette
note, cette attribution, cette bonne réponse, quand, et de quoi à quoi. On
vérifie chaque geste, depuis l'application ET depuis l'administration — un
journal qui laisse passer l'administration a un trou là où on en a le plus
besoin.

On vérifie aussi la suppression d'un lot téléversé par erreur : refusée sans
le droit de corriger, refusée pendant la lecture, et quand elle a lieu, les
fichiers disparaissent du disque mais la trace reste au journal.
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
from django.core.files.base import ContentFile          # noqa: E402
from django.core.files.storage import default_storage   # noqa: E402
from django.test import Client                          # noqa: E402

from grader.models import (Answer, ClassGroup, Modification,  # noqa: E402
                           Question, Quiz, ScanBatch, SheetScan, Student)
from test_commun import enseignant_complet              # noqa: E402


def _gestes(**filtre):
    return list(Modification.objects.filter(**filtre)
                .values_list("action", flat=True))


def main():
    logging.disable(logging.ERROR)
    User = get_user_model()
    Quiz.objects.filter(class_group__name__startswith="JR ").delete()
    ClassGroup.objects.filter(name__startswith="JR ").delete()
    Modification.objects.filter(quiz_titre__startswith="JR ").delete()
    User.objects.filter(username__startswith="jr_").delete()

    prof = enseignant_complet(User.objects.create_user("jr_prof", password="x"))
    lecteur = User.objects.create_user("jr_lecteur", password="x")   # aucun droit
    patron = User.objects.create_superuser("jr_admin", email="", password="x")

    groupe = ClassGroup.objects.create(name="JR classe", owner=prof)
    alice = Student.objects.create(class_group=groupe, last_name="ALICE",
                                   first_name="A", student_number="1")
    bruno = Student.objects.create(class_group=groupe, last_name="BRUNO",
                                   first_name="B", student_number="2")
    quiz = Quiz.objects.create(title="JR épreuve", class_group=groupe, owner=prof)
    qcm = Question.objects.create(quiz=quiz, order=1, qtype="qcm", points=1,
                                  num_choices=4, correct_choice=0)
    redac = Question.objects.create(quiz=quiz, order=2, qtype="open", points=4)
    lot = ScanBatch.objects.create(quiz=quiz, label="JR lot", status="done",
                                   total_pages=1)
    # Copie non identifiée : c'est la seule qu'on affecte à la main (une
    # copie reconnue par son QR ou son n° garde son étudiant).
    copie = SheetScan.objects.create(batch=lot, source_name="jr.jpg",
                                     status="no_match", student=None)
    copie.image.save("jr.jpg", ContentFile(b"faux scan"), save=True)
    r_qcm = Answer.objects.create(sheet=copie, question=qcm, detected_choice=1,
                                  points_awarded=0.0)
    r_redac = Answer.objects.create(sheet=copie, question=redac,
                                    points_awarded=None)

    c = Client()
    c.force_login(prof)

    # ---------------------------------------------------------------- 1
    # Vérification d'une copie : attribution et note manuscrite.
    envoi = {"student": bruno.pk, f"points_{r_redac.pk}": "3"}
    assert c.post(f"/copies/{copie.pk}/", envoi).status_code == 302
    gestes = _gestes(copie=copie)
    assert "identification" in gestes, "réattribution d'une copie non tracée"
    assert "note" in gestes, "note manuscrite non tracée"
    entree = Modification.objects.get(copie=copie, action="identification")
    assert entree.avant == "non attribuée" and entree.apres == "BRUNO B", (
        f"valeurs mal tracées : {entree.avant!r} → {entree.apres!r}")
    assert entree.auteur_nom == "jr_prof", f"auteur : {entree.auteur_nom!r}"
    note = Modification.objects.get(copie=copie, action="note")
    assert (note.avant, note.apres) == ("—", "3"), (note.avant, note.apres)

    # Renvoyer le même formulaire ne doit rien ajouter — ni réaffecter la
    # copie, désormais identifiée, même si l'envoi désigne quelqu'un d'autre.
    avant = Modification.objects.count()
    c.post(f"/copies/{copie.pk}/", envoi)
    c.post(f"/copies/{copie.pk}/", {"student": alice.pk,
                                    f"points_{r_redac.pk}": "3"})
    assert Modification.objects.count() == avant, (
        "un formulaire renvoyé tel quel a rempli le journal de non-événements")
    copie.refresh_from_db()
    assert copie.student_id == bruno.pk, "une copie identifiée a été réaffectée"
    print("  copie          attribution et note tracées, rien de plus OK")

    # ---------------------------------------------------------------- 2
    # Épreuve : bonne réponse, barème, pénalité, suppression de question.
    c.post(f"/quiz/{quiz.pk}/", {"edit_question": qcm.pk, "correct": "2",
                                 "points": "2"})
    c.post(f"/quiz/{quiz.pk}/", {"update_settings": "1", "wrong_penalty": "0,5",
                                 "sheet_mode": quiz.sheet_mode,
                                 "id_mode": quiz.id_mode, "id_digits": "6"})
    c.post(f"/quiz/{quiz.pk}/", {"delete_question": redac.pk})
    gestes = _gestes(quiz=quiz, copie=None)
    for attendu in ("bonne_reponse", "bareme", "penalite", "question_supprimee"):
        assert attendu in gestes, f"« {attendu} » absent du journal : {gestes}"
    bonne = Modification.objects.get(quiz=quiz, action="bonne_reponse")
    assert (bonne.avant, bonne.apres) == ("A", "C"), (bonne.avant, bonne.apres)
    print("  épreuve        bonne réponse, barème, pénalité, suppression OK")

    # ---------------------------------------------------------------- 3
    # La réponse lue sur un QCM ne se modifie pour personne, administrateur
    # compris : ni par l'écran de vérification, ni par l'administration de
    # Django, qui court-circuite les vues.
    a = Client()
    a.force_login(patron)
    page = a.get(f"/copies/{copie.pk}/").content.decode()
    assert f'name="choice_{r_qcm.pk}"' not in page, "liste « Correction » affichée"
    r_qcm.refresh_from_db()
    points_avant = r_qcm.points_awarded   # pénalité de la section 2 comprise
    a.post(f"/copies/{copie.pk}/", {f"choice_{r_qcm.pk}": "2"})
    a.post(f"/admin/grader/answer/{r_qcm.pk}/change/", {
        "detected_choice": "3", "points_awarded": "1", "fill_ratios": "null"})
    r_qcm.refresh_from_db()
    assert r_qcm.detected_choice == 1 and r_qcm.points_awarded == points_avant, (
        f"réponse de QCM modifiée : case {r_qcm.detected_choice}, "
        f"{r_qcm.points_awarded} pt")
    assert not Modification.objects.filter(copie=copie, action="case_qcm").exists()
    assert a.get("/admin/grader/answer/add/").status_code == 403, \
        "une réponse ne doit pas pouvoir être saisie à la main"
    print("  administration réponse de QCM non modifiable, même par l'administrateur OK")

    # ---------------------------------------------------------------- 4
    # Le journal ne se retouche pas, même par un administrateur.
    entree = Modification.objects.filter(quiz=quiz).first()
    for url in ("/admin/grader/modification/add/",
                f"/admin/grader/modification/{entree.pk}/delete/"):
        assert a.get(url).status_code == 403, f"{url} accessible"
    a.post(f"/admin/grader/modification/{entree.pk}/change/",
           {"avant": "falsifié", "apres": "falsifié"})
    entree.refresh_from_db()
    assert entree.avant != "falsifié", "le journal a été modifié"
    assert a.get("/admin/grader/modification/").status_code == 200
    print("  inaltérable    ni ajout, ni modification, ni suppression OK")

    # ---------------------------------------------------------------- 5
    # Visible là où on en a besoin.
    assert "Historique de cette copie" in c.get(f"/copies/{copie.pk}/").content.decode()
    assert "Journal de l'épreuve" in c.get(f"/quiz/{quiz.pk}/").content.decode()
    print("  affichage      sur la copie et sur l'épreuve OK")

    # ---------------------------------------------------------------- 6
    # Suppression d'un lot.
    fichier = copie.image.name
    assert default_storage.exists(fichier)
    l = Client()
    l.force_login(lecteur)
    quiz.owner = lecteur
    quiz.save(update_fields=["owner"])
    assert l.post(f"/lots/{lot.pk}/supprimer/").status_code == 403, (
        "un compte sans le droit de corriger a supprimé un lot")
    quiz.owner = prof
    quiz.save(update_fields=["owner"])

    lot.status = "processing"
    lot.save(update_fields=["status", "updated_at"])
    c.post(f"/lots/{lot.pk}/supprimer/")
    assert ScanBatch.objects.filter(pk=lot.pk).exists(), (
        "un lot en cours de lecture a été supprimé")
    lot.status = "done"
    lot.save(update_fields=["status", "updated_at"])

    assert c.get(f"/lots/{lot.pk}/supprimer/").status_code == 200
    envoi = c.post(f"/lots/{lot.pk}/supprimer/")
    assert envoi.status_code == 302, f"suppression -> {envoi.status_code}"
    assert not ScanBatch.objects.filter(pk=lot.pk).exists(), "lot toujours là"
    assert not default_storage.exists(fichier), "le scan est resté sur le disque"

    trace = Modification.objects.filter(action="lot_supprime",
                                        quiz_titre="JR épreuve").first()
    assert trace and "1 page" in trace.avant, "la suppression n'est pas tracée"
    # Les entrées de la copie disparue survivent, en clair.
    orphelines = Modification.objects.filter(quiz=quiz, copie=None,
                                             action="identification")
    assert orphelines.exists(), (
        "les entrées d'une copie supprimée ont disparu avec elle")
    print("  suppression    droit, lecture en cours, fichiers, trace OK")

    print("\n✅ TEST DU JOURNAL DES MODIFICATIONS RÉUSSI")


if __name__ == "__main__":
    main()
