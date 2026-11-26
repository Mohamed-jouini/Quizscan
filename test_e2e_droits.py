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


def _issue(reponse):
    """« refus » : droit manquant, retour au tableau de bord sans page 403 (26 novembre
    2026) ; sinon le code de la réponse."""
    if reponse.status_code == 302 and reponse.url == "/":
        return "refus"
    return reponse.status_code


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
                            (correcteur, "refus"), (spectateur, "refus")):
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
        assert _issue(reponse) == attendu, (
            f"{compte.username} : création d'épreuve -> "
            f"{_issue(reponse)}, attendu {attendu}{detail}")
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
    # vues ne montrant à un compte que ses propres données. « Lancer la
    # correction » est sur la page de l'épreuve, QCM ou écrit, et non plus sur
    # celle d'un lot (16 novembre 2026).
    ecrit = Quiz.objects.create(title="DR écrit", class_group=quiz.class_group,
                                owner=les_deux, type_epreuve="ecrit")
    lot_ecrit = ScanBatch.objects.create(quiz=ecrit, label="DR lot écrit",
                                         status="pending", total_pages=1)
    for epreuve, lot_epreuve in ((quiz, lot), (ecrit, lot_ecrit)):
        for compte, attendu in ((createur, "refus"), (spectateur, "refus"),
                                (correcteur, 302)):
            epreuve.owner = compte
            epreuve.save(update_fields=["owner"])
            reponse = _client(compte).post(f"/quiz/{epreuve.pk}/",
                                           {"launch_grading": "1"})
            assert _issue(reponse) == attendu, (
                f"{compte.username} : lancement de la correction ({epreuve.title}) "
                f"-> {_issue(reponse)}, attendu {attendu}")
        epreuve.owner = les_deux
        epreuve.save(update_fields=["owner"])
        lot_epreuve.refresh_from_db()
        assert lot_epreuve.status != "pending", f"{epreuve.title} : lot jamais lancé"
    # la page d'un lot ne lance plus rien, même avec le droit
    lot_seul = ScanBatch.objects.create(quiz=ecrit, label="DR lot seul",
                                        status="pending", total_pages=1)
    reponse = _client(les_deux).post(f"/lots/{lot_seul.pk}/", {"launch_grading": "1"})
    lot_seul.refresh_from_db()
    assert reponse.status_code == 200 and lot_seul.status == "pending", (
        reponse.status_code, lot_seul.status)
    assert 'name="launch_grading"' not in reponse.content.decode()
    print("  correction    refusée sans le droit, acceptée avec (QCM et écrit) OK")

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

    # Sans le droit de corriger (26 novembre 2026) : l'écran de vérification est en lecture
    # seule, et un envoi fabriqué ramène au tableau de bord sans rien changer ni page 403.
    manuscrite = Question.objects.create(quiz=quiz, order=2, qtype="open", points=2)
    reponse_open = Answer.objects.create(sheet=copie, question=manuscrite, points_awarded=1.5)
    quiz.owner = createur
    quiz.save(update_fields=["owner"])
    page = _client(createur).get(f"/copies/{copie.pk}/").content.decode()
    assert f'name="points_{reponse_open.pk}"' not in page, "note modifiable sans le droit"
    assert "Enregistrer et retour au lot" not in page and "Retour au lot" in page
    envoi = _client(createur).post(f"/copies/{copie.pk}/", {f"points_{reponse_open.pk}": "2"})
    reponse_open.refresh_from_db()
    assert _issue(envoi) == "refus" and reponse_open.points_awarded == 1.5, (
        _issue(envoi), reponse_open.points_awarded)
    quiz.owner = les_deux
    quiz.save(update_fields=["owner"])
    page = _client(les_deux).get(f"/copies/{copie.pk}/").content.decode()
    assert f'name="points_{reponse_open.pk}"' in page and "Enregistrer et retour au lot" in page
    print("  vérification  lecture seule sans le droit de corriger ; envoi direct : retour au "
          "tableau de bord, rien de changé OK")

    # L'administrateur non plus : la réponse lue ne se modifie pour personne.
    User.objects.filter(username="dr_admin").delete()
    patron = User.objects.create_superuser("dr_admin", email="",
                                           password="Mdp-Droits-123")
    _client(patron).post(f"/copies/{copie.pk}/", {
        "student": etudiant.pk,
        f"choice_{reponse_qcm.pk}": "2",
    })
    reponse_qcm.refresh_from_db()
    assert reponse_qcm.detected_choice == 0, (
        "l'administrateur a modifié la case lue sur un QCM "
        f"(devenue {reponse_qcm.detected_choice})")
    # Écran de vérification : ni liste « Correction », ni « Changer d'étudiant ».
    page = _client(patron).get(f"/copies/{copie.pk}/").content.decode()
    assert f'name="choice_{reponse_qcm.pk}"' not in page, "colonne « Correction » affichée"
    assert "Changer d'étudiant" not in page and 'name="student"' not in page, (
        "copie identifiée : l'étudiant ne doit pas pouvoir être changé")
    print("  administrateur pas de retouche des réponses ni de l'étudiant OK")

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

    assert libre.enseignants.filter(pk=enseignant.pk).exists(), (
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
    assert not libre.enseignants.filter(pk=enseignant.pk).exists(), (
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

    classe.enseignants.add(titulaire)
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

    # ---------------------------------------------------------------- 6 bis
    # Une classe, plusieurs enseignants ; un enseignant, plusieurs classes.
    from django.core.files.base import ContentFile
    from grader import layout as L
    from grader import sheet_pdf
    collegue = _compte("dr_collegue", droits.CREER, droits.CORRIGER)
    classe.enseignants.add(collegue)             # deux enseignants (+ créateur)
    autre = ClassGroup.objects.create(name="DR seconde classe", owner=None)
    autre.enseignants.add(collegue)              # le collègue en tient deux
    tenants = set(classe.enseignants.values_list("username", flat=True))
    assert tenants >= {"dr_titulaire", "dr_collegue"}, tenants
    client_collegue = _client(collegue)
    for c in (client_titulaire, client_collegue):
        assert c.get(f"/quiz/{epreuve.pk}/").status_code == 200, (
            "un des enseignants de la classe ne voit pas l'épreuve")
    noms = client_collegue.get("/classes/").content.decode()
    assert "DR titulaire" in noms and "DR seconde classe" in noms, (
        "le collègue ne voit pas ses deux classes")
    # Les documents suivent la même règle que les pages : la fiche PDF de
    # l'épreuve s'ouvre pour chacun des enseignants de la classe (avant :
    # 404 pour tout autre que l'auteur de l'épreuve).
    epreuve.layout_json = L.build_layout(epreuve)
    epreuve.sheet_pdf.save("dr.pdf", ContentFile(
        sheet_pdf.generate_sheet_pdf(epreuve, epreuve.layout_json)), save=True)
    for c in (client_titulaire, client_collegue):
        assert c.get(epreuve.sheet_pdf.url).status_code == 200, (
            "fiche PDF refusée à un enseignant de la classe")
    assert _client(enseignant).get(epreuve.sheet_pdf.url).status_code == 404, (
        "fiche PDF visible d'un enseignant étranger à la classe")
    # Confier la classe à un nouveau venu depuis l'administration n'en
    # retire pas ceux qui l'ont déjà.
    envoi = patron_client.post(
        f"/admin/auth/user/{enseignant.pk}/change/", {
            "username": enseignant.username,
            "first_name": "", "last_name": "", "email": "",
            "role": "enseignant", "is_active": "on",
            "classes": [str(classe.pk)],
            "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00",
        })
    assert envoi.status_code == 302, f"affectation -> {envoi.status_code}"
    tenants = set(classe.enseignants.values_list("username", flat=True))
    assert tenants >= {"dr_titulaire", "dr_collegue", enseignant.username}, (
        f"affecter la classe à un collègue l'a retirée aux autres : {tenants}")
    # La liste des classes de l'administration montre leurs enseignants.
    page = patron_client.get("/admin/grader/classgroup/").content.decode()
    assert "dr_collegue" in page and "dr_titulaire" in page, (
        "colonne des enseignants absente de la liste des classes")
    print("  co-enseignement une classe à plusieurs enseignants, un enseignant "
          "à plusieurs classes ; pages et documents pour chacun OK")

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

    # ---------------------------------------------------------------- 8
    # Droits fins (26 novembre 2026, demande de l'utilisateur) : téléverser les copies,
    # lancer la correction, saisir les questions se donnent seuls, et chacun ne permet que
    # son geste. Les droits larges les englobent (sections 1 et 2).
    televerseur = _compte("dr_televerser", droits.TELEVERSER)
    lanceur = _compte("dr_lancer", droits.LANCER)
    redacteur = _compte("dr_questions", droits.QUESTIONS)
    classe_fine = ClassGroup.objects.create(name="DR droits fins", owner=les_deux)
    fine = Quiz.objects.create(title="DR droits fins", class_group=classe_fine, owner=les_deux)
    q_fine = Question.objects.create(quiz=fine, order=1, qtype="qcm", points=1,
                                     num_choices=4, correct_choice=0)
    lot_fin = ScanBatch.objects.create(quiz=fine, label="DR lot fin", status="pending",
                                       total_pages=1)

    def essai(compte, url, donnees=None, methode="post"):
        fine.owner = compte
        fine.save(update_fields=["owner"])
        return _issue(getattr(_client(compte), methode)(url, donnees or {}))

    for compte, permis in ((televerseur, True), (lanceur, False), (redacteur, False)):
        issue = essai(compte, f"/quiz/{fine.pk}/upload/")
        assert (issue != "refus") == permis, (compte.username, "téléverser", issue)
    for compte, permis in ((televerseur, False), (redacteur, False), (lanceur, True)):
        issue = essai(compte, f"/quiz/{fine.pk}/", {"launch_grading": "1"})
        assert (issue != "refus") == permis, (compte.username, "lancer", issue)
    lot_fin.refresh_from_db()
    assert lot_fin.status != "pending", "le droit de lancer n'a pas lancé la correction"
    for compte, permis in ((televerseur, False), (lanceur, False), (redacteur, True)):
        issue = essai(compte, f"/quiz/{fine.pk}/", {"edit_question": q_fine.pk, "correct": "2"})
        q_fine.refresh_from_db()
        assert (issue != "refus", q_fine.correct_choice == 2) == (permis, permis), (
            compte.username, "questions", issue, q_fine.correct_choice)
    for compte, permis in ((televerseur, False), (redacteur, True)):
        issue = essai(compte, f"/quiz/{fine.pk}/corrige/")
        assert (issue != "refus") == permis, (compte.username, "corrigé", issue)
    # saisir les questions ne règle pas l'épreuve, ne la génère pas, n'en crée pas
    assert essai(redacteur, f"/quiz/{fine.pk}/",
                 {"update_settings": "1", "title": "DR piratée"}) == "refus"
    fine.refresh_from_db()
    assert fine.title == "DR droits fins"
    assert essai(redacteur, f"/quiz/{fine.pk}/", {"generate_pdf": "1"}) == "refus"
    assert essai(redacteur, "/quiz/nouveau/", methode="get") == "refus"
    # aucun des trois ne vérifie une copie
    copie_fine = SheetScan.objects.create(batch=lot_fin, source_name="fin.jpg", status="ok")
    for compte in (televerseur, lanceur, redacteur):
        assert essai(compte, f"/copies/{copie_fine.pk}/", {"student": ""}) == "refus", compte
    # l'écran ne propose que le geste permis
    fine.owner = televerseur
    fine.save(update_fields=["owner"])
    page = _client(televerseur).get(f"/quiz/{fine.pk}/").content.decode()
    assert f'action="/quiz/{fine.pk}/upload/"' in page and 'name="edit_question"' not in page
    fine.owner = redacteur
    fine.save(update_fields=["owner"])
    page = _client(redacteur).get(f"/quiz/{fine.pk}/").content.decode()
    assert 'name="edit_question"' in page and 'name="add_question"' in page
    assert f'action="/quiz/{fine.pk}/upload/"' not in page and 'name="update_settings"' not in page
    print("  droits fins   téléverser, lancer, saisir les questions : chacun son geste, "
          "rien de plus OK")

    # la fiche du compte : les trois cases, et les permissions dans la liste détaillée
    envoi = patron_client.post(f"/admin/auth/user/{redacteur.pk}/change/", {
        "username": redacteur.username, "first_name": "", "last_name": "", "email": "",
        "role": "enseignant", "is_active": "on", "classes": [],
        "peut_televerser": "on", "peut_lancer": "on",
        "date_joined_0": "2026-01-01", "date_joined_1": "00:00:00"})
    assert envoi.status_code == 302, f"fiche du compte -> {envoi.status_code}"
    compte = get_user_model().objects.get(pk=redacteur.pk)
    assert compte.has_perm(droits.TELEVERSER) and compte.has_perm(droits.LANCER)
    assert not compte.has_perm(droits.QUESTIONS), "case décochée : le droit reste"
    assert "téléverser" in patron_client.get("/admin/auth/user/").content.decode()
    page = patron_client.get(f"/admin/auth/user/{redacteur.pk}/change/").content.decode()
    for nom in ("Téléverser les copies scannées", "Lancer la correction",
                "Saisir les questions et le corrigé",
                "Créer les concours et les quiz, et leurs questions"):
        assert f"quiz | {nom}" in page, f"« {nom} » absent des permissions détaillées"
    assert 'name="peut_saisir_questions"' in page and 'name="peut_televerser"' in page
    print("  administration les trois droits fins cochés un à un, et dans les permissions "
          "détaillées OK")

    print("\n✅ TEST DES DROITS DES ENSEIGNANTS RÉUSSI")


if __name__ == "__main__":
    main()
