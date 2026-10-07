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
from test_commun import enseignant_complet

FEUILLE = "grader/quizscan.css"
SCRIPT_THEME = "grader/theme.js"
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
    prof = enseignant_complet(prof)
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
            # Le bouton « Mot de passe » a quitté la barre du haut : c'est
            # désormais le nom de la personne qui y mène. Sans ce contrôle,
            # le réglage pourrait disparaître de l'interface sans bruit.
            if etiquette != "connexion":
                assert 'class="qs-identite"' in corps and 'password_change' in corps, (
                    f"{url} : plus aucun accès au changement de mot de passe")
                assert "qs-moncompte" not in corps, (
                    f"{url} : le bouton « Mot de passe » est de retour dans "
                    "la barre du haut — le nom suffit")
            # Un commentaire {# … #} de gabarit ne tient QUE sur une ligne :
            # ouvert sur deux, Django n'y voit pas un commentaire et l'affiche
            # tel quel au milieu de la page. L'erreur est passée trois fois.
            assert "{#" not in corps, (
                f"{url} affiche un commentaire de gabarit : un {{# … #}} "
                "ouvert sur plusieurs lignes n'est pas un commentaire")
            # Un seul mécanisme de thème, partout : notre script, notre bouton.
            assert SCRIPT_THEME in corps, f"{url} ne charge pas {SCRIPT_THEME}"
            assert 'class="theme-toggle"' in corps, \
                f"{url} n'offre pas la bascule clair / sombre"
            for ecarte in ("admin/js/theme.js", "admin/css/dark_mode.css"):
                assert ecarte not in corps, (
                    f"{url} charge encore {ecarte} : ce serait un second "
                    "mécanisme de thème à côté du nôtre")
        print(f"  {etiquette:14} {len(urls)} pages : feuille unique, bascule "
              "de thème, aucun second système OK")

    def menu(corps):
        """Extrait la barre latérale d'une page d'administration."""
        debut = corps.index('<aside class="side">')
        return corps[debut:corps.index("</aside>", debut)]

    controle(Client(), anonyme, "connexion")
    c = Client(); c.force_login(prof)
    controle(c, enseignant, "enseignant")
    c = Client(); c.force_login(admin)
    controle(c, administration, "administration")

    # Les tables administrables se listent à UN seul endroit : la page
    # d'accueil de l'administration. Les reprendre dans la barre latérale
    # obligeait à chercher deux fois le même lien.
    accueil = c.get("/admin/").content.decode()
    for table in ("Étudiants", "Questions", "Utilisateurs"):
        assert f">{table}</a>" in accueil, \
            f"la page d'accueil de l'administration ne propose pas « {table} »"
    for url in administration:
        corps = menu(c.get(url).content.decode())
        liens = re.findall(r'href="(/admin/\w+/\w+/)"', corps)
        assert not liens, (f"{url} : la barre latérale reliste les tables "
                           f"({liens[:3]}) — elles vivent sur /admin/")
        assert "Retour à l" not in corps, (
            f"{url} : le lien de retour fait doublon avec les rubriques "
            "de l'application, juste au-dessus dans le même menu")
    print(f"  tables         listées sur /admin/ seulement, absentes des "
          f"{len(administration)} barres latérales OK")

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
        # Le sélecteur doit COMMENCER par cette classe : « .note .btn{ » est un
        # ajustement dans un contexte précis, pas une seconde définition du
        # bouton. Seul « .btn{ » en tête de sélecteur en serait une.
        n = len(re.findall(r"(?:^|[,}])\s*" + re.escape(classe), hors_media,
                           re.M))
        assert n == 1, f"{classe} est défini {n} fois hors requête média"

    # Et pas seulement ces huit-là : TOUTE classe définie seule (« .x{ », sans
    # contexte) ne doit l'être qu'une fois. « .note » a été créé une seconde
    # fois pour l'encart d'information alors qu'il servait déjà à la mention
    # des pages de connexion : la seconde règle écrasait la première, et les
    # encarts de toute l'application ont perdu leur marge sans que rien ne
    # le signale.
    VOULUS = {".shell__inner"}   # règle partagée avec #container + ajustement
    sans_commentaires = re.sub(r"/\*.*?\*/", "", hors_media, flags=re.S)
    definitions = {}
    for regle in re.finditer(r"([^{}]+)\{", sans_commentaires):
        for selecteur in regle.group(1).split(","):
            selecteur = " ".join(selecteur.split())
            if re.fullmatch(r"\.[A-Za-z][\w-]*", selecteur):
                definitions[selecteur] = definitions.get(selecteur, 0) + 1
    doublons = {c: n for c, n in definitions.items() if n > 1 and c not in VOULUS}
    assert not doublons, (
        f"classes définies plusieurs fois, la dernière écrase l'autre : {doublons}")
    print(f"  jetons         chaque couleur et chaque composant défini une "
          f"seule fois ({len(definitions)} classes, {len(css.splitlines())} "
          "lignes) OK")

    # Le thème sombre ne doit rien oublier : chaque jeton de couleur du bloc
    # :root de base a sa valeur nocturne, sinon la page bascule à moitié.
    # Les formes et les mesures (rayons, largeurs) n'ont pas à changer, et
    # --qs-grad reste volontairement identique : ce dégradé de marque porte
    # du texte blanc et se lit aussi bien sur fond sombre.
    SANS_VARIANTE = {
        "--qs-r-panel", "--qs-r-card", "--qs-r-field", "--qs-side-w",
        "--qs-max-w", "--qs-gutter", "--qs-pad", "--qs-band", "--qs-grad",
    }
    branches = set(re.findall(r"(--[a-z0-9-]+):var\(--dk-", css))
    oublies = [j for j in re.findall(r"(--[a-z0-9-]+):", racine)
               if j.startswith(("--qs-", "--art-"))
               and j not in branches and j not in SANS_VARIANTE]
    assert not oublies, f"jetons sans valeur sombre : {oublies}"

    # Les valeurs nocturnes vivent une seule fois, dans les jetons --dk-*.
    definis = set(re.findall(r"(--dk-[a-z0-9-]+):", css))
    employes = set(re.findall(r"var\((--dk-[a-z0-9-]+)\)", css))
    assert not employes - definis, f"--dk-* employés sans valeur : {employes - definis}"
    assert not definis - employes, f"--dk-* définis sans emploi : {definis - employes}"
    print(f"  thème sombre   {len(branches)} jetons basculent, aucun oubli OK")

    # L'administration doit reprendre les jetons, pas redéfinir des couleurs.
    pont = css[css.index("Pont vers l'administration"):]
    assert "--primary:var(--qs-blue)" in pont, \
        "l'administration doit pointer sur les jetons communs"
    print("  administration les couleurs de Django pointent sur nos jetons OK")

    print("\n✅ TEST DU SYSTÈME DE STYLE UNIQUE RÉUSSI")


if __name__ == "__main__":
    main()
