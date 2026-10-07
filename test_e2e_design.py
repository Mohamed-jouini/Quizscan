"""Vérifie qu'il n'y a QU'UN seul système de style pour toute l'application.

L'espace enseignant, les pages de connexion et l'administration ont longtemps
eu trois feuilles séparées qui redéfinissaient les mêmes couleurs sous des
noms différents et les mêmes composants : une règle oubliée dans l'une
écrasait l'autre (le logo de la barre latérale est resté blanc comme ça).

Ce test monte la garde : toutes les pages chargent la même feuille, et
aucune ne redéclare le système de style en ligne.
"""
import os
import re
import sys

# Les tests affichent des caractères accentués et des symboles : on force la
# sortie en UTF-8 pour ne pas échouer sur une console Windows en cp1252.
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

from django.contrib.auth import get_user_model
from django.test import Client

from grader.models import ClassGroup, Question, Quiz

FEUILLE = "grader/quizscan.css"
# Au-delà de ce seuil, un bloc <style> n'est plus une variante ponctuelle mais
# un second système de style qui commence.
MAX_LIGNES_EN_LIGNE = 20


def _sans_media(css):
    """Retire les blocs @media : une règle y est une adaptation à l'écran,
    pas une seconde définition du composant."""
    sortie, i = [], 0
    while True:
        debut = css.find("@media", i)
        if debut == -1:
            sortie.append(css[i:])
            return "".join(sortie)
        sortie.append(css[i:debut])
        j = css.index("{", debut)
        profondeur = 1
        j += 1
        while profondeur:
            if css[j] == "{":
                profondeur += 1
            elif css[j] == "}":
                profondeur -= 1
            j += 1
        i = j


def main():
    logging.disable(logging.ERROR)
    User = get_user_model()
    # Quiz protège ClassGroup (on_delete=PROTECT) : on retire les quiz d'abord,
    # sinon la suppression du compte échoue sur la clé protégée.
    Quiz.objects.filter(owner__username__startswith="dsg_").delete()
    ClassGroup.objects.filter(name="DSG").delete()
    User.objects.filter(username__startswith="dsg_").delete()
    prof = User.objects.create_user("dsg_prof", password="Mdp-Design-123")
    admin = User.objects.create_superuser("dsg_admin", email="",
                                          password="Mdp-Design-123")
    groupe = ClassGroup.objects.create(name="DSG", owner=prof)
    quiz = Quiz.objects.create(title="Design", class_group=groupe, owner=prof)
    Question.objects.create(quiz=quiz, order=1, qtype="qcm", points=1,
                            correct_choice=0)

    anonyme = ["/comptes/login/", "/comptes/administration/"]
    enseignant = ["/", "/classes/", f"/classes/{groupe.pk}/", "/concours/",
                  "/quiz/nouveau/", f"/quiz/{quiz.pk}/",
                  f"/quiz/{quiz.pk}/resultats/", "/comptes/password_change/"]
    administration = ["/admin/", "/admin/auth/user/", "/admin/auth/user/add/",
                      f"/admin/auth/user/{admin.pk}/change/",
                      f"/admin/auth/user/{admin.pk}/password/",
                      "/admin/password_change/", "/admin/grader/quiz/"]

    def controle(client, urls, etiquette):
        for url in urls:
            reponse = client.get(url)
            assert reponse.status_code == 200, f"{url} -> {reponse.status_code}"
            corps = reponse.content.decode()
            assert FEUILLE in corps, f"{url} n'utilise pas la feuille unique"
            for bloc in re.findall(r"<style[^>]*>(.*?)</style>", corps, re.S):
                lignes = [l for l in bloc.splitlines() if l.strip()]
                assert len(lignes) <= MAX_LIGNES_EN_LIGNE, (
                    f"{url} redéclare du style en ligne ({len(lignes)} lignes) : "
                    "tout doit vivre dans grader/static/grader/quizscan.css")
        print(f"  {etiquette:14} {len(urls)} pages : feuille unique, "
              "aucun second système de style OK")

    controle(Client(), anonyme, "connexion")
    c = Client(); c.force_login(prof)
    controle(c, enseignant, "enseignant")
    c = Client(); c.force_login(admin)
    controle(c, administration, "administration")

    # Les trois contextes partagent les mêmes jetons et la même barre latérale.
    feuille = (django.apps.apps.get_app_config("grader").path
               + "/static/" + FEUILLE)
    with open(feuille, encoding="utf-8") as fh:
        css = fh.read()
    # On compte dans le bloc :root de base. Une redéfinition sous requête
    # média (la barre latérale s'élargit au-delà de 1700 px) est voulue ;
    # c'est une seconde DÉCLARATION du même jeton qui serait le problème.
    racine = css[css.index(":root{"):]
    racine = racine[:racine.index("\n}")]
    for jeton in ("--qs-blue", "--qs-grad", "--qs-ink", "--qs-line",
                  "--qs-r-card", "--qs-side-w", "--qs-page", "--qs-muted"):
        n = racine.count(jeton + ":")
        assert n == 1, f"{jeton} est défini {n} fois dans :root"
    hors_media = _sans_media(css)
    for classe in (".side{", ".nav__item{", ".card{", ".btn{", ".tile{",
                   ".badge{", ".auth{", ".topbar{"):
        n = hors_media.count(classe)
        assert n == 1, f"{classe} est défini {n} fois hors requête média"
    print(f"  jetons         chaque couleur et chaque composant défini une "
          f"seule fois ({len(css.splitlines())} lignes) OK")

    # L'administration doit reprendre les jetons, pas redéfinir des couleurs.
    pont = css[css.index("Pont vers l'administration"):]
    assert "--primary:var(--qs-blue)" in pont, \
        "l'administration doit pointer sur les jetons communs"
    print("  administration les couleurs de Django pointent sur nos jetons OK")

    print("\n✅ TEST DU SYSTÈME DE STYLE UNIQUE RÉUSSI")


if __name__ == "__main__":
    main()
