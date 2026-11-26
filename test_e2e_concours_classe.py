"""La liste de candidats d'un concours n'est pas une classe (demande du 23 novembre 2026 :
« lorsque fait un concours ne pas créer une classe »).

Créer un concours lui fabrique sa liste de candidats (un ClassGroup, une par étape). Elle ne
paraît plus parmi les classes : ni au tableau de bord (ses candidats y sont comptés à part),
ni dans le menu « Classe » d'une épreuve de classe, ni dans l'administration (liste des
classes, « Classes de cet enseignant »). La fiche d'un enseignant la propose à part
(« Concours de cet enseignant ») pour lui confier le concours. Supprimer le concours depuis
l'administration supprime sa liste, qui passerait sinon pour une classe.
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
from django.test.utils import setup_test_environment    # noqa: E402
from django.urls import reverse                         # noqa: E402

from grader import droits                               # noqa: E402
from grader.models import ClassGroup, Quiz, Student     # noqa: E402
from test_commun import enseignant_complet              # noqa: E402

User = get_user_model()
CONCOURS = {"type_epreuve": "qcm", "language": "fr", "sheet_mode": "grid",
            "id_mode": "sticker", "id_digits": "6", "anonymat": "talon",
            "grading_mode": "immediate", "wrong_penalty": "0"}


def _nettoyer():
    for quiz in Quiz.objects.filter(owner__username__startswith="cc_").order_by("-pk"):
        quiz.delete()
    ClassGroup.objects.filter(owner__username__startswith="cc_").delete()
    User.objects.filter(username__startswith="cc_").delete()


def main():
    logging.disable(logging.ERROR)
    setup_test_environment()            # r.context dans les réponses
    _nettoyer()
    prof = enseignant_complet(User.objects.create_user("cc_prof", password="x-pass-123"))
    admin = User.objects.create_superuser("cc_admin", email="", password="x-pass-123")
    classe = ClassGroup.objects.create(name="CC 3ème A", owner=prof)
    Student.objects.create(class_group=classe, last_name="ELEVE", first_name="Un",
                           student_number="1")
    c = Client()
    c.force_login(prof)
    avant = c.get(reverse("dashboard")).context

    def creer_concours(titre):
        r = c.post(reverse("quiz_create") + "?concours=1", dict(CONCOURS, title=titre, annee="1"))
        assert r.status_code == 302, r.context["form"].errors
        return Quiz.objects.get(title=titre)

    quiz = creer_concours("CC concours")
    liste = quiz.class_group
    assert quiz.auto_enroll and liste.pk != classe.pk
    for n in range(2):
        Student.objects.create(class_group=liste, last_name=f"CANDIDAT{n}", first_name="P",
                               student_number=str(500 + n))
    print("Liste de candidats d'un concours :")

    # ---- tableau de bord : toujours une classe et un étudiant, les candidats à part
    reponse = c.get(reverse("dashboard"))
    apres = reponse.context
    assert (avant["n_classes"], avant["n_students"]) == (1, 1), avant
    assert (apres["n_classes"], apres["n_students"], apres["n_candidats"]) == (1, 1, 2), \
        (apres["n_classes"], apres["n_students"], apres["n_candidats"])
    assert "1 classe · 1 étudiant · 2 candidats aux concours" in reponse.content.decode()
    print("  tableau de bord : le concours n'ajoute pas de classe, ses candidats comptés à part OK")

    # ---- page « Classes » et menu « Classe » d'une épreuve de classe
    assert [g.pk for g in c.get(reverse("class_list")).context["classes"]] == [classe.pk]
    menu = c.get(reverse("quiz_create")).context["form"].fields["class_group"].queryset
    assert list(menu) == [classe], list(menu)
    print("  page Classes et menu « Classe » d'une épreuve : sans la liste du concours OK")

    # ---- administration : liste des classes, fiche de l'enseignant
    a = Client()
    a.force_login(admin)
    noms = [g.name for g in a.get("/admin/grader/classgroup/").context["cl"].result_list]
    assert "CC 3ème A" in noms and "CC concours" not in noms, noms
    assert a.get(f"/admin/grader/classgroup/{liste.pk}/change/").status_code == 200
    fiche = a.get(f"/admin/auth/user/{prof.pk}/change/").context["adminform"].form
    les_deux = [classe.pk, liste.pk]
    assert list(fiche.fields["classes"].queryset.filter(pk__in=les_deux)) == [classe]
    assert list(fiche.fields["concours"].queryset.filter(pk__in=les_deux)) == [liste]
    assert {g.pk for g in fiche.fields["concours"].initial} == {liste.pk}, \
        "le concours de l'enseignant n'est pas coché sur sa fiche"
    print("  administration : liste des classes sans le concours, coché à part sur la fiche OK")

    def enregistre(**champs):
        # la fiche envoyée comme le navigateur : le tableau des droits et la liste détaillée
        # tels qu'affichés (26 novembre 2026)
        base = {"username": "cc_prof", "first_name": "", "last_name": "", "email": "",
                "role": "enseignant", "is_active": "on", "gestes": droits.cases_permises(prof),
                "groups": [], "user_permissions": [str(p.pk) for p in prof.user_permissions.all()],
                "_save": "1"}
        base.update(champs)
        r = a.post(f"/admin/auth/user/{prof.pk}/change/", base)
        assert r.status_code == 302, r.context["adminform"].form.errors
        return set(prof.classes_attribuees.values_list("pk", flat=True))

    assert enregistre(classes=[classe.pk], concours=[liste.pk]) == {classe.pk, liste.pk}
    assert enregistre(classes=[classe.pk]) == {classe.pk}, "concours non retiré"
    assert enregistre(concours=[liste.pk]) == {liste.pk}, "concours perdu avec les classes"
    assert enregistre(classes=[classe.pk], concours=[liste.pk]) == {classe.pk, liste.pk}
    print("  fiche de l'enseignant : classes et concours se cochent séparément OK")

    # ---- suppression depuis l'administration : la liste du concours part avec lui
    page = a.get(f"/admin/grader/quiz/{quiz.pk}/delete/").content.decode()
    assert "Liste de candidats du concours : CC concours (2 candidats)" in page, \
        "la confirmation n'annonce pas la liste de candidats"
    assert a.post(f"/admin/grader/quiz/{quiz.pk}/delete/", {"post": "yes"}).status_code == 302
    assert not ClassGroup.objects.filter(pk=liste.pk).exists(), \
        "liste du concours supprimé restée (elle passerait pour une classe)"
    assert not Student.objects.filter(class_group_id=liste.pk).exists()
    deuxieme = creer_concours("CC concours bis")
    r = a.post("/admin/grader/quiz/", {"action": "delete_selected",
                                       "_selected_action": [deuxieme.pk], "post": "yes"})
    assert r.status_code == 302
    assert not ClassGroup.objects.filter(pk=deuxieme.class_group_id).exists(), \
        "liste restée après la suppression par l'action de la liste des quiz"
    devoir = Quiz.objects.create(title="CC devoir", class_group=classe, owner=prof)
    assert a.post(f"/admin/grader/quiz/{devoir.pk}/delete/", {"post": "yes"}).status_code == 302
    assert ClassGroup.objects.filter(pk=classe.pk).exists(), \
        "la classe d'une épreuve de classe supprimée avec elle"
    print("  suppression d'un concours : sa liste aussi (annoncée), jamais une classe OK")

    _nettoyer()
    print("\n✅ TEST LISTE DE CANDIDATS D'UN CONCOURS ≠ CLASSE RÉUSSI")


if __name__ == "__main__":
    main()
