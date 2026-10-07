"""Les droits accordés compte par compte tiennent-ils vraiment ?

Trois questions, et on ne se fie pas à l'écran pour y répondre : un bouton
caché n'est pas une protection. Chaque vérification envoie la requête que
ferait un formulaire fabriqué à la main.

  1. Un enseignant sans le droit de créer peut-il créer une épreuve ?
  2. Un enseignant sans le droit de corriger peut-il lancer la correction ?
  3. Un enseignant, même autorisé à corriger, peut-il changer la note d'un
     QCM ? C'est la règle la plus importante : la note vient de la lecture
     optique, et l'enseignant ne la retouche pas.
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
from django.contrib.auth.models import Permission       # noqa: E402
from django.test import Client                          # noqa: E402

from grader import droits                               # noqa: E402
from grader.models import (Answer, ClassGroup, Question, Quiz,  # noqa: E402
                           ScanBatch, SheetScan, Student)


def _permission(nom_complet):
    application, code = nom_complet.split(".")
    return Permission.objects.get(content_type__app_label=application,
                                  codename=code)


def _compte(nom, *droits_accordes):
    User = get_user_model()
    User.objects.filter(username=nom).delete()
    compte = User.objects.create_user(nom, password="Mdp-Droits-123")
    for d in droits_accordes:
        compte.user_permissions.add(_permission(d))
    return compte


def _client(compte):
    c = Client()
    c.force_login(compte)
    return c


def main():
    logging.disable(logging.ERROR)
    User = get_user_model()
    # Depuis que supprimer un compte laisse ses données sans titulaire, le
    # passage précédent a pu laisser des classes orphelines : on vise le nom
    # plutôt que le propriétaire, et les quiz avant les classes qu'ils
    # protègent.
    Quiz.objects.filter(class_group__name__startswith="DR ").delete()
    Quiz.objects.filter(owner__username__startswith="dr_").delete()
    ClassGroup.objects.filter(name__startswith="DR ").delete()
    User.objects.filter(username__startswith="dr_").delete()

    createur = _compte("dr_createur", droits.CREER)
    correcteur = _compte("dr_correcteur", droits.CORRIGER)
    les_deux = _compte("dr_deux", droits.CREER, droits.CORRIGER)
    spectateur = _compte("dr_spectateur")

    # ---------------------------------------------------------------- 1
    for compte, attendu in ((createur, 302), (les_deux, 302),
                            (correcteur, 403), (spectateur, 403)):
        groupe = ClassGroup.objects.create(name=f"DR {compte.username}",
                                           owner=compte)
        reponse = _client(compte).post("/quiz/nouveau/", {
            "title": "Épreuve de contrôle", "class_group": groupe.pk,
            "language": "fr", "sheet_mode": "grid", "id_mode": "name",
            "wrong_penalty": "0", "id_digits": "6",
        })
        detail = ""
        if reponse.status_code == 200 and hasattr(reponse, "context"):
            formulaire = (reponse.context or {}).get("form")
            detail = f" — erreurs du formulaire : {formulaire.errors}" if formulaire else ""
        assert reponse.status_code == attendu, (
            f"{compte.username} : création d'épreuve -> "
            f"{reponse.status_code}, attendu {attendu}{detail}")
    print("  création      refusée sans le droit, acceptée avec OK")

    # ---------------------------------------------------------------- 2
    quiz = Quiz.objects.filter(owner=les_deux).first()
    assert quiz, "le compte autorisé n'a pas créé son épreuve"
    Question.objects.create(quiz=quiz, order=1, qtype="qcm", points=1,
                            num_choices=4, correct_choice=0)
    lot = ScanBatch.objects.create(quiz=quiz, label="DR lot",
                                   status="pending", total_pages=1)

    # createur sait faire une épreuve mais pas la corriger ; correcteur
    # l'inverse. Le lot est prêté à chacun le temps de sa vérification, les
    # vues ne montrant à un compte que ses propres données.
    for compte, attendu in ((createur, 403), (spectateur, 403),
                            (correcteur, 302)):
        quiz.owner = compte
        quiz.save(update_fields=["owner"])
        reponse = _client(compte).post(f"/lots/{lot.pk}/", {"launch_grading": "1"})
        assert reponse.status_code == attendu, (
            f"{compte.username} : lancement de la correction -> "
            f"{reponse.status_code}, attendu {attendu}")
    quiz.owner = les_deux
    quiz.save(update_fields=["owner"])
    print("  correction    refusée sans le droit, acceptée avec OK")

    # ---------------------------------------------------------------- 3
    etudiant = Student.objects.create(class_group=quiz.class_group,
                                      last_name="DROIT", first_name="Test",
                                      student_number="990001")
    copie = SheetScan.objects.create(batch=lot, source_name="dr.jpg",
                                     status="ok", student=etudiant)
    question = quiz.questions.first()
    reponse_qcm = Answer.objects.create(
        sheet=copie, question=question, detected_choice=0,
        points_awarded=1.0, manually_set=False)

    # L'enseignant a le droit de corriger : il passe la porte, mais la case
    # cochée qu'il envoie doit rester sans effet.
    quiz.owner = les_deux
    quiz.save(update_fields=["owner"])
    envoi = _client(les_deux).post(f"/copies/{copie.pk}/", {
        "student": etudiant.pk,
        f"choice_{reponse_qcm.pk}": "2",      # « C » : réponse fausse
    })
    assert envoi.status_code == 302, f"enregistrement -> {envoi.status_code}"
    reponse_qcm.refresh_from_db()
    assert reponse_qcm.detected_choice == 0, (
        "un enseignant a modifié la case lue sur un QCM "
        f"(devenue {reponse_qcm.detected_choice})")
    assert reponse_qcm.points_awarded == 1.0, (
        f"la note du QCM a bougé : {reponse_qcm.points_awarded}")
    print("  note de QCM   inchangée malgré un envoi direct OK")

    # L'administrateur, lui, peut rectifier une case mal lue.
    User.objects.filter(username="dr_admin").delete()
    patron = User.objects.create_superuser("dr_admin", email="",
                                           password="Mdp-Droits-123")
    _client(patron).post(f"/copies/{copie.pk}/", {
        "student": etudiant.pk,
        f"choice_{reponse_qcm.pk}": "2",
    })
    reponse_qcm.refresh_from_db()
    assert reponse_qcm.detected_choice == 2, (
        "l'administrateur doit pouvoir rectifier une case mal lue "
        f"(restée {reponse_qcm.detected_choice})")
    print("  administrateur rectifie une case mal lue OK")

    # ---------------------------------------------------------------- 4
    # L'écran ne propose pas ce que le compte ne peut pas faire.
    corps = _client(spectateur).get("/").content.decode()
    assert "Nouveau quiz" not in corps, (
        "le tableau de bord propose « Nouveau quiz » à un compte qui ne "
        "peut pas créer d'épreuve")
    corps = _client(les_deux).get("/").content.decode()
    assert "Nouveau quiz" in corps, "le bouton manque à un compte autorisé"
    print("  écrans        les boutons suivent les droits OK")

    # ---------------------------------------------------------------- 5
    # L'écran d'administration : affecter une classe et retirer un droit.
    patron_client = _client(patron)
    enseignant = _compte("dr_nouveau")
    libre = ClassGroup.objects.create(name="DR libre", owner=None)

    envoi = patron_client.post(
        f"/admin/auth/user/{enseignant.pk}/change/", {
            "username": enseignant.username,
            "first_name": "", "last_name": "", "email": "",
            "role": "enseignant", "is_active": "on",
            "classes": [str(libre.pk)],
            "peut_corriger": "on",      # « créer » volontairement décoché
            "last_login_0": "", "last_login_1": "",
            "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
        })
    assert envoi.status_code == 302, (
        f"enregistrement du compte -> {envoi.status_code} "
        f"(le formulaire a été refusé)")

    libre.refresh_from_db()
    assert libre.owner_id == enseignant.pk, (
        "la classe cochée n'a pas été affectée à l'enseignant")

    enseignant = get_user_model().objects.get(pk=enseignant.pk)
    assert not droits.peut_creer(enseignant), (
        "« Créer des épreuves » était décoché, le droit a pourtant été accordé")
    assert droits.peut_corriger(enseignant), (
        "« Corriger les copies » était coché, le droit manque")
    print("  administration classe affectée, droits posés un à un OK")

    # Et le retrait : on décoche la classe, elle redevient sans enseignant.
    envoi = patron_client.post(
        f"/admin/auth/user/{enseignant.pk}/change/", {
            "username": enseignant.username,
            "first_name": "", "last_name": "", "email": "",
            "role": "enseignant", "is_active": "on",
            "classes": [],
            "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
        })
    assert envoi.status_code == 302, f"retrait -> {envoi.status_code}"
    libre.refresh_from_db()
    assert libre.owner_id is None, (
        "la classe décochée appartient toujours à l'enseignant")
    enseignant = get_user_model().objects.get(pk=enseignant.pk)
    assert not droits.peut_corriger(enseignant), (
        "le droit décoché n'a pas été retiré")
    print("  administration classe et droits retirés OK")

    # ---------------------------------------------------------------- 6
    # Affecter une classe donne accès à ses épreuves — et pas à celles des
    # autres. C'est le sens de l'affectation : confier un périmètre.
    titulaire = _compte("dr_titulaire", droits.CORRIGER)
    classe = ClassGroup.objects.create(name="DR titulaire", owner=les_deux)
    epreuve = Quiz.objects.create(title="DR épreuve de la classe",
                                  class_group=classe, owner=les_deux)
    ailleurs = Quiz.objects.filter(owner=createur).first()

    avant_affectation = _client(titulaire).get(f"/quiz/{epreuve.pk}/")
    assert avant_affectation.status_code == 404, (
        "l'épreuve est visible avant toute affectation de la classe")

    classe.owner = titulaire
    classe.save(update_fields=["owner"])
    client_titulaire = _client(titulaire)
    assert client_titulaire.get(f"/quiz/{epreuve.pk}/").status_code == 200, (
        "la classe est affectée mais son épreuve reste invisible")
    if ailleurs is not None:
        assert client_titulaire.get(f"/quiz/{ailleurs.pk}/").status_code == 404, (
            "l'affectation d'une classe donne accès à une épreuve étrangère")

    # L'auteur ne perd pas la sienne pour autant.
    assert _client(les_deux).get(f"/quiz/{epreuve.pk}/").status_code == 200, (
        "l'auteur a perdu l'accès à son épreuve")
    print("  affectation   la classe emporte ses épreuves, et rien de plus OK")

    # ---------------------------------------------------------------- 7
    # Supprimer le compte d'un enseignant qui part : possible, et sans
    # emporter ses classes ni ses épreuves — ce sont les archives de
    # l'établissement. Avec on_delete=CASCADE, la suppression échouait sur
    # la clé protégée Quiz.class_group dès qu'une épreuve existait.
    partant = _compte("dr_partant", droits.CREER)
    sa_classe = ClassGroup.objects.create(name="DR partant", owner=partant)
    son_epreuve = Quiz.objects.create(title="DR épreuve conservée",
                                      class_group=sa_classe, owner=partant)

    envoi = _client(patron).post(
        f"/admin/auth/user/{partant.pk}/delete/", {"post": "yes"})
    assert envoi.status_code == 302, (
        f"suppression du compte -> {envoi.status_code} "
        "(l'administration a refusé)")
    assert not get_user_model().objects.filter(pk=partant.pk).exists(), (
        "le compte n'a pas été supprimé")

    sa_classe.refresh_from_db()
    son_epreuve.refresh_from_db()
    assert sa_classe.owner_id is None and son_epreuve.owner_id is None, (
        "la classe ou l'épreuve a gardé un enseignant supprimé")
    print("  suppression   compte retiré, classe et épreuve conservées OK")

    print("\n✅ TEST DES DROITS DES ENSEIGNANTS RÉUSSI")


if __name__ == "__main__":
    main()
